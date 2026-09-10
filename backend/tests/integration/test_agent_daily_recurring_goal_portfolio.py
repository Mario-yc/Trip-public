"""Recorded Controller daily-goal golden for the Creative Portfolio data plane.

The fixture intentionally starts from the accepted V3 ``dayStrategies``.  It
proves daily multiplicity reaches occurrence compilation, deterministic
portfolio fallback, staging, and proposal verification without a backend
phrase matcher deciding the per-day scope.
"""
from __future__ import annotations

from collections import defaultdict

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.creative_portfolio_provider_service import CreativePortfolioProviderService
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.daily_capacity_planner import DailyCapacityPlanner
from src.services.goal_occurrence_compiler import GoalOccurrenceCompiler
from src.services.shared_candidate_universe_service import SharedCandidateUniverse


class _Store:
    def __init__(self) -> None:
        self.saved = None

    def create(self, portfolio, proposals, **_kwargs) -> None:
        self.saved = (portfolio, proposals)


def _candidate(amap_id: str, *, intent_type: str, score: float) -> dict:
    kinds = {
        "campus_visit": ("清华大学" if amap_id.endswith("1") else "北京大学", "科教文化服务;学校;高等院校"),
        "museum": ("中国国家博物馆" if amap_id.endswith("1") else "中国美术馆", "科教文化服务;博物馆;博物馆"),
        "meal": ("护国寺小吃" if amap_id.endswith("1") else "姚记炒肝店", "餐饮服务;中餐厅;北京菜"),
        "art_walk": ("北京艺术街区", "科教文化服务;文化场馆;艺术中心"),
        "heritage_walk": ("北京历史文化街区", "风景名胜;特色街区;历史文化街区"),
        "local_life": ("北京社区文化市场", "购物服务;综合市场;社区市场"),
        "market_walk": ("北京传统市集", "购物服务;综合市场;农贸市场"),
        "park": ("北京城市公园", "风景名胜;公园广场;城市公园"),
    }
    name, provider_type = kinds.get(intent_type, ("北京历史街区", "风景名胜;特色街区"))
    type_codes = {
        "campus_visit": "141201",
        "museum": "140100",
        "meal": "050100",
        "art_walk": "140500",
        "heritage_walk": "110200",
        "local_life": "060703",
        "market_walk": "060703",
        "park": "110101",
    }
    claim_keys = {
        "meal": "local_food",
        "art_walk": "art_walk",
        "heritage_walk": "heritage_walk",
        "local_life": "local_life",
        "market_walk": "market_walk",
    }
    claim_key = claim_keys.get(intent_type)
    return {
        "amapId": amap_id,
        "id": amap_id,
        "name": name,
        "city": "北京",
        "type": provider_type,
        "providerType": provider_type,
        "providerTypeCode": type_codes.get(intent_type, "110200"),
        "tags": provider_type.split(";"),
        "category": intent_type,
        "longitude": 116.30 + score / 1000,
        "latitude": 39.90 + score / 1000,
        "source": "amap-place-search",
        "confidence": 0.95,
        "semanticPassed": True,
        "candidateScore": score,
        "localRouteScore": score,
        "sourceClaims": (
            [
                {
                    "claimKey": claim_key,
                    "stance": "support",
                    "summary": f"录制夹具已核验 {name} 与 {claim_key} 体验相符",
                    "sourceName": f"recorded-fixture-{amap_id}",
                    "sourceUrlHash": (amap_id[-1:].lower() or "a") * 64,
                }
            ]
            if claim_key
            else []
        ),
    }


