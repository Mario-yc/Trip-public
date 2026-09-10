from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.services.agent_autonomy_service import AgentDecisionResult
from src.services.agent_executor_registry import (
    AgentActionExecutionRequest,
    AgentActionExecutorRegistry,
    AgentActionOutcome,
)
from src.services.agent_observation_service import AgentObservation


@dataclass(frozen=True)
class CoordinatedTurnAction:
    primary_action: str
    execution_route: str
    accepted: bool
    decision_id: str | None


@dataclass(frozen=True)
class AgentControlCycle:
    cycle_index: int
    observation: dict[str, Any]
    decision: dict[str, Any]
    outcome: dict[str, Any] | None = None
    observed_at: str = ""
    decision_started_at: str = ""
    decided_at: str = ""
    policy_gate_at: str = ""
    action_started_at: str = ""
    outcome_at: str = ""


@dataclass(frozen=True)
class CoordinatedTurnResult:
    cycles: list[AgentControlCycle] = field(default_factory=list)
    outcomes: list[AgentActionOutcome] = field(default_factory=list)
    terminal_decision: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    final_context: dict[str, Any] = field(default_factory=dict)


class AgentTurnCoordinator:
    """The sole natural-language observe -> decide -> gate -> act root loop."""

    def __init__(self, executor_registry: AgentActionExecutorRegistry | None = None) -> None:
        self.executor_registry = executor_registry or AgentActionExecutorRegistry()

    def coordinate(self, request_context: dict[str, Any]) -> CoordinatedTurnAction:
        state = request_context.get("agentDecisionState")
        if not isinstance(state, dict):
            return CoordinatedTurnAction("finish", "no_safe_action", False, None)
        accepted = state.get("accepted") is True
        action = str(state.get("primaryAction") or "finish")
        server_profile = request_context.get("serverExecutionProfile")
        if server_profile in {"strict_portfolio", "simple_open_v1"}:
            state["serverExecutionProfile"] = server_profile
        route = self.executor_registry.route(state)
        proposed = str(state.get("proposedExecutionRoute") or "")
        state["actualExecutionRoute"] = route
        state["executionOverride"] = bool(proposed and proposed != route)
        if state["executionOverride"]:
            state["executionOverrideReason"] = "executor_registry_safety_route"
        return CoordinatedTurnAction(
            primary_action=action,
            execution_route=route,
            accepted=accepted,
            decision_id=str(state.get("decisionId") or "") or None,
        )

    def run(
        self,
        request_context: dict[str, Any],
        *,
        observe: Callable[[dict[str, Any], int, AgentActionOutcome | None], AgentObservation],
        decide: Callable[[dict[str, Any], AgentObservation, int], AgentDecisionResult],
        reload_context: Callable[[dict[str, Any], AgentActionOutcome, int], dict[str, Any]],
        event_sink: Callable[[Any], None] | None = None,
        max_cycles: int = 3,
        max_writes: int = 2,
    ) -> CoordinatedTurnResult:
        """Run bounded model control; every non-terminal action is followed by persisted reload."""

        context = request_context
        cycles: list[AgentControlCycle] = []
        outcomes: list[AgentActionOutcome] = []
        previous_fingerprint: str | None = None
        previous_action_signature: str | None = None
        last_outcome: AgentActionOutcome | None = None
        terminal_decision: dict[str, Any] = {}
        stop_reason = "max_cycles"
        writes_used = 0

        for cycle_index in range(max(1, max_cycles)):
            observation = observe(context, cycle_index, last_outcome)
            observed_at = datetime.now(timezone.utc).isoformat()
            runtime_limits = context.get("runtimeLimits") if isinstance(context.get("runtimeLimits"), dict) else {}
            cancelled = bool(context.get("cancelRequested") or context.get("runCancelled"))
            deadline_exceeded = isinstance(runtime_limits.get("remainingRunMs"), (int, float)) and float(
                runtime_limits["remainingRunMs"]
            ) <= 0
            if cancelled or deadline_exceeded:
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index,
                        observation=observation.model_dump(by_alias=True),
                        decision={},
                        observed_at=observed_at,
                    )
                )
                stop_reason = "cancelled" if cancelled else "run_deadline_exceeded"
                break
            decision_started_at = datetime.now(timezone.utc).isoformat()
            decision_result = decide(context, observation, cycle_index)
            decided_at = datetime.now(timezone.utc).isoformat()
            decision_state = decision_result.to_event_metadata()
            decision_state["schemaVersion"] = decision_result.decision.schema_version
            decision_state["observationFingerprint"] = observation.state_fingerprint
            decision_state["cycleIndex"] = cycle_index
            server_profile = context.get("serverExecutionProfile")
            if server_profile in {"strict_portfolio", "simple_open_v1"}:
                decision_state["serverExecutionProfile"] = server_profile
            decision_payload = decision_result.decision.model_dump(by_alias=True, exclude_none=True)
            actual_route = self.executor_registry.route(decision_state)
            proposed_route = str(decision_state.get("proposedExecutionRoute") or "")
            decision_state["actualExecutionRoute"] = actual_route
            decision_state["executionOverride"] = bool(proposed_route and proposed_route != actual_route)
            if decision_state["executionOverride"]:
                decision_state["executionOverrideReason"] = "executor_registry_safety_route"
            context["agentObservation"] = observation.model_dump(by_alias=True)
            context["agentDecisionState"] = decision_state
            context["agentDecisionShadow"] = decision_state
            context["agentDecision"] = decision_payload
            terminal_decision = decision_state
            policy_gate_at = datetime.now(timezone.utc).isoformat()

            action = str(decision_state.get("primaryAction") or "finish")
            signature = f"{action}:{decision_payload.get('actionDirective') or decision_state.get('targetScope') or {}}"
            if previous_fingerprint == observation.state_fingerprint and previous_action_signature == signature:
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index,
                        observation=observation.model_dump(by_alias=True),
                        decision=decision_state,
                        observed_at=observed_at,
                        decision_started_at=decision_started_at,
                        decided_at=decided_at,
                        policy_gate_at=policy_gate_at,
                    )
                )
                stop_reason = "no_progress_repeated_state_action"
                break

            if decision_state.get("accepted") is not True:
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index,
                        observation=observation.model_dump(by_alias=True),
                        decision=decision_state,
                        observed_at=observed_at,
                        decision_started_at=decision_started_at,
                        decided_at=decided_at,
                        policy_gate_at=policy_gate_at,
                    )
                )
                stop_reason = "policy_gate_rejected"
                break

            if (
                action == "draft_itinerary"
                and last_outcome is not None
                and last_outcome.goal_delta.get("planningAttemptPersisted") is True
            ):
                # A persisted planning attempt is a checkpoint, not permission
                # to replay the same generic draft action. The next model turn
                # may ask the user or choose a scoped recovery action, but a
                # second draft would only nest the checkpoint and repeat the
                # same provider/cache state. Explicit refresh/expand/selection
                # continuations enter through their own structured executors.
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index,
                        observation=observation.model_dump(by_alias=True),
                        decision=decision_state,
                        observed_at=observed_at,
                        decision_started_at=decision_started_at,
                        decided_at=decided_at,
                        policy_gate_at=policy_gate_at,
                    )
                )
                stop_reason = "needs_confirmation"
                break

            write_action = action in {"draft_itinerary", "patch_itinerary", "optimize_route"}
            if write_action and writes_used >= max(1, max_writes):
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index,
                        observation=observation.model_dump(by_alias=True),
                        decision=decision_state,
                        observed_at=observed_at,
                        decision_started_at=decision_started_at,
                        decided_at=decided_at,
                        policy_gate_at=policy_gate_at,
                    )
                )
                stop_reason = "max_writes_reached"
                break

            action_started_at = datetime.now(timezone.utc).isoformat()
            outcome = self.executor_registry.execute(
                AgentActionExecutionRequest(
                    decision_state=decision_state,
                    decision=decision_payload,
                    request_context=context,
                    observation=observation,
                    cycle_index=cycle_index,
                    event_sink=event_sink,
                )
            )
            outcome_at = datetime.now(timezone.utc).isoformat()
            outcomes.append(outcome)
            cycles.append(
                AgentControlCycle(
                    cycle_index=cycle_index,
                    observation=observation.model_dump(by_alias=True),
                    decision=decision_state,
                    outcome=outcome.to_context(),
                    observed_at=observed_at,
                    decision_started_at=decision_started_at,
                    decided_at=decided_at,
                    policy_gate_at=policy_gate_at,
                    action_started_at=action_started_at,
                    outcome_at=outcome_at,
                )
            )
            if write_action and (outcome.result_version_id or outcome.patch_ids):
                writes_used += 1
            if self._is_verified_partial_terminal(action, outcome):
                context = reload_context(context, outcome, cycle_index)
                stop_reason = "partial_timeline_needs_confirmation"
                break
            if self._is_simple_open_terminal(action, outcome):
                context = reload_context(context, outcome, cycle_index)
                stop_reason = "simple_open_terminal"
                break
            if self._is_verified_timeline_mutation_terminal(action, outcome):
                context = reload_context(context, outcome, cycle_index)
                stop_reason = "verified_timeline_mutation"
                break
            if action == "draft_itinerary" and outcome.goal_delta.get("planningAttemptPersisted") is True:
                # A durable planning checkpoint is the truthful terminal result
                # of this turn.  Calling decide() again before stopping used to
                # invoke the Controller once more, even though the next action
                # must come from a later persisted choice.
                context = reload_context(context, outcome, cycle_index)
                stop_reason = "needs_confirmation"
                break
            if action in {"ask_user", "finish"}:
                if outcome.status in {"failed", "rolled_back"} or outcome.verifier.get("passed") is False:
                    stop_reason = outcome.status
                else:
                    stop_reason = "needs_confirmation" if action == "ask_user" else "model_finish"
                break
            previous_fingerprint = observation.state_fingerprint
            previous_action_signature = signature
            last_outcome = outcome
            context = reload_context(context, outcome, cycle_index)
            if outcome.status in {"failed", "rolled_back"} or not outcome.safe_to_continue:
                post_observation = observe(context, cycle_index + 1, outcome)
                cycles.append(
                    AgentControlCycle(
                        cycle_index=cycle_index + 1,
                        observation=post_observation.model_dump(by_alias=True),
                        decision={},
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                stop_reason = outcome.status
                break
        return CoordinatedTurnResult(
            cycles=cycles,
            outcomes=outcomes,
            terminal_decision=terminal_decision,
            stop_reason=stop_reason,
            final_context=context,
        )

    @staticmethod
    def _is_verified_partial_terminal(action: str, outcome: AgentActionOutcome) -> bool:
        return bool(
            action == "draft_itinerary"
            and outcome.status == "partial"
            and outcome.result_version_id
            and outcome.observed_active_version_id == outcome.result_version_id
            and outcome.patch_ids
            and outcome.candidate_summary.get("terminalStatus") == "needs_confirmation"
            and outcome.verifier.get("passed") is True
            and outcome.verifier.get("pendingSlotTruthValid") is True
            and outcome.control_loop_disposition == "terminal_needs_confirmation"
            and not outcome.rollback_performed
        )

    @staticmethod
    def _is_simple_open_terminal(action: str, outcome: AgentActionOutcome) -> bool:
        return bool(
            action == "draft_itinerary"
            and outcome.execution_route == "simple_open_initial_pipeline"
            and outcome.status in {"success", "partial"}
            and outcome.result_version_id
            and outcome.observed_active_version_id == outcome.result_version_id
            and outcome.verifier.get("passed") is True
            and outcome.candidate_summary.get("terminalStatus") == "simple_open_terminal"
            and outcome.control_loop_disposition == "terminal_success"
            and not outcome.rollback_performed
        )

    @staticmethod
    def _is_verified_timeline_mutation_terminal(action: str, outcome: AgentActionOutcome) -> bool:
        """Stop after the canonical local writer has verified and activated its patch."""

        return bool(
            action == "patch_itinerary"
            and outcome.execution_route == "timeline_mutation_executor"
            and outcome.status == "success"
            and outcome.result_version_id
            and outcome.observed_active_version_id == outcome.result_version_id
            and outcome.patch_ids
            and outcome.verifier.get("passed") is True
            and outcome.control_loop_disposition == "terminal_success"
            and not outcome.rollback_performed
        )
