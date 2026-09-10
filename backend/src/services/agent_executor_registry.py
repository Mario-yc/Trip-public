from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from src.services.agent_observation_service import AgentObservation


ActionStatus = Literal["success", "needs_confirmation", "partial", "failed", "no_change"]
ControlLoopDisposition = Literal[
    "continue",
    "terminal_success",
    "terminal_needs_confirmation",
    "terminal_failure",
]


@dataclass(frozen=True)
class AgentActionOutcome:
    action: str
    execution_route: str
    status: ActionStatus
    base_version_id: str | None = None
    result_version_id: str | None = None
    observed_active_version_id: str | None = None
    patch_ids: list[str] = field(default_factory=list)
    changed_segment_ids: list[str] = field(default_factory=list)
    external_call_counts: dict[str, int] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    execution_events: list[dict[str, Any]] = field(default_factory=list)
    rollback_performed: bool = False
    goal_delta: dict[str, Any] = field(default_factory=dict)
    recommended_next_actions: list[str] = field(default_factory=list)
    control_loop_disposition: ControlLoopDisposition = "continue"
    safe_for_future_user_continuation: bool = False
    safe_to_continue: bool = False
    route_status: str | None = None
    terminal_payload: Any = field(default=None, repr=False, compare=False)

    def to_context(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "executionRoute": self.execution_route,
            "status": self.status,
            "baseVersionId": self.base_version_id,
            "resultVersionId": self.result_version_id,
            "observedActiveVersionId": self.observed_active_version_id,
            "patchIds": list(self.patch_ids),
            "changedSegmentIds": list(self.changed_segment_ids),
            "externalCallCounts": dict(self.external_call_counts),
            "candidateSummary": dict(self.candidate_summary),
            "verifier": dict(self.verifier),
            "executionEvents": [dict(item) for item in self.execution_events],
            "rollbackPerformed": self.rollback_performed,
            "goalDelta": dict(self.goal_delta),
            "recommendedNextActions": list(self.recommended_next_actions),
            "controlLoopDisposition": self.control_loop_disposition,
            "safeForFutureUserContinuation": self.safe_for_future_user_continuation,
            "safeToContinue": self.safe_to_continue,
            "routeStatus": self.route_status,
        }


@dataclass(frozen=True)
class AgentActionExecutionRequest:
    """Everything an executor may consume after the policy gate accepted a decision."""

    decision_state: dict[str, Any]
    decision: dict[str, Any]
    request_context: dict[str, Any]
    observation: AgentObservation
    cycle_index: int
    event_sink: Callable[[Any], None] | None = None


class AgentActionExecutor(Protocol):
    action: str
    execution_route: str

    def execute(self, request: AgentActionExecutionRequest) -> AgentActionOutcome: ...


class BoundActionExecutor:
    """A concrete, action-owned adapter around an existing safe service kernel."""

    action = ""
    execution_route = "no_safe_action"

    def __init__(self, handler: Callable[[AgentActionExecutionRequest], AgentActionOutcome]) -> None:
        self._handler = handler

    def execute(self, request: AgentActionExecutionRequest) -> AgentActionOutcome:
        outcome = self._handler(request)
        if outcome.action != self.action:
            raise ValueError(f"executor_outcome_action_mismatch:{self.action}:{outcome.action}")
        return outcome


class AskUserExecutor(BoundActionExecutor):
    action = "ask_user"
    execution_route = "clarification"


class DraftItineraryExecutor(BoundActionExecutor):
    action = "draft_itinerary"
    execution_route = "staged_initial_pipeline"


class SimpleOpenDraftItineraryExecutor(BoundActionExecutor):
    """The initial-itinerary executor selected exclusively by server profile."""

    action = "draft_itinerary"
    execution_route = "simple_open_initial_pipeline"


class ReadItineraryExecutor(BoundActionExecutor):
    action = "read_itinerary"
    execution_route = "read_only"


class ResolvePoiExecutor(BoundActionExecutor):
    action = "resolve_poi"
    execution_route = "poi_grounding"