def _recorded_directive() -> dict:
    return {
        "type": "draft_itinerary",
        "goalPriority": ["goal_campus", "goal_museum", "goal_meal"],
        "dayStrategies": [
            {
                "dayNumber": day,
                "theme": "recorded daily cultural route",
                "requiredGoalIds": ["goal_campus", "goal_museum"],
                "requiredGoalCounts": {"goal_campus": 1, "goal_museum": 1},
                "optionalGoalIds": ["goal_meal"],
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


def _snapshot_builder(required: dict, soft: dict, optional: list, brief_id: str) -> dict:
    by_day: dict[int, list[dict]] = defaultdict(list)
    for candidate in [*required.values(), *soft.values(), *optional]:
        goal_id = candidate.get("sourceGoalId") or candidate.get("goalId") or candidate.get("softGoalId")
        occurrence_id = candidate.get("occurrenceId") or (f"occ:{goal_id}:day:{candidate['dayNumber']}" if goal_id else "")
        is_optional = not bool(goal_id)
        start_time = str(candidate.get("startTime") or {"morning": "09:00", "lunch": "12:00", "afternoon": "14:00", "evening": "18:00"}.get(str(candidate.get("timeWindow") or ""), "14:00"))
        start_hour, start_minute = (int(value) for value in start_time.split(":"))
        end_minutes = start_hour * 60 + start_minute + int(candidate.get("durationMinutes") or 60)
        end_time = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        semantic = {
            "goalId": goal_id,
            "sourceGoalId": goal_id,
            "occurrenceId": occurrence_id,
            "intentType": candidate.get("intentType") or "area_walk",
            "poolId": candidate["poolId"],
            "planningSlotId": candidate["planningSlotId"],
            "creativeBriefId": candidate["briefId"],
            "required": candidate.get("requirementLevel") == "hard",
            "routeAnchor": True,
            "groundingStatus": "selected",
            "portfolioOptional": is_optional,
            "optionalExperienceFamily": candidate.get("optionalExperienceFamily") or "",
            "dayRole": candidate.get("dayRole") or "",
        }
        by_day[int(candidate["dayNumber"])].append(
            {
                "startTime": start_time,
                "endTime": end_time,
                "kind": candidate.get("kind") or "visit",
                "poi": candidate,
                "semanticMetadata": semantic,
            }
        )
    for segments in by_day.values():
        segments.sort(key=lambda segment: segment["startTime"])
        previous_end = 0
        for segment in segments:
            hour, minute = (int(value) for value in segment["startTime"].split(":"))
            start_minutes = max(hour * 60 + minute, previous_end)
            end_hour, end_minute = (int(value) for value in segment["endTime"].split(":"))
            duration = end_hour * 60 + end_minute - (hour * 60 + minute)
            previous_end = start_minutes + duration
            segment["startTime"] = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
            segment["endTime"] = f"{previous_end // 60:02d}:{previous_end % 60:02d}"

    return {
        "id": "plan_recorded_daily",
        "title": brief_id,
        "creativeBrief": {"briefId": brief_id},
        "days": [
            {"dayNumber": day, "segments": segments}
            for day, segments in sorted(by_day.items())
        ],
    }


def test_recorded_daily_strategies_preserve_all_occurrences_and_distinct_identities():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {"goalId": "goal_campus", "intentType": "campus_visit", "requiredMin": 1},
                    {"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1},
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "requiredMin": 1,
                        "requirementLevel": "soft_experience",
                    },
                ],
            },
        }
    )
    directive = _recorded_directive()
    occurrence_plan = GoalOccurrenceCompiler().compile(ledger, directive)
    capacity = DailyCapacityPlanner().plan(occurrence_plan, pace=ledger.pace, day_count=2)
    generated = CreativePortfolioProviderService().deterministic_fallback(
        ledger=ledger,
        directive=directive,
        schema_repair_attempts=0,
        occurrence_plan=occurrence_plan,
        daily_capacity=capacity,
    )
    store = _Store()
    service = CreativePortfolioStagingService(store)
    universe = SharedCandidateUniverse(
        {
            "campus_visit": [_candidate("campus-1", intent_type="campus_visit", score=1), _candidate("campus-2", intent_type="campus_visit", score=2)],
            "museum": [_candidate("museum-1", intent_type="museum", score=1), _candidate("museum-2", intent_type="museum", score=2)],
            "meal": [_candidate("meal-1", intent_type="meal", score=1), _candidate("meal-2", intent_type="meal", score=2)],
            "area_walk": [_candidate("walk-1", intent_type="area_walk", score=1), _candidate("walk-2", intent_type="area_walk", score=2)],
            "art_walk": [_candidate("art-1", intent_type="art_walk", score=1), _candidate("art-2", intent_type="art_walk", score=2)],
            "heritage_walk": [_candidate("heritage-1", intent_type="heritage_walk", score=1), _candidate("heritage-2", intent_type="heritage_walk", score=2)],
            "local_life": [_candidate("local-1", intent_type="local_life", score=1), _candidate("local-2", intent_type="local_life", score=2)],
            "market_walk": [_candidate("market-1", intent_type="market_walk", score=1), _candidate("market-2", intent_type="market_walk", score=2)],
            "park": [_candidate("park-1", intent_type="park", score=1), _candidate("park-2", intent_type="park", score=2)],
        },
        6,
        0,
    )

    portfolio, visible = service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=_snapshot_builder,
        goal_occurrence_plan=occurrence_plan.model_dump(by_alias=True),
    )

    assert portfolio.status == "awaiting_selection", portfolio.failure_reason
    assert store.saved is not None
    assert visible
    assert len(occurrence_plan.occurrences) == 6
    assert all(item.source == "controller_day_strategy" for item in occurrence_plan.occurrences)
    for proposal in visible:
        verifier = proposal.verifier
        assert verifier["dayAnchorActuals"] == verifier["dayAnchorTargets"]
        assert verifier["hardFailures"] == []
        occurrence_segments = [
            segment
            for day in proposal.itinerary_snapshot["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("sourceGoalId")
        ]
        assert len(occurrence_segments) == 6
        assert all((segment["semanticMetadata"] or {}).get("occurrenceId") for segment in occurrence_segments)
        assert all(segment["endTime"] > segment["startTime"] for segment in occurrence_segments)
        by_goal_day = {
            (
                segment["semanticMetadata"]["sourceGoalId"],
                day["dayNumber"],
            ): segment
            for day in proposal.itinerary_snapshot["days"]
            for segment in day["segments"]
            if segment["semanticMetadata"].get("sourceGoalId")
        }
        assert set(by_goal_day) == {
            ("goal_campus", 1), ("goal_campus", 2),
            ("goal_museum", 1), ("goal_museum", 2),
            ("goal_meal", 1), ("goal_meal", 2),
        }
        assert by_goal_day[("goal_campus", 1)]["poi"]["amapId"] != by_goal_day[("goal_campus", 2)]["poi"]["amapId"]
        assert by_goal_day[("goal_museum", 1)]["poi"]["amapId"] != by_goal_day[("goal_museum", 2)]["poi"]["amapId"]
