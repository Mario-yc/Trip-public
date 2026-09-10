from __future__ import annotations

from copy import deepcopy
from collections import Counter
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore
import json
import re
import time
from typing import Any, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from src.services.agent_action_directive import ActionDirective, DraftItineraryDirective
from src.services.clarification_checkpoint_service import ClarificationCheckpointService
from src.services.agent_decision_contract_service import AgentDecisionContractService
from src.services.agent_decision_normalizer import AgentDecisionNormalizer, DecisionNormalizationError
from src.services.agent_executor_registry import AgentActionExecutorRegistry
from src.services.agent_observation_service import AgentObservation, AgentObservationBuilder
from src.services.request_activity_coverage_service import (
    RequestActivityCoverageError,
    RequestActivityCoverageService,
    RequiredGoalOptionalConflictError,
)
from src.services.agent_planner_service import AgentPlannerService
from src.services.controller_failure_classifier import ControllerFailure, classify_controller_failure
from src.services.controller_context_projection_service import ControllerContextProjectionService
from src.services.controller_response_integrity import (
    ControllerOutputTruncatedError,
    ControllerResponseIntegrityError,
)
from src.services.conversation_intent_router import ConversationIntentModelOutcome
from src.services.timeline_mutation_models import TimelineMutationIntent


PRIMARY_ACTION_VALUES = (
    "ask_user",
    "draft_itinerary",
    "read_itinerary",
    "resolve_poi",
    "optimize_route",
    "patch_itinerary",
    "verify_external_facts",
    "finish",
)
_DATE_CONTRACT_CLARIFICATION_REASONS = frozenset(
    {
        "ambiguous_multiple_explicit_date_groups",
        "ambiguous_national_day_relative_duration",
        "date_duration_conflict",
        "duration_without_dates",
        "holiday_dates_unspecified",
        "invalid_calendar_date",
        "non_contiguous_explicit_date_list",
        "unparseable_explicit_date",
    }
)
CLARIFICATION_SAFE_ACTIONS = (
    "ask_user",
    "read_itinerary",
    "verify_external_facts",
)

PrimaryAction = Literal[
    "ask_user",
    "draft_itinerary",
    "read_itinerary",
    "resolve_poi",
    "optimize_route",
    "patch_itinerary",
    "verify_external_facts",
    "finish",
]
WriteRisk = Literal["none", "low", "medium", "high"]


def _unresolved_trip_date_reason(*contexts: Optional[dict[str, Any]]) -> Optional[str]:
    """Return a typed canonical-date blocker without interpreting free text."""

    for context in contexts:
        if not isinstance(context, dict):
            continue
        resolved_dates = context.get("resolvedTripDates")
        if not isinstance(resolved_dates, dict):
            continue
        if str(resolved_dates.get("status") or "") != "unresolved":
            continue
        reason = str(resolved_dates.get("reason") or "")
        # Every typed unresolved canonical date is a planning blocker.  Keep
        # known reasons precise while collapsing unknown persisted values to a
        # stable fail-closed reason rather than allowing Controller/AMap work.
        return reason if reason in _DATE_CONTRACT_CLARIFICATION_REASONS else "unparseable_explicit_date"
    return None


def _parse_controller_json_object(raw: Any) -> tuple[Any, list[str]]:
    """Parse one Controller object while tolerating only redundant closers.

    DeepSeek JSON mode can occasionally return one otherwise complete object
    followed by an extra ``}``/``]``.  That suffix carries no second value and
    no action authority, so it is safe to trim before the normal schema and
    policy gates run.  A second JSON value, prose, a code fence, or an
    incomplete first object remains a hard parse failure.
    """

    if not isinstance(raw, str):
        return raw, []
    try:
        return json.loads(raw), []
    except json.JSONDecodeError as strict_error:
        text = raw.lstrip()
        try:
            parsed, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            raise strict_error
        suffix = text[end:]
        if (
            not isinstance(parsed, dict)
            or not suffix.strip()
            or re.fullmatch(r"[\s}]+", suffix) is None
        ):
            raise strict_error
        return parsed, ["controller_json_inert_suffix_trimmed"]


def clarification_safe_actions_for_request_context(
    request_context: Optional[dict[str, Any]],
) -> tuple[str, ...]:
    actions = CLARIFICATION_SAFE_ACTIONS
    context = request_context if isinstance(request_context, dict) else {}
    message = str(context.get("latestUserMessage") or context.get("message") or "")
    conversation_intent = (
        context.get("conversationIntent")
        if isinstance(context.get("conversationIntent"), dict)
        else {}
    )
    no_write_turn = str(conversation_intent.get("executionDisposition") or "") in {
        "read_only",
        "no_write",
    }
    if no_write_turn or any(
        marker in message
        for marker in ("不需要继续", "不用继续", "停止规划", "取消规划", "先到这里", "先这样")
    ):
        actions = (*actions, "finish")
    if controller_route_policy_may_resolve_detour(context):
        actions = tuple(dict.fromkeys((*actions, "draft_itinerary")))
    return actions


def controller_allowed_actions_for_request_context(
    request_context: Optional[dict[str, Any]],
) -> tuple[str, ...]:
    """Expose every safe route for the live Controller to choose from.

    A non-sensitive route gap may be closed by a typed Controller estimate, but
    that is an additional safe action rather than authority to suppress a
    model-authored material preference question.  Keeping both actions in the
    signed contract lets the Controller decide from the actual request while
    preserving the same fail-closed validation for either result.
    """

    return clarification_safe_actions_for_request_context(request_context)


