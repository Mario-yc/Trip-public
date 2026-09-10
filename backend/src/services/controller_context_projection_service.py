from __future__ import annotations

import copy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.services.clarification_checkpoint_service import ClarificationCheckpointService
from src.services.planning_attempt_resume_service import PlanningAttemptResumeService


FULL_REQUEST_BYTE_LIMIT = 15_360
LITE_REQUEST_BYTE_LIMIT = 8_192
# Leave a deterministic envelope for the fixed system prompt and for JSON
# string escaping when this projection is embedded in the provider request.
# A 10 KB context can still exceed the 15 KB request wall even though the
# projection itself passes, as each JSON key is escaped inside messages[].
FULL_CONTEXT_BYTE_LIMIT = 8_500
LITE_CONTEXT_BYTE_LIMIT = 5_500


def controller_decision_message(context: dict[str, Any], fallback: str = "") -> str:
    """Choose one canonical message for projection and Coordinator decision."""

    latest = str(context.get("latestUserMessage") or fallback or "").strip()
    effective = str(context.get("effectiveUserMessage") or latest).strip()
    continuation = context.get("continuationContext") if isinstance(context.get("continuationContext"), dict) else {}
    selected_choice = context.get("selectedAgentChoice") if isinstance(context.get("selectedAgentChoice"), dict) else {}
    selected_option = selected_choice.get("option") if isinstance(selected_choice.get("option"), dict) else {}
    selected_action = str(selected_choice.get("action") or selected_option.get("action") or "")
    pure_retry = bool(
        selected_action == "retry_model_planning"
        or str(continuation.get("kind") or "") == "retry_model_planning"
        or PlanningAttemptResumeService.is_retry_intent(latest)
    )
    return effective if pure_retry else latest or effective


class GoalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goalId: str
    intentType: Optional[str] = None
    requirementLevel: Optional[str] = None
    requiredMin: int = 0
    preferredCount: Optional[int] = None
    maxCount: Optional[int] = None
    distributionPolicy: Optional[str] = None
    allowedDayNumbers: list[int] = Field(default_factory=list)


class PendingSlotSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goalId: Optional[str] = None
    briefId: Optional[str] = None
    poolId: Optional[str] = None
    planningSlotId: Optional[str] = None
    dayNumber: Optional[int] = None
    timeWindow: Optional[dict[str, Any]] = None


class ItineraryLifecycleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    activeVersionId: Optional[str] = None
    meaningfulSegmentCount: int = 0
    planningAttemptPersisted: bool = False
    cycleIndex: int = 0


class FingerprintSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requestContractFingerprint: Optional[str] = None
    observationFingerprint: Optional[str] = None
    planningSelectionRootTurnId: Optional[str] = None
    rootPortfolioId: Optional[str] = None
    expectedBaseVersionId: Optional[str] = None
    focusBriefId: Optional[str] = None


class FullControllerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: str = "controller-context-full-v1"
    latestUserMessage: str
    selectedCity: Optional[str] = None
    itineraryLifecycle: ItineraryLifecycleSummary
    goalRequirements: list[GoalSummary] = Field(default_factory=list)
    pendingSlots: list[PendingSlotSummary] = Field(default_factory=list)
    targetScope: dict[str, Any] = Field(default_factory=dict)
    allowedActions: list[str] = Field(default_factory=list)
    fingerprints: FingerprintSummary
    clarificationCheckpoint: Optional[dict[str, Any]] = None
    experienceSpecs: list[dict[str, Any]] = Field(default_factory=list)
    candidateGapSummary: Optional[dict[str, Any]] = None
    clarificationDimensions: list[dict[str, Any]] = Field(default_factory=list)
    completionCriteria: list[Any] = Field(default_factory=list)
    provisionalGoalOccurrenceProjection: Optional[dict[str, Any]] = None
    routePlanningPolicyRequirement: Optional[dict[str, Any]] = None
    untrustedSourceEvidence: list[dict[str, Any]] = Field(default_factory=list)
    sourceMaterialGoalHints: list[dict[str, Any]] = Field(default_factory=list)
    requestActivityClauses: Optional[list[dict[str, Any]]] = None
    decisionConstraints: dict[str, Any] = Field(default_factory=dict)
    decisionContractRef: dict[str, str]


class LiteControllerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: str = "controller-context-lite-v1"
    latestUserMessage: str
    itineraryLifecycle: ItineraryLifecycleSummary
    goalSummaries: list[GoalSummary] = Field(default_factory=list)
    pendingSlots: list[PendingSlotSummary] = Field(default_factory=list)
    allowedActions: list[str] = Field(default_factory=list)
    fingerprints: FingerprintSummary
    outputSchema: dict[str, Any]


class ControllerProviderContext(dict):
    """Serialized payload plus non-enumerable in-process compatibility state."""

    def __init__(self, payload: dict[str, Any], server_context: dict[str, Any]):
        super().__init__(payload)
        self._server_context = server_context

    def get(self, key: str, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.get(self, key, default)
        return self._server_context.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        if key in self._server_context:
            return self._server_context[key]
        raise KeyError(key)


@dataclass(frozen=True)
class ControllerContextProjection:
    full: dict[str, Any]
    lite: dict[str, Any]
    telemetry: dict[str, Any]


class ControllerContextProjectionService:
    """Builds independent, bounded Controller payloads from server-owned state."""

    def build(
        self,
        context: dict[str, Any],
        *,
        allowed_actions: tuple[str, ...] | list[str],
        decision_contract: dict[str, Any],
        normalization_context: Optional[dict[str, Any]] = None,
    ) -> ControllerContextProjection:
        observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
        continuation = (
            context.get("continuationContext") if isinstance(context.get("continuationContext"), dict) else {}
        )
        normalization = normalization_context or {}
        itinerary = observation.get("itinerary") if isinstance(observation.get("itinerary"), dict) else {}
        planning_attempt = (
            observation.get("planningAttempt") if isinstance(observation.get("planningAttempt"), dict) else {}
        )
        lifecycle = ItineraryLifecycleSummary(
            state=str(
                itinerary.get("lifecycleState") or ("active" if context.get("activeVersionId") else "empty_scaffold")
            ),
            activeVersionId=self._text(context.get("activeVersionId") or itinerary.get("activeVersionId"), 160) or None,
            meaningfulSegmentCount=self._integer(itinerary.get("meaningfulSegmentCount")),
            planningAttemptPersisted=planning_attempt.get("persisted") is True,
            cycleIndex=self._integer(observation.get("cycleIndex")),
        )
        goals = self._goal_summaries(context, normalization)
        pending = self._pending_summaries(context, normalization)
        fingerprints = FingerprintSummary(
            requestContractFingerprint=self._text(
                continuation.get("requestContractFingerprint")
                or planning_attempt.get("requestContractFingerprint")
                or (context.get("requestIntentContract") or {}).get("fingerprint"),
                160,
            )
            or None,
            observationFingerprint=self._text(context.get("observationFingerprint"), 160) or None,
            planningSelectionRootTurnId=self._text(
                continuation.get("planningSelectionRootTurnId") or planning_attempt.get("planningSelectionRootTurnId"),
                160,
            )
            or None,
            rootPortfolioId=self._text(
                continuation.get("rootPortfolioId") or planning_attempt.get("rootPortfolioId"), 160
            )
            or None,
            expectedBaseVersionId=self._text(continuation.get("expectedBaseVersionId"), 160) or None,
            focusBriefId=self._text(continuation.get("focusBriefId") or context.get("focusBriefId"), 160) or None,
        )
        contract_json = json.dumps(decision_contract, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        contract_ref = {
            "schemaVersion": self._text(decision_contract.get("schemaVersion"), 80) or "agent-decision-contract-v3",
            "sha256": self._text(decision_contract.get("contractHash"), 80) or sha256(contract_json).hexdigest(),
        }
        decision_constraints = {
            "requiredGoalCounts": dict(normalization.get("requiredGoalCounts") or {}),
            "optionalGoalIds": list(normalization.get("optionalGoalIds") or [])[:16],
            "availableDayNumbers": list(normalization.get("availableDayNumbers") or [])[:14],
        }
        target_scope = {
            "requestedDayNumber": normalization.get("requestedDayNumber"),
            "segmentIds": [
                self._text(item.get("segmentId") or item.get("id"), 160)
                for item in list(normalization.get("segmentRefs") or [])[:16]
                if isinstance(item, dict) and (item.get("segmentId") or item.get("id"))
            ],
        }
        clarification_checkpoint = (
            context.get("clarificationCheckpoint") if isinstance(context.get("clarificationCheckpoint"), dict) else None
        )
        experience_specs = context.get("experienceSpecs") or (
            clarification_checkpoint.get("experienceSpecs") if clarification_checkpoint is not None else None
        )
        request_contract = (
            context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
        )
        candidate_gap_summary = (
            context.get("candidateGapSummary")
            if isinstance(context.get("candidateGapSummary"), dict)
            else (clarification_checkpoint or {}).get("candidateGapSummary")
        )
        # The normalization context is compiled from the final frozen request
        # contract immediately before this projection is built.  Prefer its
        # registry even when it is explicitly empty: the broader autonomy
        # context can still contain pre-default or pre-answer dimensions from
        # an earlier checkpoint and must not reopen them for the Controller.
        clarification_dimension_source = (
            normalization.get("clarificationDimensions")
            if "clarificationDimensions" in normalization
            else request_contract.get("clarificationDimensions")
        )
        clarification_dimensions = self._clarification_dimensions(clarification_dimension_source)
        completion_criteria = self._completion_criteria(request_contract.get("completionCriteria"))
        provisional_occurrences = (
            context.get("provisionalGoalOccurrenceProjection")
            if isinstance(context.get("provisionalGoalOccurrenceProjection"), dict)
            else request_contract.get("provisionalGoalOccurrenceProjection")
            if isinstance(request_contract.get("provisionalGoalOccurrenceProjection"), dict)
            else None
        )
        controller_user_message = self._text(controller_decision_message(context), 1_200)
        full_model = FullControllerContext(
            latestUserMessage=controller_user_message,
            selectedCity=self._text(context.get("selectedCity"), 80) or None,
            itineraryLifecycle=lifecycle,
            goalRequirements=goals,
            pendingSlots=pending,
            targetScope=target_scope,
            allowedActions=list(dict.fromkeys(str(item) for item in allowed_actions)),
            fingerprints=fingerprints,
            clarificationCheckpoint=self._clarification_checkpoint(clarification_checkpoint),
            experienceSpecs=self._experience_specs(experience_specs),
            candidateGapSummary=(
                self._candidate_gap_summary(candidate_gap_summary) if isinstance(candidate_gap_summary, dict) else None
            ),
            clarificationDimensions=clarification_dimensions,
            completionCriteria=completion_criteria,
            provisionalGoalOccurrenceProjection=(
                self._provisional_goal_occurrence_projection(provisional_occurrences)
                if provisional_occurrences is not None
                else None
            ),
            routePlanningPolicyRequirement=(
                copy.deepcopy(normalization.get("routePlanningPolicyRequirement"))
                if isinstance(normalization.get("routePlanningPolicyRequirement"), dict)
                else None
            ),
            untrustedSourceEvidence=self._untrusted_source_evidence(context.get("sourceMaterialEvidence")),
            sourceMaterialGoalHints=self._source_material_goal_hints(context.get("sourceMaterialGoalHints")),
            requestActivityClauses=copy.deepcopy(normalization.get("requestActivityClauses")),
            decisionConstraints=decision_constraints,
            decisionContractRef=contract_ref,
        )
        lite_model = LiteControllerContext(
            latestUserMessage=controller_user_message,
            itineraryLifecycle=lifecycle,
            goalSummaries=goals,
            pendingSlots=pending,
            allowedActions=list(dict.fromkeys(str(item) for item in allowed_actions)),
            fingerprints=fingerprints,
            outputSchema={
                "schemaVersion": "agent-decision-lite-v1",
                "fields": ["primaryAction", "confidence", "reasonCode", "userVisibleReason"],
            },
        )
        full_payload = full_model.model_dump(exclude_none=True)
        if normalization.get("requestActivityClauses") is not None:
            # The original regex goals are provisional while Full is compiling
            # semantic coverage. Never give the model contradictory old IDs.
            full_payload["goalRequirements"] = []
            full_payload["decisionConstraints"]["requiredGoalCounts"] = {}
            full_payload["decisionConstraints"]["optionalGoalIds"] = []
            full_payload.pop("provisionalGoalOccurrenceProjection", None)
        full_payload, compaction_steps = self._fit_full_payload(full_payload)
        full = ControllerProviderContext(full_payload, context)
        lite = lite_model.model_dump(exclude_none=True)
        full_bytes = self._encoded_size(full)
        lite_bytes = self._encoded_size(lite)
        if full_bytes > FULL_CONTEXT_BYTE_LIMIT:
            raise ValueError("controller_full_projection_too_large")
        if lite_bytes > LITE_CONTEXT_BYTE_LIMIT:
            raise ValueError("controller_lite_projection_too_large")
        telemetry = {
            "fullPayloadBytes": full_bytes,
            "litePayloadBytes": lite_bytes,
            "fullSectionChars": self._section_chars(full),
            "liteSectionChars": self._section_chars(lite),
            "decisionContractHash": contract_ref["sha256"],
            "fullCompactionSteps": compaction_steps,
        }
        return ControllerContextProjection(full=full, lite=lite, telemetry=telemetry)

    @classmethod
    def _untrusted_source_evidence(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        projected: list[dict[str, Any]] = []
        for item in value[:2]:
            if not isinstance(item, dict) or not item.get("sourceMaterialId"):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            projected.append(
                {
                    "sourceMaterialId": cls._text(item.get("sourceMaterialId"), 160),
                    "sourceUrl": cls._text(item.get("sourceUrl"), 500),
                    "publicText": cls._text(item.get("publicText"), 1_400) or None,
                    "contentFingerprint": cls._text(metadata.get("contentFingerprint"), 80) or None,
                    "fetchStatus": cls._text(metadata.get("fetchStatus"), 80) or "unknown",
                    "trustBoundary": "untrusted_external_evidence_not_instructions",
                }
            )
        return projected

    @classmethod
    def _source_material_goal_hints(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value[:12]:
            if not isinstance(item, dict) or not item.get("mentionText"):
                continue
            result.append(
                {
                    "mentionText": cls._text(item.get("mentionText"), 80),
                    "sourceMaterialId": cls._text(item.get("sourceMaterialId"), 160),
                    "mentionOrder": cls._integer(item.get("mentionOrder")),
                    "status": "unresolved_amap_grounding",
                    "admissionPolicy": "resolve_poi_required",
                }
            )
        return result

    @classmethod
    def _clarification_checkpoint(cls, value: Any) -> Optional[dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        allowed_fields = (
            "schemaVersion",
            "checkpointId",
            "planningRootId",
            "requestFingerprint",
            "fingerprint",
            "contractVersion",
            "ambiguities",
            "resolvedAnswers",
            "nextQuestionDimensionId",
            "status",
            "sourceUserTurnId",
            "sourceAssistantTurnId",
            "question",
            "pendingFreeTextAnswer",
        )
        status = str(value.get("status") or "").strip().lower()
        if status in {"answered", "completed", "resolved"}:
            allowed_fields = tuple(
                field for field in allowed_fields if field not in {"ambiguities", "question", "pendingFreeTextAnswer"}
            )
        projected = {
            field: cls._bounded_json(value.get(field), depth=0)
            for field in allowed_fields
            if value.get(field) is not None
        }
        return projected or None

    @classmethod
    def _clarification_dimensions(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        projected: list[dict[str, Any]] = []
        for item in value[:12]:
            if not isinstance(item, dict) or not item.get("dimensionId"):
                continue
            dimension = {
                key: cls._bounded_json(item.get(key), depth=0)
                for key in (
                    "dimensionId",
                    "intentType",
                    "status",
                    "impactCode",
                    "candidateScope",
                    "allowedSemanticFields",
                    "allowFreeText",
                )
                if item.get(key) is not None
            }
            # Exact value schemas are only needed while the controller is
            # authoring an answerable question for this dimension. Keeping
            # the full enum/object schema on resolved dimensions needlessly
            # inflates the subsequent planning request and can push an
            # otherwise valid turn over the controller projection limit.
            if str(item.get("status") or "").strip().lower() == "unresolved":
                semantic_options = [
                    {
                        "id": cls._text(option.get("id"), 120),
                        "defaultLabel": cls._text(option.get("defaultLabel"), 160),
                    }
                    for option in item.get("semanticOptions") or []
                    if isinstance(option, dict) and cls._text(option.get("id"), 120)
                ][:3]
                if semantic_options:
                    dimension["semanticOptions"] = semantic_options
                    dimension["semanticOptionPolicy"] = "server_owned_v1"
                semantic_schemas = ClarificationCheckpointService.semantic_field_schemas(
                    item.get("allowedSemanticFields")
                )
                if semantic_schemas:
                    dimension["semanticFieldSchemas"] = semantic_schemas
            projected.append(dimension)
        return projected

    @classmethod
    def _completion_criteria(cls, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        return [cls._bounded_json(item, depth=0) for item in value[:8]]

    @classmethod
    def _candidate_gap_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        projected: dict[str, Any] = {
            key: cls._bounded_json(value.get(key), depth=0)
            for key in (
                "schemaVersion",
                "status",
                "missingOccurrenceCount",
                "source",
                "fingerprint",
            )
            if value.get(key) is not None
        }
        reasons = value.get("rejectedReasonCounts")
        if isinstance(reasons, dict):
            projected["rejectedReasonCounts"] = {
                cls._text(key, 120): cls._integer(count)
                for key, count in sorted(
                    reasons.items(),
                    key=lambda item: (-cls._integer(item[1]), str(item[0])),
                )[:12]
                if cls._text(key, 120)
            }
        goals: list[dict[str, Any]] = []
        for item in list(value.get("goals") or [])[:8]:
            if not isinstance(item, dict):
                continue
            goal = {
                key: cls._bounded_json(item.get(key), depth=0)
                for key in (
                    "goalId",
                    "intentType",
                    "requirementLevel",
                    "missingOccurrenceCount",
                    "requiredMin",
                    "targetCount",
                    "allowedDayNumbers",
                    "reasonCode",
                )
                if item.get(key) is not None
            }
            if goal:
                goals.append(goal)
        if goals:
            projected["goals"] = goals
        pools: list[dict[str, Any]] = []
        for item in list(value.get("poolEvidence") or [])[:8]:
            if not isinstance(item, dict):
                continue
            pool = {
                key: cls._bounded_json(item.get(key), depth=0)
                for key in (
                    "poolId",
                    "briefId",
                    "planningSlotId",
                    "intentType",
                    "dayNumber",
                    "providerState",
                    "coverageStatus",
                    "rawCandidateCount",
                    "candidateCount",
                    "eligibleCandidateCount",
                    "admittedCandidateCount",
                    "failureReason",
                )
                if item.get(key) is not None
            }
            if pool:
                pools.append(pool)
        if pools:
            projected["poolEvidence"] = pools
        return projected

    @classmethod
    def _fit_full_payload(
        cls,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Deterministically compact valid server state instead of failing a turn.

        The controller needs authoritative goals, lifecycle, actions and a compact
        account of the unresolved gap.  Verbose checkpoint history and diagnostic
        evidence are useful for audit, but they must never make the executable
        planning request impossible.
        """

        compacted = copy.deepcopy(payload)
        steps: list[str] = []

        def fits() -> bool:
            return cls._encoded_size(compacted) <= FULL_CONTEXT_BYTE_LIMIT

        # Once every clarification dimension is resolved, the selected values
        # have already been materialized into the authoritative goal summaries,
        # experience specs, occurrence projection and typed route requirement.
        # Re-sending the full answer/option lineage and resolved dimension
        # descriptors only duplicates server state.  The V4 production shape
        # stayed just below the projection budget but exceeded the complete
        # Provider HTTP budget after message JSON escaping.  Compact this
        # resolved history before the generic size gate; unresolved dimensions
        # deliberately retain the existing question-authoring projection.
        checkpoint = compacted.get("clarificationCheckpoint")
        dimensions = compacted.get("clarificationDimensions")
        if cls._completed_clarification_is_fully_materialized(compacted):
            compacted_checkpoint = {
                key: checkpoint[key]
                for key in (
                    "schemaVersion",
                    "checkpointId",
                    "planningRootId",
                    "requestFingerprint",
                    "fingerprint",
                    "contractVersion",
                    "status",
                    "sourceUserTurnId",
                    "sourceAssistantTurnId",
                )
                if key in checkpoint
            }
            resolved_answers = [
                {key: item[key] for key in ("dimensionId", "semanticValue", "source") if key in item}
                for item in checkpoint.get("resolvedAnswers") or []
                if isinstance(item, dict) and item.get("dimensionId")
            ]
            if resolved_answers:
                compacted_checkpoint["resolvedAnswers"] = resolved_answers
            compacted["clarificationCheckpoint"] = compacted_checkpoint
            compacted["clarificationDimensions"] = []
            steps.append("compact_resolved_clarification_history")

        if fits():
            return compacted, steps

        checkpoint = compacted.get("clarificationCheckpoint")
        if isinstance(checkpoint, dict):
            compacted["clarificationCheckpoint"] = {
                key: checkpoint[key]
                for key in (
                    "schemaVersion",
                    "checkpointId",
                    "planningRootId",
                    "requestFingerprint",
                    "fingerprint",
                    "contractVersion",
                    "nextQuestionDimensionId",
                    "status",
                    "sourceUserTurnId",
                    "sourceAssistantTurnId",
                    "resolvedAnswers",
                )
                if key in checkpoint
            }
            steps.append("compact_clarification_checkpoint")
        if fits():
            return compacted, steps

        dimensions = compacted.get("clarificationDimensions")
        if isinstance(dimensions, list) and any(
            str(item.get("status") or "").strip().lower() == "unresolved"
            for item in dimensions
            if isinstance(item, dict)
        ):
            # A mixed contract may still need the resolved descriptors to bind
            # the remaining questions, while an unknown status must fail
            # closed.  Each dimension is already field- and length-bounded by
            # _clarification_dimensions(), so retain the complete projected
            # set and compact lower-authority diagnostics instead.
            steps.append("retain_all_clarification_dimensions")
        compacted["experienceSpecs"] = cls._minimal_experience_specs(compacted.get("experienceSpecs"))
        steps.append("compact_experience_specs")
        if fits():
            return compacted, steps

        evidence = compacted.get("untrustedSourceEvidence")
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict) and isinstance(item.get("publicText"), str):
                    item["publicText"] = cls._text(item.get("publicText"), 400)
            steps.append("compact_untrusted_source_text")
        if fits():
            return compacted, steps

        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    item.pop("publicText", None)
            steps.append("retain_source_identity_and_hints_only")
        if fits():
            return compacted, steps

        gap = compacted.get("candidateGapSummary")
        if isinstance(gap, dict) and isinstance(gap.get("poolEvidence"), list):
            gap["poolEvidence"] = gap["poolEvidence"][:4]
            steps.append("limit_candidate_gap_pools")
        if fits():
            return compacted, steps

        for key in ("completionCriteria",):
            if compacted.get(key):
                compacted.pop(key, None)
                steps.append(f"drop_{key}")
                if fits():
                    return compacted, steps

        provisional = compacted.get("provisionalGoalOccurrenceProjection")
        if isinstance(provisional, dict):
            compacted["provisionalGoalOccurrenceProjection"] = {
                key: provisional[key]
                for key in (
                    "schemaVersion",
                    "authority",
                    "status",
                    "sourceContractFingerprint",
                    "unresolvedDimensionIds",
                    "fingerprint",
                )
                if key in provisional
            }
            steps.append("compact_provisional_occurrences")
        if fits():
            return compacted, steps

        if isinstance(gap, dict):
            gap.pop("poolEvidence", None)
            gap["goals"] = list(gap.get("goals") or [])[:4]
            steps.append("summarize_candidate_gap")
        compacted["goalRequirements"] = list(compacted.get("goalRequirements") or [])[:8]
        compacted["pendingSlots"] = list(compacted.get("pendingSlots") or [])[:8]
        compacted["experienceSpecs"] = list(compacted.get("experienceSpecs") or [])[:4]
        compacted["latestUserMessage"] = cls._text(compacted.get("latestUserMessage"), 600)
        steps.append("limit_authoritative_collections")
        if fits():
            return compacted, steps

        # Final safe projection: retain only the information required to choose
        # ask_user versus draft_itinerary.  Full state stays available in-process
        # through ControllerProviderContext and in the persisted audit trace.
        keep = {
            "schemaVersion",
            "latestUserMessage",
            # These original clauses are the pre-freeze coverage obligation,
            # not disposable evidence. Over-budget clauses must fail closed.
            "requestActivityClauses",
            "selectedCity",
            "itineraryLifecycle",
            "goalRequirements",
            "pendingSlots",
            "allowedActions",
            "fingerprints",
            "experienceSpecs",
            "candidateGapSummary",
            "clarificationDimensions",
            "routePlanningPolicyRequirement",
            "decisionConstraints",
            "decisionContractRef",
        }
        compacted = {key: value for key, value in compacted.items() if key in keep}
        steps.append("minimal_executable_projection")
        if fits():
            return compacted, steps

        compacted["goalRequirements"] = [
            {
                key: item[key]
                for key in (
                    "goalId",
                    "intentType",
                    "requirementLevel",
                    "requiredMin",
                    "preferredCount",
                    "maxCount",
                    "distributionPolicy",
                    "allowedDayNumbers",
                )
                if key in item
            }
            for item in list(compacted.get("goalRequirements") or [])[:16]
            if isinstance(item, dict)
        ]
        compacted["pendingSlots"] = [
            {
                key: item[key]
                for key in (
                    "goalId",
                    "briefId",
                    "poolId",
                    "planningSlotId",
                    "dayNumber",
                    "timeWindow",
                )
                if key in item
            }
            for item in list(compacted.get("pendingSlots") or [])[:16]
            if isinstance(item, dict)
        ]
        compacted["experienceSpecs"] = cls._minimal_experience_specs(compacted.get("experienceSpecs"))
        gap = compacted.get("candidateGapSummary")
        if isinstance(gap, dict):
            compacted["candidateGapSummary"] = {
                key: gap[key]
                for key in (
                    "schemaVersion",
                    "status",
                    "missingOccurrenceCount",
                    "source",
                    "fingerprint",
                    "rejectedReasonCounts",
                    "goals",
                )
                if key in gap
            }
            compacted["candidateGapSummary"]["goals"] = list(compacted["candidateGapSummary"].get("goals") or [])[:8]
        constraints = compacted.get("decisionConstraints")
        if isinstance(constraints, dict):
            required_counts = constraints.get("requiredGoalCounts")
            compacted["decisionConstraints"] = {
                "requiredGoalCounts": {
                    cls._text(key, 160): cls._integer(value)
                    for key, value in list(required_counts.items() if isinstance(required_counts, dict) else [])[:16]
                    if cls._text(key, 160)
                },
                "optionalGoalIds": [
                    cls._text(item, 160)
                    for item in list(constraints.get("optionalGoalIds") or [])[:16]
                    if cls._text(item, 160)
                ],
                "availableDayNumbers": [
                    cls._integer(item) for item in list(constraints.get("availableDayNumbers") or [])[:14]
                ],
            }
        compacted["latestUserMessage"] = cls._text(
            compacted.get("latestUserMessage"),
            400,
        )
        steps.append("compact_minimal_authority_fields")
        return compacted, steps

    @staticmethod
    def _completed_clarification_is_fully_materialized(payload: dict[str, Any]) -> bool:
        checkpoint = payload.get("clarificationCheckpoint")
        dimensions = payload.get("clarificationDimensions")
        if not isinstance(checkpoint, dict):
            return False
        if str(checkpoint.get("status") or "").strip().lower() not in {
            "answered",
            "completed",
            "resolved",
        }:
            return False
        if not isinstance(dimensions, list) or not dimensions:
            return False
        resolved_statuses = {"answered", "completed", "resolved"}
        if not all(
            isinstance(item, dict) and str(item.get("status") or "").strip().lower() in resolved_statuses
            for item in dimensions
        ):
            return False
        dimension_ids = {str(item.get("dimensionId") or "").strip() for item in dimensions if isinstance(item, dict)}
        materialized_dimensions = {
            "night_view.cardinality",
            "night_view.experience_mode",
            "route_decision.mobility_profile",
            "route_decision.detour_tolerance",
        }
        if not dimension_ids or not dimension_ids.issubset(materialized_dimensions):
            return False
        answer_ids = {
            str(item.get("dimensionId") or "").strip()
            for item in checkpoint.get("resolvedAnswers") or []
            if isinstance(item, dict)
        }
        if answer_ids and not answer_ids.issubset(dimension_ids):
            return False

        goals = [item for item in payload.get("goalRequirements") or [] if isinstance(item, dict)]
        specs = [item for item in payload.get("experienceSpecs") or [] if isinstance(item, dict)]
        if "night_view.cardinality" in dimension_ids and not (
            any(item.get("intentType") == "night_view" for item in goals)
            and isinstance(payload.get("provisionalGoalOccurrenceProjection"), dict)
        ):
            return False
        if "night_view.experience_mode" in dimension_ids and not any(
            item.get("intentType") == "night_view" for item in specs
        ):
            return False
        if any(item.startswith("route_decision.") for item in dimension_ids) and not isinstance(
            payload.get("routePlanningPolicyRequirement"), dict
        ):
            return False
        return True

    @classmethod
    def _provisional_goal_occurrence_projection(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: cls._bounded_json(value.get(key), depth=0)
            for key in (
                "schemaVersion",
                "authority",
                "status",
                "checkpointId",
                "planningRootId",
                "contractVersion",
                "source",
                "sourceContractFingerprint",
                "goalConstraints",
                "projectedPlacements",
                "unresolvedDimensionIds",
                "fingerprint",
            )
            if value.get(key) is not None
        }

    @classmethod
    def _experience_specs(cls, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []
        return [cls._bounded_json(item, depth=0) for item in value[:8] if isinstance(item, dict)]

    @classmethod
    def _minimal_experience_specs(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value[:8]:
            if not isinstance(item, dict):
                continue
            projected = {
                key: cls._bounded_json(item.get(key), depth=0)
                for key in (
                    "intentType",
                    "experienceFamilies",
                    "frequency",
                    "occurrencePolicy",
                    "accessPolicy",
                    "allowedDayNumbers",
                    "timeWindow",
                    "distinctIdentityPolicy",
                    "evidenceFreshness",
                    "specFingerprint",
                )
                if item.get(key) is not None
            }
            if projected:
                result.append(projected)
        return result

    @classmethod
    def _bounded_json(cls, value: Any, *, depth: int) -> Any:
        if isinstance(value, str):
            return value[:240] if depth >= 4 else value[:400]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 4:
            return cls._text(value, 240)
        if isinstance(value, dict):
            return {
                cls._text(key, 80): cls._bounded_json(item, depth=depth + 1)
                for key, item in list(value.items())[:16]
                if cls._text(key, 80)
            }
        if isinstance(value, list):
            return [cls._bounded_json(item, depth=depth + 1) for item in value[:16]]
        return cls._text(value, 240)

    def _goal_summaries(self, context: dict[str, Any], normalization: dict[str, Any]) -> list[GoalSummary]:
        raw_goals = list(normalization.get("goalRequirements") or [])
        if not raw_goals:
            contract = (
                context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
            )
            raw_goals = list(contract.get("goals") or contract.get("goalRequirements") or [])
        result: list[GoalSummary] = []
        for item in raw_goals[:16]:
            if not isinstance(item, dict) or not item.get("goalId"):
                continue
            result.append(
                GoalSummary(
                    goalId=self._text(item.get("goalId"), 160),
                    intentType=self._text(item.get("intentType"), 120) or None,
                    requirementLevel=self._text(item.get("requirementLevel"), 80) or None,
                    requiredMin=self._integer(item.get("requiredMin") or item.get("minCount")),
                    preferredCount=self._optional_integer(item.get("preferredCount")),
                    maxCount=self._optional_integer(item.get("maxCount")),
                    distributionPolicy=self._text(item.get("distributionPolicy"), 120) or None,
                    allowedDayNumbers=[self._integer(day) for day in list(item.get("allowedDayNumbers") or [])[:14]],
                )
            )
        return result

    def _pending_summaries(self, context: dict[str, Any], normalization: dict[str, Any]) -> list[PendingSlotSummary]:
        raw_pending = list(normalization.get("unresolvedSlots") or [])
        if not raw_pending:
            observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
            raw_pending = list(observation.get("unresolvedSlots") or [])
        if not raw_pending:
            continuation = (
                context.get("continuationContext") if isinstance(context.get("continuationContext"), dict) else {}
            )
            raw_pending = list(continuation.get("portfolioPendingSlots") or continuation.get("pendingSlots") or [])
        result: list[PendingSlotSummary] = []
        for item in raw_pending[:16]:
            if not isinstance(item, dict):
                continue
            time_window = item.get("timeWindow") if isinstance(item.get("timeWindow"), dict) else None
            result.append(
                PendingSlotSummary(
                    goalId=self._text(item.get("goalId") or item.get("sourceGoalId"), 160) or None,
                    briefId=self._text(item.get("briefId"), 160) or None,
                    poolId=self._text(item.get("poolId"), 160) or None,
                    planningSlotId=self._text(item.get("planningSlotId"), 160) or None,
                    dayNumber=self._optional_integer(item.get("dayNumber")),
                    timeWindow=self._bounded_time_window(time_window),
                )
            )
        return result

    @staticmethod
    def _bounded_time_window(value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not value:
            return None
        return {
            key: value.get(key)
            for key in ("start", "end", "startTime", "endTime", "durationMinutes")
            if value.get(key) is not None
        } or None

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _optional_integer(cls, value: Any) -> Optional[int]:
        return cls._integer(value) if value is not None else None

    @staticmethod
    def _encoded_size(value: dict[str, Any]) -> int:
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        )

    @staticmethod
    def _section_chars(value: dict[str, Any]) -> dict[str, int]:
        return {
            str(key): len(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
            for key, item in sorted(value.items())
        }
