import pytest
from pydantic import ValidationError

from src.services.creative_planning_models import SlotExperienceIntent, TripExperienceIntent
from src.services.experience_intent_service import ExperienceIntentService


def test_experience_intent_contract_is_identity_free_and_fingerprinted():
    trip = TripExperienceIntent(
        schemaVersion="experience-intent-v1",
        tripThesis="体验城市日常生活",
        desiredSignals=["community_market", "resident_activity"],
        avoidSignals=["tourist_only"],
        decisionAxes=["local_immersion", "comfort"],
        allowedExperienceShapes=["area", "open_walk"],
    )
    slot = SlotExperienceIntent(
        schemaVersion="experience-intent-v1",
        slotId="slot_local_life",
        family="local_life",
        requirementLevel="soft",
        experienceShape="area",
        experienceGoal="观察本地居民的日常生活",
        desiredSignals=["community_market"],
        avoidSignals=["museum_only"],
        evidencePolicy={"minimumIndependentClaims": 1},
        groundingPolicy={"consumerRecheckRequired": True},
        routeContext={"maxDetourMinutes": 20},
    )

    assert len(trip.fingerprint) == 64
    assert len(slot.intent_fingerprint) == 64
    assert trip.fingerprint != slot.intent_fingerprint
    assert "amapId" not in trip.model_dump_json(by_alias=True)


def test_experience_intent_rejects_grounded_identity_fields():
    with pytest.raises(ValidationError):
        SlotExperienceIntent(
            schemaVersion="experience-intent-v1",
            slotId="slot_bad",
            family="local_life",
            requirementLevel="soft",
            experienceShape="single_poi",
            experienceGoal="体验本地生活",
            desiredSignals=[],
            avoidSignals=[],
            evidencePolicy={},
            groundingPolicy={},
            routeContext={},
            amapId="B000000001",
        )


def test_cross_city_intent_inference_uses_same_contract_without_city_answers():
    service = ExperienceIntentService()
    shenzhen = service.infer("带父母去深圳，想体验本地人的日常生活和地方饮食，还没想好哪一种作为主线")
    guangzhou = service.infer("带父母去广州，想体验本地人的日常生活和地方饮食，还没想好哪一种作为主线")

    assert shenzhen["contract"]["desiredSignals"] == guangzhou["contract"]["desiredSignals"]
    assert shenzhen["contract"]["allowedExperienceShapes"] == guangzhou["contract"]["allowedExperienceShapes"]
    assert shenzhen["highImpactAmbiguityDetected"] is True
    assert shenzhen["clarificationDimensionId"] == "experience_intent.primary_axis"
    assert shenzhen["clarificationQuestion"] == ""
    assert shenzhen["clarificationOptions"] == []
    assert "amapId" not in str(shenzhen)


@pytest.mark.parametrize("shape", ["single_poi", "area", "micro_route", "open_walk"])
def test_slot_experience_intent_expresses_each_p0_shape_without_collapsing_it(shape: str):
    slot = SlotExperienceIntent(
        schemaVersion="experience-intent-v1",
        slotId=f"slot_{shape}",
        family="local_life",
        requirementLevel="soft",
        experienceShape=shape,
        experienceGoal="按结构化形态保留体验位置",
        desiredSignals=["resident_daily_life"],
        avoidSignals=["name_only_match"],
        evidencePolicy={"minimumIndependentClaims": 1},
        groundingPolicy={"allowPending": True},
        routeContext={"maxDetourMinutes": 20},
    )

    assert slot.experience_shape == shape
