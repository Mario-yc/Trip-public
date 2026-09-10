from src.services.agent_autonomy_loop import AgentAutonomyLoop
from src.services.agent_observation_service import AgentObservationBuilder
from src.services.agent_stop_policy import AgentStopPolicy


def _observation(*, unresolved: bool = False, marker: str = ""):
    segment = {
        "id": "seg_1",
        "kind": "meal" if unresolved else "visit",
        "poi": {"name": "午餐" if unresolved else "景点", "groundingStatus": "waiting_for_poi_grounding" if unresolved else ""},
        "notes": "pendingMeal=true" if unresolved else "",
    }
    return AgentObservationBuilder().build(
        {"latestUserMessage": marker, "activeVersionId": "ver_1", "currentItinerarySnapshot": {"id": "plan_1", "versionId": "ver_1", "days": [{"segments": [segment]}]}}
    )


def test_stop_policy_continues_for_actionable_unresolved_slot():
    stop = AgentStopPolicy().evaluate(_observation(unresolved=True), cycle_index=1, max_cycles=3, verifier_passed=True)

    assert stop.should_stop is False
    assert stop.reason is None


def test_stop_policy_stops_repeated_fingerprint_and_deadline():
    observation = _observation()

    repeated = AgentStopPolicy().evaluate(
        observation,
        cycle_index=1,
        max_cycles=3,
        previous_fingerprint=observation.state_fingerprint,
    )
    deadline = AgentStopPolicy().evaluate(observation, cycle_index=1, max_cycles=3, deadline_exceeded=True)

    assert repeated.reason == "repeated_state_fingerprint"
    assert deadline.reason == "run_deadline_exceeded"


def test_bounded_loop_executes_one_action_per_cycle_then_stops():
    observations, decisions, outcomes, stop = AgentAutonomyLoop(max_cycles=1).run(
        observe=lambda cycle, _outcome: _observation(marker=str(cycle)),
        decide=lambda _observation: "draft_itinerary",
        act=lambda decision, _observation: {"action": decision},
        outcome_to_stop=lambda _outcome: (True, False),
    )

    assert len(decisions) == 1
    assert len(outcomes) == 1
    assert len(observations) >= 1
    assert stop.reason == "max_cycles_reached"


def test_bounded_loop_can_continue_when_no_stop_condition_is_met():
    observations, decisions, outcomes, stop = AgentAutonomyLoop(max_cycles=2).run(
        observe=lambda cycle, _outcome: _observation(marker=str(cycle)),
        decide=lambda observation: "resolve_poi" if observation.unresolved_slots else "draft_itinerary",
        act=lambda decision, _observation: {"action": decision},
        outcome_to_stop=lambda _outcome: (None, False),
    )

    assert decisions == ["draft_itinerary", "draft_itinerary"]
    assert len(outcomes) == 2
    assert stop.reason == "max_cycles_reached"
