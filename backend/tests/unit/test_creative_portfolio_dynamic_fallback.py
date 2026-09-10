import json

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService
from src.services.creative_direction_generator import CreativeDirectionGenerator
from src.services.creative_planning_models import TripExperienceIntent


def test_dynamic_axes_select_non_default_fallback_seeds_without_resetting_to_first_four():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    portfolio = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={"dayStrategies": [{"dayNumber": 1, "maxRouteAnchors": 4, "requiredGoalIds": ["campus"]}]},
        schema_repair_attempts=0,
        direction_axes=["photo_night", "family_light", "citywalk_hidden_gems", "classic"],
    )

    assert [proposal.brief.primary_axis for proposal in portfolio.proposals] == [
        "photo_night",
        "family_light",
        "citywalk_hidden_gems",
        "classic",
    ]
    assert all(proposal.brief.brief_id.startswith("dynamic_") for proposal in portfolio.proposals)
    assert len({proposal.brief.direction_signature for proposal in portfolio.proposals}) == 4
    assert all(proposal.brief.generation_source == "seed_composition" for proposal in portfolio.proposals)
    assert len(portfolio.proposals) == 4


def test_deterministic_fallback_carries_trip_experience_intent_into_every_brief():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {"goalId": "night", "intentType": "night_view"},
                ],
            },
        }
    )
    intent = TripExperienceIntent(
        schemaVersion="experience-intent-v1",
        tripThesis="两天认识北京高校并在晚上看城市夜景",
        desiredSignals=["campus_character", "night_view"],
        avoidSignals=["name_only_match"],
        decisionAxes=["photo_night", "culture_deep_dive"],
        allowedExperienceShapes=["single_poi", "area"],
    )
    ledger = ledger.model_copy(update={"experience_intent": intent.model_dump(by_alias=True)})

    portfolio = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "maxRouteAnchors": 4,
                    "requiredGoalIds": ["campus", "night"],
                }
            ]
        },
        schema_repair_attempts=0,
        direction_axes=["photo_night", "culture_deep_dive"],
    )

    assert [proposal.brief.primary_axis for proposal in portfolio.proposals] == [
        "photo_night",
        "culture_deep_dive",
    ]
    assert all(proposal.brief.experience_intent is not None for proposal in portfolio.proposals)
    assert {
        proposal.brief.experience_intent.fingerprint
        for proposal in portfolio.proposals
        if proposal.brief.experience_intent is not None
    } == {intent.fingerprint}


def test_provider_output_is_rebound_to_authoritative_trip_experience_intent():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "深圳",
            "requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]},
        }
    )
    intent = TripExperienceIntent(
        schemaVersion="experience-intent-v1",
        tripThesis="从高校认识深圳的城市发展",
        desiredSignals=["campus_character"],
        avoidSignals=["name_only_match"],
        decisionAxes=["culture_deep_dive"],
        allowedExperienceShapes=["single_poi"],
    )
    ledger = ledger.model_copy(update={"experience_intent": intent.model_dump(by_alias=True)})
    service = CreativePortfolioProviderService()
    generated = service.deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "maxRouteAnchors": 3,
                    "requiredGoalIds": ["campus"],
                }
            ]
        },
        schema_repair_attempts=0,
        direction_axes=["culture_deep_dive"],
    )
    provider_payload = generated.model_dump(by_alias=True)
    provider_payload.pop("parserMetadata", None)
    for proposal in provider_payload["proposals"]:
        proposal["brief"]["experienceIntent"] = None

    rebound = service._validate(json.dumps(provider_payload, ensure_ascii=False), ledger)

    assert rebound.proposals[0].brief.experience_intent is not None
    assert rebound.proposals[0].brief.experience_intent.fingerprint == intent.fingerprint


def test_provider_contract_rejects_structurally_duplicate_direction():
    import copy
    import json

    import pytest

    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    service = CreativePortfolioProviderService()
    portfolio = service.deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "maxRouteAnchors": 4,
                    "requiredGoalIds": ["campus"],
                }
            ]
        },
        schema_repair_attempts=0,
    ).model_dump(by_alias=True)
    duplicate = copy.deepcopy(portfolio["proposals"][0])
    duplicate["brief"]["briefId"] = "duplicate_direction"
    slot_ids = {item["slotId"]: f"duplicate_slot_{index}" for index, item in enumerate(duplicate["daySlots"])}
    for item in duplicate["daySlots"]:
        item["slotId"] = slot_ids[item["slotId"]]
    for index, pool in enumerate(duplicate["intentPools"]):
        pool["briefId"] = "duplicate_direction"
        pool["poolId"] = f"duplicate_pool_{index}"
        pool["assignToSlots"] = [slot_ids[item] for item in pool["assignToSlots"]]
    portfolio["proposals"] = [portfolio["proposals"][0], duplicate]

    with pytest.raises(ValueError, match="creative_portfolio_duplicate_direction_signature"):
        service._validate(json.dumps(portfolio, ensure_ascii=False), ledger)


def test_fallback_materializes_supply_backed_direction_metadata_and_dynamic_title():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    inventory = [
        {
            "candidateId": f"B0000ART{index}",
            "name": f"朝阳艺术空间 {index}",
            "family": "art_walk",
            "intentType": "area_walk",
            "dayNumber": 1,
            "timeWindow": "afternoon",
            "areaKey": "朝阳艺术带",
            "scoreEligible": True,
            "longitude": 116.49 + index * 0.001,
            "latitude": 39.98 + index * 0.001,
        }
        for index in (1, 2)
    ]
    directions = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus:1"],
        day_anchor_targets={1: 2},
        used_signatures=[],
        limit=1,
        candidate_inventory=inventory,
        city="北京",
    )

    portfolio = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "maxRouteAnchors": 2,
                    "requiredGoalIds": ["campus"],
                }
            ]
        },
        schema_repair_attempts=0,
        direction_candidates=directions,
    )

    brief = portfolio.proposals[0].brief
    assert brief.title == "朝阳艺术带 · 艺术空间"
    assert brief.theme_families == ["art_walk"]
    assert brief.candidate_supply["scoreEligibleCount"] == 2
    assert brief.direction_signature == directions[0]["directionSignature"]
    assert brief.novelty_evidence["newPhysicalPoiIds"]
    assert brief.feasibility_evidence["candidateSufficient"] is True
    assert brief.generation_source == "admitted_candidate_inventory"


def test_supply_backed_family_uses_two_distinct_slots_when_two_days_have_capacity():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    direction = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus_visit:1"],
        day_anchor_targets={1: 2, 2: 2},
        used_signatures=[],
        limit=1,
        candidate_inventory=[
            {
                "candidateId": f"B0000ART{index}",
                "family": "art_walk",
                "intentType": "area_walk",
                "dayNumber": index,
                "timeWindow": "afternoon",
                "areaKey": "朝阳艺术带",
                "scoreEligible": True,
                "longitude": 116.49 + index * 0.001,
                "latitude": 39.98 + index * 0.001,
            }
            for index in (1, 2)
        ],
    )[0]

    portfolio = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive={
            "dayStrategies": [
                {"dayNumber": 1, "maxRouteAnchors": 3, "requiredGoalIds": ["campus"]},
                {"dayNumber": 2, "maxRouteAnchors": 3, "requiredGoalIds": ["campus"]},
            ]
        },
        schema_repair_attempts=0,
        direction_candidates=[direction],
    )

    optional_slots = [
        item for item in portfolio.proposals[0].day_slots if item.optional_experience_family == "art_walk"
    ]
    assert len(optional_slots) == 2
    assert len({item.slot_id for item in optional_slots}) == 2
