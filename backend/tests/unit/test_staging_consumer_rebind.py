from src.services.creative_planning_models import CreativePortfolioDaySlot, CreativePortfolioIntentPool
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService


def _metrics():
    return {
        "consumerAdmissionEvaluatedCount": 0,
        "consumerAdmissionAdmittedCount": 0,
        "consumerAdmissionPendingCount": 0,
        "consumerAdmissionRejectedCount": 0,
        "consumerAdmissionAreaSeedOnlyCount": 0,
        "consumerFingerprintMismatchCount": 0,
        "staleAdmissionInvalidatedCount": 0,
        "consumerReevaluationCount": 0,
        "identityGateRejectCount": 0,
        "exactEntityGateRejectCount": 0,
        "semanticAffordanceRejectCount": 0,
        "anchorEligibilityRejectCount": 0,
        "specializedPolicyRejectCount": 0,
        "evidenceSufficiencyPendingCount": 0,
        "nameOnlyPositiveSignalCount": 0,
    }


def test_staging_rebind_recomputes_consumer_admission_in_enforce_mode():
    service = CreativePortfolioStagingService(object(), experience_grounding_v2_mode="enforce")
    pool = CreativePortfolioIntentPool(
        poolId="pool_local",
        briefId="brief_a",
        rawNeed="体验本地生活",
        city="广州",
        intentType="area_walk",
        targetCount=1,
        requirementLevel="optional",
        assignToSlots=["slot_local"],
        optionalExperienceFamily="local_life",
    )
    slot = CreativePortfolioDaySlot(
        slotId="slot_local",
        dayNumber=1,
        timeWindow="afternoon",
        durationMinutes=90,
        kind="visit",
        rawNeed="体验本地生活",
        routeAnchor=True,
        requirementLevel="soft",
        experienceShape="area",
        experienceGoal="观察居民日常活动",
        evidenceRequirements={"minimumIndependentClaims": 1},
    )
    candidate = {
        "id": "B000000001",
        "amapId": "B000000001",
        "name": "社区农贸市场",
        "type": "购物服务;综合市场;农贸市场",
        "category": "农贸市场",
        "providerTypeCode": "060703",
        "tags": ["社区商业"],
        "city": "广州",
        "longitude": 113.26,
        "latitude": 23.13,
        "source": "amap-place-search",
        "sourceClaims": [{
            "claimKey": "resident_market",
            "stance": "support",
            "sourceName": "public-guide",
        }],
        "semanticPassed": True,
    }
    metrics = _metrics()

    assert service._apply_consumer_admission(candidate, brief_id="brief_a", pool=pool, slot=slot, family="local_life", metrics=metrics) is True
    first_fingerprint = candidate["consumerAdmissionReport"]["consumerFingerprint"]
    assert service._apply_consumer_admission(candidate, brief_id="brief_b", pool=pool, slot=slot, family="local_life", metrics=metrics) is True

    assert candidate["consumerAdmissionReport"]["consumerFingerprint"] != first_fingerprint
    assert candidate["scoreEligible"] is True
    assert metrics["consumerAdmissionEvaluatedCount"] == 2
    assert metrics["staleAdmissionInvalidatedCount"] == 1
    assert metrics["consumerFingerprintMismatchCount"] == 1
    assert metrics["consumerReevaluationCount"] == 2