def allowed_primary_actions_for_observation(
    observation: Optional[AgentObservation],
) -> tuple[str, ...]:
    if observation is None:
        return PRIMARY_ACTION_VALUES
    allowed_actions = PRIMARY_ACTION_VALUES
    has_persisted_timeline = any(
        item.poi_amap_id
        or item.user_locked
        or item.grounding_status not in {"", "draft_only", "waiting_for_poi_grounding", "provider_rate_limited"}
        for item in observation.segment_refs
    )
    if observation.itinerary.meaningful_segment_count > 0 and has_persisted_timeline:
        allowed_actions = tuple(action for action in allowed_actions if action != "draft_itinerary")
    if not isinstance(observation.last_outcome, dict):
        return allowed_actions
    outcome = observation.last_outcome
    candidate_summary = outcome.get("candidateSummary") if isinstance(outcome.get("candidateSummary"), dict) else {}
    pending_group_ids = {
        str(item.get("id") or "")
        for item in observation.candidate_state.pending_groups
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    outcome_pending_ids = {str(item) for item in candidate_summary.get("pendingCandidateRecordIds") or [] if str(item)}
    if (
        str(outcome.get("action") or "") == "resolve_poi"
        and str(outcome.get("status") or "") == "needs_confirmation"
        and candidate_summary.get("materialTradeoff") is True
        and pending_group_ids
        and outcome_pending_ids.intersection(pending_group_ids)
    ):
        return ("ask_user",)
    return allowed_actions


def request_context_requires_clarification(
    request_context: Optional[dict[str, Any]],
) -> bool:
    """Return whether the current planning root must stay in clarification.

    This is deliberately derived from server-owned request/checkpoint state,
    not from the Controller decision.  It is used both when constructing the
    Controller's allowed-action contract and again at the policy gate so an
    invalid or adversarial ``draft_itinerary`` response cannot bypass a
    pending clarification.
    """

    if not isinstance(request_context, dict):
        return False
    if _unresolved_trip_date_reason(request_context) is not None:
        return True
    checkpoint = (
        request_context.get("clarificationCheckpoint")
        if isinstance(request_context.get("clarificationCheckpoint"), dict)
        else {}
    )
    if str(checkpoint.get("status") or "") in {
        "awaiting_answer",
        "awaiting_agent_resolution",
        "candidate_refresh_required",
    }:
        return True
    request_contract = (
        request_context.get("requestIntentContract")
        if isinstance(request_context.get("requestIntentContract"), dict)
        else {}
    )
    requirements = (
        request_context.get("understoodRequirements")
        if isinstance(request_context.get("understoodRequirements"), dict)
        else {}
    )
    route_contract = (
        request_contract.get("routeDecisionContract")
        if isinstance(request_contract.get("routeDecisionContract"), dict)
        else {}
    )
    route_clarification_required = bool(
        route_contract
        and (
            str(route_contract.get("status") or "") != "ready"
            or bool(route_contract.get("missingFields"))
            or not isinstance(route_contract.get("mobilityProfile"), dict)
            or not route_contract.get("mobilityProfile")
            or not isinstance(route_contract.get("detourTolerance"), dict)
            or not route_contract.get("detourTolerance")
        )
    )
    return bool(
        request_contract.get("clarificationRequired") is True
        or requirements.get("highImpactAmbiguityDetected") is True
        or route_clarification_required
    )


def controller_route_policy_may_resolve_detour(
    request_context: Optional[dict[str, Any]],
    decision: Optional["AgentDecision"] = None,
) -> bool:
    """Allow a typed Controller estimate to close non-material route defaults.

    This does not make arbitrary missing planning facts model-owned.  Only the
    mobility/detour pair may be estimated, accessibility-sensitive requests and
    non-route ambiguities still require the user, and an existing user-facing
    checkpoint cannot be bypassed.  The estimate remains visibly attributable
    to the Controller and is expanded into server-owned scoring weights later.
    """

    context = request_context if isinstance(request_context, dict) else {}
    if _unresolved_trip_date_reason(context) is not None:
        return False
    if str(context.get("serverExecutionProfile") or "") != "simple_open_v1":
        return False
    checkpoint = context.get("clarificationCheckpoint") if isinstance(context.get("clarificationCheckpoint"), dict) else {}
    if str(checkpoint.get("status") or "") == "awaiting_answer":
        return False
    pending_free_text = (
        checkpoint.get("pendingFreeTextAnswer")
        if isinstance(checkpoint.get("pendingFreeTextAnswer"), dict)
        else {}
    )
    if str(pending_free_text.get("text") or "").strip():
        # The user has already supplied material intent.  Until the Controller
        # normalizes that exact answer, its defaults must not replace it.
        return False
    contract = context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
    route = contract.get("routeDecisionContract") if isinstance(contract.get("routeDecisionContract"), dict) else {}
    missing = {str(item) for item in route.get("missingFields") or [] if str(item)}
    supported_route_gaps = {"mobilityProfile", "detourTolerance"}
    if not missing or not missing.issubset(supported_route_gaps):
        return False
    provenance = route.get("provenance") if isinstance(route.get("provenance"), dict) else {}
    if (
        route.get("accessibilityFallbackRequired") is True
        or provenance.get("mobilitySensitive") is True
    ):
        return False
    unresolved_contract_dimensions = {
        str(item.get("dimensionId") or "")
        for item in contract.get("clarificationDimensions") or []
        if isinstance(item, dict)
        and str(item.get("status") or "unresolved") == "unresolved"
        and str(item.get("dimensionId") or "")
    }
    if any(not dimension.startswith("route_decision.") for dimension in unresolved_contract_dimensions):
        return False
    requirements = context.get("understoodRequirements")
    if isinstance(requirements, dict) and requirements.get("highImpactAmbiguityDetected") is True:
        return False
    unresolved = {
        str(item)
        for item in checkpoint.get("unresolvedDimensions") or []
        if str(item)
    }
    unresolved.update(
        str(item.get("dimensionId") or "")
        for item in checkpoint.get("ambiguities") or []
        if isinstance(item, dict)
        and item.get("resolved") is not True
        and str(item.get("dimensionId") or "")
    )
    if any(not dimension.startswith("route_decision.") for dimension in unresolved):
        return False
    detour_source = str(route.get("detourToleranceSource") or "")
    if detour_source in {"user_explicit", "opaque_clarification_answer"} and "detourTolerance" in missing:
        return False
    if decision is None:
        return True
    if decision.primary_action != "draft_itinerary":
        return False
    directive = decision.action_directive
    policy = getattr(directive, "route_planning_policy", None)
    if policy is None or getattr(policy, "source", None) != "controller_estimate":
        return False
    if "mobilityProfile" in missing:
        mobility = getattr(policy, "mobility_profile", None)
        if (
            mobility is None
            or getattr(mobility, "transport_mode", None)
            not in {"transit", "walking", "bicycling", "driving"}
            or getattr(mobility, "pace_class", None)
            not in {"relaxed", "standard", "intensive"}
        ):
            return False
    if "detourTolerance" in missing:
        envelope = getattr(policy, "detour_envelope", None)
        try:
            max_delta = float(getattr(envelope, "max_generalized_cost_delta"))
            max_ratio = float(getattr(envelope, "max_detour_ratio"))
        except (AttributeError, TypeError, ValueError):
            return False
        if max_delta <= 0 or not 0 <= max_ratio <= 2:
            return False
    return True


def controller_route_policy_requirement(
    request_context: Optional[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the compact producer contract for one safe route-only gap."""

    if not controller_route_policy_may_resolve_detour(request_context):
        return None
    context = request_context if isinstance(request_context, dict) else {}
    contract = (
        context.get("requestIntentContract")
        if isinstance(context.get("requestIntentContract"), dict)
        else {}
    )
    route = (
        contract.get("routeDecisionContract")
        if isinstance(contract.get("routeDecisionContract"), dict)
        else {}
    )
    missing = sorted(
        {
            str(item)
            for item in route.get("missingFields") or []
            if str(item) in {"mobilityProfile", "detourTolerance"}
        }
    )
    if not missing:
        return None
    return {
        "required": True,
        "source": "controller_estimate",
        "missingFields": missing,
        "mobilityProfile": {
            "required": "mobilityProfile" in missing,
            "fields": ["transportMode", "paceClass"],
        },
        "detourEnvelope": {
            "required": "detourTolerance" in missing,
            "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
        },
    }


def controller_draft_route_policy_repair_requirement() -> dict[str, Any]:
    """Return the complete typed policy contract for one draft repair.

    A repair response is a complete ModelDecisionV3 object rather than a patch.
    Therefore every draft-specific repair must carry both typed route structures,
    even when the authoritative request contract had already resolved one or both
    route dimensions before the Controller call.
    """

    return {
        "required": True,
        "source": "controller_estimate",
        "missingFields": ["detourTolerance", "mobilityProfile"],
        "mobilityProfile": {
            "required": True,
            "fields": ["transportMode", "paceClass"],
        },
        "detourEnvelope": {
            "required": True,
            "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
        },
    }


def controller_ready_route_policy_requires_typed_draft(
    request_context: Optional[dict[str, Any]],
) -> bool:
    """Require a complete draft policy when Simple Open already has route authority."""

    if not isinstance(request_context, dict):
        return False
    if str(request_context.get("serverExecutionProfile") or "") != "simple_open_v1":
        return False
    contract = (
        request_context.get("requestIntentContract")
        if isinstance(request_context.get("requestIntentContract"), dict)
        else {}
    )
    route_contract = contract.get("routeDecisionContract")
    return bool(
        isinstance(route_contract, dict)
        and route_contract.get("status") == "ready"
        and not list(route_contract.get("missingFields") or [])
    )


def simple_direction_generation_authorized(request_context: Optional[dict[str, Any]]) -> bool:
    """Recognize only a server-bound request to append one Simple direction."""

    if not isinstance(request_context, dict):
        return False
    resolution = (
        request_context.get("viewResolution")
        if isinstance(request_context.get("viewResolution"), dict)
        else {}
    )
    capability = (
        request_context.get("conversationCapability")
        if isinstance(request_context.get("conversationCapability"), dict)
        else {}
    )
    return bool(
        request_context.get("_simpleDirectionGenerationAuthorized") is True
        and str(resolution.get("schemaVersion") or "") == "agent-view-resolution-v1"
        and str(resolution.get("resolutionSource") or "") == "server_validated_opaque_choice"
        and str(resolution.get("resolvedAction") or "") == "generate_new_direction"
        and str(resolution.get("planningSelectionRootTurnId") or "")
        and str(resolution.get("rootPortfolioId") or "")
        and str(capability.get("reasonCode") or "") == "server_validated_opaque_choice"
        and str(capability.get("status") or "") == "unique"
        and str(capability.get("capability") or "") == "create_itinerary"
        and str(capability.get("planningSelectionRootTurnId") or "")
        == str(resolution.get("planningSelectionRootTurnId") or "")
        and str(capability.get("rootPortfolioId") or "")
        == str(resolution.get("rootPortfolioId") or "")
    )


class MemoryCandidate(BaseModel):
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    category: str = "preference"
    confidence: float = Field(ge=0.0, le=1.0)
    trip_only: bool = Field(default=False, alias="tripOnly")
    inferred: bool = False
    changes_long_term_experience: bool = Field(default=False, alias="changesLongTermExperience")
    evidence: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class _StrictScope(BaseModel):
    model_config = {"populate_by_name": True, "extra": "forbid"}


class EmptyTargetScope(_StrictScope):
    pass


class ResolvePoiTargetScope(_StrictScope):
    segment_ids: list[str] = Field(default_factory=list, alias="segmentIds")
    candidate_id: Optional[str] = Field(default=None, alias="candidateId")
    goal_id: Optional[str] = Field(default=None, alias="goalId")
    day_number: Optional[int] = Field(default=None, alias="dayNumber", ge=1)
    query: Optional[str] = None

    @model_validator(mode="after")
    def require_identity(self) -> "ResolvePoiTargetScope":
        if not self.segment_ids and not self.candidate_id and not self.query:
            raise ValueError("resolve_poi_scope_requires_identity")
        return self


class OptimizeRouteTargetScope(_StrictScope):
    base_version_id: str = Field(alias="baseVersionId", min_length=1)
    day_ids: list[str] = Field(default_factory=list, alias="dayIds")
    day_numbers: list[int] = Field(default_factory=list, alias="dayNumbers")
    route_pair_ids: list[str] = Field(default_factory=list, alias="routePairIds")
    optimization_objective: str = Field(default="balanced", alias="optimizationObjective")

    @model_validator(mode="after")
    def require_single_concrete_scope(self) -> "OptimizeRouteTargetScope":
        if self.base_version_id == "planner_fallback" or not (self.day_ids or self.day_numbers or self.route_pair_ids):
            raise ValueError("optimize_route_scope_requires_concrete_version_and_target")
        if len(self.day_ids) + len(self.day_numbers) > 1:
            raise ValueError("optimize_route_scope_must_be_single_day")
        return self


class PatchItineraryTargetScope(_StrictScope):
    base_version_id: Optional[str] = Field(default=None, alias="baseVersionId", min_length=1)
    segment_ids: list[str] = Field(default_factory=list, alias="segmentIds")
    day_ids: list[str] = Field(default_factory=list, alias="dayIds")
    operation_scope: Optional[str] = Field(default=None, alias="operationScope", min_length=1)
    mutation_intent: Optional[TimelineMutationIntent] = Field(default=None, alias="mutationIntent")
    start_time: Optional[str] = Field(default=None, alias="startTime")
    candidate_id: Optional[str] = Field(default=None, alias="candidateId")
    amap_poi_id: Optional[str] = Field(default=None, alias="amapPoiId")
    preserve: list[str] = Field(default_factory=list, alias="preserve")

    @model_validator(mode="after")
    def require_concrete_scope(self) -> "PatchItineraryTargetScope":
        if self.mutation_intent is not None:
            return self
        if self.base_version_id == "planner_fallback" or self.operation_scope in {
            "generic",
            "planner_fallback",
            "taskType",
        }:
            raise ValueError("patch_scope_contains_sentinel")
        if not self.segment_ids and not self.day_ids:
            raise ValueError("patch_scope_requires_target")
        if not self.base_version_id or not self.operation_scope:
            raise ValueError("patch_scope_requires_version_and_operation")
        return self


class VerifyExternalFactsTargetScope(_StrictScope):
    fact_types: list[Literal["weather", "risk", "reservation"]] = Field(alias="factTypes", min_length=1)
    segment_ids: list[str] = Field(default_factory=list, alias="segmentIds")
    date_range: dict[str, str] = Field(default_factory=dict, alias="dateRange")
    write_back_mode: Literal["derived_facts_only"] = Field(default="derived_facts_only", alias="writeBackMode")


TargetScope = Union[
    EmptyTargetScope,
    ResolvePoiTargetScope,
    OptimizeRouteTargetScope,
    PatchItineraryTargetScope,
    VerifyExternalFactsTargetScope,
]


class AgentDecision(BaseModel):
    schema_version: Literal["agent-decision-v1", "agent-decision-v2", "agent-decision-v3"] = Field(
        default="agent-decision-v1", alias="schemaVersion"
    )
    decision_id: str = Field(default_factory=lambda: f"decision_{uuid4().hex}", alias="decisionId")
    primary_action: PrimaryAction = Field(alias="primaryAction")
    confidence: float = Field(ge=0.0, le=1.0)
    decision_summary: str = Field(alias="decisionSummary")
    user_visible_reason: str = Field(default="", alias="userVisibleReason")
    reason_codes: list[str] = Field(default_factory=list, alias="reasonCodes")
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")
    required_capabilities: list[str] = Field(default_factory=list, alias="requiredCapabilities")
    action_directive: Optional[ActionDirective] = Field(default=None, alias="actionDirective")
    proposed_write_risk: WriteRisk = Field(default="none", alias="proposedWriteRisk")
    target_scope: TargetScope = Field(default_factory=EmptyTargetScope, alias="targetScope")
    assumption_policy: Literal["none", "allow_reversible", "require_confirmation"] = Field(
        default="none", alias="assumptionPolicy"
    )
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    clarification: Optional[dict[str, Any]] = None
    memory_policy: Literal["none", "trip_only", "propose_long_term", "confirm_long_term"] = Field(
        default="none", alias="memoryPolicy"
    )
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, alias="memoryCandidates")
    uncertainty_policy: Literal["none", "state_limit", "ask_user", "defer"] = Field(
        default="none", alias="uncertaintyPolicy"
    )
    uncertainties: list[dict[str, Any]] = Field(default_factory=list)
    execution_limits: dict[str, Any] = Field(default_factory=dict, alias="executionLimits")
    expected_outcome: dict[str, Any] = Field(default_factory=dict, alias="expectedOutcome")
    stop_condition: dict[str, Any] = Field(default_factory=dict, alias="stopCondition")
    fallback_action: PrimaryAction = Field(default="finish", alias="fallbackAction")
    side_effects: dict[str, bool] = Field(default_factory=dict, alias="sideEffects")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def scope_must_match_action(self) -> "AgentDecision":
        action_scope = {
            "resolve_poi": ResolvePoiTargetScope,
            "optimize_route": OptimizeRouteTargetScope,
            "patch_itinerary": PatchItineraryTargetScope,
            "verify_external_facts": VerifyExternalFactsTargetScope,
        }.get(self.primary_action)
        if action_scope is not None and not isinstance(self.target_scope, action_scope):
            if not (self.schema_version == "agent-decision-v3" and isinstance(self.target_scope, EmptyTargetScope)):
                raise ValueError("target_scope_action_mismatch")
        if action_scope is None and not isinstance(self.target_scope, EmptyTargetScope):
            raise ValueError("unexpected_target_scope_for_action")
        if self.schema_version in {"agent-decision-v2", "agent-decision-v3"}:
            if self.action_directive is None:
                raise ValueError("action_directive_required_for_v2")
            if self.action_directive.type != self.primary_action:
                raise ValueError("action_directive_action_mismatch")
        if self.schema_version == "agent-decision-v3" and not str(self.stop_condition.get("type") or "").strip():
            raise ValueError("stop_condition_type_required_for_v3")
        return self


class ModelDecisionV3(BaseModel):
    """Provider-facing V3 decision.

    The model owns only the next business action and its action-specific
    directive.  Risk, tools, stop conditions, user-visible text and target
    scope are server decisions derived after strict validation.
    """

    schema_version: Literal["agent-decision-v3"] = Field(alias="schemaVersion")
    primary_action: PrimaryAction = Field(alias="primaryAction")
    action_directive: ActionDirective = Field(alias="actionDirective")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def directive_must_match_action(self) -> "ModelDecisionV3":
        if self.action_directive.type != self.primary_action:
            raise ValueError("action_directive_action_mismatch")
        return self


class AgentDecisionLite(BaseModel):
    schema_version: Literal["agent-decision-lite-v1"] = Field(alias="schemaVersion")
    primary_action: PrimaryAction = Field(alias="primaryAction")
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(alias="reasonCode", min_length=1, max_length=120)
    user_visible_reason: str = Field(alias="userVisibleReason", min_length=1, max_length=300)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class GatedAgentDecision(BaseModel):
    decision: AgentDecision
    effective_write_risk: WriteRisk = Field(alias="effectiveWriteRisk")
    effective_tools: list[str] = Field(default_factory=list, alias="effectiveTools")
    policy_reason_codes: list[str] = Field(default_factory=list, alias="policyReasonCodes")
    accepted: bool = True

    model_config = {"populate_by_name": True}


class AutonomyContextProjector:
    """Projects the full request context into a bounded decision-only contract."""

    EXCLUDED_KEYS = {
        "providerdebug",
        "photos",
        "polyline",
        "routeoptions",
        "snapshot",
        "fullitinerary",
        "rawproviderr esponse",
        "rawproviderresponse",
        "fullnotes",
    }

    def __init__(self, *, max_turns: int = 6, max_text_chars: int = 1200, max_list_items: int = 8):
        self.max_turns = max_turns
        self.max_text_chars = max_text_chars
        self.max_list_items = max_list_items

    def project(self, request_context: dict[str, Any]) -> dict[str, Any]:
        snapshot = request_context.get("currentItinerarySnapshot") or {}
        timeline = request_context.get("timelineContext") or {}
        turns = request_context.get("activeConversationTurns") or []
        pending = request_context.get("pendingAmapPoiCandidates") or []
        observation = self._bounded_mapping(request_context.get("agentObservation"))
        projected = {
            "schemaVersion": "model-first-autonomy-context-v2",
            "sessionId": request_context.get("sessionId"),
            "latestUserMessage": self._text(request_context.get("latestUserMessage")),
            "effectiveUserMessage": self._text(request_context.get("effectiveUserMessage")),
            "selectedCity": request_context.get("selectedCity"),
            "activeVersionId": self._active_version_id(request_context, snapshot),
            "hasItinerary": bool(snapshot or timeline),
            "itinerarySummary": self._itinerary_summary(snapshot or timeline),
            "activeTarget": {
                "day": request_context.get("activeDay"),
                "segmentId": request_context.get("activeSegment"),
                "selectedMapPoiId": request_context.get("selectedMapPoiId"),
            },
            "pendingCandidates": [self._pending_summary(item) for item in pending[:5]],
            "selectedAgentChoice": self._bounded_mapping(request_context.get("selectedAgentChoice")),
            "clarificationCheckpoint": self._bounded_mapping(request_context.get("clarificationCheckpoint")),
            "continuationContext": self._bounded_mapping(request_context.get("continuationContext")),
            "retryIntent": self._bounded_mapping(request_context.get("retryIntent")),
            "retryExecutionPlan": self._bounded_mapping(request_context.get("retryExecutionPlan")),
            "viewResolution": self._bounded_mapping(request_context.get("viewResolution")),
            "simpleDirectionGenerationAuthorized": simple_direction_generation_authorized(request_context),
            "requestIntentContract": self._bounded_mapping(request_context.get("requestIntentContract")),
            "requirements": self._bounded_mapping(request_context.get("understoodRequirements")),
            "resolvedTripDates": self._bounded_mapping(request_context.get("resolvedTripDates")),
            "memoryRules": self._bounded_mapping(request_context.get("memoryRules")),
            "pendingMemoryConfirmations": list(request_context.get("pendingMemoryConfirmations") or [])[:5],
            "supportedPatchOperations": list(request_context.get("supportedPatchOperations") or []),
            "runtimeLimits": self._bounded_mapping(request_context.get("runtimeLimits")),
            "observation": observation,
            "allowedActions": list(AgentActionExecutorRegistry.ROUTES),
            "policyHints": {
                "allWritesVersioned": True,
                "finalPoiMustBeAmapGrounded": True,
                "askOnlyWhenMaterialChoiceRemains": True,
            },
            "recentTurns": [
                {
                    "role": item.get("role"),
                    "content": self._text(item.get("content"), 320),
                    "turnIndex": item.get("turnIndex"),
                    "turnId": item.get("id") or item.get("turnId"),
                    "itineraryVersionId": item.get("itineraryVersionId"),
                }
                for item in turns[-self.max_turns :]
                if isinstance(item, dict)
            ],
        }
        encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        projected["projectionTelemetry"] = {
            "contextBytes": len(encoded),
            "maxListItems": self.max_list_items,
            "maxTextChars": self.max_text_chars,
            "recursiveProjection": True,
        }
        return projected

    def _text(self, value: Any, limit: Optional[int] = None) -> str:
        text = str(value or "").strip()
        return text[: limit or self.max_text_chars]

    def _bounded_mapping(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            if str(key).lower().replace("_", "") in {item.replace(" ", "") for item in self.EXCLUDED_KEYS}:
                continue
            result[str(key)] = self._bounded_value(item)
        return result

    def _bounded_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._text(value, 500)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, dict):
            return self._bounded_mapping(value)
        if isinstance(value, list):
            return [self._bounded_value(item) for item in value[: self.max_list_items]]
        return self._text(value, 200)

    @staticmethod
    def _active_version_id(request_context: dict[str, Any], snapshot: Any) -> Optional[str]:
        if isinstance(snapshot, dict):
            return (
                snapshot.get("versionId") or snapshot.get("activeVersionId") or request_context.get("activeVersionId")
            )
        return request_context.get("activeVersionId")

    @staticmethod
    def _itinerary_summary(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        days = value.get("days") if isinstance(value.get("days"), list) else []
        return {
            "planId": value.get("id") or value.get("planId"),
            "title": value.get("title"),
            "dayCount": len(days),
            "segmentCount": sum(len(day.get("segments") or []) for day in days if isinstance(day, dict)),
        }

    @staticmethod
    def _pending_summary(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
        return {
            "id": item.get("id"),
            "status": item.get("status"),
            "sourceSegmentId": item.get("sourceSegmentId"),
            "candidateCount": len(item.get("candidates") or []),
            "candidates": [
                {
                    "candidateId": candidate.get("candidateId") or candidate.get("id"),
                    "amapPoiId": candidate.get("amapPoiId") or candidate.get("amapId"),
                    "name": candidate.get("name"),
                    "category": candidate.get("category"),
                }
                for candidate in candidates[:5]
                if isinstance(candidate, dict)
            ],
        }


class AgentDecisionPolicyGate:
    ACTION_ALLOWED_TOOLS: dict[str, set[str]] = {
        "ask_user": set(),
        "draft_itinerary": {"resolve_poi", "web_search", "amap_weather", "patch_itinerary"},
        "read_itinerary": {"read_itinerary"},
        "resolve_poi": {"resolve_poi"},
        "optimize_route": {"read_itinerary", "optimize_route", "patch_itinerary"},
        "patch_itinerary": {"read_itinerary", "resolve_poi", "patch_itinerary"},
        "verify_external_facts": {"web_search", "ticket_lookup", "amap_weather"},
        "finish": set(),
    }
    WRITE_RISK_BY_ACTION: dict[str, WriteRisk] = {
        "ask_user": "none",
        "read_itinerary": "none",
        "resolve_poi": "none",
        "finish": "none",
        "optimize_route": "medium",
        "patch_itinerary": "medium",
        "verify_external_facts": "none",
        "draft_itinerary": "high",
    }

    def evaluate(
        self,
        decision: AgentDecision,
        *,
        available_tools: set[str],
        runtime_budget_tools: set[str],
        observation: Optional[AgentObservation] = None,
        allow_partial_plan_expansion: bool = False,
        request_context: Optional[dict[str, Any]] = None,
    ) -> GatedAgentDecision:
        allowed = self.ACTION_ALLOWED_TOOLS[decision.primary_action]
        effective_tools = sorted(set(decision.required_tools) & allowed & available_tools & runtime_budget_tools)
        reasons: list[str] = []
        if set(decision.required_tools) != set(effective_tools):
            reasons.append("required_tools_clipped")
        effective_risk = self.WRITE_RISK_BY_ACTION[decision.primary_action]
        if effective_risk != decision.proposed_write_risk:
            reasons.append("write_risk_recomputed")
        accepted = True
        if (
            request_context_requires_clarification(request_context)
            and decision.primary_action not in clarification_safe_actions_for_request_context(request_context)
        ):
            accepted = False
            reasons.append("pending_clarification_requires_ask_user")
        elif (
            request_context_requires_clarification(request_context)
            and decision.primary_action == "draft_itinerary"
            and not controller_route_policy_may_resolve_detour(request_context, decision)
        ):
            accepted = False
            reasons.append("controller_route_policy_not_authorized")
        observation_allowed_actions = allowed_primary_actions_for_observation(observation)
        if allow_partial_plan_expansion or simple_direction_generation_authorized(request_context):
            observation_allowed_actions = tuple(dict.fromkeys((*observation_allowed_actions, "draft_itinerary")))
        if observation is not None and decision.primary_action not in observation_allowed_actions:
            accepted = False
            reasons.append("primary_action_not_allowed_by_observation")
        if not self._valid_target_scope(decision.primary_action, decision.target_scope.model_dump(by_alias=True)):
            accepted = False
            reasons.append("invalid_target_scope")
        elif decision.schema_version == "agent-decision-v2" and not self._directive_matches_target_scope(decision):
            accepted = False
            reasons.append("action_directive_target_scope_mismatch")
        elif observation is not None and not self._scope_belongs_to_observation(decision, observation):
            accepted = False
            reasons.append("target_scope_not_in_observation")
        if accepted and observation is not None and decision.primary_action == "draft_itinerary":
            draft_policy_errors = self._draft_goal_policy_errors(
                decision,
                observation,
                request_context=request_context,
            )
            if draft_policy_errors:
                accepted = False
                reasons.extend(draft_policy_errors)
        if not self._valid_stop_condition(decision.stop_condition):
            accepted = False
            reasons.append("invalid_stop_condition")
        clarification_valid = self._valid_clarification(
            decision.clarification
        ) or self._valid_nonplanning_retry_clarification(decision)
        if decision.primary_action == "ask_user" and not clarification_valid:
            accepted = False
            reasons.append("structured_clarification_required")
        if decision.uncertainty_policy == "ask_user" and not clarification_valid:
            accepted = False
            reasons.append("uncertainty_clarification_required")
        if decision.memory_policy in {"propose_long_term", "confirm_long_term"} and any(
            item.trip_only for item in decision.memory_candidates
        ):
            accepted = False
            reasons.append("trip_only_memory_escalation_forbidden")
        if decision.memory_candidates and decision.memory_policy == "none":
            accepted = False
            reasons.append("memory_policy_required")
        if any(not item.key.strip() or not item.value.strip() for item in decision.memory_candidates):
            accepted = False
            reasons.append("memory_candidate_schema_invalid")
        if decision.memory_policy == "trip_only" and any(not item.trip_only for item in decision.memory_candidates):
            accepted = False
            reasons.append("trip_only_memory_scope_required")
        if decision.assumption_policy == "allow_reversible" and any(
            not bool(item.get("reversible")) for item in decision.assumptions if isinstance(item, dict)
        ):
            accepted = False
            reasons.append("irreversible_assumption_forbidden")
        if decision.memory_policy == "propose_long_term" and any(
            item.confidence < 0.8 or item.inferred or item.changes_long_term_experience
            for item in decision.memory_candidates
        ):
            accepted = False
            reasons.append("long_term_memory_confirmation_required")
        return GatedAgentDecision(
            decision=decision,
            effectiveWriteRisk=effective_risk,
            effectiveTools=effective_tools,
            policyReasonCodes=reasons,
            accepted=accepted,
        )

    @staticmethod
    def _valid_clarification(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if not all(
            str(value.get(field) or "").strip() for field in ("question", "dimensionId", "whyItMatters")
        ) or not isinstance(value.get("allowFreeText"), bool):
            return False
        options = value.get("options")
        if not isinstance(options, list) or len(options) < 2:
            return False
        normalized = ClarificationCheckpointService._options(options)
        if len(normalized) != len(options):
            return False
        return True

    @staticmethod
    def _valid_nonplanning_retry_clarification(decision: AgentDecision) -> bool:
        """Validate the narrow legacy carrier for controller/runtime retries.

        Retry choices do not answer a planning ambiguity and therefore must
        not be persisted as a clarification checkpoint.  They remain valid
        only when the typed directive supplies opaque ``choiceIds`` and every
        mirrored option is an explicit safe continuation action without a
        business semantic value.
        """

        directive = decision.action_directive
        if directive is None or getattr(directive, "type", None) != "ask_user":
            return False
        if getattr(directive, "dimension_id", None):
            return False
        choice_ids = [str(item) for item in getattr(directive, "choice_ids", []) if str(item)]
        value = decision.clarification
        options = value.get("options") if isinstance(value, dict) else None
        if not choice_ids or not isinstance(options, list) or not options:
            return False
        option_ids = [str(item.get("id") or "") for item in options if isinstance(item, dict)]
        if option_ids != choice_ids:
            return False
        return all(
            isinstance(item, dict)
            and item.get("semanticValue") is None
            and str(item.get("action") or "")
            in {
                "retry_model_planning",
                "confirm_rule_safe_draft",
                "manual_continuation",
            }
            for item in options
        )

    @staticmethod
    def _valid_stop_condition(value: Any) -> bool:
        return isinstance(value, dict) and bool(str(value.get("type") or "").strip())

    @staticmethod
    def _valid_target_scope(action: str, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        if action in {"ask_user", "draft_itinerary", "read_itinerary", "finish"}:
            return True
        if action == "resolve_poi":
            return bool(value.get("segmentIds") or value.get("candidateId") or value.get("query"))
        if action == "optimize_route":
            return bool(value.get("baseVersionId")) and bool(
                value.get("dayIds") or value.get("dayNumbers") or value.get("routePairIds")
            )
        if action == "patch_itinerary":
            semantic_intent = value.get("mutationIntent")
            if isinstance(semantic_intent, dict):
                return bool(semantic_intent.get("operation")) and isinstance(semantic_intent.get("selector"), dict)
            return bool(value.get("baseVersionId")) and bool(
                value.get("segmentIds") or value.get("dayIds") or value.get("operationScope")
            )
        if action == "verify_external_facts":
            return bool(value.get("factTypes")) and value.get("writeBackMode") == "derived_facts_only"
        return False

    @staticmethod
    def _directive_matches_target_scope(decision: AgentDecision) -> bool:
        directive = decision.action_directive
        if directive is None:
            return False
        action = decision.primary_action
        scope = decision.target_scope.model_dump(by_alias=True, exclude_none=True)
        value = directive.model_dump(by_alias=True, exclude_none=True)

        def same_ids(directive_key: str, scope_key: str) -> bool:
            return {str(item) for item in value.get(directive_key) or []} == {
                str(item) for item in scope.get(scope_key) or []
            }

        if action == "patch_itinerary":
            return (
                str(value.get("operationIntent") or "") == str(scope.get("operationScope") or "")
                and str(value.get("baseVersionId") or "") == str(scope.get("baseVersionId") or "")
                and same_ids("targetSegmentIds", "segmentIds")
                and str(value.get("startTime") or "") == str(scope.get("startTime") or "")
                and str(value.get("candidateId") or "") == str(scope.get("candidateId") or "")
                and str(value.get("amapPoiId") or "") == str(scope.get("amapPoiId") or "")
                and same_ids("preserve", "preserve")
            )
        if action == "resolve_poi":
            return (
                same_ids("targetSegmentIds", "segmentIds")
                and str(value.get("targetGoalId") or "") == str(scope.get("goalId") or "")
                and str(value.get("searchIntent") or "") == str(scope.get("query") or "")
            )
        if action == "optimize_route":
            return (
                str(value.get("baseVersionId") or "") == str(scope.get("baseVersionId") or "")
                and same_ids("dayIds", "dayIds")
                and same_ids("dayNumbers", "dayNumbers")
                and str(value.get("optimizationObjective") or "") == str(scope.get("optimizationObjective") or "")
            )
        if action == "verify_external_facts":
            return same_ids("factTypes", "factTypes") and same_ids("segmentIds", "segmentIds")
        return True

    @staticmethod
    def _scope_belongs_to_observation(decision: AgentDecision, observation: AgentObservation) -> bool:
        value = decision.target_scope.model_dump(by_alias=True)
        inventory = observation.target_inventory
        expected_version_id = str(observation.version_lineage.current_version_id or "")
        requested_version_id = str(value.get("baseVersionId") or "")
        if requested_version_id and requested_version_id != expected_version_id:
            return False
        if value.get("segmentIds") and not set(str(item) for item in value["segmentIds"]).issubset(
            set(inventory.segment_ids)
        ):
            return False
        goal_id = str(value.get("goalId") or "")
        if goal_id:
            observed_goal_ids = {
                str(item.get("goalId") or item.get("intentType") or "")
                for item in observation.requirement_coverage.required
                if isinstance(item, dict)
            }
            if goal_id not in observed_goal_ids:
                return False
            requested_segment_ids = {str(item) for item in value.get("segmentIds") or [] if str(item)}
            if requested_segment_ids:
                segment_goals = {
                    str(item.segment_id): str(item.goal_id or "")
                    for item in observation.segment_refs
                    if str(item.segment_id) in requested_segment_ids
                }
                if set(segment_goals) != requested_segment_ids or any(
                    segment_goal != goal_id for segment_goal in segment_goals.values()
                ):
                    return False
        if value.get("dayIds") and not set(str(item) for item in value["dayIds"]).issubset(set(inventory.day_ids)):
            return False
        if value.get("dayNumbers") and not set(int(item) for item in value["dayNumbers"]).issubset(
            set(inventory.day_numbers)
        ):
            return False
        candidate_id = str(value.get("candidateId") or "")
        amap_poi_id = str(value.get("amapPoiId") or "")
        if candidate_id or amap_poi_id:
            if not candidate_id or not amap_poi_id:
                return False
            group = next(
                (
                    item
                    for item in observation.candidate_state.pending_groups
                    if isinstance(item, dict) and str(item.get("id") or "") == candidate_id
                ),
                None,
            )
            if group is None:
                return False
            safe_ids = {
                str(candidate.get("id") or "")
                for candidate in group.get("safeCandidates") or []
                if isinstance(candidate, dict) and str(candidate.get("id") or "")
            }
            if amap_poi_id not in safe_ids:
                return False
            source_segment_id = str(group.get("sourceSegmentId") or "")
            requested_segment_ids = {str(item) for item in value.get("segmentIds") or [] if str(item)}
            if source_segment_id and source_segment_id not in requested_segment_ids:
                return False
        return True

    @staticmethod
    def _draft_goal_policy_errors(
        decision: AgentDecision,
        observation: AgentObservation,
        *,
        request_context: Optional[dict[str, Any]] = None,
    ) -> list[str]:
        directive = decision.action_directive
        if directive is None or getattr(directive, "type", None) != "draft_itinerary":
            return ["draft_directive_missing"]
        observed_goals = {
            str(item.get("goalId") or item.get("intentType") or "").strip(): int(
                item.get("requiredMin") or item.get("target") or item.get("requiredCount") or 0
            )
            for item in observation.requirement_coverage.required
            if str(item.get("goalId") or item.get("intentType") or "").strip()
        }
        hard_required_goal_counts = {
            str(item.get("goalId") or item.get("intentType") or "").strip(): int(
                item.get("requiredMin") or item.get("target") or item.get("requiredCount") or 0
            )
            for item in observation.requirement_coverage.required
            if int(item.get("requiredMin") or item.get("target") or item.get("requiredCount") or 0) > 0
            and str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
            and str(item.get("goalId") or item.get("intentType") or "").strip()
        }
        goal_cardinality = {
            str(item.get("goalId") or item.get("intentType") or "").strip(): {
                "min": int(item.get("requiredMin") or item.get("minCount") or item.get("target") or 0),
                "max": int(item["maxCount"]) if item.get("maxCount") is not None else None,
                "allowedDays": {int(day) for day in item.get("allowedDayNumbers") or [] if isinstance(day, int)},
                "distributionPolicy": str(item.get("distributionPolicy") or "spread_across_distinct_days"),
                "requirementLevel": str(item.get("requirementLevel") or "required"),
                "intentType": str(item.get("intentType") or ""),
            }
            for item in observation.requirement_coverage.required
            if str(item.get("goalId") or item.get("intentType") or "").strip()
        }
        scheduled_goal_counts: dict[str, int] = {}
        required_scheduled_goal_counts: dict[str, int] = {}
        scheduled_days: dict[str, set[int]] = {}
        required_scheduled_days: dict[str, set[int]] = {}
        referenced_goal_ids = set(getattr(directive, "goal_priority", []) or [])
        daily_cardinality_invalid = False
        day_not_allowed = False
        soft_goal_misclassified = False
        for day in getattr(directive, "day_strategies", []) or []:
            explicit_counts = dict(getattr(day, "required_goal_counts", {}) or {})
            day_number = int(getattr(day, "day_number", 0) or 0)
            required_ids = list(getattr(day, "required_goal_ids", []) or [])
            optional_ids = list(getattr(day, "optional_goal_ids", []) or [])
            referenced_goal_ids.update(required_ids)
            referenced_goal_ids.update(optional_ids)
            referenced_goal_ids.update(explicit_counts)
            if len(required_ids) != len(set(required_ids)):
                daily_cardinality_invalid = True
            for goal_id in required_ids:
                if goal_cardinality.get(goal_id, {}).get("requirementLevel") in {
                    "soft_experience",
                    "optional",
                }:
                    soft_goal_misclassified = True
                count = int(explicit_counts.get(goal_id) or 1)
                if count != 1:
                    daily_cardinality_invalid = True
                scheduled_goal_counts[goal_id] = scheduled_goal_counts.get(goal_id, 0) + count
                required_scheduled_goal_counts[goal_id] = required_scheduled_goal_counts.get(goal_id, 0) + count
                scheduled_days.setdefault(goal_id, set()).add(day_number)
                required_scheduled_days.setdefault(goal_id, set()).add(day_number)
                allowed_days = goal_cardinality.get(goal_id, {}).get("allowedDays") or set()
                if allowed_days and day_number not in allowed_days:
                    day_not_allowed = True
            for goal_id in optional_ids:
                if goal_id in required_ids:
                    daily_cardinality_invalid = True
                    continue
                scheduled_goal_counts[goal_id] = scheduled_goal_counts.get(goal_id, 0) + 1
                scheduled_days.setdefault(goal_id, set()).add(day_number)
                allowed_days = goal_cardinality.get(goal_id, {}).get("allowedDays") or set()
                if allowed_days and day_number not in allowed_days:
                    day_not_allowed = True
        errors: list[str] = []
        intent_contract = observation.request.intent_contract
        if isinstance(request_context, dict):
            projected_contract = request_context.get("requestIntentContract")
            if isinstance(projected_contract, dict):
                intent_contract = projected_contract
            elif any(
                key in request_context
                for key in ("requiredPlanningDayNumbers", "explicitRestDayNumbers")
            ):
                intent_contract = request_context
        if isinstance(intent_contract, dict) and "requiredPlanningDayNumbers" in intent_contract:
            required_day_numbers = {
                int(day)
                for day in intent_contract.get("requiredPlanningDayNumbers") or []
                if isinstance(day, int) and not isinstance(day, bool) and int(day) > 0
            }
            strategy_day_counts = Counter(
                int(getattr(day, "day_number", 0) or 0)
                for day in getattr(directive, "day_strategies", []) or []
            )
            if any(strategy_day_counts.get(day, 0) != 1 for day in required_day_numbers):
                errors.append("draft_required_day_coverage_incomplete")
            authoritative_non_meal_goal_ids = {
                goal_id
                for goal_id, rules in goal_cardinality.items()
                if rules.get("intentType") != "meal"
                and rules.get("requirementLevel") not in {"soft_experience", "optional"}
                and int(rules.get("min") or 0) > 0
            }
            scheduled_goal_ids_by_day: dict[int, set[str]] = {}
            for goal_id, days in scheduled_days.items():
                for day_number in days:
                    scheduled_goal_ids_by_day.setdefault(day_number, set()).add(goal_id)
            if any(
                not scheduled_goal_ids_by_day.get(day_number, set()).intersection(
                    authoritative_non_meal_goal_ids
                )
                for day_number in required_day_numbers
            ):
                errors.append("draft_required_day_anchor_missing")
        if not referenced_goal_ids.issubset(set(observed_goals)):
            errors.append("draft_goal_id_not_in_observation")
        if any(
            required_scheduled_goal_counts.get(goal_id, 0) < required_count
            for goal_id, required_count in hard_required_goal_counts.items()
        ):
            errors.append("required_goal_omitted_from_planning_directive")
            errors.append("draft_goal_cardinality_underallocated")
        if daily_cardinality_invalid:
            errors.append("draft_goal_daily_cardinality_exceeded")
        if soft_goal_misclassified:
            errors.append("draft_soft_goal_misclassified_as_required")
        if any(
            rules.get("max") is not None and scheduled_goal_counts.get(goal_id, 0) > int(rules["max"])
            for goal_id, rules in goal_cardinality.items()
        ):
            errors.append("draft_goal_cardinality_overallocated")
        if day_not_allowed:
            errors.append("draft_goal_day_not_allowed")
        if any(
            rules.get("distributionPolicy") == "every_allowed_day"
            and rules.get("allowedDays")
            and (
                required_scheduled_days.get(goal_id, set())
                if rules.get("requirementLevel") not in {"soft_experience", "optional"}
                else scheduled_days.get(goal_id, set())
            )
            != rules.get("allowedDays")
            for goal_id, rules in goal_cardinality.items()
            if int(rules.get("min") or 0) > 0
        ):
            errors.append("draft_goal_distribution_invalid")
        return errors


class AgentDecisionArbitrator:
    """Chooses only high-confidence, no-model routes from canonical observation."""

    _INITIAL_REQUEST_RE = re.compile(r"(旅行|行程|\d+\s*日游|[一二三四五六七八九十两]+日游|参观|游览|体验|规划|安排)")
    _ROUTE_COMMAND_RE = re.compile(r"(优化.*路线|路线.*优化|少换乘|调整交通|换成.*(?:地铁|公交)|重排路线)")
    _READ_RE = re.compile(
        r"(看看|查看|当前行程|安排是什么|只读|不要改|先不改|哪个行程|安排在哪|第几天|哪一天|几点|有没有)"
    )
    _FACT_RE = re.compile(r"(预约|开放时间|门票|风险|预警|天气|官方|联网|核验)")

    def decide(self, observation: AgentObservation) -> Optional[AgentDecision]:
        # Action arbitration is about what the user asked *this turn*.  The
        # effective message may deliberately retain initial constraints for
        # downstream planning, so using it here can replay an old draft action
        # on a later read-only fact check.
        message = observation.request.latest_message or observation.request.effective_message
        if observation.invariant_errors:
            return self._finish("观察状态存在矛盾，已停止自动写入。", "observation_invalid", "observation_invalid")
        if (
            observation.itinerary.lifecycle_state == "empty_scaffold"
            and self._INITIAL_REQUEST_RE.search(message)
            and self._is_complete_initial_request(message)
        ):
            return AgentDecision(
                primaryAction="ask_user",
                confidence=1.0,
                decisionSummary="Controller 不可用，初始规划需由用户选择安全降级方式。",
                userVisibleReason="模型规划暂不可用；可重试、显式使用规则安全草稿，或手动补充。",
                reasonCodes=["empty_scaffold_controller_fallback_requires_choice"],
                requiredTools=[],
                actionDirective={
                    "type": "ask_user",
                    "question": "模型规划暂不可用，下一步怎么处理？",
                    "choiceIds": [
                        "retry_model_planning",
                        "confirm_rule_safe_draft",
                        "manual_continuation",
                    ],
                },
                proposedWriteRisk="none",
                targetScope={},
                clarification={
                    "question": "模型规划暂不可用，下一步怎么处理？",
                    "options": [
                        {
                            "id": "retry_model_planning",
                            "action": "retry_model_planning",
                            "label": "重试模型规划",
                            "allowsManualInput": False,
                        },
                        {
                            "id": "confirm_rule_safe_draft",
                            "action": "confirm_rule_safe_draft",
                            "label": "使用规则安全草稿",
                            "allowsManualInput": False,
                        },
                        {
                            "id": "manual_continuation",
                            "action": "manual_continuation",
                            "label": "我自己补充",
                            "allowsManualInput": True,
                        },
                    ],
                },
                uncertaintyPolicy="ask_user",
                expectedOutcome={"taskRoute": "clarification"},
                stopCondition={"type": "needs_confirmation"},
                sideEffects={"conversation": True, "itinerary": False},
            )
        if (
            observation.itinerary.meaningful_segment_count
            and self._FACT_RE.search(message)
            and not self._ROUTE_COMMAND_RE.search(message)
        ):
            return AgentDecision(
                primaryAction="verify_external_facts",
                confidence=0.98,
                decisionSummary="用户只要求核验外部事实，默认不改行程。",
                userVisibleReason="将核验预约、风险或天气信息，不改动时间轴。",
                reasonCodes=["external_fact_request"],
                requiredTools=["web_search", "ticket_lookup", "amap_weather"],
                proposedWriteRisk="none",
                targetScope={"factTypes": self._fact_types(message), "writeBackMode": "derived_facts_only"},
                expectedOutcome={"taskRoute": "verify_external_facts"},
                stopCondition={"type": "facts_collected"},
                sideEffects={"conversation": True, "derivedFacts": True},
            )
        if observation.request.explicit_no_write or self._READ_RE.search(message):
            return AgentDecision(
                primaryAction="read_itinerary",
                confidence=0.98,
                decisionSummary="用户请求只读查看。",
                userVisibleReason="只读取当前行程，不会修改版本。",
                reasonCodes=["explicit_read_only"],
                requiredTools=["read_itinerary"],
                proposedWriteRisk="none",
                targetScope={},
                expectedOutcome={"taskRoute": "read_only"},
                stopCondition={"type": "read_complete"},
                sideEffects={"conversation": True},
            )
        # Transport preference itself is intentionally not a route command.
        return None

    @staticmethod
    def _fact_types(message: str) -> list[str]:
        types = []
        if re.search(r"天气", message):
            types.append("weather")
        if re.search(r"风险|预警|公告|限流", message):
            types.append("risk")
        if re.search(r"预约|门票|开放时间|官方", message):
            types.append("reservation")
        return types or ["risk", "reservation"]

    @staticmethod
    def _is_complete_initial_request(message: str) -> bool:
        """Only bypass the controller for a truly specified trip, not a terse edit-like request."""
        has_dates = bool(re.search(r"(\d{1,2}月\d{1,2}日|国庆|十一|明天|后天)", message))
        has_days = bool(
            re.search(r"(\d+\s*天|[一二三四五六七八九十两]+天|\d+\s*日游|[一二三四五六七八九十两]+日游)", message)
        )
        has_party = bool(re.search(r"\d+\s*人", message))
        has_budget = bool(re.search(r"(预算|\d+\s*(?:元|块)|低预算|中等预算|高预算)", message))
        has_transport = bool(re.search(r"(公交|地铁|打车|自驾|公共交通)", message))
        return has_dates and has_days and has_party and has_budget and has_transport

    @staticmethod
    def _finish(summary: str, reason: str, stop: str) -> AgentDecision:
        return AgentDecision(
            primaryAction="finish",
            confidence=1.0,
            decisionSummary=summary,
            userVisibleReason=summary,
            reasonCodes=[reason],
            proposedWriteRisk="none",
            targetScope={},
            uncertaintyPolicy="state_limit",
            expectedOutcome={"taskRoute": "terminal_response"},
            stopCondition={"type": stop},
            sideEffects={"conversation": True},
        )


@dataclass(frozen=True)
class AgentDecisionResult:
    decision: AgentDecision
    gated_decision: GatedAgentDecision
    source: Literal[
        "controller",
        "planner_fallback",
        "deterministic_fast_path",
        "deterministic_arbitrator",
        "safe_fallback",
    ]
    controller_error: Optional[str]
    planner_plan: dict[str, Any]
    decision_duration_ms: int = 0
    schema_repair_attempts: int = 0
    planner_called: bool = False
    deterministic_arbitrator_called: bool = False
    decision_path: Literal["full", "lite", "fallback", "deterministic"] = "fallback"
    controller_full_called: bool = False
    controller_full_succeeded: bool = False
    controller_lite_called: bool = False
    controller_lite_succeeded: bool = False
    controller_failures: tuple[ControllerFailure, ...] = ()
    controller_performance: tuple[dict[str, Any], ...] = ()
    configured_full_timeout_seconds: float = 0.0
    configured_lite_timeout_seconds: float = 0.0
    configured_total_budget_seconds: float = 0.0
    decision_contract_version: str = ""
    decision_contract_hash: str = ""
    normalization_aliases: tuple[str, ...] = ()
    initial_call_ms: int = 0
    normalization_ms: int = 0
    repair_reserved_ms: int = 0
    repair_call_ms: int = 0
    validation_ms: int = 0
    provider_raw_decisions: tuple[dict[str, Any], ...] = ()
    normalized_decision: Optional[dict[str, Any]] = None
    invocation_ledger: Optional[dict[str, int]] = None

    def to_event_metadata(self, *, actual_execution_route: Optional[str] = None) -> dict[str, Any]:
        decision = self.decision.model_dump(by_alias=True, exclude_none=True)
        proposed_route = AgentActionExecutorRegistry().route(
            {
                "accepted": self.gated_decision.accepted,
                "primaryAction": self.decision.primary_action,
                "actionDirective": decision.get("actionDirective"),
            }
        )
        actual_route = actual_execution_route
        controller_called = self.controller_full_called or self.controller_lite_called
        return {
            "decisionId": decision["decisionId"],
            "shadowMode": False,
            "controlMode": "active",
            "source": self.source,
            "primaryAction": decision["primaryAction"],
            "confidence": decision["confidence"],
            "decisionSummary": decision["decisionSummary"],
            "userVisibleReason": decision.get("userVisibleReason", ""),
            "reasonCodes": decision.get("reasonCodes", []),
            "assumptions": decision.get("assumptions", []),
            "clarification": decision.get("clarification"),
            "uncertaintyPolicy": decision.get("uncertaintyPolicy", "none"),
            "uncertainties": decision.get("uncertainties", []),
            "memoryPolicy": decision.get("memoryPolicy", "none"),
            "memoryCandidates": decision.get("memoryCandidates", []),
            "requiredTools": decision.get("requiredTools", []),
            "requiredCapabilities": decision.get("requiredCapabilities", []),
            "actionDirective": decision.get("actionDirective"),
            "effectiveTools": self.gated_decision.effective_tools,
            "effectiveWriteRisk": self.gated_decision.effective_write_risk,
            "targetScope": decision.get("targetScope", {}),
            "stopCondition": decision.get("stopCondition", {}),
            "expectedOutcome": decision.get("expectedOutcome", {}),
            "policyReasonCodes": self.gated_decision.policy_reason_codes,
            "accepted": self.gated_decision.accepted,
            "controllerError": self.controller_error,
            "plannerTaskRoute": self.planner_plan.get("taskRoute"),
            "plannerCalled": self.planner_called,
            "plannerFallbackUsed": self.source == "planner_fallback",
            "deterministicArbitratorCalled": self.deterministic_arbitrator_called,
            "timelineParserCalledBeforeDecision": False,
            "decisionPath": self.decision_path,
            "controlOwner": (
                "model_controller_lite"
                if self.decision_path == "lite"
                else "model_controller"
                if self.decision_path == "full"
                else "safe_fallback"
            ),
            "controllerCalled": controller_called,
            "controllerSucceeded": self.source == "controller",
            "controllerFullCalled": self.controller_full_called,
            "controllerFullSucceeded": self.controller_full_succeeded,
            "controllerLiteCalled": self.controller_lite_called,
            "controllerLiteSucceeded": self.controller_lite_succeeded,
            "controllerFailureClass": (
                self.controller_failures[-1].failure_class if self.controller_failures else None
            ),
            "controllerFailures": [failure.to_dict() for failure in self.controller_failures],
            "controllerPerformance": [dict(item) for item in self.controller_performance],
            "invocationLedger": dict(self.invocation_ledger or {}),
            "controllerTimeoutConfig": {
                "fullSeconds": self.configured_full_timeout_seconds,
                "liteSeconds": self.configured_lite_timeout_seconds,
                "totalBudgetSeconds": self.configured_total_budget_seconds,
                "deadlineSource": "agent_runtime_remaining_and_controller_total_budget",
            },
            "actionDirectiveSource": "model"
            if self.source == "controller" and decision.get("actionDirective")
            else None,
            "proposedExecutionRoute": proposed_route,
            "actualExecutionRoute": actual_route,
            "executionOverride": actual_route != proposed_route,
            "decisionDurationMs": self.decision_duration_ms,
            "schemaRepairAttempts": self.schema_repair_attempts,
            "decisionContractVersion": self.decision_contract_version,
            "decisionContractHash": self.decision_contract_hash,
            "normalizationAliases": list(self.normalization_aliases),
            "decisionTiming": {
                "initialCallMs": self.initial_call_ms,
                "normalizationMs": self.normalization_ms,
                "repairReservedMs": self.repair_reserved_ms,
                "repairCallMs": self.repair_call_ms,
                "validationMs": self.validation_ms,
            },
            "providerRawDecisions": list(self.provider_raw_decisions),
            "normalizedDecision": self.normalized_decision,
        }


class AgentAutonomyController:
    """Active decision controller. Provider implementations may expose decide(context)."""

    # A bounded shared executor avoids creating a new non-cancellable worker
    # for every controller decision. Provider calls still receive the same
    # per-decision deadline; timed-out work cannot grow unbounded across turns.
    _decision_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-autonomy-decision")
    _decision_worker_slots = BoundedSemaphore(value=4)
    _AUTHORITATIVE_DRAFT_CONTRACT_REASON_CODES = frozenset(
        {
            "draft_goal_daily_cardinality_exceeded",
            "draft_goal_cardinality_underallocated",
            "draft_goal_cardinality_overallocated",
            "draft_goal_maximum_cardinality_exceeded",
            "draft_goal_day_not_allowed",
            "draft_goal_distribution_invalid",
            "required_goal_omitted_from_planning_directive",
            "draft_soft_goal_misclassified_as_required",
            "draft_optional_experience_budget_exceeded",
            "draft_scheduled_goal_day_duplicate",
            "draft_required_day_coverage_incomplete",
            "draft_required_day_anchor_missing",
            "draft_required_day_occurrence_coverage_incomplete",
            "draft_occurrence_schedule_hint_duplicate",
            "draft_occurrence_schedule_hint_coverage_incomplete",
            "draft_occurrence_schedule_hint_extra",
            "draft_occurrence_schedule_hint_clock_required",
            "draft_occurrence_schedule_hint_clock_invalid",
        }
    )

    def __init__(
        self,
        provider: Any = None,
        planner_service: Optional[AgentPlannerService] = None,
        fallback_decision_resolver: Any = None,
        *,
        decision_timeout_seconds: float = 10.0,
        lite_timeout_seconds: float = 2.5,
        total_budget_seconds: float = 12.5,
        max_schema_repair_attempts: int = 1,
    ):
        self.provider = provider
        self.planner_service = planner_service or AgentPlannerService()
        self.fallback_decision_resolver = fallback_decision_resolver
        # Honour bounded short deadlines used by callers and tests. Clamping an
        # explicit 30 ms deadline to 100 ms made the fail-closed path miss its
        # own latency contract under ordinary scheduler overhead.
        self.decision_timeout_seconds = max(0.01, float(decision_timeout_seconds))
        self.lite_timeout_seconds = max(0.01, float(lite_timeout_seconds))
        self.total_budget_seconds = max(
            0.01,
            min(float(total_budget_seconds), self.decision_timeout_seconds + self.lite_timeout_seconds),
        )
        self.max_schema_repair_attempts = min(1, max(0, int(max_schema_repair_attempts)))
        self.contract_service = AgentDecisionContractService(
            decision_model=ModelDecisionV3,
            allowed_actions=PRIMARY_ACTION_VALUES,
        )
        self.normalizer = AgentDecisionNormalizer()

    @classmethod
    def _requires_authoritative_draft_fallback(cls, error: Exception) -> bool:
        if isinstance(error, DecisionNormalizationError):
            return error.reason_code in cls._AUTHORITATIVE_DRAFT_CONTRACT_REASON_CODES

        # Pydantic wraps model-validator ``ValueError`` instances in a
        # ValidationError before the explicit normalization pass runs.  Read
        # only the structured error message/context here; never scan the raw
        # provider input, otherwise a POI/theme containing a reason-code-like
        # string could accidentally authorize the deterministic fallback.
        errors = getattr(error, "errors", None)
        if not callable(errors):
            return False
        try:
            validation_details = errors(include_input=False, include_url=False)
        except TypeError:  # pragma: no cover - compatibility with older Pydantic
            validation_details = errors()
        for detail in validation_details:
            if not isinstance(detail, dict):
                continue
            # A live Full Controller response used the scalar value ``1`` for
            # DraftDayStrategy.requiredGoalCounts.  The strict schema must keep
            # rejecting that value, but the server already owns the complete
            # per-day goal ledger and can rebuild this one directive without a
            # second model call.  Match only the structured Pydantic error type
            # and exact directive path; never normalize provider input here.
            location = tuple(str(item) for item in detail.get("loc") or ())
            if str(detail.get("type") or "") == "extra_forbidden" and location == (
                "actionDirective",
                "draft_itinerary",
                "requiredGoalCounts",
            ):
                # One recorded Full response copied the authoritative aggregate
                # into the directive root even though the field is legal only
                # on dayStrategies[*]. Keep extra="forbid" intact and recover
                # only this exact path from the server-owned per-day ledger.
                return True
            if (
                str(detail.get("type") or "") == "dict_type"
                and len(location) >= 5
                and location[:3] == ("actionDirective", "draft_itinerary", "dayStrategies")
                and location[-1] == "requiredGoalCounts"
            ):
                return True
            context = detail.get("ctx") if isinstance(detail.get("ctx"), dict) else {}
            messages = (detail.get("msg"), context.get("error"))
            for message in messages:
                tokens = set(re.findall(r"[a-z][a-z0-9_]+", str(message or "")))
                if tokens.intersection(cls._AUTHORITATIVE_DRAFT_CONTRACT_REASON_CODES):
                    return True
        return False

    def decide(
        self,
        latest_message: str,
        request_context: dict[str, Any],
        autonomy_context: dict[str, Any],
        *,
        available_tools: set[str],
        runtime_budget_tools: set[str],
        deterministic_fast_path: bool = False,
        deterministic_decision: Optional[AgentDecision] = None,
        observation: Optional[AgentObservation] = None,
    ) -> AgentDecisionResult:
        started = time.monotonic()
        runtime_limits = (
            request_context.get("runtimeLimits") if isinstance(request_context.get("runtimeLimits"), dict) else {}
        )
        remaining_run_ms = runtime_limits.get("remainingRunMs")
        runtime_budget_seconds = (
            max(0.0, float(remaining_run_ms) / 1000.0)
            if isinstance(remaining_run_ms, (int, float))
            else self.total_budget_seconds
        )
        effective_total_budget = min(self.total_budget_seconds, runtime_budget_seconds)
        total_deadline = started + effective_total_budget
        planner_plan: dict[str, Any] = {}
        planner_called = False
        deterministic_arbitrator_called = False
        source: Literal[
            "controller",
            "planner_fallback",
            "deterministic_fast_path",
            "deterministic_arbitrator",
            "safe_fallback",
        ] = "planner_fallback"
        controller_error: Optional[str] = None
        controller_failures: list[ControllerFailure] = []
        controller_performance: list[dict[str, Any]] = []
        controller_full_called = False
        controller_full_succeeded = False
        controller_lite_called = False
        controller_lite_succeeded = False
        decision_path: Literal["full", "lite", "fallback", "deterministic"] = "fallback"
        schema_repair_attempts = 0
        allowed_actions = allowed_primary_actions_for_observation(observation)
        clarification_required = request_context_requires_clarification(request_context)
        if clarification_required:
            allowed_actions = controller_allowed_actions_for_request_context(request_context)
        else:
            if (
                str(request_context.get("serverExecutionProfile") or "") == "simple_open_v1"
                and allowed_actions != ("ask_user",)
            ):
                # ClarificationPlanCompiler is authoritative for whether a Simple
                # Open request may ask a business question. Once the frozen
                # contract has no blocker, stale pre-default context or model
                # improvisation cannot re-open ask_user.
                allowed_actions = tuple(
                    action for action in allowed_actions if action != "ask_user"
                )
            if (
                request_context.get("_partialPlanExpansionAuthorized") is True
                or simple_direction_generation_authorized(request_context)
            ):
                allowed_actions = tuple(dict.fromkeys((*allowed_actions, "draft_itinerary")))
        contract_service = AgentDecisionContractService(
            decision_model=ModelDecisionV3,
            allowed_actions=allowed_actions,
        )
        contract = contract_service.build()
        autonomy_context = dict(autonomy_context)
        normalization_context = self._normalization_context(autonomy_context)
        request_contract = (
            request_context.get("requestIntentContract")
            if isinstance(request_context.get("requestIntentContract"), dict)
            else {}
        )
        activity_coverage_pending = (request_contract.get("requestActivityCoverage") or {}).get("status") == "pending"
        if activity_coverage_pending:
            normalization_context["_requestActivityCoverageContract"] = deepcopy(request_contract)
            normalization_context["requestActivityClauses"] = [
                {key: deepcopy(clause[key]) for key in ("clauseId", "text", "goalIds")}
                for clause in request_contract["requestActivityCoverage"]["clauses"]
            ]
        if isinstance(request_contract.get("clarificationDimensions"), list):
            # Keep executable option semantics on the host side. The Controller
            # projection receives only opaque IDs and wording seeds, while the
            # strict normalizer injects values from this frozen request contract.
            normalization_context["clarificationDimensions"] = deepcopy(
                request_contract.get("clarificationDimensions") or []
            )
        route_policy_requirement = controller_route_policy_requirement(request_context)
        if (
            route_policy_requirement is None
            and "draft_itinerary" in allowed_actions
            and controller_ready_route_policy_requires_typed_draft(request_context)
        ):
            route_policy_requirement = controller_draft_route_policy_repair_requirement()
        if route_policy_requirement is not None:
            normalization_context["routePlanningPolicyRequirement"] = deepcopy(
                route_policy_requirement
            )
        normalization_aliases: list[str] = []
        initial_call_ms = normalization_ms = repair_call_ms = validation_ms = 0
        repair_reserved_ms = 0
        attempted_repair_action = ""
        repair_action_resolution_failed = False
        provider_raw_decisions: list[dict[str, Any]] = []
        normalized_decision: Optional[dict[str, Any]] = None
        authoritative_contract_fallback_required = False
        decision: AgentDecision
        unresolved_date_reason = _unresolved_trip_date_reason(
            request_context,
            autonomy_context,
        )
        if unresolved_date_reason is not None:
            decision = self._date_contract_clarification_decision(unresolved_date_reason)
            gated = AgentDecisionPolicyGate().evaluate(
                decision,
                available_tools=available_tools,
                runtime_budget_tools=runtime_budget_tools,
                observation=observation,
                request_context=request_context,
            )
            source = "safe_fallback"
            decision_path = "fallback"
        elif deterministic_fast_path:
            planner_plan = self.planner_service.plan(latest_message, request_context)
            planner_called = True
            decision = self._decision_from_planner(planner_plan)
            source = "deterministic_fast_path"
            decision_path = "deterministic"
        else:
            context_projection = ControllerContextProjectionService().build(
                autonomy_context,
                allowed_actions=allowed_actions,
                decision_contract=contract,
                normalization_context=normalization_context,
            )
            full_controller_context = context_projection.full
            lite_controller_context = context_projection.lite
            full_started = time.monotonic()
            full_timeout = max(0.0, min(self.decision_timeout_seconds, total_deadline - full_started))
            raw: Any = None
            terminal_controller_stage = "full"
            try:
                if full_timeout <= 0.005:
                    raise TimeoutError("controller_runtime_budget_exhausted")
                controller_full_called = True
                repair_reserved = (
                    max(0.0, min(2.0, full_timeout * 0.25, full_timeout - 0.005))
                    if self.max_schema_repair_attempts
                    else 0.0
                )
                repair_reserved_ms = int(repair_reserved * 1000)
                initial_deadline = full_started + max(0.01, full_timeout - repair_reserved)
                full_deadline = full_started + full_timeout
                initial_call_started = time.monotonic()
                try:
                    raw = self._call_provider(
                        full_controller_context,
                        repair_feedback="",
                        deadline=initial_deadline,
                        timeout_seconds=max(0.01, initial_deadline - time.monotonic()),
                        performance_evidence=controller_performance,
                    )
                except ControllerOutputTruncatedError as truncation_error:
                    initial_call_ms = max(0, int((time.monotonic() - initial_call_started) * 1000))
                    provider_raw_decisions.append(
                        {
                            "truncated": True,
                            **truncation_error.to_safe_dict(),
                        }
                    )
                    if self.max_schema_repair_attempts < 1 or activity_coverage_pending:
                        raise
                    schema_repair_attempts = 1
                    repair_timeout = max(0.0, full_deadline - time.monotonic())
                    if repair_timeout <= 0.005:
                        raise TimeoutError("controller_full_repair_budget_exhausted") from truncation_error
                    repair_feedback = self._truncation_repair_feedback(
                        error=truncation_error,
                        contract=contract,
                    )
                    terminal_controller_stage = "repair"
                    repair_started = time.monotonic()
                    raw = self._call_provider(
                        full_controller_context,
                        repair_feedback=json.dumps(repair_feedback, ensure_ascii=False, default=str),
                        deadline=full_deadline,
                        timeout_seconds=repair_timeout,
                        performance_evidence=controller_performance,
                    )
                    provider_raw_decisions.append(self._redacted_provider_decision(raw))
                    repair_call_ms += max(0, int((time.monotonic() - repair_started) * 1000))
                    validation_started = time.monotonic()
                    decision, aliases, normalize_elapsed, normalized_decision = self._validate_provider_decision(
                        raw,
                        normalization_context,
                        allowed_actions=allowed_actions,
                    )
                    normalization_aliases.extend(aliases)
                    normalization_ms += normalize_elapsed
                    validation_ms += max(0, int((time.monotonic() - validation_started) * 1000))
                else:
                    provider_raw_decisions.append(self._redacted_provider_decision(raw))
                    initial_call_ms = max(0, int((time.monotonic() - initial_call_started) * 1000))
                    try:
                        validation_started = time.monotonic()
                        decision, aliases, normalize_elapsed, normalized_decision = self._validate_provider_decision(
                            raw,
                            normalization_context,
                            allowed_actions=allowed_actions,
                        )
                        normalization_aliases.extend(aliases)
                        normalization_ms += normalize_elapsed
                        validation_ms += max(0, int((time.monotonic() - validation_started) * 1000))
                    except Exception as schema_error:
                        if activity_coverage_pending or isinstance(schema_error, RequiredGoalOptionalConflictError):
                            # Coverage uncertainty is not authorization for a
                            # second model interpretation or a regex draft.
                            raise
                        if self._requires_authoritative_draft_fallback(schema_error) and self._authoritative_server_draft_allowed(
                            normalization_context,
                            observation,
                        ):
                            # The server already owns an exact per-day
                            # occurrence contract.  A second model call cannot
                            # weaken or reinterpret that contract, so rebuild
                            # deterministically instead of spending the repair
                            # budget and risking a semantically different
                            # action (for example, ``finish``).
                            authoritative_contract_fallback_required = True
                            raise
                        if self.max_schema_repair_attempts < 1:
                            raise
                        repair_timeout = max(0.0, full_deadline - time.monotonic())
                        if repair_timeout <= 0.005:
                            raise TimeoutError("controller_full_repair_budget_exhausted") from schema_error
                        try:
                            repair_feedback = self._repair_feedback(
                                raw=raw,
                                error=schema_error,
                                contract=contract,
                                contract_service=contract_service,
                                context=normalization_context,
                                aliases=normalization_aliases,
                            )
                        except DecisionNormalizationError as repair_selection_error:
                            if repair_selection_error.reason_code == "controller_repair_action_unresolved":
                                repair_action_resolution_failed = True
                            raise
                        schema_repair_attempts = 1
                        attempted_repair_action = str(repair_feedback.get("action") or "")
                        terminal_controller_stage = "repair"
                        repair_started = time.monotonic()
                        raw = self._call_provider(
                            full_controller_context,
                            repair_feedback=json.dumps(repair_feedback, ensure_ascii=False, default=str),
                            deadline=full_deadline,
                            timeout_seconds=repair_timeout,
                            performance_evidence=controller_performance,
                        )
                        provider_raw_decisions.append(self._redacted_provider_decision(raw))
                        repair_call_ms += max(0, int((time.monotonic() - repair_started) * 1000))
                        validation_started = time.monotonic()
                        decision, aliases, normalize_elapsed, normalized_decision = self._validate_provider_decision(
                            raw,
                            normalization_context,
                            allowed_actions=allowed_actions,
                        )
                        normalization_aliases.extend(aliases)
                        normalization_ms += normalize_elapsed
                        validation_ms += max(0, int((time.monotonic() - validation_started) * 1000))
                source = "controller"
                decision_path = "full"
                controller_full_succeeded = True
            except Exception as error:  # fallback is intentionally fail-closed and immediate
                controller_error = type(error).__name__ + ":" + str(error)[:160]
                controller_failures.append(
                    classify_controller_failure(
                        error,
                        stage=(
                            error.call_kind
                            if isinstance(error, ControllerResponseIntegrityError)
                            else terminal_controller_stage
                        ),
                        provider=type(self.provider).__name__ if self.provider is not None else "unavailable",
                        model=str(getattr(self.provider, "model", "") or "unknown"),
                        duration_ms=max(0, int((time.monotonic() - full_started) * 1000)),
                        timeout_seconds=full_timeout,
                    )
                )
                partial_expansion = request_context.get("_partialPlanExpansionAuthorized") is True
                raw_action = ""
                raw_payload = raw
                if isinstance(raw_payload, str):
                    try:
                        raw_payload, _parser_aliases = _parse_controller_json_object(raw_payload)
                    except json.JSONDecodeError:
                        raw_payload = {}
                if isinstance(raw_payload, dict):
                    raw_action = str(raw_payload.get("primaryAction") or "")
                repair_budget_fallback = None
                authoritative_contract_fallback = (
                    self._deterministic_cardinality_draft_decision(normalization_context)
                    if authoritative_contract_fallback_required
                    else None
                )
                if (
                    schema_repair_attempts == 1
                    and "draft_itinerary" in {raw_action, attempted_repair_action}
                    and controller_failures[-1].stage == "repair"
                    and controller_failures[-1].failure_class == "request_budget_exceeded"
                    and self._authoritative_server_draft_allowed(normalization_context, observation)
                ):
                    repair_budget_fallback = self._deterministic_cardinality_draft_decision(
                        normalization_context
                    )
                if isinstance(error, RequiredGoalOptionalConflictError):
                    decision = self._required_optional_conflict_decision(error)
                    source = "safe_fallback"
                    decision_path = "fallback"
                elif activity_coverage_pending:
                    decision = self._request_coverage_failure_decision(controller_error)
                    source = "safe_fallback"
                    decision_path = "fallback"
                elif authoritative_contract_fallback is not None:
                    decision = authoritative_contract_fallback.model_copy(
                        update={
                            "reason_codes": [
                                *authoritative_contract_fallback.reason_codes,
                                "server_directive_after_authoritative_contract_rejection",
                            ]
                        }
                    )
                    source = "safe_fallback"
                    decision_path = "fallback"
                elif repair_budget_fallback is not None:
                    decision = repair_budget_fallback.model_copy(
                        update={
                            "reason_codes": [
                                *repair_budget_fallback.reason_codes,
                                "server_directive_after_controller_repair_budget",
                            ]
                        }
                    )
                    source = "safe_fallback"
                    decision_path = "fallback"
                elif self._must_fail_closed_existing_itinerary(observation) and not partial_expansion:
                    decision = self._controller_fail_closed_decision(request_context, controller_error)
                    source = "safe_fallback"
                    decision_path = "fallback"
                elif self._controller_capability_unavailable(error) and not partial_expansion:
                    fallback_decision = None
                    if self.fallback_decision_resolver is not None and observation is not None:
                        resolver = getattr(self.fallback_decision_resolver, "resolve", None) or getattr(
                            self.fallback_decision_resolver, "decide", None
                        )
                        if callable(resolver):
                            deterministic_arbitrator_called = True
                            fallback_decision = resolver(observation)
                    if fallback_decision is not None:
                        decision = fallback_decision
                        source = "deterministic_arbitrator"
                        decision_path = "fallback"
                    else:
                        decision = self._controller_fail_closed_decision(request_context, controller_error)
                        source = "safe_fallback"
                        decision_path = "fallback"
                else:
                    fallback_decision = None
                    lite = getattr(self.provider, "decide_autonomy_lite", None)
                    if (
                            schema_repair_attempts == 0
                            and not repair_action_resolution_failed
                            and (controller_failures[-1].retryable or partial_expansion)
                        and callable(lite)
                        and total_deadline - time.monotonic() > 0.005
                    ):
                        controller_lite_called = True
                        lite_started = time.monotonic()
                        lite_timeout = max(0.0, min(self.lite_timeout_seconds, total_deadline - lite_started))
                        try:
                            raw_lite = self._call_lite_provider(
                                lite_controller_context,
                                deadline=lite_started + lite_timeout,
                                timeout_seconds=lite_timeout,
                                performance_evidence=controller_performance,
                            )
                            lite_decision = self._validate_lite_provider_decision(raw_lite)
                            if lite_decision.primary_action == "draft_itinerary" and self._authoritative_server_draft_allowed(
                                normalization_context,
                                observation,
                            ):
                                server_draft = self._deterministic_cardinality_draft_decision(normalization_context)
                                if server_draft is not None:
                                    decision = server_draft.model_copy(
                                        update={
                                            "reason_codes": [
                                                *server_draft.reason_codes,
                                                "model_controller_lite_success",
                                                "lite_classification_server_directive",
                                            ]
                                        }
                                    )
                                else:
                                    decision = self._decision_from_lite(lite_decision, lite_controller_context)
                            else:
                                decision = self._decision_from_lite(lite_decision, lite_controller_context)
                            if decision.primary_action not in allowed_actions:
                                raise DecisionNormalizationError(
                                    "lite_primary_action_not_allowed_by_observation",
                                    "primaryAction",
                                    decision.primary_action,
                                )
                            source = "controller"
                            decision_path = "lite"
                            controller_lite_succeeded = True
                        except Exception as lite_error:
                            controller_error = type(lite_error).__name__ + ":" + str(lite_error)[:160]
                            controller_failures.append(
                                classify_controller_failure(
                                    lite_error,
                                    stage="lite",
                                    provider=type(self.provider).__name__,
                                    model=str(getattr(self.provider, "model", "") or "unknown"),
                                    duration_ms=max(0, int((time.monotonic() - lite_started) * 1000)),
                                    timeout_seconds=lite_timeout,
                                )
                            )
                    if not controller_lite_succeeded:
                        fallback_decision = (
                            None if partial_expansion or schema_repair_attempts else deterministic_decision
                        )
                        server_cardinality_fallback_used = False
                        server_draft_recoverable_failure_classes = {
                            "provider_timeout",
                            "invalid_json",
                            "schema_validation_failed",
                            "action_directive_missing",
                        }
                        controller_recoverable_failure_stages = {
                            failure.stage
                            for failure in controller_failures
                            if failure.retryable and failure.failure_class in server_draft_recoverable_failure_classes
                        }
                        controller_timeout_stages = {
                            failure.stage
                            for failure in controller_failures
                            if failure.failure_class == "provider_timeout"
                        }
                        full_attempts = [
                            item for item in controller_performance if item.get("callKind") in {"full", "repair"}
                        ]
                        lite_attempts = [item for item in controller_performance if item.get("callKind") == "lite"]
                        terminal_controller_attempts_invoked_provider = (
                            bool(full_attempts)
                            and full_attempts[-1].get("providerInvoked") is True
                            and bool(lite_attempts)
                            and lite_attempts[-1].get("providerInvoked") is True
                        )
                        if (
                            fallback_decision is None
                            and controller_full_called
                            and controller_lite_called
                            and {"full", "lite"}.issubset(controller_recoverable_failure_stages)
                            and terminal_controller_attempts_invoked_provider
                            and self._authoritative_server_draft_allowed(normalization_context, observation)
                        ):
                            fallback_decision = self._deterministic_cardinality_draft_decision(normalization_context)
                            if fallback_decision is not None:
                                server_cardinality_fallback_used = True
                                recovery_reason_code = (
                                    "server_directive_after_controller_timeout"
                                    if {"full", "lite"}.issubset(controller_timeout_stages)
                                    else "server_directive_after_controller_invalid_response"
                                )
                                fallback_decision = fallback_decision.model_copy(
                                    update={
                                        "reason_codes": [
                                            *fallback_decision.reason_codes,
                                            "controller_full_lite_unavailable",
                                            recovery_reason_code,
                                        ]
                                    }
                                )
                        if (
                            fallback_decision is None
                            and self.fallback_decision_resolver is not None
                            and observation is not None
                            and not partial_expansion
                            and schema_repair_attempts == 0
                            and not repair_action_resolution_failed
                        ):
                            resolver = getattr(self.fallback_decision_resolver, "resolve", None) or getattr(
                                self.fallback_decision_resolver, "decide", None
                            )
                            if callable(resolver):
                                deterministic_arbitrator_called = True
                                fallback_decision = resolver(observation)
                        if fallback_decision is not None:
                            if fallback_decision.primary_action in {
                                "ask_user",
                                "draft_itinerary",
                                "read_itinerary",
                                "verify_external_facts",
                                "finish",
                            }:
                                decision = fallback_decision
                                source = (
                                    "safe_fallback" if server_cardinality_fallback_used else "deterministic_arbitrator"
                                )
                            else:
                                decision = self._controller_fail_closed_decision(request_context, controller_error)
                                source = "safe_fallback"
                            decision_path = "fallback"
                        else:
                            decision = self._controller_fail_closed_decision(request_context, controller_error)
                            source = "safe_fallback"
                            decision_path = "fallback"
        compiled_request_contract = normalization_context.get("_compiledRequestIntentContract")
        if controller_full_succeeded and isinstance(compiled_request_contract, dict):
            request_context["requestIntentContract"] = deepcopy(compiled_request_contract)
            canonical = request_context.get("canonicalRequestContext")
            if isinstance(canonical, dict):
                canonical["requestIntentContract"] = deepcopy(compiled_request_contract)
                canonical.pop("provisionalGoalOccurrenceProjection", None)
            request_context.pop("provisionalGoalOccurrenceProjection", None)
            observation = AgentObservationBuilder().build(request_context)
            request_context["agentObservation"] = observation.model_dump(by_alias=True)
        if (
            decision.primary_action != "draft_itinerary"
            and not activity_coverage_pending
            and controller_error
            and (
                "draft_goal_" in controller_error
                or "draft_soft_goal_misclassified_as_required" in controller_error
                or "draft_optional_experience_budget_exceeded" in controller_error
                or "draft_required_day_coverage_incomplete" in controller_error
                or "draft_required_day_anchor_missing" in controller_error
                or "draft_required_day_occurrence_coverage_incomplete" in controller_error
            )
            and self._authoritative_server_draft_allowed(normalization_context, observation)
        ):
            cardinality_fallback = self._deterministic_cardinality_draft_decision(normalization_context)
            if cardinality_fallback is not None:
                decision = cardinality_fallback
                source = "safe_fallback"
                decision_path = "fallback"
        try:
            gated = AgentDecisionPolicyGate().evaluate(
                decision,
                available_tools=available_tools,
                runtime_budget_tools=runtime_budget_tools,
                observation=observation,
                allow_partial_plan_expansion=request_context.get("_partialPlanExpansionAuthorized") is True,
                request_context=request_context,
            )
        except Exception as error:
            controller_error = type(error).__name__ + ":" + str(error)[:160]
            fallback_decision = (
                None if request_context.get("_partialPlanExpansionAuthorized") is True else deterministic_decision
            )
            if (
                fallback_decision is None
                and self.fallback_decision_resolver is not None
                and observation is not None
                and request_context.get("_partialPlanExpansionAuthorized") is not True
            ):
                resolver = getattr(self.fallback_decision_resolver, "resolve", None) or getattr(
                    self.fallback_decision_resolver, "decide", None
                )
                if callable(resolver):
                    deterministic_arbitrator_called = True
                    fallback_decision = resolver(observation)
            if fallback_decision is not None and fallback_decision.primary_action in {
                "ask_user",
                "draft_itinerary",
                "read_itinerary",
                "verify_external_facts",
                "finish",
            }:
                decision = fallback_decision
                source = "deterministic_arbitrator"
            else:
                decision = self._controller_fail_closed_decision(request_context, controller_error)
                source = "safe_fallback"
            gated = AgentDecisionPolicyGate().evaluate(
                decision,
                available_tools=available_tools,
                runtime_budget_tools=runtime_budget_tools,
                observation=observation,
                allow_partial_plan_expansion=request_context.get("_partialPlanExpansionAuthorized") is True,
                request_context=request_context,
            )
        route_policy_rejected = "controller_route_policy_not_authorized" in set(
            gated.policy_reason_codes
        )
        route_retry_fallback = bool(
            controller_route_policy_may_resolve_detour(request_context)
            and (
                route_policy_rejected
                or (
                    controller_error
                    and source in {"safe_fallback", "deterministic_arbitrator"}
                    and decision.primary_action == "ask_user"
                )
            )
        )
        if route_retry_fallback:
            # A server cardinality fallback cannot invent the user's detour
            # tolerance.  If the bounded Controller attempts did not return a
            # typed route policy/question, expose an actionable zero-write
            # retry instead of allowing a rejected draft to collapse into the
            # generic no-safe-action response.
            decision = self._route_clarification_controller_retry_decision(controller_error)
            gated = AgentDecisionPolicyGate().evaluate(
                decision,
                available_tools=available_tools,
                runtime_budget_tools=runtime_budget_tools,
                observation=observation,
                allow_partial_plan_expansion=request_context.get("_partialPlanExpansionAuthorized") is True,
                request_context=request_context,
            )
            source = "safe_fallback"
            decision_path = "fallback"
        cardinality_policy_codes = {
            "draft_goal_daily_cardinality_exceeded",
            "draft_goal_cardinality_underallocated",
            "draft_goal_cardinality_overallocated",
            "draft_goal_day_not_allowed",
            "draft_goal_distribution_invalid",
            "required_goal_omitted_from_planning_directive",
            "draft_soft_goal_misclassified_as_required",
            "draft_required_day_coverage_incomplete",
            "draft_required_day_anchor_missing",
            "draft_required_day_occurrence_coverage_incomplete",
        }
        if (
            not gated.accepted
            and cardinality_policy_codes.intersection(gated.policy_reason_codes)
        ):
            if self._authoritative_server_draft_allowed(normalization_context, observation):
                cardinality_fallback = self._deterministic_cardinality_draft_decision(
                    normalization_context,
                    controller_decision=decision,
                )
                if cardinality_fallback is not None:
                    decision = cardinality_fallback
                    gated = AgentDecisionPolicyGate().evaluate(
                        decision,
                        available_tools=available_tools,
                        runtime_budget_tools=runtime_budget_tools,
                        observation=observation,
                        allow_partial_plan_expansion=request_context.get("_partialPlanExpansionAuthorized") is True,
                        request_context=request_context,
                    )
                    if gated.accepted:
                        source = "safe_fallback"
                        decision_path = "fallback"
            else:
                decision = self._controller_fail_closed_decision(request_context, controller_error)
                gated = AgentDecisionPolicyGate().evaluate(
                    decision,
                    available_tools=available_tools,
                    runtime_budget_tools=runtime_budget_tools,
                    observation=observation,
                    allow_partial_plan_expansion=request_context.get("_partialPlanExpansionAuthorized") is True,
                    request_context=request_context,
                )
                source = "safe_fallback"
                decision_path = "fallback"
        return AgentDecisionResult(
            decision=decision,
            gated_decision=gated,
            source=source,
            controller_error=controller_error,
            planner_plan=planner_plan,
            decision_duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            schema_repair_attempts=schema_repair_attempts,
            planner_called=planner_called,
            deterministic_arbitrator_called=deterministic_arbitrator_called,
            decision_path=decision_path,
            controller_full_called=controller_full_called,
            controller_full_succeeded=controller_full_succeeded,
            controller_lite_called=controller_lite_called,
            controller_lite_succeeded=controller_lite_succeeded,
            controller_failures=tuple(controller_failures),
            controller_performance=tuple(controller_performance),
            configured_full_timeout_seconds=self.decision_timeout_seconds,
            configured_lite_timeout_seconds=self.lite_timeout_seconds,
            configured_total_budget_seconds=self.total_budget_seconds,
            decision_contract_version=contract["contractVersion"],
            decision_contract_hash=contract["contractHash"],
            normalization_aliases=tuple(sorted(set(normalization_aliases))),
            initial_call_ms=initial_call_ms,
            normalization_ms=normalization_ms,
            repair_reserved_ms=repair_reserved_ms,
            repair_call_ms=repair_call_ms,
            validation_ms=validation_ms,
            provider_raw_decisions=tuple(provider_raw_decisions),
            normalized_decision=normalized_decision,
        )

    @staticmethod
    def _request_coverage_failure_decision(controller_error: Optional[str]) -> AgentDecision:
        # controller_error is persisted as exception-name + ':' + a bounded
        # exception message. Attribute output failures only to known validator
        # exceptions; transport or pre-call errors prove no usable output.
        error_name, _, detail = str(controller_error or "").partition(":")
        detail = detail.strip()
        http_match = re.match(r"(\d{3}):", detail) if error_name == "HTTPException" else None
        http_status = int(http_match.group(1)) if http_match else None
        if error_name == "ControllerOutputTruncatedError":
            failure = "已调用规划模型，但本轮输出被截断，未形成完整的可验证决策"
        elif error_name == "ControllerOutputIncompleteError":
            failure = "模型服务已响应，但返回内容未形成完整的可验证决策"
        elif error_name in {"TimeoutError", "FutureTimeoutError"} or http_status in {408, 504}:
            failure = "本轮规划模型请求超时，未获得完整的可验证响应"
        elif (error_name == "RuntimeError" and detail == "controller_provider_unavailable") or (
            error_name == "HTTPException" and "DEEPSEEK_API_KEY is not configured" in detail
        ):
            failure = "本轮规划模型服务不可用，未获得可验证响应"
        elif error_name in {"HTTPException", "HTTPError", "URLError", "ConnectionError", "OSError"}:
            failure = "本轮规划模型请求发生连接或 HTTP 错误，未获得可验证响应"
        elif error_name == "RequestActivityCoverageError" and detail.startswith(
            "request_activity_coverage_duplicate_activity:"
        ):
            failure = "规划模型将同一项活动重复定义，无法安全对应活动次数和日期，已停止本轮规划"
        elif error_name in {
            "ValidationError", "DecisionNormalizationError", "JSONDecodeError",
            "RequestActivityCoverageError", "RequiredGoalOptionalConflictError",
        }:
            failure = "规划模型已返回内容，但本轮格式或活动、次数和时段合同校验未通过"
        elif error_name == "ValueError" and detail in {
            "controller_full_payload_too_large", "controller_full_projection_too_large",
        }:
            failure = "本轮规划请求触及调用前的容量限制，未获得模型响应"
        else:
            failure = "本轮尚未获得可用于执行的规划模型决策，具体失败原因尚未确认"
        message = f"{failure}。原始需求已保留，本轮尚未查询地点、生成可确认方案或写入正式行程。"
        return AgentDecision(
            schemaVersion="agent-decision-v2", primaryAction="ask_user", confidence=1.0,
            decisionSummary=message, userVisibleReason=message,
            reasonCodes=["request_activity_coverage_incomplete"], requiredTools=[],
            actionDirective={"type": "ask_user", "question": message, "choiceIds": []},
            proposedWriteRisk="none", targetScope={}, clarification={"question": message, "options": []},
            uncertaintyPolicy="ask_user",
            expectedOutcome={"taskRoute": "clarification", "controllerError": controller_error, "failureStage": "full_request_coverage", "timelineWriteDelta": 0},
            stopCondition={"type": "needs_confirmation"}, sideEffects={"conversation": True, "itinerary": False},
        )

    @classmethod
    def _required_optional_conflict_decision(cls, error: RequiredGoalOptionalConflictError) -> AgentDecision:
        decision = cls._request_coverage_failure_decision(type(error).__name__ + ":" + str(error))
        return decision.model_copy(update={
            "reason_codes": [*decision.reason_codes, "draft_required_goal_misclassified_as_optional"],
            "expected_outcome": {**decision.expected_outcome, "failureStage": "full_required_optional_conflict",
                                 "conflictingGoalId": error.goal_id},
        })

    @staticmethod
    def _controller_capability_unavailable(error: Exception) -> bool:
        return isinstance(error, RuntimeError) and str(error) == "controller_provider_unavailable"

    @staticmethod
    def _date_contract_clarification_decision(reason: str) -> AgentDecision:
        questions = {
            "date_duration_conflict": "明确日期覆盖的天数与旅行时长不一致。请确认最终出行日期或修改旅行天数。",
            "duration_without_dates": "你修改了旅行天数，但没有给出对应的新日期。请确认最终开始和结束日期。",
            "holiday_dates_unspecified": "你指定了国庆出行时长，但没有说明具体日期。请提供明确的开始和结束日期。",
            "invalid_calendar_date": "明确日期中包含无效的日历日期。请更正后重新提交。",
            "non_contiguous_explicit_date_list": "列出的出行日期不连续。请确认连续日期范围，或明确说明分段出行。",
            "ambiguous_multiple_explicit_date_groups": "检测到多组无法唯一对账的显式日期。请确认唯一的开始和结束日期。",
            "ambiguous_national_day_relative_duration": "“国庆前几天”存在节前或假期头几天两种含义。请提供明确日期。",
            "unparseable_explicit_date": "无法可靠解析你提供的显式日期。请按“10月1日至10月2日”补充明确日期。",
        }
        question = questions.get(reason, questions["unparseable_explicit_date"])
        return AgentDecision(
            schemaVersion="agent-decision-v2",
            primaryAction="ask_user",
            confidence=1.0,
            decisionSummary="日期合同尚未解决，已在任何规划或外部查询前停止。",
            userVisibleReason=question,
            reasonCodes=[reason, "date_contract_clarification_required"],
            requiredTools=[],
            actionDirective={
                "type": "ask_user",
                "question": question,
                "choiceIds": ["provide_explicit_trip_dates"],
            },
            proposedWriteRisk="none",
            targetScope={},
            clarification={
                "question": question,
                "options": [
                    {
                        "id": "provide_explicit_trip_dates",
                        "action": "manual_continuation",
                        "label": "补充明确日期",
                        "allowsManualInput": True,
                    }
                ],
            },
            uncertaintyPolicy="ask_user",
            expectedOutcome={
                "taskRoute": "clarification",
                "reason": reason,
                "timelineWriteDelta": 0,
            },
            stopCondition={"type": "needs_confirmation"},
            sideEffects={"conversation": True, "itinerary": False},
        )

    @staticmethod
    def _must_fail_closed_existing_itinerary(observation: Optional[AgentObservation]) -> bool:
        return bool(
            observation is not None
            and (observation.itinerary.meaningful_segment_count > 0 or observation.itinerary.active_version_id)
        )

    @classmethod
    def _authoritative_server_draft_allowed(
        cls,
        normalization_context: dict[str, Any],
        observation: Optional[AgentObservation],
    ) -> bool:
        server_new_direction = bool(
            normalization_context.get("simpleDirectionGenerationAuthorized") is True
            and isinstance(normalization_context.get("viewResolution"), dict)
            and normalization_context["viewResolution"].get("resolvedAction") == "generate_new_direction"
            and normalization_context["viewResolution"].get("resolutionSource")
            == "server_validated_opaque_choice"
        )
        return bool(
            normalization_context.get("tripDatesResolved") is True
            and normalization_context.get("authoritativeGoalLedger") is True
            and (
                server_new_direction
                or not cls._must_fail_closed_existing_itinerary(observation)
            )
        )

    @classmethod
    def _controller_fail_closed_decision(
        cls, request_context: dict[str, Any], controller_error: Optional[str]
    ) -> AgentDecision:
        if request_context.get("_partialPlanExpansionAuthorized") is True:
            return cls._partial_plan_expansion_fail_closed_decision(controller_error)
        return cls._existing_itinerary_fail_closed_decision(controller_error)

    @staticmethod
    def _route_clarification_controller_retry_decision(
        controller_error: Optional[str],
    ) -> AgentDecision:
        message = (
            "路线绕路偏好仍待确认，但规划控制器本轮未能生成可验证的动态选项。"
            "你可以重试生成偏好选项，或直接说明更看重少绕路还是体验差异；当前没有创建或修改行程。"
        )
        return AgentDecision(
            schemaVersion="agent-decision-v2",
            primaryAction="ask_user",
            confidence=1.0,
            decisionSummary="路线偏好仍未形成可执行合同，已保持零写入并提供明确恢复入口。",
            userVisibleReason=message,
            reasonCodes=["route_clarification_controller_retry_required"],
            requiredTools=[],
            actionDirective={
                "type": "ask_user",
                "question": message,
                "choiceIds": ["retry_route_clarification", "manual_route_preference"],
            },
            proposedWriteRisk="none",
            targetScope={},
            clarification={
                "question": message,
                "options": [
                    {
                        "id": "retry_route_clarification",
                        "action": "retry_model_planning",
                        "label": "重试生成路线偏好选项",
                        "allowsManualInput": False,
                    },
                    {
                        "id": "manual_route_preference",
                        "action": "manual_continuation",
                        "label": "我直接说明绕路偏好",
                        "allowsManualInput": True,
                    },
                ],
            },
            uncertaintyPolicy="ask_user",
            expectedOutcome={
                "taskRoute": "clarification",
                "controllerError": controller_error,
                "retryable": True,
                "timelineWriteDelta": 0,
            },
            stopCondition={"type": "needs_confirmation"},
            sideEffects={"conversation": True, "itinerary": False},
        )

    @staticmethod
    def _partial_plan_expansion_fail_closed_decision(
        controller_error: Optional[str],
    ) -> AgentDecision:
        message = "生成其他方案时模型请求超时，当前方案和时间轴未发生变化。"
        return AgentDecision(
            schemaVersion="agent-decision-v2",
            primaryAction="ask_user",
            confidence=1.0,
            decisionSummary="Portfolio 扩展未产生新的可验证方案，已保持零写入。",
            userVisibleReason=message,
            reasonCodes=["partial_plan_expansion_controller_failed_retryable"],
            requiredTools=[],
            actionDirective={
                "type": "ask_user",
                "question": message,
                "choiceIds": ["retry_partial_plan_expansion"],
            },
            proposedWriteRisk="none",
            targetScope={},
            clarification={
                "question": message,
                "options": [
                    {
                        "id": "retry_partial_plan_expansion",
                        "action": "retry_model_planning",
                        "label": "重试生成其他方案",
                        "allowsManualInput": False,
                    }
                ],
            },
            uncertaintyPolicy="ask_user",
            expectedOutcome={
                "taskRoute": "clarification",
                "controllerError": controller_error,
                "retryable": True,
                "timelineWriteDelta": 0,
            },
            stopCondition={"type": "needs_confirmation"},
            sideEffects={"conversation": True, "itinerary": False},
        )

    @staticmethod
    def _existing_itinerary_fail_closed_decision(controller_error: Optional[str]) -> AgentDecision:
        return AgentDecision(
            schemaVersion="agent-decision-v2",
            primaryAction="ask_user",
            confidence=1.0,
            decisionSummary="Controller 未形成可验证的结构化修改决策，已停止写入。",
            userVisibleReason="当前行程保持不变；请重试本次修改，或改用时间轴上的结构化编辑控件。",
            reasonCodes=["controller_failure_existing_itinerary_fail_closed"],
            requiredTools=[],
            actionDirective={
                "type": "ask_user",
                "question": "模型暂时无法安全解析这次修改。你可以重试，或在时间轴中直接选择目标后编辑。",
                "choiceIds": ["retry_controller", "manual_timeline_edit"],
            },
            proposedWriteRisk="none",
            targetScope={},
            clarification={
                "question": "模型暂时无法安全解析这次修改。你可以重试，或在时间轴中直接选择目标后编辑。",
                "options": [
                    {
                        "id": "retry_controller",
                        "action": "retry_model_planning",
                        "label": "重试本次修改",
                        "allowsManualInput": False,
                    },
                    {
                        "id": "manual_timeline_edit",
                        "action": "manual_continuation",
                        "label": "手动选择目标",
                        "allowsManualInput": True,
                    },
                ],
            },
            uncertaintyPolicy="ask_user",
            expectedOutcome={"taskRoute": "clarification", "controllerError": controller_error},
            stopCondition={"type": "needs_confirmation"},
            sideEffects={"conversation": True, "itinerary": False},
        )

    @staticmethod
    def _deterministic_cardinality_draft_decision(
        normalization_context: dict[str, Any],
        *,
        controller_decision: Optional[AgentDecision] = None,
    ) -> Optional[AgentDecision]:
        """Build the smallest server-valid draft directive after controller cardinality failure."""
        requirements = [
            item
            for item in normalization_context.get("goalRequirements") or []
            if isinstance(item, dict) and item.get("goalId")
        ]
        hard = [
            item
            for item in requirements
            if int(item.get("requiredMin") or 0) > 0
            and item.get("requirementLevel") not in {"soft_experience", "optional"}
        ]
        available_days = [int(day) for day in normalization_context.get("availableDayNumbers") or []]
        planning_days = (
            [
                int(day)
                for day in normalization_context.get("requiredPlanningDayNumbers") or []
                if int(day) in available_days
            ]
            if "requiredPlanningDayNumbers" in normalization_context
            else available_days
        )
        planning_days = list(dict.fromkeys(planning_days))
        if not hard or not planning_days:
            return None
        placements: dict[int, list[str]] = {day: [] for day in planning_days}
        for item in hard:
            goal_id = str(item["goalId"])
            allowed_days = [
                int(day) for day in item.get("allowedDayNumbers") or planning_days if int(day) in planning_days
            ]
            maximum = item.get("maxCount")
            required = int(item.get("requiredMin") or 0)
            if maximum is not None and required > int(maximum):
                return None
            if required > len(allowed_days):
                return None
            for day in allowed_days[:required]:
                placements[day].append(goal_id)
        optional_requirements = [
            item for item in requirements if item.get("requirementLevel") in {"soft_experience", "optional"}
        ]
        optional_ids = [str(item["goalId"]) for item in optional_requirements]
        optional_by_day: dict[int, list[str]] = {day: [] for day in planning_days}
        optional_occurrence_count = 0
        optional_occurrence_limit = 3
        for item in hard:
            goal_id = str(item["goalId"])
            required = int(item.get("requiredMin") or 0)
            preferred = int(item.get("preferredCount") or required)
            maximum = item.get("maxCount")
            if maximum is not None:
                preferred = min(preferred, int(maximum))
            allowed_days = [
                int(day) for day in item.get("allowedDayNumbers") or planning_days if int(day) in planning_days
            ]
            required_days = {day for day, goals in placements.items() if goal_id in goals}
            for day in [item for item in allowed_days if item not in required_days][: max(0, preferred - required)]:
                if optional_occurrence_count >= optional_occurrence_limit:
                    break
                optional_by_day[day].append(goal_id)
                optional_occurrence_count += 1
        for item in optional_requirements:
            goal_id = str(item["goalId"])
            allowed_days = [
                int(day) for day in item.get("allowedDayNumbers") or planning_days if int(day) in planning_days
            ]
            target_count = (
                len(allowed_days)
                if item.get("distributionPolicy") == "every_allowed_day"
                else max(1, int(item.get("requiredMin") or 1))
            )
            if item.get("maxCount") is not None:
                target_count = min(target_count, int(item["maxCount"]))
            target_days = allowed_days[:target_count]
            for day in target_days:
                if optional_occurrence_count >= optional_occurrence_limit:
                    break
                optional_by_day[day].append(goal_id)
                optional_occurrence_count += 1
        day_strategies = []
        for day in planning_days:
            goal_ids = placements[day]
            day_strategies.append(
                {
                    "dayNumber": day,
                    "theme": "满足已确认约束" if goal_ids else "保留可执行弹性",
                    "requiredGoalIds": goal_ids,
                    "requiredGoalCounts": {goal_id: 1 for goal_id in goal_ids},
                    "optionalGoalIds": optional_by_day[day],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                }
            )
        goal_priority = [str(item["goalId"]) for item in hard] + optional_ids
        route_policy: Optional[dict[str, Any]] = None
        schedule_hints: list[dict[str, Any]] = []
        source_directive = (
            controller_decision.action_directive
            if controller_decision is not None
            and isinstance(controller_decision.action_directive, DraftItineraryDirective)
            else None
        )
        if (
            source_directive is not None
            and source_directive.route_planning_policy is not None
            and source_directive.route_planning_policy.source == "controller_estimate"
        ):
            route_policy = source_directive.route_planning_policy.model_dump(by_alias=True, exclude_none=True)
        authorized_goal_days = {
            (goal_id, day)
            for day, goal_ids in placements.items()
            for goal_id in goal_ids
        } | {
            (goal_id, day)
            for day, goal_ids in optional_by_day.items()
            for goal_id in goal_ids
        }
        if source_directive is not None:
            schedule_hints = [
                hint.model_dump(by_alias=True, exclude_none=True)
                for hint in source_directive.occurrence_schedule_hints
                if (hint.goal_id, hint.day_number) in authorized_goal_days
            ]
        route_gap_hints = []
        if source_directive is not None:
            allowed_days = set(placements) | set(optional_by_day)
            route_gap_hints = [
                hint.model_dump(by_alias=True, exclude_none=True)
                for hint in source_directive.route_gap_supplement_hints
                if hint.day_number in allowed_days
            ][:optional_occurrence_count]
        return AgentDecision(
            schemaVersion="agent-decision-v2",
            primaryAction="draft_itinerary",
            confidence=1.0,
            decisionSummary="Controller 的目标基数分配无效，已按服务器权威基数生成确定性规划指令。",
            userVisibleReason="已修正目标次数分配并继续生成行程。",
            reasonCodes=["controller_cardinality_invalid", "deterministic_cardinality_fallback"],
            requiredTools=["resolve_poi", "patch_itinerary"],
            actionDirective={
                "type": "draft_itinerary",
                "goalPriority": goal_priority,
                "dayStrategies": day_strategies,
                "optionalExperienceBudget": optional_occurrence_count,
                "searchPriority": [str(item["goalId"]) for item in hard],
                "candidateSelectionPolicy": {
                    "autoSelectWhenDominant": True,
                    "askWhenMaterialTradeoff": True,
                    "preferLowDetour": True,
                    "avoidRecentEntities": True,
                },
                "schedulePolicy": {
                    "respectOpeningWindowsWhenKnown": True,
                    "allowProvisionalWhenUnknown": True,
                },
                "routePlanningPolicy": route_policy,
                "occurrenceScheduleHints": schedule_hints,
                "routeGapSupplementHints": route_gap_hints,
            },
            proposedWriteRisk="high",
            targetScope={},
            uncertaintyPolicy="none",
            expectedOutcome={"taskRoute": "initial_staged_pipeline"},
            stopCondition={"type": "verified_goal_state"},
            sideEffects={"conversation": True, "itinerary": True},
        )

    # Compatibility only for persisted Phase-0 callers. New execution code
    # must use decide() and AgentDecisionResult.
    def decide_shadow(
        self,
        latest_message: str,
        request_context: dict[str, Any],
        autonomy_context: dict[str, Any],
        *,
        available_tools: set[str],
        runtime_budget_tools: set[str],
        deterministic_fast_path: bool = False,
        deterministic_decision: Optional[AgentDecision] = None,
        observation: Optional[AgentObservation] = None,
    ) -> AgentDecisionResult:
        return self.decide(
            latest_message,
            request_context,
            autonomy_context,
            available_tools=available_tools,
            runtime_budget_tools=runtime_budget_tools,
            deterministic_fast_path=deterministic_fast_path,
            deterministic_decision=deterministic_decision,
            observation=observation,
        )

    @staticmethod
    def _controller_performance_snapshot(value: dict[str, Any]) -> dict[str, Any]:
        def nonnegative_int(key: str) -> Optional[int]:
            item = value.get(key)
            return max(0, int(item)) if isinstance(item, int) and not isinstance(item, bool) else None

        context_counts = value.get("contextCharCounts")
        safe_context_counts = (
            {
                str(key): max(0, int(item))
                for key, item in sorted(context_counts.items())
                if isinstance(item, int) and not isinstance(item, bool)
            }
            if isinstance(context_counts, dict)
            else {}
        )
        return {
            "callKind": str(value.get("callKind") or "unknown")
            if value.get("callKind") in {"full", "repair", "lite", "semantic_action"}
            else "unknown",
            "captureState": str(value.get("captureState") or "unknown")
            if value.get("captureState")
            in {
                "queued",
                "worker_started",
                "completed",
                "provider_failed",
                "response_rejected",
                "controller_deadline_snapshot",
                "worker_queue_saturated",
            }
            else "unknown",
            "payloadBytes": nonnegative_int("payloadBytes"),
            "requestByteLimit": nonnegative_int("requestByteLimit"),
            "reservedOutputTokens": nonnegative_int("reservedOutputTokens"),
            "maxOutputTokens": nonnegative_int("maxOutputTokens"),
            "responseSchemaVersion": str(value.get("responseSchemaVersion") or "")[:80] or None,
            "httpStatus": nonnegative_int("httpStatus"),
            "responseBytes": nonnegative_int("responseBytes"),
            "finishReason": str(value.get("finishReason") or "")[:64] or None,
            "contentLength": nonnegative_int("contentLength"),
            "contentBytes": nonnegative_int("contentBytes"),
            "toolCallCount": nonnegative_int("toolCallCount"),
            "toolCallArgumentsChars": nonnegative_int("toolCallArgumentsChars"),
            "toolCallArgumentsBytes": nonnegative_int("toolCallArgumentsBytes"),
            "reasoningContentLength": nonnegative_int("reasoningContentLength"),
            "responseIntegrity": str(value.get("responseIntegrity") or "")
            if value.get("responseIntegrity") in {"complete", "truncated", "incomplete"}
            else None,
            "parseErrorCategory": str(value.get("parseErrorCategory") or "")[:120] or None,
            "parseErrorPosition": nonnegative_int("parseErrorPosition"),
            "tokenUsage": {
                str(key): max(0, int(item))
                for key, item in sorted((value.get("tokenUsage") or {}).items())
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(item, int)
                and not isinstance(item, bool)
            }
            if isinstance(value.get("tokenUsage"), dict)
            else {},
            "contextCharCounts": safe_context_counts,
            "workerQueueMs": nonnegative_int("workerQueueMs"),
            "connectDurationMs": nonnegative_int("connectDurationMs"),
            "connectTimingAvailable": value.get("connectTimingAvailable") is True,
            "preHeaderWaitDurationMs": nonnegative_int("preHeaderWaitDurationMs"),
            "ttfbDurationMs": nonnegative_int("ttfbDurationMs"),
            "readDurationMs": nonnegative_int("readDurationMs"),
            "currentReadElapsedMs": nonnegative_int("currentReadElapsedMs"),
            "responseHeadersReceived": value.get("responseHeadersReceived") is True,
            "providerInvoked": value.get("providerInvoked") is True,
            "promptCacheSupported": value.get("promptCacheSupported") is True,
            "promptCacheHit": value.get("promptCacheHit") if isinstance(value.get("promptCacheHit"), bool) else None,
            "instrumentationStatus": str(value.get("instrumentationStatus") or "unknown")
            if value.get("instrumentationStatus") in {"not_supported", "ready", "prepare_failed"}
            else "unknown",
            "transportTimingBoundary": "urlopen_response_headers"
            if value.get("transportTimingBoundary") == "urlopen_response_headers"
            else None,
            "ttfbMeasurement": "response_headers_available"
            if value.get("ttfbMeasurement") == "response_headers_available"
            else None,
        }

    def _call_provider(
        self,
        autonomy_context: dict[str, Any],
        *,
        repair_feedback: str,
        deadline: float,
        timeout_seconds: float,
        performance_evidence: list[dict[str, Any]],
    ) -> Any:
        if self.provider is None:
            raise RuntimeError("controller_provider_unavailable")
        decide_autonomy = getattr(self.provider, "decide_autonomy", None)
        decide = getattr(self.provider, "decide", None)
        if not callable(decide_autonomy) and not callable(decide):
            raise RuntimeError("controller_provider_unavailable")

        submitted_at = time.monotonic()
        performance: dict[str, Any] = {
            "callKind": "repair" if repair_feedback else "full",
            "captureState": "queued",
            "workerQueueMs": None,
            "payloadBytes": None,
            "contextCharCounts": {},
            "connectDurationMs": None,
            "connectTimingAvailable": False,
            "preHeaderWaitDurationMs": None,
            "ttfbDurationMs": None,
            "readDurationMs": None,
            "responseHeadersReceived": False,
            "providerInvoked": False,
            "instrumentationStatus": "not_supported",
        }

        def invoke() -> Any:
            performance["workerQueueMs"] = max(0, int((time.monotonic() - submitted_at) * 1000))
            performance["captureState"] = "worker_started"
            prepare = getattr(self.provider, "prepare_controller_performance", None)
            if callable(prepare):
                try:
                    prepare(
                        autonomy_context,
                        performance,
                        call_kind="repair" if repair_feedback else "full",
                    )
                    performance["instrumentationStatus"] = "ready"
                except Exception:
                    performance["instrumentationStatus"] = "prepare_failed"
            performance["providerInvoked"] = True
            if callable(decide_autonomy):
                return decide_autonomy(
                    autonomy_context,
                    timeout_seconds=timeout_seconds,
                    repair_feedback=repair_feedback,
                )
            return decide(autonomy_context)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_decision_timeout")
        worker_slots = self._decision_worker_slots
        if not worker_slots.acquire(blocking=False):
            performance["captureState"] = "worker_queue_saturated"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_worker_queue_saturated")
        try:
            future = self._decision_executor.submit(invoke)
        except Exception:
            worker_slots.release()
            raise
        future.add_done_callback(lambda _future: worker_slots.release())
        try:
            value = future.result(timeout=remaining)
        except FutureTimeoutError as error:
            future.cancel()
            if (
                performance.get("responseHeadersReceived") is False
                and performance.get("preHeaderWaitDurationMs") is None
                and isinstance(performance.get("workerQueueMs"), int)
            ):
                performance["preHeaderWaitDurationMs"] = max(
                    0,
                    int((time.monotonic() - submitted_at) * 1000) - int(performance["workerQueueMs"]),
                )
            if performance.get("responseHeadersReceived") is True and performance.get("readDurationMs") is None:
                read_started = performance.get("_readStartedMonotonic")
                if isinstance(read_started, (int, float)):
                    performance["currentReadElapsedMs"] = max(0, int((time.monotonic() - read_started) * 1000))
            performance["captureState"] = "controller_deadline_snapshot"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_decision_timeout") from error
        except ControllerResponseIntegrityError as error:
            performance.update(error.evidence.to_safe_dict())
            performance["responseIntegrity"] = (
                "truncated" if isinstance(error, ControllerOutputTruncatedError) else "incomplete"
            )
            performance["captureState"] = "response_rejected"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise
        except Exception:
            performance["captureState"] = "provider_failed"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise
        performance["captureState"] = "completed"
        performance_evidence.append(self._controller_performance_snapshot(performance))
        return value

    def classify_conversation_intent(
        self,
        context: dict[str, Any],
    ) -> ConversationIntentModelOutcome:
        """Reuse Lite while preserving whether the provider actually ran."""

        performance_evidence: list[dict[str, Any]] = []
        timeout_seconds = self.lite_timeout_seconds
        try:
            value = self._call_lite_provider(
                context,
                deadline=time.monotonic() + timeout_seconds,
                timeout_seconds=timeout_seconds,
                performance_evidence=performance_evidence,
            )
        except Exception as error:
            return ConversationIntentModelOutcome(
                error_code=str(error) or type(error).__name__,
                provider_invoked=any(item.get("providerInvoked") is True for item in performance_evidence),
                performance_evidence=tuple(performance_evidence),
            )
        return ConversationIntentModelOutcome(
            value=value,
            provider_invoked=any(item.get("providerInvoked") is True for item in performance_evidence),
            performance_evidence=tuple(performance_evidence),
        )

    def _call_lite_provider(
        self,
        autonomy_context: dict[str, Any],
        *,
        deadline: float,
        timeout_seconds: float,
        performance_evidence: list[dict[str, Any]],
    ) -> Any:
        decide_lite = getattr(self.provider, "decide_autonomy_lite", None)
        if not callable(decide_lite):
            raise RuntimeError("controller_provider_unavailable")

        submitted_at = time.monotonic()
        performance: dict[str, Any] = {
            "callKind": "lite",
            "captureState": "queued",
            "workerQueueMs": None,
            "payloadBytes": None,
            "contextCharCounts": {},
            "connectDurationMs": None,
            "connectTimingAvailable": False,
            "preHeaderWaitDurationMs": None,
            "ttfbDurationMs": None,
            "readDurationMs": None,
            "responseHeadersReceived": False,
            "providerInvoked": False,
            "instrumentationStatus": "not_supported",
        }

        def invoke() -> Any:
            performance["workerQueueMs"] = max(0, int((time.monotonic() - submitted_at) * 1000))
            performance["captureState"] = "worker_started"
            prepare = getattr(self.provider, "prepare_controller_performance", None)
            if callable(prepare):
                try:
                    prepare(autonomy_context, performance, call_kind="lite")
                    performance["instrumentationStatus"] = "ready"
                except Exception:
                    performance["instrumentationStatus"] = "prepare_failed"
            if autonomy_context.get("schemaVersion") != "conversation-action-context-v1":
                performance["providerInvoked"] = True
            return decide_lite(autonomy_context, timeout_seconds=timeout_seconds)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_lite_timeout")
        worker_slots = self._decision_worker_slots
        if not worker_slots.acquire(blocking=False):
            performance["captureState"] = "worker_queue_saturated"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_worker_queue_saturated")
        try:
            future = self._decision_executor.submit(invoke)
        except Exception:
            worker_slots.release()
            raise
        future.add_done_callback(lambda _future: worker_slots.release())
        try:
            value = future.result(timeout=remaining)
        except FutureTimeoutError as error:
            future.cancel()
            if (
                performance.get("responseHeadersReceived") is False
                and performance.get("preHeaderWaitDurationMs") is None
                and isinstance(performance.get("workerQueueMs"), int)
            ):
                performance["preHeaderWaitDurationMs"] = max(
                    0,
                    int((time.monotonic() - submitted_at) * 1000) - int(performance["workerQueueMs"]),
                )
            if performance.get("responseHeadersReceived") is True and performance.get("readDurationMs") is None:
                read_started = performance.get("_readStartedMonotonic")
                if isinstance(read_started, (int, float)):
                    performance["currentReadElapsedMs"] = max(0, int((time.monotonic() - read_started) * 1000))
            performance["captureState"] = "controller_deadline_snapshot"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise TimeoutError("controller_lite_timeout") from error
        except Exception:
            performance["captureState"] = "provider_failed"
            performance_evidence.append(self._controller_performance_snapshot(performance))
            raise
        performance["captureState"] = "completed"
        performance_evidence.append(self._controller_performance_snapshot(performance))
        return value

    def _validate_provider_decision(
        self,
        raw: Any,
        normalization_context: dict[str, Any],
        *,
        allowed_actions: tuple[str, ...] = PRIMARY_ACTION_VALUES,
    ) -> tuple[AgentDecision, list[str], int, dict[str, Any]]:
        raw, parser_aliases = _parse_controller_json_object(raw)
        pending_contract = normalization_context.get("_requestActivityCoverageContract")
        directive = raw.get("actionDirective") if isinstance(raw.get("actionDirective"), dict) else {}
        if isinstance(pending_contract, dict) and raw.get("primaryAction") != "draft_itinerary":
            raise RequestActivityCoverageError("request_activity_coverage_non_draft_action:" + str(raw.get("primaryAction") or "missing"))
        if isinstance(pending_contract, dict) and raw.get("primaryAction") == "draft_itinerary":
            compiled = RequestActivityCoverageService.compile(pending_contract, directive.get("requestCoverage"))
            RequestActivityCoverageService.validate_directive(compiled, directive)
            rebuilt = self._normalization_context({
                "requestIntentContract": compiled,
                "resolvedTripDates": {"status": "resolved", "dates": normalization_context.get("availableDayNumbers") or []},
                "observation": {"requirementCoverage": {"required": compiled["requiredIntents"]}},
            })
            for key in ("goalRequirements", "requiredGoalCounts", "optionalGoalIds", "authoritativeGoalLedger"):
                normalization_context[key] = rebuilt[key]
            normalization_context["_compiledRequestIntentContract"] = compiled
        elif directive.get("requestCoverage") is not None:
            raise RequestActivityCoverageError("request_activity_coverage_frozen")
        normalize_started = time.monotonic()
        normalized = self.normalizer.normalize(raw, context=normalization_context)
        normalize_elapsed = max(0, int((time.monotonic() - normalize_started) * 1000))
        if normalized.normalized.get("schemaVersion") != "agent-decision-v3":
            raise DecisionNormalizationError(
                "controller_full_requires_model_decision_v3",
                "schemaVersion",
            )
        model_decision = ModelDecisionV3.model_validate(normalized.normalized)
        if model_decision.primary_action not in allowed_actions:
            raise DecisionNormalizationError(
                "controller_primary_action_not_allowed",
                "primaryAction",
                model_decision.primary_action,
            )
        # Validate the frozen occurrence/day contract before asking the model
        # to repair secondary route-policy metadata.  A sparse multi-day draft
        # must deterministically rebuild from the server contract; otherwise a
        # repair response could switch actions (for example to ``finish``) and
        # bypass the missing-day failure.
        self._validate_draft_goal_buckets(model_decision, normalization_context)
        self._validate_controller_route_policy_requirement(
            model_decision,
            normalization_context,
        )
        decision = self._bind_server_target_scope(model_decision, normalization_context)
        return decision, [*parser_aliases, *normalized.aliases_applied], normalize_elapsed, normalized.normalized

    @staticmethod
    def _validate_controller_route_policy_requirement(
        decision: ModelDecisionV3,
        context: dict[str, Any],
    ) -> None:
        requirement = (
            context.get("routePlanningPolicyRequirement")
            if isinstance(context.get("routePlanningPolicyRequirement"), dict)
            else None
        )
        directive = decision.action_directive
        if (
            requirement is None
            or requirement.get("required") is not True
            or decision.primary_action != "draft_itinerary"
            or getattr(directive, "type", None) != "draft_itinerary"
        ):
            return
        policy = getattr(directive, "route_planning_policy", None)
        if policy is None:
            raise DecisionNormalizationError(
                "controller_route_policy_required",
                "actionDirective.routePlanningPolicy",
            )
        if getattr(policy, "source", None) != "controller_estimate":
            raise DecisionNormalizationError(
                "controller_route_policy_source_invalid",
                "actionDirective.routePlanningPolicy.source",
            )
        mobility_requirement = requirement.get("mobilityProfile")
        if (
            isinstance(mobility_requirement, dict)
            and mobility_requirement.get("required") is True
            and getattr(policy, "mobility_profile", None) is None
        ):
            raise DecisionNormalizationError(
                "controller_route_mobility_estimate_required",
                "actionDirective.routePlanningPolicy.mobilityProfile",
            )
        detour_requirement = requirement.get("detourEnvelope")
        if (
            isinstance(detour_requirement, dict)
            and detour_requirement.get("required") is True
            and getattr(policy, "detour_envelope", None) is None
        ):
            raise DecisionNormalizationError(
                "controller_route_detour_envelope_required",
                "actionDirective.routePlanningPolicy.detourEnvelope",
            )

    @staticmethod
    def _validate_draft_goal_buckets(
        decision: ModelDecisionV3,
        context: dict[str, Any],
    ) -> None:
        directive = decision.action_directive
        if decision.primary_action != "draft_itinerary" or getattr(directive, "type", None) != "draft_itinerary":
            return
        strategies = list(getattr(directive, "day_strategies", []) or [])
        hard_goal_quotas = {
            str(item["goalId"]): int(item["requiredMin"]) for item in context.get("goalRequirements") or []
            if isinstance(item, dict) and item.get("goalId")
            and int(item.get("requiredMin") or 0) > 0
            and item.get("requirementLevel") not in {"soft_experience", "optional"}
        }
        required_counts: Counter[str] = Counter()
        required_days: dict[str, set[int]] = {}
        for strategy in strategies:
            for goal_id in getattr(strategy, "required_goal_ids", []) or []:
                required_counts[goal_id] += int((getattr(strategy, "required_goal_counts", {}) or {}).get(goal_id) or 1)
                required_days.setdefault(goal_id, set()).add(strategy.day_number)
        for strategy in strategies:
            # A preferred extra occurrence may stay optional on another day
            # after the actual required bucket has fulfilled its quota. It may
            # not pay the required quota or duplicate a required goal/day.
            conflicts = sorted(goal_id for goal_id in getattr(strategy, "optional_goal_ids", []) or []
                               if goal_id in hard_goal_quotas
                               and (required_counts[goal_id] < hard_goal_quotas[goal_id]
                                    or strategy.day_number in required_days.get(goal_id, set())))
            if conflicts:
                raise RequiredGoalOptionalConflictError(conflicts[0])
        required_day_contract_present = context.get("requiredPlanningDayContractPresent")
        enforce_required_day_contract = (
            required_day_contract_present is True
            or (
                required_day_contract_present is None
                and "requiredPlanningDayNumbers" in context
            )
        )
        if enforce_required_day_contract:
            required_day_numbers = sorted(
                {
                    int(day)
                    for day in context.get("requiredPlanningDayNumbers") or []
                    if isinstance(day, int) and not isinstance(day, bool) and int(day) > 0
                }
            )
            explicit_rest_day_numbers = {
                int(day)
                for day in context.get("explicitRestDayNumbers") or []
                if isinstance(day, int) and not isinstance(day, bool) and int(day) > 0
            }
            overlapping_day_numbers = sorted(set(required_day_numbers).intersection(explicit_rest_day_numbers))
            if overlapping_day_numbers:
                raise DecisionNormalizationError(
                    "draft_required_day_contract_conflict",
                    "requiredPlanningDayNumbers",
                    json.dumps(
                        {"conflictingDayNumbers": overlapping_day_numbers},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            strategy_day_counts = Counter(
                int(getattr(strategy, "day_number", 0) or 0) for strategy in strategies
            )
            missing_day_numbers = [
                day_number
                for day_number in required_day_numbers
                if strategy_day_counts.get(day_number, 0) == 0
            ]
            duplicate_day_numbers = [
                day_number
                for day_number in required_day_numbers
                if strategy_day_counts.get(day_number, 0) > 1
            ]
            if missing_day_numbers or duplicate_day_numbers:
                detail: dict[str, list[int]] = {}
                if missing_day_numbers:
                    detail["missingDayNumbers"] = missing_day_numbers
                if duplicate_day_numbers:
                    detail["duplicateDayNumbers"] = duplicate_day_numbers
                raise DecisionNormalizationError(
                    "draft_required_day_coverage_incomplete",
                    "actionDirective.dayStrategies",
                    json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                )
        soft_goal_ids = {
            str(item.get("goalId") or "")
            for item in context.get("goalRequirements") or []
            if isinstance(item, dict)
            and str(item.get("requirementLevel") or "required")
            in {
                "soft_experience",
                "optional",
            }
            and str(item.get("goalId") or "")
        }
        goal_maximums = {
            str(item.get("goalId") or ""): int(item["maxCount"])
            for item in context.get("goalRequirements") or []
            if isinstance(item, dict) and str(item.get("goalId") or "") and item.get("maxCount") is not None
        }
        scheduled_counts: dict[str, int] = {}
        scheduled_goal_days: list[tuple[str, int]] = []
        optional_occurrence_count = 0
        scheduled_goal_ids_by_day: dict[int, set[str]] = {}
        for index, strategy in enumerate(strategies):
            day_number = int(getattr(strategy, "day_number", 0) or 0)
            required_goal_ids = list(getattr(strategy, "required_goal_ids", []) or [])
            required_goal_counts = dict(getattr(strategy, "required_goal_counts", {}) or {})
            required_ids = set(required_goal_ids)
            required_ids.update(required_goal_counts)
            invalid = sorted(required_ids.intersection(soft_goal_ids))
            if invalid:
                raise DecisionNormalizationError(
                    "draft_soft_goal_misclassified_as_required",
                    f"actionDirective.dayStrategies[{index}].requiredGoalIds",
                    invalid[0],
                )
            for goal_id in required_goal_ids:
                scheduled_counts[goal_id] = scheduled_counts.get(goal_id, 0) + int(
                    required_goal_counts.get(goal_id) or 1
                )
                scheduled_goal_days.append((goal_id, day_number))
                scheduled_goal_ids_by_day.setdefault(day_number, set()).add(goal_id)
            for goal_id in getattr(strategy, "optional_goal_ids", []) or []:
                scheduled_counts[goal_id] = scheduled_counts.get(goal_id, 0) + 1
                scheduled_goal_days.append((goal_id, day_number))
                scheduled_goal_ids_by_day.setdefault(day_number, set()).add(goal_id)
                optional_occurrence_count += 1
        optional_budget = int(getattr(directive, "optional_experience_budget", 0) or 0)
        if optional_occurrence_count > optional_budget:
            raise DecisionNormalizationError(
                "draft_optional_experience_budget_exceeded",
                "actionDirective.optionalExperienceBudget",
                f"{optional_occurrence_count}/{optional_budget}",
            )
        for goal_id, maximum in sorted(goal_maximums.items()):
            actual = scheduled_counts.get(goal_id, 0)
            if actual > maximum:
                raise DecisionNormalizationError(
                    "draft_goal_maximum_cardinality_exceeded",
                    "actionDirective.dayStrategies",
                    f"{goal_id}:{actual}/{maximum}",
                )
        scheduled_goal_day_counts = Counter(scheduled_goal_days)
        duplicate_scheduled_pair = next(
            (
                pair
                for pair, count in sorted(scheduled_goal_day_counts.items())
                if count != 1
            ),
            None,
        )
        if duplicate_scheduled_pair is not None:
            raise DecisionNormalizationError(
                "draft_scheduled_goal_day_duplicate",
                "actionDirective.dayStrategies",
                f"{duplicate_scheduled_pair[0]}@day:{duplicate_scheduled_pair[1]}",
            )
        if enforce_required_day_contract:
            required_day_numbers = {
                int(day)
                for day in context.get("requiredPlanningDayNumbers") or []
                if isinstance(day, int) and not isinstance(day, bool) and int(day) > 0
            }
            goal_requirements = {
                str(item.get("goalId") or ""): item
                for item in context.get("goalRequirements") or []
                if isinstance(item, dict) and str(item.get("goalId") or "")
            }
            authoritative_non_meal_goal_ids = {
                goal_id
                for goal_id, item in goal_requirements.items()
                if str(item.get("intentType") or "") != "meal"
                and str(item.get("requirementLevel") or "required")
                not in {"soft_experience", "optional"}
                and int(item.get("requiredMin") or 0) > 0
            }
            missing_anchor_days = sorted(
                day_number
                for day_number in required_day_numbers
                if not scheduled_goal_ids_by_day.get(day_number, set()).intersection(
                    authoritative_non_meal_goal_ids
                )
            )
            if missing_anchor_days:
                raise DecisionNormalizationError(
                    "draft_required_day_anchor_missing",
                    "actionDirective.dayStrategies",
                    json.dumps(
                        {"missingDayNumbers": missing_anchor_days},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            scheduled_days_by_goal = {
                goal_id: {
                    day_number
                    for scheduled_goal_id, day_number in scheduled_goal_day_counts
                    if scheduled_goal_id == goal_id
                }
                for goal_id in goal_requirements
            }
            for goal_id, item in sorted(goal_requirements.items()):
                if str(item.get("distributionPolicy") or "") != "every_allowed_day":
                    continue
                allowed_day_numbers = {
                    int(day)
                    for day in item.get("allowedDayNumbers") or []
                    if isinstance(day, int) and not isinstance(day, bool)
                }
                target_day_numbers = sorted(required_day_numbers.intersection(allowed_day_numbers))
                missing_occurrence_days = sorted(
                    set(target_day_numbers) - scheduled_days_by_goal.get(goal_id, set())
                )
                if missing_occurrence_days:
                    raise DecisionNormalizationError(
                        "draft_required_day_occurrence_coverage_incomplete",
                        "actionDirective.dayStrategies",
                        json.dumps(
                            {
                                "goalId": goal_id,
                                "missingDayNumbers": missing_occurrence_days,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )

        schedule_hints = list(getattr(directive, "occurrence_schedule_hints", []) or [])
        hinted_goal_days = [
            (str(getattr(hint, "goal_id", "") or ""), int(getattr(hint, "day_number", 0) or 0))
            for hint in schedule_hints
        ]
        hint_counts = Counter(hinted_goal_days)
        duplicate_hint_pair = next(
            (pair for pair, count in sorted(hint_counts.items()) if count != 1),
            None,
        )
        if duplicate_hint_pair is not None:
            raise DecisionNormalizationError(
                "draft_occurrence_schedule_hint_duplicate",
                "actionDirective.occurrenceScheduleHints",
                f"{duplicate_hint_pair[0]}@day:{duplicate_hint_pair[1]}",
            )
        expected_pairs = set(scheduled_goal_day_counts)
        actual_pairs = set(hint_counts)
        missing_pairs = sorted(expected_pairs - actual_pairs)
        if missing_pairs:
            goal_id, day_number = missing_pairs[0]
            raise DecisionNormalizationError(
                "draft_occurrence_schedule_hint_coverage_incomplete",
                "actionDirective.occurrenceScheduleHints",
                f"{goal_id}@day:{day_number}",
            )
        extra_pairs = sorted(actual_pairs - expected_pairs)
        if extra_pairs:
            goal_id, day_number = extra_pairs[0]
            raise DecisionNormalizationError(
                "draft_occurrence_schedule_hint_extra",
                "actionDirective.occurrenceScheduleHints",
                f"{goal_id}@day:{day_number}",
            )
        for index, hint in enumerate(schedule_hints):
            day_part = str(getattr(hint, "day_part", "") or "")
            preferred_start_time = str(getattr(hint, "preferred_start_time", "") or "")
            if day_part not in {"evening", "night"} and not preferred_start_time:
                raise DecisionNormalizationError(
                    "draft_occurrence_schedule_hint_clock_required",
                    f"actionDirective.occurrenceScheduleHints[{index}].preferredStartTime",
                    f"{hinted_goal_days[index][0]}@day:{hinted_goal_days[index][1]}",
                )
            if preferred_start_time and re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d",
                preferred_start_time,
            ) is None:
                raise DecisionNormalizationError(
                    "draft_occurrence_schedule_hint_clock_invalid",
                    f"actionDirective.occurrenceScheduleHints[{index}].preferredStartTime",
                    preferred_start_time,
                )

    @staticmethod
    def _redacted_provider_decision(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw, _parser_aliases = _parse_controller_json_object(raw)
            except json.JSONDecodeError:
                return {"invalidJson": True, "payloadLength": len(raw)}
        if not isinstance(raw, dict):
            return {"invalidPayloadType": type(raw).__name__}

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                sensitive_keys = {
                    "analysis",
                    "reasoning",
                    "reasoningtext",
                    "reasoningcontent",
                    "chainofthought",
                    "prompt",
                    "systemprompt",
                    "hiddenprompt",
                    "internalprompt",
                    "thinking",
                    "thought",
                    "authorization",
                    "proxyauthorization",
                    "apikey",
                    "xapikey",
                    "token",
                    "accesstoken",
                    "refreshtoken",
                    "secret",
                    "clientsecret",
                    "password",
                    "cookie",
                    "setcookie",
                    "header",
                    "headers",
                }
                return {
                    str(key): clean(item)
                    for key, item in value.items()
                    if str(key).casefold().replace("_", "").replace("-", "") not in sensitive_keys
                }
            if isinstance(value, list):
                return [clean(item) for item in value[:20]]
            if isinstance(value, str):
                return value[:2000]
            return value

        return clean(raw)

    @staticmethod
    def _bind_server_target_scope(decision: ModelDecisionV3, context: Optional[dict[str, Any]] = None) -> AgentDecision:
        directive = decision.action_directive.model_dump(by_alias=True, exclude_none=True)
        action = decision.primary_action
        if action == "ask_user":
            AgentAutonomyController._validate_ask_user_contract(
                directive,
                context,
            )
        scope: dict[str, Any] = {}
        if action == "resolve_poi":
            segment_ids = list(directive.get("targetSegmentIds", []))
            goal_id = str(directive.get("targetGoalId") or "")
            requested_day_number = (context or {}).get("requestedDayNumber")
            if not segment_ids and goal_id:
                observation = (
                    (context or {}).get("agentObservation")
                    if isinstance((context or {}).get("agentObservation"), dict)
                    else (context or {}).get("observation")
                    if isinstance((context or {}).get("observation"), dict)
                    else {}
                )
                if not observation and isinstance((context or {}).get("unresolvedSlots"), list):
                    observation = {"unresolvedSlots": (context or {}).get("unresolvedSlots")}
                matches = [
                    str(item.get("segmentId"))
                    for item in observation.get("unresolvedSlots") or []
                    if isinstance(item, dict)
                    and str(item.get("goalId") or "") == goal_id
                    and (requested_day_number is None or item.get("dayNumber") == requested_day_number)
                    and str(item.get("segmentId") or "")
                ]
                if len(set(matches)) == 1:
                    segment_ids = list(dict.fromkeys(matches))
            scope = {
                "segmentIds": segment_ids,
                "goalId": directive.get("targetGoalId"),
                "dayNumber": requested_day_number,
                "query": directive.get("searchIntent"),
            }
        elif action == "patch_itinerary":
            scope = {
                "baseVersionId": directive.get("baseVersionId"),
                "segmentIds": directive.get("targetSegmentIds", []),
                "operationScope": directive.get("operationIntent"),
                "mutationIntent": directive.get("mutationIntent"),
                "startTime": directive.get("startTime"),
                "candidateId": directive.get("candidateId"),
                "amapPoiId": directive.get("amapPoiId"),
                "preserve": directive.get("preserve", []),
            }
        elif action == "optimize_route":
            scope = {
                "baseVersionId": directive.get("baseVersionId"),
                "dayIds": directive.get("dayIds", []),
                "dayNumbers": directive.get("dayNumbers", []),
                "optimizationObjective": directive.get("optimizationObjective", "balanced"),
            }
        elif action == "verify_external_facts":
            scope = {
                "factTypes": directive.get("factTypes", []),
                "segmentIds": directive.get("segmentIds", []),
                "writeBackMode": "derived_facts_only",
            }
        stop_conditions = {
            "ask_user": "needs_confirmation",
            "draft_itinerary": "continue_after_observation",
            "read_itinerary": "read_complete",
            "resolve_poi": "candidate_or_patch",
            "optimize_route": "verified_route",
            "patch_itinerary": "verified_patch",
            "verify_external_facts": "facts_collected",
            "finish": "finished",
        }
        required_tools = {
            "ask_user": [],
            "draft_itinerary": ["resolve_poi", "patch_itinerary"],
            "read_itinerary": ["read_itinerary"],
            "resolve_poi": ["resolve_poi"],
            "optimize_route": ["read_itinerary", "optimize_route", "patch_itinerary"],
            "patch_itinerary": ["read_itinerary", "resolve_poi", "patch_itinerary"],
            "verify_external_facts": ["web_search", "ticket_lookup", "amap_weather"],
            "finish": [],
        }
        clarification = None
        user_visible_reason = ""
        if action == "ask_user":
            question = str(directive.get("question") or "需要你补充选择后才能继续。")
            options = [dict(item) for item in directive.get("options") or [] if isinstance(item, dict)]
            if not options:
                choice_ids = [str(item) for item in directive.get("choiceIds") or [] if str(item)]
                if len(choice_ids) < 2:
                    choice_ids = ["keep_current", "manual"]
                options = [
                    {
                        "id": choice_id,
                        "label": choice_id,
                        "allowsManualInput": index == len(choice_ids) - 1,
                    }
                    for index, choice_id in enumerate(choice_ids)
                ]
            clarification = {
                "question": question,
                "dimensionId": directive.get("dimensionId"),
                "checkpointId": directive.get("checkpointId"),
                "planningRootId": directive.get("planningRootId"),
                "requestFingerprint": directive.get("requestFingerprint"),
                "checkpointFingerprint": directive.get("checkpointFingerprint"),
                "whyItMatters": directive.get("whyItMatters"),
                "allowFreeText": directive.get("allowFreeText"),
                "options": options,
            }
            clarification = {key: value for key, value in clarification.items() if value is not None}
            user_visible_reason = question
        elif action == "finish":
            user_visible_reason = str(directive.get("assistantReply") or "")
        elif action == "patch_itinerary":
            user_visible_reason = str(directive.get("requestedOutcome") or "")
        payload = {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": action,
            "confidence": 1.0,
            "decisionSummary": f"controller_action:{action}",
            "userVisibleReason": user_visible_reason,
            "reasonCodes": ["model_controller_v3_success", "server_derived_execution_contract"],
            "requiredTools": required_tools[action],
            "actionDirective": directive,
            "proposedWriteRisk": AgentDecisionPolicyGate.WRITE_RISK_BY_ACTION[action],
            "targetScope": {key: value for key, value in scope.items() if value is not None},
            "assumptionPolicy": "none",
            "assumptions": [],
            "clarification": clarification,
            "memoryPolicy": "none",
            "memoryCandidates": [],
            "uncertaintyPolicy": "ask_user" if action == "ask_user" else "none",
            "uncertainties": [],
            "executionLimits": {},
            "expectedOutcome": {"taskRoute": AgentActionExecutorRegistry.ROUTES[action]},
            "stopCondition": {"type": stop_conditions[action]},
            "fallbackAction": "finish",
            "sideEffects": {
                "conversation": True,
                "itinerary": action in {"draft_itinerary", "patch_itinerary", "optimize_route"},
            },
        }
        return AgentDecision.model_validate(payload)

    @staticmethod
    def _validate_ask_user_checkpoint_identity(
        directive: dict[str, Any],
        context: Optional[dict[str, Any]],
    ) -> None:
        checkpoint = (
            (context or {}).get("clarificationCheckpoint")
            if isinstance((context or {}).get("clarificationCheckpoint"), dict)
            else {}
        )
        expected = {
            "checkpointId": str(checkpoint.get("checkpointId") or ""),
            "planningRootId": str(checkpoint.get("planningRootId") or ""),
            "requestFingerprint": str(checkpoint.get("requestFingerprint") or ""),
            "checkpointFingerprint": str(checkpoint.get("fingerprint") or ""),
        }
        supplied = {field: str(directive.get(field) or "") for field in expected}
        if not checkpoint:
            invented = next(
                (field for field, value in supplied.items() if value),
                None,
            )
            if invented is not None:
                raise DecisionNormalizationError(
                    "clarification_checkpoint_identity_unexpected",
                    f"actionDirective.{invented}",
                )
            return
        missing = next(
            (field for field in expected if not expected[field] or not supplied[field]),
            None,
        )
        if missing is not None:
            raise DecisionNormalizationError(
                "clarification_checkpoint_identity_missing",
                f"actionDirective.{missing}",
            )
        mismatch = next(
            (field for field in expected if supplied[field] != expected[field]),
            None,
        )
        if mismatch is not None:
            raise DecisionNormalizationError(
                "clarification_checkpoint_identity_mismatch",
                f"actionDirective.{mismatch}",
            )

    @classmethod
    def _validate_ask_user_contract(
        cls,
        directive: dict[str, Any],
        context: Optional[dict[str, Any]],
    ) -> None:
        """Validate model-authored clarification against the server contract.

        Pydantic proves the envelope shape. This check binds the selected
        dimension and every semantic option to the exact unresolved request
        dimension before the decision can reach AgentService persistence.
        """

        cls._validate_ask_user_checkpoint_identity(directive, context)
        dimensions = [
            item
            for item in (context or {}).get("clarificationDimensions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        ]
        supplied_batch = directive.get("questions")
        questions = supplied_batch if isinstance(supplied_batch, list) and supplied_batch else [directive]
        if not 1 <= len(questions) <= 3:
            raise DecisionNormalizationError(
                "clarification_question_batch_invalid",
                "actionDirective.questions",
            )
        seen_dimensions: set[str] = set()
        for index, question in enumerate(questions):
            path = f"actionDirective.questions.{index}" if isinstance(supplied_batch, list) else "actionDirective"
            if not isinstance(question, dict):
                raise DecisionNormalizationError(
                    "clarification_question_invalid",
                    path,
                )
            dimension_id = str(question.get("dimensionId") or "").strip()
            if not dimensions:
                if dimension_id:
                    raise DecisionNormalizationError(
                        "clarification_dimension_not_in_contract",
                        f"{path}.dimensionId",
                        dimension_id,
                    )
                return
            if not dimension_id:
                raise DecisionNormalizationError(
                    "clarification_dimension_required",
                    f"{path}.dimensionId",
                )
            if dimension_id in seen_dimensions:
                raise DecisionNormalizationError(
                    "clarification_dimension_duplicate",
                    f"{path}.dimensionId",
                    dimension_id,
                )
            seen_dimensions.add(dimension_id)
            dimension = next(
                (
                    item
                    for item in dimensions
                    if str(item.get("dimensionId") or "") == dimension_id
                    and str(item.get("status") or "unresolved") == "unresolved"
                ),
                None,
            )
            if dimension is None:
                raise DecisionNormalizationError(
                    "clarification_dimension_not_unresolved",
                    f"{path}.dimensionId",
                    dimension_id,
                )
            if not str(question.get("whyItMatters") or "").strip():
                raise DecisionNormalizationError(
                    "clarification_why_required",
                    f"{path}.whyItMatters",
                )
            if not isinstance(question.get("allowFreeText"), bool):
                raise DecisionNormalizationError(
                    "clarification_free_text_policy_required",
                    f"{path}.allowFreeText",
                )
            options = question.get("options")
            normalized = ClarificationCheckpointService._options(options)
            if not isinstance(options, list) or len(options) < 2 or len(normalized) != len(options):
                raise DecisionNormalizationError(
                    "clarification_semantic_options_invalid",
                    f"{path}.options",
                )
            allowed_fields = {
                str(item)
                for item in dimension.get("allowedSemanticFields") or []
                if str(item).strip()
            }
            if not allowed_fields or any(
                not ClarificationCheckpointService.valid_controller_semantic_patch(
                    option.get("semanticValue"),
                    allowed_fields=allowed_fields,
                )
                for option in options
                if isinstance(option, dict)
            ):
                raise DecisionNormalizationError(
                    "clarification_semantic_fields_invalid",
                    f"{path}.options.semanticValue",
                )

    @staticmethod
    def _normalization_context(autonomy_context: dict[str, Any]) -> dict[str, Any]:
        observation = (
            autonomy_context.get("observation") if isinstance(autonomy_context.get("observation"), dict) else {}
        )
        candidate_state = (
            observation.get("candidateState") if isinstance(observation.get("candidateState"), dict) else {}
        )
        coverage = (
            observation.get("requirementCoverage") if isinstance(observation.get("requirementCoverage"), dict) else {}
        )
        resolved_dates = (
            autonomy_context.get("resolvedTripDates")
            if isinstance(autonomy_context.get("resolvedTripDates"), dict)
            else {}
        )
        resolved_date_values = [item for item in resolved_dates.get("dates") or [] if item]
        trip_dates_resolved = resolved_dates.get("status") == "resolved" and bool(resolved_date_values)
        available_days = list(range(1, len(resolved_date_values) + 1))
        request = observation.get("request") if isinstance(observation.get("request"), dict) else {}
        projected_contract = (
            autonomy_context.get("requestIntentContract")
            if isinstance(autonomy_context.get("requestIntentContract"), dict)
            else {}
        )
        intent_contract = projected_contract or (
            request.get("intentContract") if isinstance(request.get("intentContract"), dict) else {}
        )

        def contract_day_numbers(key: str) -> Optional[list[int]]:
            source = intent_contract if key in intent_contract else autonomy_context
            if key not in source or not isinstance(source.get(key), list):
                return None
            return sorted(
                {
                    int(day)
                    for day in source.get(key) or []
                    if isinstance(day, int)
                    and not isinstance(day, bool)
                    and int(day) in available_days
                }
            )

        explicit_rest_days = contract_day_numbers("explicitRestDayNumbers") or []
        required_planning_day_contract_present = (
            "requiredPlanningDayNumbers" in intent_contract
            or "requiredPlanningDayNumbers" in autonomy_context
        )
        required_planning_days = contract_day_numbers("requiredPlanningDayNumbers")
        if required_planning_days is None:
            required_planning_days = [day for day in available_days if day not in explicit_rest_days]
        goal_requirements = []
        for item in coverage.get("required") or []:
            if not isinstance(item, dict):
                continue
            goal_requirements.append(
                {
                    "goalId": item.get("goalId") or f"goal_{item.get('intentType')}",
                    "intentType": item.get("intentType"),
                    "requiredMin": int(item.get("requiredMin") or item.get("minCount") or item.get("target") or 0),
                    "preferredCount": item.get("preferredCount"),
                    "maxCount": item.get("maxCount"),
                    "cardinalitySource": item.get("cardinalitySource") or item.get("source"),
                    "distributionPolicy": item.get("distributionPolicy") or "spread_across_distinct_days",
                    "requirementLevel": item.get("requirementLevel") or "required",
                    "allowedDayNumbers": list(item.get("allowedDayNumbers") or available_days),
                }
            )
        contract_required_goal_ids = {
            str(item.get("goalId"))
            for item in intent_contract.get("requiredIntents") or []
            if isinstance(item, dict) and item.get("goalId")
        }
        hard_required_goals = [
            item
            for item in goal_requirements
            if int(item.get("requiredMin") or 0) > 0
            and item.get("requirementLevel") not in {"soft_experience", "optional"}
        ]
        authoritative_goal_ledger = bool(hard_required_goals) and {
            str(item["goalId"]) for item in hard_required_goals if item.get("goalId")
        }.issubset(contract_required_goal_ids)
        return {
            "tripDatesResolved": trip_dates_resolved,
            "authoritativeGoalLedger": authoritative_goal_ledger,
            "activeVersionId": autonomy_context.get("activeVersionId")
            or (observation.get("itinerary") or {}).get("activeVersionId"),
            "segmentRefs": observation.get("segmentRefs") or [],
            "unresolvedSlots": observation.get("unresolvedSlots") or [],
            "requestedDayNumber": (
                AgentAutonomyController._explicit_day_number(str(autonomy_context.get("latestUserMessage") or ""))
                or AgentAutonomyController._explicit_day_number(str(autonomy_context.get("effectiveUserMessage") or ""))
            ),
            "candidateGroups": candidate_state.get("pendingGroups") or autonomy_context.get("pendingCandidates") or [],
            "goalRequirements": goal_requirements,
            "requiredGoalCounts": {
                str(item["goalId"]): int(item.get("requiredMin") or 1)
                for item in hard_required_goals
                if item.get("goalId")
            },
            "optionalGoalIds": [
                str(item["goalId"])
                for item in goal_requirements
                if item.get("goalId") and item.get("requirementLevel") in {"soft_experience", "optional"}
            ],
            "availableDayNumbers": available_days,
            "requiredPlanningDayNumbers": required_planning_days,
            "explicitRestDayNumbers": explicit_rest_days,
            "requiredPlanningDayContractPresent": required_planning_day_contract_present,
            "clarificationCheckpoint": deepcopy(autonomy_context.get("clarificationCheckpoint"))
            if isinstance(autonomy_context.get("clarificationCheckpoint"), dict)
            else None,
            "clarificationDimensions": deepcopy(intent_contract.get("clarificationDimensions") or []),
            "viewResolution": deepcopy(autonomy_context.get("viewResolution"))
            if isinstance(autonomy_context.get("viewResolution"), dict)
            else None,
            "simpleDirectionGenerationAuthorized": (
                autonomy_context.get("simpleDirectionGenerationAuthorized") is True
            ),
        }

    @staticmethod
    def _explicit_day_number(message: str) -> Optional[int]:
        match = re.search(r"第\s*([一二三四五六七八九十]|\d{1,2})\s*天", message)
        if not match:
            return None
        token = match.group(1)
        if token.isdigit():
            return int(token)
        chinese_days = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        return chinese_days.get(token)

    @staticmethod
    def _truncation_repair_feedback(
        *,
        error: ControllerOutputTruncatedError,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one content-free reissue contract for a known truncation.

        The partial bytes are diagnostic evidence only.  They are never parsed
        for an action, patched, echoed to the model, or accepted as authority.
        """

        return {
            "repairMode": "full_response_truncation_reissue",
            "contractVersion": contract.get("contractVersion"),
            "contractHash": contract.get("contractHash"),
            "allowedActions": list(contract.get("allowedActions") or []),
            "failure": error.to_safe_dict(),
            "instruction": "Return one complete ModelDecisionV3 JSON object; do not continue the partial bytes.",
        }

    def _repair_feedback(
        self,
        *,
        raw: Any,
        error: Exception,
        contract: dict[str, Any],
        contract_service: AgentDecisionContractService,
        context: dict[str, Any],
        aliases: list[str],
    ) -> dict[str, Any]:
        allowed_actions = tuple(str(item) for item in contract.get("allowedActions") or [])
        action = self._repair_action_from_raw(raw, allowed_actions=allowed_actions)
        if not action:
            unique_actions = tuple(dict.fromkeys(item for item in allowed_actions if item))
            if len(unique_actions) == 1 and (
                unique_actions[0] != "draft_itinerary"
                or self._authoritative_initial_draft_repair_allowed(
                    context,
                    allowed_actions=unique_actions,
                )
            ):
                # The authoritative observation contract exposes exactly one
                # repair schema. Full ModelDecisionV3 validation and the normal
                # action gates still apply after the bounded repair call.
                action = unique_actions[0]
            else:
                # A missing/unparseable primaryAction is not authority to choose
                # between two safe paths. In particular, route-only planning may
                # legitimately allow either ask_user or draft_itinerary.
                raise DecisionNormalizationError(
                    "controller_repair_action_unresolved",
                    "primaryAction",
                )
        paths: list[str] = []
        errors = getattr(error, "errors", None)
        if callable(errors):
            for item in errors():
                path = ".".join(str(part) for part in item.get("loc") or [])
                if path:
                    paths.append(path)
        if "draft_action_does_not_accept_poi_assumptions" in str(error):
            paths.append("assumptions")
        if "draft_required_goals_not_scheduled" in str(error):
            paths.extend(
                [
                    "actionDirective.goalPriority",
                    "actionDirective.dayStrategies.requiredGoalIds",
                    "actionDirective.dayStrategies.requiredGoalCounts",
                ]
            )
        if isinstance(error, DecisionNormalizationError):
            paths.append(error.path)
        allowed_ids = {
            "segmentIds": [
                str(item.get("segmentId") or item.get("id"))
                for item in context.get("segmentRefs") or []
                if isinstance(item, dict) and (item.get("segmentId") or item.get("id"))
            ],
            "versionIds": [str(context.get("activeVersionId"))] if context.get("activeVersionId") else [],
            "candidateIds": [],
            "amapPoiIds": [],
            "goalIds": [
                str(item.get("goalId")) for item in context.get("goalRequirements") or [] if item.get("goalId")
            ],
        }
        route_policy_repair_requirement = (
            context.get("routePlanningPolicyRequirement")
            if isinstance(context.get("routePlanningPolicyRequirement"), dict)
            else None
        )
        if action == "draft_itinerary" and any(
            path.startswith("actionDirective.routePlanningPolicy") for path in paths
        ):
            route_policy_repair_requirement = (
                controller_draft_route_policy_repair_requirement()
            )
        return contract_service.repair_payload(
            action=action,
            invalid_paths=paths or [type(error).__name__],
            allowed_ids=allowed_ids,
            aliases_applied=aliases,
            goal_requirements=context.get("goalRequirements") or [],
            route_policy_requirement=(
                route_policy_repair_requirement
                if action == "draft_itinerary"
                else None
            ),
        )

    @staticmethod
    def _repair_action_from_raw(raw: Any, *, allowed_actions: tuple[str, ...]) -> str:
        candidate = ""
        raw_text = raw if isinstance(raw, str) else ""
        parsed = raw
        if raw_text:
            try:
                parsed, _parser_aliases = _parse_controller_json_object(raw_text)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict):
            candidate = str(parsed.get("primaryAction") or "")
        return candidate if candidate in set(allowed_actions) else ""

    @staticmethod
    def _authoritative_initial_draft_repair_allowed(
        context: dict[str, Any],
        *,
        allowed_actions: tuple[str, ...],
    ) -> bool:
        if (
            "draft_itinerary" not in allowed_actions
            or context.get("tripDatesResolved") is not True
            or context.get("authoritativeGoalLedger") is not True
        ):
            return False
        if not context.get("activeVersionId"):
            return True
        view_resolution = context.get("viewResolution")
        return bool(
            context.get("simpleDirectionGenerationAuthorized") is True
            and isinstance(view_resolution, dict)
            and view_resolution.get("resolvedAction") == "generate_new_direction"
            and view_resolution.get("resolutionSource") == "server_validated_opaque_choice"
        )

    @staticmethod
    def _validate_lite_provider_decision(raw: Any) -> AgentDecisionLite:
        raw, _parser_aliases = _parse_controller_json_object(raw)
        return AgentDecisionLite.model_validate(raw)

    @staticmethod
    def _decision_from_lite(lite: AgentDecisionLite, autonomy_context: dict[str, Any]) -> AgentDecision:
        action = lite.primary_action
        reason_codes = ["model_controller_lite_success", lite.reason_code]
        required_tools: list[str] = []
        clarification = None
        action_directive: Optional[dict[str, Any]] = None
        proposed_risk: WriteRisk = "none"
        stop_condition = {"type": "executor_terminal"}
        if action in {"patch_itinerary", "resolve_poi", "optimize_route"}:
            action = "finish"
            reason_codes.append("lite_target_binding_required")
        elif action == "draft_itinerary":
            action = "ask_user"
            reason_codes.append("lite_high_risk_write_authority_forbidden")
            clarification = {
                "question": "完整决策合同暂未形成，当前未创建行程。你可以重试模型规划或补充目标。",
                "options": [
                    {
                        "id": "retry_controller",
                        "action": "retry_model_planning",
                        "label": "重试模型规划",
                        "allowsManualInput": False,
                    },
                    {"id": "manual", "action": "manual_continuation", "label": "补充目标", "allowsManualInput": True},
                ],
            }
            action_directive = {
                "type": "ask_user",
                "question": clarification["question"],
                "choiceIds": ["retry_controller", "manual"],
            }
            stop_condition = {"type": "needs_confirmation"}
        elif action == "read_itinerary":
            required_tools = ["read_itinerary"]
            stop_condition = {"type": "read_complete"}
        elif action == "verify_external_facts":
            required_tools = ["web_search", "ticket_lookup", "amap_weather"]
            stop_condition = {"type": "facts_collected"}
        elif action == "ask_user":
            clarification = {
                "question": lite.user_visible_reason,
                "options": [
                    {
                        "id": "retry_controller",
                        "action": "retry_model_planning",
                        "label": "重试模型规划",
                        "allowsManualInput": False,
                    },
                    {
                        "id": "manual",
                        "action": "manual_continuation",
                        "label": "我自己补充",
                        "allowsManualInput": True,
                    },
                ],
            }
            action_directive = {
                "type": "ask_user",
                "question": clarification["question"],
                "choiceIds": ["retry_controller", "manual"],
            }
            stop_condition = {"type": "needs_confirmation"}
        return AgentDecision(
            schemaVersion="agent-decision-v1",
            primaryAction=action,
            confidence=lite.confidence,
            decisionSummary=lite.user_visible_reason,
            userVisibleReason=lite.user_visible_reason,
            reasonCodes=reason_codes,
            requiredTools=required_tools,
            actionDirective=action_directive,
            proposedWriteRisk=proposed_risk,
            targetScope={},
            clarification=clarification,
            uncertaintyPolicy="ask_user" if action == "ask_user" else "none",
            expectedOutcome={"taskRoute": AgentActionExecutorRegistry.ROUTES[action]},
            stopCondition=stop_condition,
            sideEffects={"conversation": True, "itinerary": False},
        )

    @staticmethod
    def _decision_from_planner(plan: dict[str, Any]) -> AgentDecision:
        route = str(plan.get("taskRoute") or "finish")
        task_type = str(plan.get("taskType") or "")
        action: PrimaryAction = {
            "clarification": "ask_user",
            "initial_staged_pipeline": "draft_itinerary",
            "read_only": "read_itinerary",
            "poi_resolution": "resolve_poi",
            "route_optimization": "optimize_route",
            "timeline_patch": "patch_itinerary",
        }.get(route, "finish")  # type: ignore[assignment]
        if task_type == "initial_planning" or route.startswith("initial_planning"):
            action = "draft_itinerary"
        elif plan.get("requiresPatch"):
            action = "patch_itinerary"
        elif plan.get("requiresPoiResolution"):
            action = "resolve_poi"
        elif plan.get("readOnly"):
            action = "read_itinerary"
        target_scope: dict[str, Any] = {}
        if action == "patch_itinerary":
            candidate_scope = plan.get("targetScope") if isinstance(plan.get("targetScope"), dict) else {}
            if candidate_scope.get("baseVersionId") and (
                candidate_scope.get("segmentIds") or candidate_scope.get("dayIds")
            ):
                target_scope = candidate_scope
            else:
                action = "ask_user"
        elif action == "optimize_route":
            candidate_scope = plan.get("targetScope") if isinstance(plan.get("targetScope"), dict) else {}
            if candidate_scope.get("baseVersionId") and (
                candidate_scope.get("dayIds") or candidate_scope.get("dayNumbers")
            ):
                target_scope = candidate_scope
            else:
                action = "ask_user"
        clarification = None
        if action == "ask_user":
            clarification = {
                "question": "请补充本轮规划所需信息",
                "options": [
                    {"id": "use_defaults", "label": "按可逆默认值继续", "allowsManualInput": False},
                    {"id": "manual", "label": "我自己填写", "allowsManualInput": True},
                ],
            }
        if action == "resolve_poi":
            target_scope = {"query": str(plan.get("intent") or "planner_fallback")}
        return AgentDecision(
            primaryAction=action,
            confidence=0.6,
            decisionSummary="AgentPlannerService deterministic fallback",
            userVisibleReason="自治决策不可用，已使用现有确定性规划器。",
            reasonCodes=["planner_fallback"],
            requiredTools=list(plan.get("allowedTools") or []),
            proposedWriteRisk="none",
            targetScope=target_scope,
            clarification=clarification,
            expectedOutcome={"taskRoute": route},
            stopCondition={"type": "existing_route_terminal"},
            fallbackAction="finish",
        )


# Deprecated import compatibility for external callers; active execution uses
# AgentDecisionResult and no longer describes real decisions as shadow state.
AutonomyShadowResult = AgentDecisionResult
