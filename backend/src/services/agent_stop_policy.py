"""Fail-closed stop decisions for the bounded autonomy loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.services.agent_observation_service import AgentObservation


@dataclass(frozen=True)
class AgentStopDecision:
    should_stop: bool
    reason: Optional[str] = None


class AgentStopPolicy:
    def evaluate(
        self,
        observation: AgentObservation,
        *,
        cycle_index: int,
        max_cycles: int,
        previous_fingerprint: Optional[str] = None,
        action_signature: Optional[str] = None,
        previous_action_signature: Optional[str] = None,
        no_progress_cycles: int = 0,
        max_no_progress_cycles: int = 1,
        cancelled: bool = False,
        deadline_exceeded: bool = False,
        verifier_passed: Optional[bool] = None,
    ) -> AgentStopDecision:
        if cancelled:
            return AgentStopDecision(True, "cancelled")
        if deadline_exceeded:
            return AgentStopDecision(True, "run_deadline_exceeded")
        if observation.invariant_errors:
            return AgentStopDecision(True, "observation_invalid")
        if verifier_passed is False:
            return AgentStopDecision(True, "verifier_failed_and_rolled_back")
        if previous_fingerprint and previous_fingerprint == observation.state_fingerprint:
            return AgentStopDecision(True, "repeated_state_fingerprint")
        if action_signature and previous_action_signature and action_signature == previous_action_signature:
            return AgentStopDecision(True, "repeated_action_signature")
        if no_progress_cycles >= max(1, int(max_no_progress_cycles)):
            return AgentStopDecision(True, "no_progress")
        if observation.unresolved_slots:
            material_choice = any(slot.candidate_count > 1 or slot.next_action == "ask_user" for slot in observation.unresolved_slots)
            if material_choice:
                return AgentStopDecision(True, "needs_confirmation")
            exhausted = any(
                slot.reason in {"external_budget_exhausted", "provider_unavailable", "provider_rate_limited", "unsafe_to_assume"}
                for slot in observation.unresolved_slots
            )
            if exhausted:
                return AgentStopDecision(True, "unresolved_not_actionable")
            # A zero-candidate slot with an explicit resolve/retry action is
            # actionable; the bounded loop may spend one safe cycle on it.
        if cycle_index >= max_cycles:
            return AgentStopDecision(True, "max_cycles_reached")
        if verifier_passed and observation.route_state.complete_door_to_door_status == "ready":
            return AgentStopDecision(True, "verified_goal_satisfied")
        return AgentStopDecision(False, None)
