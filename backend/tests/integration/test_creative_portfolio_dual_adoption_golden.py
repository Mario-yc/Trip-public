"""Deterministic two-card Creative Portfolio adoption golden.

The second proposal is produced by the production continuation staging path
(`offer_repaired_proposal`), never by copying an existing proposal row.  The
only mocked boundary is the recorded AMap route provider response.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from evals.creative_portfolio_dual_artifact import (
    evaluate_dual_adoption_artifact_bundle,
    write_dual_adoption_artifact_bundle,
)
from evals.creative_portfolio_evaluator import evaluate_dual_adoption_golden
from evals.run_offline import dual_adoption_golden_failures
from src.api.schemas.agent import AgentMessageRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.models.route_option import RouteOption, normalize_route_mode
from src.runtime.state_exporter import AgentStateExporter
from src.services.agent_service import AgentService
from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.consumer_candidate_admission_service import (
    ConsumerCandidateAdmissionService,
)
from src.services.conversation_service import ConversationService
from src.services.creative_portfolio_provider_service import (
    CreativePortfolioProviderService,
)
from src.services.creative_portfolio_staging_service import (
    CreativePortfolioStagingService,
)
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.portfolio_pending_slot_schedule_service import (
    PortfolioPendingSlotScheduleService,
)
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.proposal_route_evidence_normalizer import (
    ProposalRouteEvidenceNormalizer,
)
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import RouteService
from src.services.shared_candidate_universe_service import SharedCandidateUniverse


@pytest.fixture
def db_connection():
    connection = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _recorded_candidate(
    amap_id: str,
    *,
    name: str,
    intent_type: str,
    longitude: float,
    latitude: float,
    theme_family: str = "",
) -> dict[str, Any]:
    provider_type, provider_type_code, tags = {
        "campus_visit": (
            "科教文化服务;学校;高等院校",
            "141201",
            ["大学", "高校", "校园"],
        ),
        "night_view": (
            "风景名胜;风景名胜相关;旅游景点",
            "110000",
            ["夜景", "观景台", "夜游"],
        ),
        "meal": (
            "餐饮服务;中餐厅;特色/地方风味餐厅",
            "050118",
            ["地方风味", "特色午餐"],
        ),
        "heritage_walk": (
            "风景名胜;风景名胜相关;特色街区",
            "110200",
            ["历史街区", "胡同", "文化街区"],
        ),
        "art_walk": (
            "科教文化服务;文化场馆;艺术中心",
            "140500",
            ["艺术街区", "创意园区", "艺术中心"],
        ),
    }[intent_type]
    family = theme_family or (
        "local_food" if intent_type == "meal" else intent_type if intent_type in {"heritage_walk", "art_walk"} else ""
    )
    return {
        "id": amap_id,
        "amapId": amap_id,
        "name": name,
        "city": "北京",
        "type": provider_type,
        "providerType": provider_type,
        "providerTypeCode": provider_type_code,
        "tags": tags,
        "category": intent_type,
        "longitude": longitude,
        "latitude": latitude,
        "source": "amap-place-search",
        "confidence": 0.98,
        "candidateScore": 0.98,
        "localRouteScore": 0.95,
        "semanticPassed": True,
        "sourcePrecheck": {"passed": True, "scope": "recorded-amap-golden"},
        **(
            {
                "openTimeToday": "18:00-23:00",
                "nightAvailabilityStatus": "verified_available",
            }
            if intent_type == "night_view"
            else {}
        ),
        "sourceClaims": (
            [
                {
                    "claimKey": family,
                    "stance": "support",
                    "summary": f"录制夹具已核验 {name} 与 {family} 体验相符",
                    "sourceName": f"recorded-provider-{amap_id}",
                    "sourceUrlHash": hashlib.sha256(f"https://recorded.example/{amap_id}".encode("utf-8")).hexdigest(),
                    **({"locality": "北京"} if intent_type == "meal" else {}),
                }
            ]
            if family
            else []
        ),
    }


def _universe(direction: str) -> SharedCandidateUniverse:
    if direction == "heritage":
        campus = [
            _recorded_candidate(
                "B0GOLDH01",
                name="清华大学",
                intent_type="campus_visit",
                longitude=116.326,
                latitude=40.003,
            ),
            _recorded_candidate(
                "B0GOLDH02",
                name="北京大学",
                intent_type="campus_visit",
                longitude=116.310,
                latitude=39.992,
            ),
        ]
        night = [
            _recorded_candidate(
                "B0GOLDH03",
                name="前门步行街夜景段",
                intent_type="night_view",
                longitude=116.397,
                latitude=39.899,
            ),
            _recorded_candidate(
                "B0GOLDH04",
                name="亮马河夜游步道",
                intent_type="night_view",
                longitude=116.474,
                latitude=39.953,
            ),
        ]
        meals = [
            _recorded_candidate(
                "B0GOLDH07",
                name="京味坊特色午餐",
                intent_type="meal",
                longitude=116.316,
                latitude=39.995,
            ),
            _recorded_candidate(
                "B0GOLDH08",
                name="胡同小院地方菜",
                intent_type="meal",
                longitude=116.409,
                latitude=39.932,
            ),
        ]
        area = [
            _recorded_candidate(
                "B0GOLDH05",
                name="南锣鼓巷历史文化街区",
                intent_type="heritage_walk",
                longitude=116.404,
                latitude=39.937,
            ),
            _recorded_candidate(
                "B0GOLDH06",
                name="前门历史文化街区",
                intent_type="heritage_walk",
                longitude=116.397,
                latitude=39.899,
            ),
        ]
    else:
        campus = [
            _recorded_candidate(
                "B0GOLDA01",
                name="中国人民大学",
                intent_type="campus_visit",
                longitude=116.321,
                latitude=39.970,
            ),
            _recorded_candidate(
                "B0GOLDA02",
                name="北京师范大学",
                intent_type="campus_visit",
                longitude=116.365,
                latitude=39.962,
            ),
        ]
        night = [
            _recorded_candidate(
                "B0GOLDA03",
                name="奥林匹克森林公园夜景步道",
                intent_type="night_view",
                longitude=116.389,
                latitude=40.016,
            ),
            _recorded_candidate(
                "B0GOLDA04",
                name="通惠河滨水夜游步道",
                intent_type="night_view",
                longitude=116.478,
                latitude=39.907,
            ),
        ]
        meals = [
            _recorded_candidate(
                "B0GOLDA07",
                name="学院路地方风味馆",
                intent_type="meal",
                longitude=116.358,
                latitude=39.969,
            ),
            _recorded_candidate(
                "B0GOLDA08",
                name="酒仙桥特色午餐馆",
                intent_type="meal",
                longitude=116.493,
                latitude=39.980,
            ),
        ]
        area = [
            _recorded_candidate(
                "B0GOLDA05",
                name="798艺术区",
                intent_type="art_walk",
                longitude=116.498,
                latitude=39.985,
            ),
            _recorded_candidate(
                "B0GOLDA06",
                name="郎园Station艺术园区",
                intent_type="art_walk",
                longitude=116.522,
                latitude=39.921,
            ),
        ]
    return SharedCandidateUniverse(
        pools={
            "campus_visit": campus,
            "night_view": night,
            "meal": meals,
            "area_walk": area,
        },
        unique_query_count=4,
        deduped_query_count=0,
    )


def _assert_initial_seven_night_candidates_fail_closed() -> None:
    """Replay the trace's exhausted first night-view discovery round.

    All seven rows carry deterministic Provider-shaped data, but none may
    count toward the required 2/2 coverage.  The later `_universe` calls are
    the recorded Provider refresh after the user accepts public outdoor night
    experiences.
    """

    base = {
        "city": "北京",
        "longitude": 116.40,
        "latitude": 39.92,
        "source": "amap-place-search",
        "category": "旅游景点",
        "providerType": "风景名胜;旅游景点;观景点",
        "providerTypeCode": "110000",
        "tags": ["城市公共空间"],
        "sourceClaims": [],
    }
    candidates = [
        {
            **base,
            "id": "B0REJECT01",
            "amapId": "B0REJECT01",
            "name": "城市观景台",
            "openTimeToday": "暂停开放",
        },
        {
            **base,
            "id": "B0REJECT02",
            "amapId": "B0REJECT02",
            "name": "公共天际观景平台",
            "nightAvailabilityStatus": "unknown",
        },
        {
            **base,
            "id": "B0REJECT03",
            "amapId": "B0REJECT03",
            "name": "城市图书馆夜景打卡点",
        },
        {
            **base,
            "id": "B0REJECT04",
            "amapId": "B0REJECT04",
            "name": "云景餐厅夜景",
            "providerType": "餐饮服务;中餐厅;中餐厅",
            "category": "中餐厅",
            "providerTypeCode": "050100",
        },
        {
            **base,
            "id": "B0REJECT05",
            "amapId": "B0REJECT05",
            "parentPoiId": "B0PARENT01",
            "name": "滨河公园-售票处",
        },
        {
            **base,
            "id": "B0REJECT06",
            "amapId": "B0REJECT06",
            "parentPoiId": "B0PARENT01",
            "name": "滨河公园-管理办公室",
        },
        {
            **base,
            "id": "B0REJECT07",
            "amapId": "B0REJECT07",
            "name": "城市生态公园",
            "providerType": "风景名胜;公园广场;城市公园",
            "category": "城市公园",
        },
    ]
    consumer = ConsumerCandidateAdmissionService.build_consumer_context(
        brief_id="trace_rejected_round",
        pool_id="night_pool_round_1",
        planning_slot_id="night_day_1",
        day_number=1,
        city="北京",
        family="night_view",
        activity_mode="night_view",
        requirement_level="hard",
        experience_shape="single_poi",
        experience_goal="每晚不同的公共户外城市夜景",
        desired_signals=["night_view"],
        rejected_types=["餐饮", "办公室", "售票处"],
    )
    reports = [ConsumerCandidateAdmissionService().evaluate(candidate, consumer) for candidate in candidates]
    assert len(reports) == 7
    assert all(report["scoreEligible"] is False for report in reports)
    assert all(report["classification"] != "admitted_final_anchor" for report in reports)
    reasons = {reason for report in reports for reason in report.get("reasonCodes") or []}
    assert {
        "night_view_explicitly_unavailable",
        "night_view_availability_unverified",
        "weak_night_view_entity",
        "night_view_dining_or_non_view",
        "night_view_signal_missing",
    }.issubset(reasons)
    assert PoiPhysicalIdentityService.canonical_amap_id(candidates[4]) == (
        PoiPhysicalIdentityService.canonical_amap_id(candidates[5])
    )


def _directive() -> dict[str, Any]:
    return {
        "type": "draft_itinerary",
        "goalPriority": ["goal_campus", "goal_meal", "goal_night"],
        "dayStrategies": [
            {
                "dayNumber": day,
                "theme": "高校、主题街区与城市夜景",
                "requiredGoalIds": ["goal_campus", "goal_meal", "goal_night"],
                "requiredGoalCounts": {
                    "goal_campus": 1,
                    "goal_meal": 1,
                    "goal_night": 1,
                },
                "optionalGoalIds": [],
                "pace": "standard",
                "maxRouteAnchors": 4,
            }
            for day in (1, 2)
        ],
        "optionalExperienceBudget": 2,
        "candidateSelectionPolicy": {
            "autoSelectWhenDominant": True,
            "askWhenMaterialTradeoff": False,
            "preferLowDetour": True,
            "avoidRecentEntities": True,
        },
        "schedulePolicy": {
            "respectOpeningWindowsWhenKnown": True,
            "allowProvisionalWhenUnknown": True,
        },
    }


def _intent_contract() -> dict[str, Any]:
    return {
        "city": "北京",
        "dayCount": 2,
        "transportPreferences": ["public_transit"],
        "experienceSpecs": [
            {
                "intentType": "night_view",
                "frequency": "every_available_evening",
                "allowedDayNumbers": [1, 2],
                "experienceFamilies": ["public_waterfront", "historic_night_walk"],
                "accessPolicy": "public_outdoor",
                "distinctnessPolicy": "distinct_physical_poi_per_day",
                "timeWindow": {"start": "18:30", "end": "22:00"},
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 35,
                    "maxDetourRatio": 0.35,
                },
                "evidenceFreshness": {
                    "maxAgeHours": 24,
                    "requiredForControlledAccess": True,
                    "requiredForPublicOutdoor": False,
                    "allowExplicitNoClosure": True,
                },
                "unresolvedDimensions": [],
                "confidence": 0.98,
            }
        ],
        "requiredIntents": [
            {
                "goalId": "goal_campus",
                "intentType": "campus_visit",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_night",
                "intentType": "night_view",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "required",
            },
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "requiredMin": 2,
                "preferredCount": 2,
                "maxCount": 2,
                "distributionPolicy": "every_allowed_day",
                "allowedDayNumbers": [1, 2],
                "requirementLevel": "soft_experience",
            },
        ],
    }


def _directions() -> list[dict[str, Any]]:
    return [
        {
            "primaryAxis": "culture_deep_dive",
            "title": "高校夜景与胡同文脉",
            "themeFamilies": ["heritage_walk"],
            "candidateSupply": {"familyCounts": {"heritage_walk": 2}},
            "generationSource": "recorded_test_provider",
            "directionSignature": "1" * 64,
        },
        {
            "primaryAxis": "citywalk_hidden_gems",
            "title": "高校夜景与当代艺术",
            "themeFamilies": ["art_walk"],
            "candidateSupply": {"familyCounts": {"art_walk": 2}},
            "generationSource": "recorded_test_provider",
            "directionSignature": "2" * 64,
        },
    ]


def _recorded_title_candidates(context: dict[str, Any]) -> str:
    axis = str((context.get("theme") or {}).get("primaryAxis") or "")
    signal = str(next(iter(context.get("requiredTitleSignals") or ["京华"])))
    titles = (
        [f"{signal}学府古巷京华", f"{signal}书声古意灯火", f"{signal}校园文脉夜色"]
        if axis == "culture_deep_dive"
        else [f"{signal}艺境新城华灯", f"{signal}画廊街巷星河", f"{signal}新艺长街夜色"]
    )
    evidence_ids = list(context.get("evidenceAmapIds") or [])
    return json.dumps(
        {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [{"title": title, "evidenceAmapIds": evidence_ids} for title in titles],
        },
        ensure_ascii=False,
    )


def _recorded_routes(
    _self,
    plan_id,
    pois,
    transport_mode=None,
    segments=None,
    route_pairs=None,
    **_kwargs,
):
    assert len(pois) >= 2
    segment_rows = list(segments or [])
    segments_by_id = {segment.id: segment for segment in segment_rows}
    if route_pairs:
        pairs = [(segments_by_id[left_id], segments_by_id[right_id]) for left_id, right_id in sorted(route_pairs)]
    else:
        pairs = []
        for day_id in dict.fromkeys(segment.day_id for segment in segment_rows):
            day_segments = [segment for segment in segment_rows if segment.day_id == day_id]
            day_segments.sort(key=lambda segment: segment.start_time)
            pairs.extend(zip(day_segments, day_segments[1:]))
    assert str(transport_mode or "").strip() not in {
        "",
        "unspecified",
    }, "recorded route fixture requires an explicit canonical transport mode"
    mode = normalize_route_mode(transport_mode)
    assert mode != "unspecified", "recorded route fixture requires an explicit canonical transport mode"
    is_meal_bypass = "meal_bypass" in str(plan_id)
    distance_meters = 3300 if is_meal_bypass else 1800
    duration_seconds = 1500 if is_meal_bypass else 900
    walking_distance = 450 if is_meal_bypass else 320
    transfer_count = 2 if is_meal_bypass else 1
    return [
        RouteOption(
            id="route_dual_"
            + hashlib.sha256(f"{plan_id}:{left.id}:{right.id}:{mode}".encode("utf-8")).hexdigest()[:20],
            plan_id=plan_id,
            from_segment_id=left.id,
            to_segment_id=right.id,
            from_poi_id=left.poi_id,
            to_poi_id=right.poi_id,
            provider="amap-webservice",
            mode=mode,
            is_selected=True,
            distance_meters=distance_meters,
            duration_seconds=duration_seconds,
            polyline=[[116.30, 39.90], [116.40, 39.95]],
            steps=[
                {
                    "instruction": "录制的公交换乘路线",
                    "mode": "transit",
                    "distance": 1800,
                }
            ],
            provider_payload={
                "fixture": "dual-adoption-golden",
                "walkingDistanceMeters": walking_distance,
                "transferCount": transfer_count,
                "waitSeconds": 180,
            },
        )
        for left, right in pairs
    ]


def _proposal_material(connection: sqlite3.Connection, portfolio_id: str) -> list[dict[str, Any]]:
    return [
        {
            **dict(row),
            "snapshot": json.loads(row["snapshot_json"]),
            "verifier": json.loads(row["verifier_json"]),
            "lineage": json.loads(row["generation_lineage_json"] or "{}"),
        }
        for row in connection.execute(
            """SELECT * FROM agent_plan_proposals
            WHERE portfolio_id = ? ORDER BY rank_index ASC""",
            (portfolio_id,),
        ).fetchall()
    ]


def _snapshot_amap_ids(snapshot: dict[str, Any]) -> set[str]:
    return {
        str(segment["poi"]["amapId"])
        for day in snapshot.get("days") or []
        for segment in day.get("segments") or []
        if isinstance(segment, dict)
        and isinstance(segment.get("poi"), dict)
        and str(segment["poi"].get("amapId") or "")
    }


def _assert_complete_proposal(material: dict[str, Any]) -> None:
    snapshot = material["snapshot"]
    verifier = material["verifier"]
    readiness = ProposalReadinessService.compute(snapshot, verifier=verifier)
    assert readiness["adoptionReady"] is True, readiness
    assert verifier["passed"] is True, verifier
    assert snapshot.get("portfolioPendingSlots") == []
    targets = {int(day): int(value) for day, value in verifier["dayAnchorTargets"].items()}
    actuals = {int(day): int(value) for day, value in verifier["dayAnchorActuals"].items()}
    assert targets == actuals == {1: 4, 2: 4}

    night_by_day: dict[int, list[str]] = {}
    meal_by_day: dict[int, list[str]] = {}
    for day in snapshot["days"]:
        for segment in day["segments"]:
            semantic = segment.get("semanticMetadata") or {}
            if semantic.get("intentType") == "night_view":
                night_by_day.setdefault(day["dayNumber"], []).append(segment["poi"]["amapId"])
            if semantic.get("intentType") == "meal":
                meal_by_day.setdefault(day["dayNumber"], []).append(segment["poi"]["amapId"])
            if semantic.get("portfolioOptional"):
                assert semantic["consumerAdmissionReport"]["scoreEligible"] is True
    assert {day: len(items) for day, items in night_by_day.items()} == {1: 1, 2: 1}
    assert len({*night_by_day[1], *night_by_day[2]}) == 2
    assert {day: len(items) for day, items in meal_by_day.items()} == {1: 1, 2: 1}

    expected_pairs = {
        (item["fromSegmentId"], item["toSegmentId"], item["dayNumber"])
        for item in ProposalReadinessService.expected_route_pairs(snapshot)
    }
    routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
    verified_pairs = {
        (item["fromSegmentId"], item["toSegmentId"], item["dayNumber"])
        for item in routes
        if ProposalRouteEvidenceNormalizer.is_verified(item)
    }
    assert verified_pairs == expected_pairs
    assert len(routes) == 6
    assert all(item["provider"] == "amap-webservice" for item in routes)
    assert all(item["distanceMeters"] == 1800 for item in routes)
    assert all(item["durationSeconds"] == 900 for item in routes)
    title_generation = snapshot["portfolioTitleGeneration"]
    title_evidence = snapshot["portfolioTitleEvidence"]
    assert title_generation["status"] == "succeeded"
    assert title_evidence["generationSource"] == "agent_generated_title_candidates"
    assert len(title_evidence["validCandidates"]) == 3
    assert set(title_evidence["selectedAmapIds"]) == _snapshot_amap_ids(snapshot)
    assert title_evidence["evidenceFingerprint"]
    title = snapshot["title"]
    assert 6 <= len(title) <= 18
    assert all("\u4e00" <= character <= "\u9fff" for character in title)
    assert not any(banned in title for banned in ("｜", "方向", "真实地点草案", "围绕", "排成", "顺路的北京漫游"))


def test_dual_complete_portfolio_adopts_second_once_and_survives_refresh(
    db_connection,
    monkeypatch,
    tmp_path,
):
    _assert_initial_seven_night_candidates_fail_closed()
    monkeypatch.setattr(RouteService, "build_routes", _recorded_routes)

    session = ConversationService(db_connection).create_session("北京", "dual complete portfolio golden")
    service = AgentService(db_connection)
    source_user_turn_id = service._insert_turn(
        session.session_id,
        "user",
        "北京两日高校游，每晚看夜景，并比较胡同文脉与当代艺术路线。",
        "active",
    )
    source_assistant_turn_id = service._insert_turn(
        session.session_id,
        "assistant",
        "正在生成两个可比较方案。",
        "active",
        agent_request_json={},
        agent_response_json={"mode": "creative_portfolio", "choiceOptions": []},
    )

    directive = _directive()
    intent_contract = _intent_contract()
    route_decision_contract = RouteInsertionScorer.build_route_decision_contract(
        source="dual_adoption_golden_fixture",
        provenance={
            "fixture": "dual-adoption-golden",
            "transportMode": "transit",
        },
        detour_tolerance={
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        mobility_profile={
            "source": "dual_adoption_golden_mobility",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6,
            "waitTimeMultiplier": 1,
            "riskPenaltyMultiplier": 1,
        },
    )
    assert route_decision_contract is not None
    intent_contract["routeDecisionContract"] = copy.deepcopy(route_decision_contract)
    request_context = {
        "selectedCity": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        },
        "requestIntentContract": intent_contract,
        "planningDirective": directive,
        "agentDecisionState": {"observationFingerprint": "d" * 64},
    }
    ledger = ConstraintLedgerCompiler().compile({**request_context, "city": "北京"})
    occurrence_plan = GoalOccurrenceCompiler().compile(ledger, directive)
    assert [(item.source_goal_id, item.day_number) for item in occurrence_plan.occurrences] == [
        ("goal_campus", 1),
        ("goal_meal", 1),
        ("goal_night", 1),
        ("goal_campus", 2),
        ("goal_meal", 2),
        ("goal_night", 2),
    ]
    daily_capacity = DailyCapacityPlanner().plan(
        occurrence_plan,
        pace=ledger.pace,
        day_count=2,
    )
    generated = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive=directive,
        schema_repair_attempts=0,
        occurrence_plan=occurrence_plan,
        daily_capacity=daily_capacity,
        direction_candidates=_directions(),
    )
    assert len(generated.proposals) == 2
    request_context["goalOccurrencePlan"] = occurrence_plan.model_dump(by_alias=True)
    request_context["dailyCapacityPlan"] = {
        str(day): item.model_dump(by_alias=True) for day, item in daily_capacity.items()
    }
    initial_plan = service._initial_plan_from_creative_portfolio(generated)
    base_snapshot = service._snapshot_from_persistable_segment_plans(
        service._session(session.session_id),
        initial_plan,
        [],
        request_context,
    )
    skeletons = {item.brief.brief_id: item for item in generated.proposals}

    def snapshot_builder(required, soft, optional, brief_id):
        snapshot = copy.deepcopy(base_snapshot)
        snapshot["routeDecisionContract"] = {
            "schemaVersion": "route-decision-contract-v1",
            "status": "ready",
            **copy.deepcopy(route_decision_contract),
        }
        service._remove_portfolio_planning_placeholders(snapshot)
        skeleton = skeletons[brief_id]
        service._materialize_portfolio_required_segments(
            snapshot,
            skeleton=skeleton,
            ledger=ledger,
            required=required,
            prepared_segment_plans=[],
        )
        service._materialize_portfolio_soft_segments(
            snapshot,
            skeleton=skeleton,
            ledger=ledger,
            soft=soft,
        )
        service._append_portfolio_optional_segments(
            snapshot,
            optional,
            skeleton.brief,
        )
        snapshot["portfolioPendingSlots"] = service._portfolio_pending_slots(
            snapshot,
            skeleton=skeleton,
        )
        snapshot = PortfolioPendingSlotScheduleService().project(snapshot)
        snapshot["decisionRationale"] = f"创意主题：{skeleton.brief.primary_axis}；所有地点来自录制的高德候选。"
        snapshot["creativeBrief"] = skeleton.brief.model_dump(by_alias=True)
        snapshot["portfolioGoalOccurrencePlan"] = occurrence_plan.model_dump(by_alias=True)
        snapshot["portfolioDailyCapacityPlan"] = request_context["dailyCapacityPlan"]
        return snapshot

    store = PlanPortfolioStore(db_connection)
    staging = CreativePortfolioStagingService(
        store,
        route_feasibility_service=service.portfolio_route_feasibility_service,
        experience_grounding_v2_mode="enforce",
        title_candidate_generator=_recorded_title_candidates,
    )
    first_generated = generated.model_copy(update={"proposals": [generated.proposals[0]]})
    first_portfolio, first_visible = staging.stage(
        session_id=session.session_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        expected_base_version_id=None,
        observation_fingerprint="d" * 64,
        request_fingerprint=ledger.source_fingerprint,
        ledger=ledger,
        generated=first_generated,
        universe=_universe("heritage"),
        snapshot_builder=snapshot_builder,
        city="北京",
        goal_occurrence_plan=occurrence_plan.model_dump(by_alias=True),
        max_route_anchors_by_day={1: 4, 2: 4},
        focus_brief_id=generated.proposals[0].brief.brief_id,
    )
    if first_portfolio.status != "awaiting_selection":
        pytest.fail(
            json.dumps(
                {
                    "failureReason": first_portfolio.failure_reason,
                    "metrics": staging.last_staging_metrics,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            pytrace=False,
        )
    assert len(first_visible) == 1
    first_summary = store.summary(portfolio_id=first_portfolio.portfolio_id)
    assert first_summary["visibleProposalIds"] == [first_visible[0].proposal_id]

    second_generated = generated.model_copy(update={"proposals": [generated.proposals[1]]})
    continued_portfolio, appended_visible = staging.stage(
        session_id=session.session_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=source_assistant_turn_id,
        expected_base_version_id=None,
        observation_fingerprint="d" * 64,
        request_fingerprint=ledger.source_fingerprint,
        ledger=ledger,
        generated=second_generated,
        universe=_universe("art"),
        snapshot_builder=snapshot_builder,
        city="北京",
        goal_occurrence_plan=occurrence_plan.model_dump(by_alias=True),
        max_route_anchors_by_day={1: 4, 2: 4},
        focus_brief_id=generated.proposals[1].brief.brief_id,
        existing_portfolio_id=first_portfolio.portfolio_id,
    )
    assert continued_portfolio.status == "awaiting_selection", (
        continued_portfolio.failure_reason,
        staging.last_staging_metrics,
    )
    assert len(appended_visible) == 1

    proposal_rows = _proposal_material(db_connection, first_portfolio.portfolio_id)
    assert len(proposal_rows) == 2
    ordered_proposal_ids = [item["id"] for item in proposal_rows]
    summary = store.summary(portfolio_id=first_portfolio.portfolio_id)
    assert summary["visibleProposalIds"] == ordered_proposal_ids
    assert summary["adoptionReadyProposalCount"] == 2
    for material in proposal_rows:
        _assert_complete_proposal(material)
        assert material["snapshot"]["routeDecisionContract"]["fingerprint"] == route_decision_contract["fingerprint"]
        decision_proofs = [
            (segment.get("semanticMetadata") or {}).get("routeInsertionMatrixProof")
            for day in material["snapshot"]["days"]
            for segment in day["segments"]
            if isinstance(
                (segment.get("semanticMetadata") or {}).get("routeInsertionMatrixProof"),
                dict,
            )
        ]
        assert decision_proofs
        assert all(proof["contractFingerprint"] == route_decision_contract["fingerprint"] for proof in decision_proofs)

    physical_sets = [_snapshot_amap_ids(material["snapshot"]) for material in proposal_rows]
    assert physical_sets[0].isdisjoint(physical_sets[1])
    titles = [str(material["snapshot"]["title"]) for material in proposal_rows]
    assert len(set(titles)) == 2
    theme_sets = [
        {
            (segment.get("semanticMetadata") or {}).get("optionalExperienceFamily")
            for day in material["snapshot"]["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("portfolioOptional")
        }
        for material in proposal_rows
    ]
    assert theme_sets == [{"heritage_walk"}, {"art_walk"}]
    novelty = proposal_rows[1]["lineage"]["paretoSelection"]["materialNoveltyAudit"]
    assert novelty["passed"] is True
    assert novelty["minimumDistance"] >= 0.40

    before_counts = [
        db_connection.execute(sql, args).fetchone()[0]
        for sql, args in (
            (
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session.session_id,),
            ),
            (
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session.session_id,),
            ),
            (
                "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
                (session.active_plan_id,),
            ),
        )
    ]
    assert before_counts == [0, 0, 0]

    projections = []
    choice_options = []
    for material in proposal_rows:
        readiness = ProposalReadinessService.compute(material["snapshot"], verifier=material["verifier"])
        projection = PlanComparisonPreviewService.project_snapshot(
            material["snapshot"],
            planning_selection_root_turn_id=source_user_turn_id,
            root_portfolio_id=first_portfolio.portfolio_id,
            proposal_id=material["id"],
            source_assistant_turn_id=source_assistant_turn_id,
            choice_id=material["choice_id"],
            active_version_id=None,
            expected_base_version_id=None,
            is_partial=False,
            is_adopted=False,
            adoption_ready=bool(readiness["adoptionReady"]),
            comparison_role="candidate_proposal",
            origin_projection_mode="full_proposal",
        )
        projections.append(projection)
        choice_options.append(
            {
                "id": material["choice_id"],
                "label": material["snapshot"]["title"],
                "value": material["snapshot"]["title"],
                "kind": "plan_proposal",
                "action": "select_plan_proposal",
                "proposalId": material["id"],
                "sourceUserTurnId": source_user_turn_id,
                "planningSelectionRootTurnId": source_user_turn_id,
                "rootPortfolioId": first_portfolio.portfolio_id,
                "expectedBaseVersionId": None,
                "comparisonProjection": projection,
            }
        )
    source_response = {
        "mode": "creative_portfolio",
        "reply": "已生成两个完整且可采用的方案，请选择。",
        "terminalStatus": "needs_confirmation",
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
        "portfolioId": first_portfolio.portfolio_id,
        "constraintLedger": ledger.model_dump(by_alias=True),
        "creativePortfolio": generated.model_dump(by_alias=True),
        "initialPlan": initial_plan.model_dump(by_alias=True),
        "choiceOptions": choice_options,
        "comparisonProjections": projections,
    }
    db_connection.execute(
        """UPDATE conversation_turns
        SET agent_request_json = ?, agent_response_json = ? WHERE id = ?""",
        (
            json.dumps(request_context, ensure_ascii=False),
            json.dumps(source_response, ensure_ascii=False),
            source_assistant_turn_id,
        ),
    )
    db_connection.commit()
    pre_adoption_state = AgentStateExporter(db_connection).export_session(session.session_id)

    second_choice = choice_options[1]
    committed = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="采用第二个方案",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "choiceId": second_choice["id"],
                }
            },
        ),
    )
    assert committed.version is not None
    assert committed.terminal_status == "success"
    adoption_event = next(item for item in committed.planning_steps if item.type == "proposal_adoption_committed")
    assert adoption_event.metadata["versionDelta"] == 1
    assert adoption_event.metadata["patchDelta"] == 1
    assert adoption_event.metadata["routeWriteDelta"] == 6
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        db_connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (session.active_plan_id,),
        ).fetchone()[0]
        == 6
    )
    lifecycle = {
        row["id"]: row["status"]
        for row in db_connection.execute(
            """SELECT id, status FROM agent_plan_proposals
            WHERE portfolio_id = ?""",
            (first_portfolio.portfolio_id,),
        ).fetchall()
    }
    assert lifecycle == {
        ordered_proposal_ids[0]: "comparison_only",
        ordered_proposal_ids[1]: "committed",
    }
    committed_state = AgentStateExporter(db_connection).export_session(session.session_id)

    replay = service.send_message(
        session.session_id,
        AgentMessageRequest(
            content="再次采用第二个方案",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "choiceId": second_choice["id"],
                }
            },
        ),
    )
    assert replay.version is not None
    assert replay.version.id == committed.version.id
    replay_apply = next(item for item in replay.planning_steps if item.type == "apply_patch")
    assert [
        replay_apply.metadata["versionDelta"],
        replay_apply.metadata["patchDelta"],
        replay_apply.metadata["routeWriteDelta"],
    ] == [0, 0, 0]
    duplicate_state = AgentStateExporter(db_connection).export_session(session.session_id)
    exported_state = AgentStateExporter(db_connection).export_state(session.session_id)
    artifact_root_value = os.environ.get("TRIP_DUAL_GOLDEN_ARTIFACT_ROOT")
    artifact_root = (
        Path(artifact_root_value).resolve()
        if artifact_root_value
        else tmp_path / "creative-portfolio-dual-adoption-artifact"
    )
    artifact_bundle = write_dual_adoption_artifact_bundle(
        artifact_root=artifact_root,
        repo_root=Path(__file__).resolve().parents[3],
        session_id=session.session_id,
        portfolio_id=first_portfolio.portfolio_id,
        selected_proposal_id=ordered_proposal_ids[1],
        turns=[
            {
                "label": "turn_1",
                "input": "北京两日高校游，每晚看夜景，并比较胡同文脉与当代艺术路线。",
                "context": request_context,
                "response": source_response,
                "state": pre_adoption_state,
                "agentPlan": initial_plan.model_dump(by_alias=True),
                "agentDecision": {"action": "ask_user", "reason": "two_complete_proposals"},
                "expectedDeltas": [0, 0, 0],
            },
            {
                "label": "turn_2",
                "input": "采用第二个方案",
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "choiceId": second_choice["id"],
                },
                "context": {
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_assistant_turn_id,
                        "choiceId": second_choice["id"],
                    }
                },
                "response": committed.model_dump(by_alias=True, exclude_none=True),
                "state": committed_state,
                "expectedDeltas": [1, 1, 6],
            },
            {
                "label": "turn_3_duplicate",
                "input": "再次采用第二个方案",
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_assistant_turn_id,
                    "choiceId": second_choice["id"],
                },
                "context": {
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_assistant_turn_id,
                        "choiceId": second_choice["id"],
                    }
                },
                "response": replay.model_dump(by_alias=True, exclude_none=True),
                "state": duplicate_state,
                "expectedDeltas": [0, 0, 0],
            },
        ],
        export_state=exported_state,
    )
    assert artifact_bundle["evaluation"]["passed"] is True

    refreshed = sqlite3.connect(
        sqlite_path_from_url(get_settings().database_url),
        check_same_thread=False,
    )
    refreshed.row_factory = sqlite3.Row
    try:
        refreshed_store = PlanPortfolioStore(refreshed)
        refreshed_summary = refreshed_store.summary(portfolio_id=first_portfolio.portfolio_id)
        assert refreshed_summary["visibleProposalIds"] == ordered_proposal_ids
        assert refreshed_summary["selectedProposalId"] == ordered_proposal_ids[1]
        refreshed_turn = AgentService(refreshed)._turn_response(committed.assistant_turn.id)
        assert [item["proposalId"] for item in refreshed_turn.comparison_projections] == ordered_proposal_ids
        metrics = evaluate_dual_adoption_golden(
            connection=refreshed,
            portfolio_id=first_portfolio.portfolio_id,
            session_id=session.session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            duplicate_assistant_turn_id=replay.assistant_turn.id,
            artifact_root=artifact_root,
        )
        if metrics["passed"] is not True:
            pytest.fail(
                json.dumps(metrics, ensure_ascii=False, sort_keys=True, default=str),
                pytrace=False,
            )
        contract_path = (
            Path(__file__).resolve().parents[2]
            / "evals"
            / "fixtures"
            / "creative_portfolio_dual_adoption_golden_contract.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))["dualAdoptionGoldenEval"]
        assert dual_adoption_golden_failures(metrics, contract) == []

        regressed_metrics = copy.deepcopy(metrics)
        regressed_metrics["duplicateVersionPatchRouteDelta"] = [1, 0, 0]
        assert (
            "dual-adoption duplicate version/patch/route delta differs from the golden contract"
            in dual_adoption_golden_failures(regressed_metrics, contract)
        )
        regressed_meals = copy.deepcopy(metrics)
        first_proposal_id = ordered_proposal_ids[0]
        regressed_meals["mealCountsByProposal"][first_proposal_id]["1"] = 0
        assert "dual-adoption daily meal coverage is not exact" in dual_adoption_golden_failures(
            regressed_meals, contract
        )

        stored_integrity = refreshed.execute(
            "SELECT canonical_signature, score_json FROM agent_plan_proposals WHERE id = ?",
            (first_proposal_id,),
        ).fetchone()
        refreshed.execute(
            "UPDATE agent_plan_proposals SET canonical_signature = ? WHERE id = ?",
            ("f" * 64, first_proposal_id),
        )
        refreshed.commit()
        tampered_signature = evaluate_dual_adoption_golden(
            connection=refreshed,
            portfolio_id=first_portfolio.portfolio_id,
            session_id=session.session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            duplicate_assistant_turn_id=replay.assistant_turn.id,
            artifact_root=artifact_root,
        )
        assert tampered_signature["passed"] is False
        assert tampered_signature["canonicalSignatureMismatchCount"] == 1
        refreshed.execute(
            "UPDATE agent_plan_proposals SET canonical_signature = ? WHERE id = ?",
            (stored_integrity["canonical_signature"], first_proposal_id),
        )
        refreshed.commit()

        score_payload = json.loads(stored_integrity["score_json"])
        score_payload["evidence"].pop("routeEfficiency")
        refreshed.execute(
            "UPDATE agent_plan_proposals SET score_json = ? WHERE id = ?",
            (json.dumps(score_payload, ensure_ascii=False), first_proposal_id),
        )
        refreshed.commit()
        tampered_score = evaluate_dual_adoption_golden(
            connection=refreshed,
            portfolio_id=first_portfolio.portfolio_id,
            session_id=session.session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            duplicate_assistant_turn_id=replay.assistant_turn.id,
            artifact_root=artifact_root,
        )
        assert tampered_score["passed"] is False
        assert tampered_score["scoreEvidenceMismatchCount"] == 1
        assert tampered_score["scoreEvidenceMissingCount"] == 1
        refreshed.execute(
            "UPDATE agent_plan_proposals SET score_json = ? WHERE id = ?",
            (stored_integrity["score_json"], first_proposal_id),
        )
        refreshed.commit()

        reply_tamper_root = tmp_path / "dual-artifact-reply-tamper"
        shutil.copytree(artifact_root, reply_tamper_root)
        reply_path = reply_tamper_root / "runs" / "turn_2" / "run_dual_turn_2" / "final_response.json"
        reply_payload = json.loads(reply_path.read_text(encoding="utf-8"))
        reply_payload["selectedSnapshotFingerprint"] = "0" * 64
        reply_path.write_text(
            json.dumps(reply_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        reply_tamper = evaluate_dual_adoption_artifact_bundle(reply_tamper_root)
        assert reply_tamper["passed"] is False
        assert "turn_2:final_response_snapshot_fingerprint_mismatch" in reply_tamper["failures"]

        event_tamper_root = tmp_path / "dual-artifact-event-tamper"
        shutil.copytree(artifact_root, event_tamper_root)
        events_path = (
            event_tamper_root / "runs" / "turn_3_duplicate" / "run_dual_turn_3_duplicate" / "tool_events.jsonl"
        )
        event_lines = events_path.read_text(encoding="utf-8").splitlines()
        event_payload = json.loads(event_lines[0])
        event_payload["bundleSequence"] = 999
        event_payload.pop("providerMetadata", None)
        event_lines[0] = json.dumps(event_payload, ensure_ascii=False)
        events_path.write_text("\n".join(event_lines) + "\n", encoding="utf-8")
        event_tamper = evaluate_dual_adoption_artifact_bundle(event_tamper_root)
        assert event_tamper["passed"] is False
        assert "bundle_event_sequence_not_contiguous" in event_tamper["failures"]
        assert "turn_3_duplicate:event_provider_metadata_missing" in event_tamper["failures"]
    finally:
        refreshed.close()
