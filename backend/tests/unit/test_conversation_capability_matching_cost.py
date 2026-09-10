"""Count-only support for filtering before authoritative capability checks."""

from collections import Counter

from src.services.conversation_intent_router import ConversationCapabilityResolver


def carrier():
    return {
        "turnId": "assistant-current",
        "response": {
            "choiceOptions": [
                {
                    "id": "more",
                    "action": "continue_plan_expansion",
                    "kind": "simple_direction_more_plans",
                    "planningSelectionRootTurnId": "root",
                    "rootPortfolioId": "portfolio",
                    "requestContractFingerprint": "contract",
                },
                {"id": "adopt", "action": "select_plan_proposal"},
                {
                    "id": "slot",
                    "action": "continue_pending_slot",
                    "briefId": "brief",
                    "poolId": "pool",
                    "planningSlotId": "slot",
                    "dayNumber": 1,
                },
            ]
        },
    }


def candidates(resolver, intent, latest):
    return resolver._matching_candidates(
        {"id": "session", "active_version_id": None},
        intent,
        snapshot={},
        selection={},
        latest_carrier=latest,
    )


def test_six_intents_do_not_repeat_availability_for_three_unrelated_choices(monkeypatch):
    # No database/Provider is used: all matching choices are denied by the
    # availability boundary, so a successful binding cannot be fabricated.
    resolver = ConversationCapabilityResolver(None)
    checks = []

    def unavailable(session_id, source_turn_id, choice_id):
        checks.append((session_id, source_turn_id, choice_id))
        return False

    monkeypatch.setattr(resolver, "_execution_available", unavailable)
    assert len(resolver._CHOICE_REQUIRED_INTENTS) == 6
    latest = carrier()
    for intent in resolver._CHOICE_REQUIRED_INTENTS:
        assert candidates(resolver, intent, latest) == []
    assert Counter(checks) == Counter(
        ("session", "assistant-current", choice_id) for choice_id in ("more", "adopt", "slot")
    )


def test_matching_choice_still_needs_availability_and_bound_state(monkeypatch):
    resolver = ConversationCapabilityResolver(None)
    checks = []
    available = False

    def execution_available(*args):
        checks.append("availability")
        return available

    def bound_state(*args, **kwargs):
        checks.append("bound_state")
        return None

    monkeypatch.setattr(resolver, "_execution_available", execution_available)
    monkeypatch.setattr(resolver, "_bound_active_state", bound_state)
    assert candidates(resolver, "adopt_plan", carrier()) == []
    assert checks == ["availability"]
    available = True
    checks.clear()
    assert candidates(resolver, "adopt_plan", carrier()) == []
    assert checks == ["availability", "bound_state"]


def test_repeated_snapshot_scans_recheck_matching_authority_without_cache(monkeypatch):
    resolver = ConversationCapabilityResolver(None)
    checks = []

    def unavailable(*args):
        checks.append(args)
        return False

    monkeypatch.setattr(resolver, "_execution_available", unavailable)
    for _ in range(2):
        assert candidates(resolver, "adopt_plan", carrier()) == []
    assert checks == [("session", "assistant-current", "adopt")] * 2
