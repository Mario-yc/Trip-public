"""Small executor-neutral observe → decide → act → observe loop primitive."""

from __future__ import annotations

from typing import Callable, Generic, Optional, TypeVar

from src.services.agent_executor_registry import AgentActionOutcome as AgentActionOutcome
from src.services.agent_observation_service import AgentObservation
from src.services.agent_stop_policy import AgentStopDecision, AgentStopPolicy


ActionT = TypeVar("ActionT")
OutcomeT = TypeVar("OutcomeT")


class AgentAutonomyLoop(Generic[ActionT, OutcomeT]):
    """Keeps execution bounded; actual business work remains in existing services."""

    def __init__(self, *, max_cycles: int, stop_policy: Optional[AgentStopPolicy] = None):
        self.max_cycles = max(1, max_cycles)
        self.stop_policy = stop_policy or AgentStopPolicy()

    def run(
        self,
        *,
        observe: Callable[[int, Optional[OutcomeT]], AgentObservation],
        decide: Callable[[AgentObservation], ActionT],
        act: Callable[[ActionT, AgentObservation], OutcomeT],
        outcome_to_stop: Callable[[OutcomeT], tuple[Optional[bool], bool]],
    ) -> tuple[list[AgentObservation], list[ActionT], list[OutcomeT], AgentStopDecision]:
        observations: list[AgentObservation] = []
        decisions: list[ActionT] = []
        outcomes: list[OutcomeT] = []
        prior_fingerprint: Optional[str] = None
        for cycle_index in range(self.max_cycles):
            observation = observe(cycle_index, outcomes[-1] if outcomes else None)
            observations.append(observation)
            verifier_passed, rollback = outcome_to_stop(outcomes[-1]) if outcomes else (None, False)
            stop = self.stop_policy.evaluate(
                observation,
                cycle_index=cycle_index,
                max_cycles=self.max_cycles,
                previous_fingerprint=prior_fingerprint,
                verifier_passed=False if rollback else verifier_passed,
            )
            if stop.should_stop:
                return observations, decisions, outcomes, stop
            decision = decide(observation)
            decisions.append(decision)
            outcome = act(decision, observation)
            outcomes.append(outcome)
            prior_fingerprint = observation.state_fingerprint
        observation = observe(self.max_cycles, outcomes[-1] if outcomes else None)
        observations.append(observation)
        return observations, decisions, outcomes, self.stop_policy.evaluate(
            observation,
            cycle_index=self.max_cycles,
            max_cycles=self.max_cycles,
            previous_fingerprint=prior_fingerprint,
        )
