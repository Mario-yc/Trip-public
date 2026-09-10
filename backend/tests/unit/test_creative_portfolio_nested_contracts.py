import json

import pytest

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService


def _ledger():
    return ConstraintLedgerCompiler().compile(
        {"city": "北京", "requestIntentContract": {"dayCount": 2, "requiredIntents": [
            {"goalId": "campus", "intentType": "campus_visit"},
            {"goalId": "museum", "intentType": "museum"},
        ]}}
    )


def _payload():
    return {
        "schemaVersion": "initial-creative-portfolio-v1",
        "proposals": [{
            "brief": {
                "briefId": "culture", "title": "文化", "primaryAxis": "culture_deep_dive",
                "dayRoles": [
                    {"dayNumber": 1, "role": "文化主线", "targetRouteAnchors": 2, "densityEvidence": ["pace=standard"]},
                    {"dayNumber": 2, "role": "博物馆主线", "targetRouteAnchors": 1, "densityEvidence": ["pace=standard"]},
                ],
                "optionalExperiences": [{"family": "local_food", "description": "当地特色美食"}],
                "requiredGoalIds": ["campus", "museum"],
            },
            "daySlots": [
                {"slotId": "campus_slot", "dayNumber": 1, "timeWindow": "09:00-10:00", "durationMinutes": 60, "kind": "visit", "rawNeed": "大学", "routeAnchor": True, "priority": 90, "requiredGoalId": "campus"},
                {"slotId": "food_slot", "dayNumber": 1, "timeWindow": "12:00-13:00", "durationMinutes": 60, "kind": "meal", "rawNeed": "本地菜", "routeAnchor": True, "priority": 30, "optionalExperienceFamily": "local_food"},
                {"slotId": "museum_slot", "dayNumber": 2, "timeWindow": "14:00-15:00", "durationMinutes": 60, "kind": "visit", "rawNeed": "美术馆", "routeAnchor": True, "priority": 90, "requiredGoalId": "museum"},
            ],
            "intentPools": [
                {"poolId": "campus_pool", "briefId": "culture", "rawNeed": "大学", "city": "北京", "intentType": "campus_visit", "targetCount": 1, "requirementLevel": "required", "goalId": "campus", "assignToSlots": ["campus_slot"], "preferredTypes": [], "rejectedTypes": [], "candidateHints": []},
                {"poolId": "museum_pool", "briefId": "culture", "rawNeed": "美术馆", "city": "北京", "intentType": "museum", "targetCount": 1, "requirementLevel": "required", "goalId": "museum", "assignToSlots": ["museum_slot"], "preferredTypes": [], "rejectedTypes": [], "candidateHints": []},
                {"poolId": "food_pool", "briefId": "culture", "rawNeed": "本地菜", "city": "北京", "intentType": "meal", "targetCount": 1, "requirementLevel": "optional", "assignToSlots": ["food_slot"], "optionalExperienceFamily": "local_food", "preferredTypes": [], "rejectedTypes": [], "candidateHints": []},
            ],
        }],
    }


def test_strict_nested_contract_rejects_cross_brief_slot_reference():
    payload = _payload()
    payload["proposals"][0]["intentPools"][2]["assignToSlots"] = ["other_brief_slot"]
    with pytest.raises(ValueError, match="cross_brief_slot"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))


def test_strict_nested_contract_rejects_invented_required_goal_and_invalid_day():
    payload = _payload()
    payload["proposals"][0]["daySlots"][0]["requiredGoalId"] = "invented"
    with pytest.raises(ValueError, match="required_goal"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))
    payload = _payload()
    payload["proposals"][0]["daySlots"][0]["dayNumber"] = 3
    with pytest.raises(ValueError, match="day_number"):
        CreativePortfolioProviderService().generate(ledger=_ledger(), invoke=lambda: json.dumps(payload))
