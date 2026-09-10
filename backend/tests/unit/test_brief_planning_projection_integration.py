import pytest

from src.services.brief_planning_projection_service import BriefPlanningProjectionService
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import InitialCreativePortfolio
from src.services.portfolio_route_feasibility_service import (
    PortfolioRouteFeasibilityService,
    _RouteAuthorizationPreconditionError,
)


def test_projection_removes_forbidden_optional_family_from_executable_pool_eligibility():
    ledger = ConstraintLedgerCompiler().compile({"city": "北京", "requestIntentContract": {"negativeConstraints": ["night_photo"], "requiredIntents": []}})
    generated = InitialCreativePortfolio.model_validate({
        "schemaVersion": "initial-creative-portfolio-v1",
        "proposals": [{
            "brief": {"briefId": "brief", "title": "夜景", "primaryAxis": "photo_night", "dayRoles": [{"dayNumber": 1, "role": "夜景", "targetRouteAnchors": 1, "densityEvidence": ["pace=standard"]}], "requiredGoalIds": [], "optionalExperiences": [{"family": "night_photo", "description": "夜景"}]},
            "daySlots": [{"slotId": "night", "dayNumber": 1, "timeWindow": "evening", "durationMinutes": 60, "kind": "experience", "rawNeed": "夜景", "routeAnchor": True, "optionalExperienceFamily": "night_photo"}],
            "intentPools": [{"poolId": "night-pool", "briefId": "brief", "rawNeed": "夜景", "city": "北京", "intentType": "night_view", "targetCount": 1, "requirementLevel": "optional", "optionalExperienceFamily": "night_photo", "assignToSlots": ["night"]}],
        }],
    })
    projection = BriefPlanningProjectionService().project(generated.proposals[0], ledger)
    assert projection.optional_families == ()
    assert projection.optional_pool_ids == ()
    assert projection.optional_slot_ids == ()


def test_projection_does_not_authorize_an_optional_pool_without_a_slot():
    ledger = ConstraintLedgerCompiler().compile({"city": "北京", "requestIntentContract": {"requiredIntents": []}})
    generated = InitialCreativePortfolio.model_validate({
        "schemaVersion": "initial-creative-portfolio-v1",
        "proposals": [{
            "brief": {"briefId": "brief", "title": "本地", "primaryAxis": "local_immersion", "dayRoles": [{"dayNumber": 1, "role": "本地", "targetRouteAnchors": 1, "densityEvidence": ["pace=standard"]}], "requiredGoalIds": [], "optionalExperiences": [{"family": "local_life", "description": "社区"}]},
            "daySlots": [{"slotId": "base", "dayNumber": 1, "timeWindow": "morning", "durationMinutes": 60, "kind": "visit", "rawNeed": "基础地点", "routeAnchor": True}],
            "intentPools": [{"poolId": "local-pool", "briefId": "brief", "rawNeed": "社区", "city": "北京", "intentType": "area_walk", "targetCount": 1, "requirementLevel": "optional", "optionalExperienceFamily": "local_life", "assignToSlots": []}],
        }],
    })
    projection = BriefPlanningProjectionService().project(generated.proposals[0], ledger)
    assert projection.optional_pool_ids == ()
    assert projection.optional_slot_ids == ()


def _transport_projection(transport_preferences):
    intent_contract: dict[str, object] = {"requiredIntents": []}
    if transport_preferences is not None:
        intent_contract["transportPreferences"] = transport_preferences
    ledger = ConstraintLedgerCompiler().compile(
        {"city": "北京", "requestIntentContract": intent_contract}
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "brief",
                        "title": "交通投影",
                        "primaryAxis": "culture_deep_dive",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["pace=standard"],
                            }
                        ],
                        "requiredGoalIds": [],
                    },
                    "daySlots": [
                        {
                            "slotId": "first",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "高校",
                            "routeAnchor": True,
                        },
                        {
                            "slotId": "second",
                            "dayNumber": 1,
                            "timeWindow": "afternoon",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "博物馆",
                            "routeAnchor": True,
                        },
                    ],
                    "intentPools": [],
                }
            ],
        }
    )
    return BriefPlanningProjectionService().project(generated.proposals[0], ledger)


def test_projection_keeps_missing_transport_unspecified_and_route_work_unauthorized():
    projection = _transport_projection(None)

    assert projection.transport_preference == "unspecified"
    with pytest.raises(_RouteAuthorizationPreconditionError) as error:
        PortfolioRouteFeasibilityService()._authorize_route_work(
            {},
            mode=projection.transport_preference,
            reason="baseline_adjacent",
            condition="route_required",
            route_pairs=[],
        )
    assert error.value.reason == "route_authorization_mode_invalid"