class OptimizeRouteExecutor(BoundActionExecutor):
    action = "optimize_route"
    execution_route = "route_optimization"


class TimelineMutationExecutor(BoundActionExecutor):
    action = "patch_itinerary"
    execution_route = "timeline_mutation_executor"


class ComplexPatchExecutor(BoundActionExecutor):
    action = "patch_itinerary"
    execution_route = "complex_patch_executor"


class VerifyExternalFactsExecutor(BoundActionExecutor):
    action = "verify_external_facts"
    execution_route = "derived_facts_only"


class FinishExecutor(BoundActionExecutor):
    action = "finish"
    execution_route = "terminal_response"


class AgentActionExecutorRegistry:
    """Action-to-executor contract. It executes accepted decisions and never infers intent."""

    ROUTES: dict[str, str] = {
        "ask_user": "clarification",
        "draft_itinerary": "staged_initial_pipeline",
        "read_itinerary": "read_only",
        "resolve_poi": "poi_grounding",
        "optimize_route": "route_optimization",
        "patch_itinerary": "timeline_mutation_executor",
        "verify_external_facts": "derived_facts_only",
        "finish": "terminal_response",
    }

    PATCH_ROUTES = {"timeline_mutation_executor", "complex_patch_executor"}

    def __init__(self, executors: list[AgentActionExecutor] | None = None) -> None:
        self._executors: dict[str, AgentActionExecutor] = {}
        for executor in executors or []:
            self.register(executor)

    def register(self, executor: AgentActionExecutor) -> None:
        action = str(executor.action or "")
        if action not in self.ROUTES:
            raise ValueError(f"unsupported_agent_action_executor:{action}")
        allowed_routes = (
            self.PATCH_ROUTES
            if action == "patch_itinerary"
            else {"staged_initial_pipeline", "simple_open_initial_pipeline"}
            if action == "draft_itinerary"
            else {self.ROUTES[action]}
        )
        if executor.execution_route not in allowed_routes:
            raise ValueError(f"agent_executor_route_mismatch:{action}:{executor.execution_route}")
        self._executors[executor.execution_route] = executor

    def route(self, decision_state: dict[str, Any]) -> str:
        if decision_state.get("accepted") is not True:
            return "no_safe_action"
        action = str(decision_state.get("primaryAction") or "")
        if action not in self.ROUTES:
            return "no_safe_action"
        if action == "draft_itinerary":
            # ``serverExecutionProfile`` is attached by the coordinator from
            # server settings.  Do not inspect a model-proposed route here.
            return (
                "simple_open_initial_pipeline"
                if decision_state.get("serverExecutionProfile") == "simple_open_v1"
                else "staged_initial_pipeline"
            )
        if action == "patch_itinerary":
            directive = decision_state.get("actionDirective")
            mutation_intent = directive.get("mutationIntent") if isinstance(directive, dict) else None
            operation_intent = str(directive.get("operationIntent") or "") if isinstance(directive, dict) else ""
            if operation_intent == "complex_patch":
                return "complex_patch_executor"
            return "timeline_mutation_executor" if isinstance(mutation_intent, dict) else self.ROUTES[action]
        return self.ROUTES[action]

    def execute(self, request: AgentActionExecutionRequest) -> AgentActionOutcome:
        if request.decision_state.get("accepted") is not True:
            return AgentActionOutcome(
                action=str(request.decision_state.get("primaryAction") or "finish"),
                execution_route="no_safe_action",
                status="failed",
                candidate_summary={"failureReason": "policy_gate_rejected"},
                safe_to_continue=False,
            )
        action = str(request.decision_state.get("primaryAction") or "")
        route = self.route(request.decision_state)
        executor = self._executors.get(route)
        if executor is None:
            return AgentActionOutcome(
                action=action or "finish",
                execution_route="no_safe_action",
                status="failed",
                candidate_summary={"failureReason": "executor_not_registered"},
                safe_to_continue=False,
            )
        return executor.execute(request)