@pytest.mark.parametrize("transport", ["public_transit", "walking", "driving"])
def test_projection_preserves_explicit_canonical_transport_without_inference(transport: str):
    projection = _transport_projection([transport])

    assert projection.transport_preference == transport


def test_projection_preserves_transport_and_day_scoped_goal_counts():
    ledger = ConstraintLedgerCompiler().compile({
        "city": "北京",
        "resolvedTripDates": {"dayCount": 2},
        "requestIntentContract": {
            "transportPreferences": ["公交地铁优先"],
            "requiredIntents": [
                {"goalId": "campus", "intentType": "campus_visit"},
                {"goalId": "museum", "intentType": "museum"},
                {
                    "goalId": "meal",
                    "intentType": "local_food",
                    "requirementLevel": "soft_experience",
                },
            ],
        },
    })
    generated = InitialCreativePortfolio.model_validate({
        "schemaVersion": "initial-creative-portfolio-v1",
        "proposals": [{
            "brief": {
                "briefId": "brief",
                "title": "文化深游",
                "primaryAxis": "culture_deep_dive",
                "dayRoles": [
                    {"dayNumber": 1, "role": "高校与饮食", "targetRouteAnchors": 3, "densityEvidence": ["availableWindow=full_day"]},
                    {"dayNumber": 2, "role": "博物馆", "targetRouteAnchors": 2, "densityEvidence": ["availableWindow=full_day"]},
                ],
                "requiredGoalIds": ["campus", "museum"],
                "optionalExperiences": [
                    {"family": "heritage_walk", "description": "历史街区"},
                    {"family": "local_life", "description": "本地生活"},
                ],
            },
            "daySlots": [
                {"slotId": "campus", "dayNumber": 1, "timeWindow": "morning", "durationMinutes": 120, "kind": "visit", "rawNeed": "985大学", "routeAnchor": True, "requiredGoalId": "campus"},
                {"slotId": "meal", "dayNumber": 1, "timeWindow": "noon", "durationMinutes": 75, "kind": "meal", "rawNeed": "特色美食", "routeAnchor": True, "softGoalId": "meal"},
                {"slotId": "heritage", "dayNumber": 1, "timeWindow": "afternoon", "durationMinutes": 60, "kind": "activity", "rawNeed": "历史街区", "routeAnchor": True, "optionalExperienceFamily": "heritage_walk"},
                {"slotId": "museum", "dayNumber": 2, "timeWindow": "morning", "durationMinutes": 120, "kind": "visit", "rawNeed": "博物馆", "routeAnchor": True, "requiredGoalId": "museum"},
                {"slotId": "local", "dayNumber": 2, "timeWindow": "afternoon", "durationMinutes": 60, "kind": "activity", "rawNeed": "本地生活", "routeAnchor": True, "optionalExperienceFamily": "local_life"},
            ],
            "intentPools": [
                {"poolId": "campus-pool", "briefId": "brief", "rawNeed": "985大学", "city": "北京", "intentType": "campus_visit", "targetCount": 1, "requirementLevel": "required", "goalId": "campus", "assignToSlots": ["campus"]},
                {"poolId": "museum-pool", "briefId": "brief", "rawNeed": "博物馆", "city": "北京", "intentType": "museum", "targetCount": 1, "requirementLevel": "required", "goalId": "museum", "assignToSlots": ["museum"]},
                {"poolId": "meal-pool", "briefId": "brief", "rawNeed": "特色美食", "city": "北京", "intentType": "local_food", "targetCount": 1, "requirementLevel": "optional", "softGoalId": "meal", "assignToSlots": ["meal"]},
                {"poolId": "heritage-pool", "briefId": "brief", "rawNeed": "历史街区", "city": "北京", "intentType": "area_walk", "targetCount": 1, "requirementLevel": "optional", "optionalExperienceFamily": "heritage_walk", "assignToSlots": ["heritage"]},
                {"poolId": "local-pool", "briefId": "brief", "rawNeed": "本地生活", "city": "北京", "intentType": "area_walk", "targetCount": 1, "requirementLevel": "optional", "optionalExperienceFamily": "local_life", "assignToSlots": ["local"]},
            ],
        }],
    })

    projection = BriefPlanningProjectionService().project(
        generated.proposals[0],
        ledger,
        density_decision_source="creative_portfolio_provider",
    )
    lineage = projection.as_lineage()

    assert lineage["densityDecisionSource"] == "creative_portfolio_provider"
    assert lineage["transportPreference"] == "public_transit"
    assert lineage["dayEvidence"] == {
        "1": {
            "requiredGoalCount": 1,
            "explicitSoftGoalCount": 1,
            "briefOptionalCount": 1,
            "availableWindow": "full_day",
        },
        "2": {
            "requiredGoalCount": 1,
            "explicitSoftGoalCount": 0,
            "briefOptionalCount": 1,
            "availableWindow": "full_day",
        },
    }
