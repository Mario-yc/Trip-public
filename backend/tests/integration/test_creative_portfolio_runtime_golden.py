from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from src.core.config import get_settings
from src.core.schema import initialize_database
from src.providers.travel_tools import (
    ResilientWebSearchProvider,
    WebSearchItem,
    WebSearchResponse,
)
from src.runtime.agent_runtime import TripAgentRuntime, clear_map_poi_runtime_state
from src.runtime.runtime_models import RuntimeRunOptions
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector
from src.services.creative_planning_models import PlanCandidate
from src.services.portfolio_route_feasibility_service import (
    PortfolioRouteFeasibilityResult,
    PortfolioRouteFeasibilityService,
)
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService
from src.services.route_service import RouteService
from evals.creative_portfolio_evaluator import evaluate_round2


TURN_1 = (
    "今年国庆参观985大学两日游，然后去体验下北京当地博物馆陶冶情操。"
    "10月1日到2日，2天，中等预算，1人，公交地铁优先。"
    "绕行最多35分钟，绕行比例最多35%。"
    "路途中能品尝北京当地特色美食。"
)

PARTIAL_TURN = (
    "今年国庆参观北京高校两日游，每晚都看北京夜景。"
    "10月1日到2日，中等预算，1人，公交地铁优先。"
    "绕行最多35分钟，绕行比例最多35%。"
    "每天午餐想体验当地特色美食。"
)


def test_recorded_public_agent_publishes_partial_after_exact_soft_meal_repair(
    monkeypatch,
    tmp_path,
):
    """Prefer any verified full proposal; otherwise publish a recoverable partial."""

    def recorded_route_prepare(self, snapshot, **_kwargs):
        work = copy.deepcopy(snapshot)
        meal = next(
            (
                (day, segment)
                for day in work.get("days") or []
                if isinstance(day, dict)
                for segment in day.get("segments") or []
                if isinstance(segment, dict) and str(segment.get("kind") or "") == "meal"
            ),
            None,
        )
        if meal is None:
            work["portfolioRouteQuality"] = {
                "status": "passed",
                "routeQualityIssues": [],
            }
            work["portfolioRouteEvidence"] = []
            work["routeEvidence"] = []
            return PortfolioRouteFeasibilityResult(
                status="passed",
                snapshot=work,
                provider_state="ok",
            )
        day, segment = meal
        semantic = segment.get("semanticMetadata") or {}
        issue = {
            "code": "meal_detour_high",
            "failureCode": "meal_detour_high",
            "dayNumber": day.get("dayNumber"),
            "mealSegmentId": segment.get("id"),
            "mealPlanningSlotId": semantic.get("planningSlotId"),
            "mealPoolId": semantic.get("poolId"),
            "mealBriefId": semantic.get("creativeBriefId"),
            "mealAmapId": (segment.get("poi") or {}).get("amapId"),
            "routePair": {
                "fromSegmentId": segment.get("id"),
                "toSegmentId": segment.get("id"),
            },
        }
        work["portfolioRouteQuality"] = {
            "status": "failed",
            "routeQualityIssues": [issue],
        }
        return PortfolioRouteFeasibilityResult(
            status="failed",
            snapshot=work,
            route_quality_issues=[issue],
            requires_route_verification=True,
            provider_state="ok",
        )

    def recorded_local_routes(
        self,
        _day_id,
        _pois,
        _transport_mode="transit",
        *,
        segments=None,
        **_kwargs,
    ):
        scoped_segments = list(segments or [])
        return [
            SimpleNamespace(
                from_segment_id=left.id,
                to_segment_id=right.id,
                distance_meters=800,
                duration_seconds=600,
                mode="transit",
                provider="amap-webservice-recorded",
                is_selected=True,
            )
            for left, right in zip(scoped_segments, scoped_segments[1:])
        ]

    web_queries: list[str] = []

    def recorded_web_search(
        _provider,
        query: str,
        count: int = 5,
        freshness: str = "noLimit",
    ) -> WebSearchResponse:
        del count, freshness
        web_queries.append(query)
        if any(marker in query for marker in ("夜景", "观景", "夜游")):
            items = [
                WebSearchItem(
                    title=f"{entity}夜景观赏指南",
                    url=f"https://recorded.example/night-view/{index}",
                    snippet=f"{entity}是北京夜景观赏和城市天际线拍摄地点。",
                    source_name="recorded-official-guide",
                    provider_name="recorded-web",
                    confidence=0.9,
                    credibility_rank="official",
                )
                for index, entity in enumerate(
                    ("中央广播电视塔", "奥林匹克塔", "中信大厦", "亮马河夜游步道"),
                    start=1,
                )
            ]
        else:
            items = [
                WebSearchItem(
                    title="便宜坊老字号烤鸭店 - 北京餐饮指南",
                    url="https://recorded.example/local-food",
                    snippet="便宜坊以北京烤鸭为特色菜，是可核验的北京当地特色餐厅。",
                    source_name="recorded-official-guide",
                    provider_name="recorded-web",
                    confidence=0.9,
                    credibility_rank="official",
                )
            ]
        return WebSearchResponse(
            query=query,
            provider_name="recorded-web",
            confidence=0.9,
            results=items,
        )

    database_path = tmp_path / "partial-public.sqlite3"
    with monkeypatch.context() as scoped:
        scoped.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
        scoped.setenv("PROVIDER_MODE", "mock")
        scoped.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
        scoped.setattr(
            PortfolioRouteFeasibilityService,
            "prepare",
            recorded_route_prepare,
        )
        scoped.setattr(RouteService, "build_routes", recorded_local_routes)
        scoped.setattr(
            ResilientWebSearchProvider,
            "search",
            recorded_web_search,
        )
        get_settings.cache_clear()
        clear_map_poi_runtime_state()
        initialize_database()
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            runtime = TripAgentRuntime(connection)
            result, exit_code = runtime.run_once(
                RuntimeRunOptions(
                    input=PARTIAL_TURN,
                    city="北京",
                    stateDir=str(tmp_path / "partial-public-state"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider={
                        "enabled": True,
                        "trustedPoiFixtures": True,
                        "forceDominantCandidates": True,
                    },
                ),
                argv=["trip-agent", "run", "--partial-public-regression"],
            )
            assert exit_code == 3, result.warnings
            if not result.active_version_id:
                session = connection.execute(
                    "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
                    (result.session_id,),
                ).fetchone()
                assert session["active_version_id"] is None
                assistant = connection.execute(
                    """SELECT agent_response_json FROM conversation_turns
                    WHERE session_id = ? AND role = 'assistant'
                    ORDER BY turn_index DESC LIMIT 1""",
                    (result.session_id,),
                ).fetchone()
                payload = json.loads(assistant["agent_response_json"])
                proposal_options = [
                    option
                    for option in payload.get("choiceOptions") or []
                    if option.get("kind") in {"plan_proposal", "portfolio_comparison_readonly"}
                    and isinstance(option.get("comparisonProjection"), dict)
                ]
                portfolio_row = connection.execute(
                    """SELECT status, failure_reason, summary_json
                    FROM agent_plan_portfolios WHERE session_id = ?
                    ORDER BY created_at DESC LIMIT 1""",
                    (result.session_id,),
                ).fetchone()
                portfolio_summary = (
                    json.loads(portfolio_row["summary_json"] or "{}")
                    if portfolio_row is not None
                    else {}
                )
                assert proposal_options, {
                    "terminalStatus": payload.get("terminalStatus"),
                    "webQueries": web_queries,
                    "portfolio": {
                        "status": portfolio_row["status"] if portfolio_row else None,
                        "failureReason": (
                            portfolio_row["failure_reason"] if portfolio_row else None
                        ),
                        "proposalCount": portfolio_summary.get("proposalCount"),
                        "visibleProposalIds": portfolio_summary.get(
                            "visibleProposalIds"
                        ),
                        "candidateGapSummary": portfolio_summary.get(
                            "candidateGapSummary"
                        ),
                        "stageReasonCodes": portfolio_summary.get(
                            "stageReasonCodes"
                        ),
                        "rejectedProposalIds": portfolio_summary.get(
                            "rejectedProposalIds"
                        ),
                    },
                    "choiceOptions": [
                        {
                            "kind": option.get("kind"),
                            "action": option.get("action"),
                            "hasComparisonProjection": isinstance(option.get("comparisonProjection"), dict),
                        }
                        for option in payload.get("choiceOptions") or []
                        if isinstance(option, dict)
                    ],
                    "failedSteps": [
                        {
                            "type": step.get("type"),
                            "failureReason": step.get("failureReason"),
                        }
                        for step in payload.get("planningSteps") or []
                        if isinstance(step, dict) and step.get("failureReason")
                    ],
                }
                projection = proposal_options[0]["comparisonProjection"]
                grounded_night_segments = [
                    (int(day.get("dayNumber") or 0), segment)
                    for day in projection.get("days") or []
                    for segment in day.get("segments") or []
                    if (segment.get("semanticMetadata") or {}).get("intentType") == "night_view"
                    and (segment.get("poi") or {}).get("amapId")
                ]
                assert len(grounded_night_segments) == 2
                assert {day_number for day_number, _segment in grounded_night_segments} == {1, 2}
                assert all(
                    (segment.get("poi") or {}).get("source") == "amap-place-search"
                    for _day_number, segment in grounded_night_segments
                )
                canonical_night_ids = {
                    PoiPhysicalIdentityService.canonical_amap_id(segment.get("poi") or {})
                    for _day_number, segment in grounded_night_segments
                }
                assert "" not in canonical_night_ids
                assert len(canonical_night_ids) == 2
                for day_number, segment in grounded_night_segments:
                    semantic = segment.get("semanticMetadata") or {}
                    admission = semantic.get("consumerAdmissionReport") or {}
                    consumer_scope = admission.get("consumerScope") or {}
                    assert semantic.get("sourceGoalId") == "goal_night_view"
                    assert semantic.get("occurrenceId") == f"occ:goal_night_view:day:{day_number}"
                    assert semantic.get("poolId") and semantic.get("planningSlotId")
                    assert admission.get("classification") == "admitted_final_anchor"
                    assert admission.get("hardGatePassed") is True
                    assert admission.get("evidenceSufficient") is True
                    assert admission.get("scoreEligible") is True
                    assert consumer_scope.get("briefId") == semantic.get("creativeBriefId")
                    assert consumer_scope.get("poolId") == semantic.get("poolId")
                    assert consumer_scope.get("planningSlotId") == semantic.get("planningSlotId")
                    assert consumer_scope.get("dayNumber") == day_number
                assert projection["pendingHardSlotCount"] == 0
                assert not any(
                    slot.get("intentType") == "night_view"
                    and slot.get("requirementLevel") in {"hard", "required"}
                    for slot in projection.get("pendingSlots") or []
                )
                # The injected meal route failure must remain a truthful, non-adoptable
                # partial rather than turning valid every-night coverage into a hard gap.
                assert projection["isPartial"] is True
                assert projection["adoptionReady"] is False
                assert projection["strictlyVerified"] is False
                assert projection["routeExpectedLegCount"] > projection["routeVerifiedLegCount"]
                assert projection["routeStatus"] == "route_pending"
                assert (
                    payload["versionDelta"],
                    payload["patchDelta"],
                    payload["routeWriteDelta"],
                ) == (0, 0, 0)
                assert "portfolio_partial_anchor_grounding_evidence_missing" not in json.dumps(
                    payload,
                    ensure_ascii=False,
                )
                assert "goal_occurrence_identity_reused" not in json.dumps(
                    payload,
                    ensure_ascii=False,
                )
                return
            assert result.active_version_id
            assert result.status == "needs_confirmation"
            session = connection.execute(
                "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
                (result.session_id,),
            ).fetchone()
            assert session["active_version_id"] == result.active_version_id
            version = connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (result.active_version_id,),
            ).fetchone()
            snapshot = json.loads(version["snapshot_json"])
            anchors = [
                segment
                for day in snapshot.get("days") or []
                for segment in day.get("segments") or []
                if (segment.get("semanticMetadata") or {}).get("routeAnchor")
            ]
            assert anchors
            assert all(
                (segment.get("poi") or {}).get("source") == "amap-place-search"
                and (segment.get("poi") or {}).get("amapId")
                for segment in anchors
            )
            pending = snapshot.get("portfolioPendingSlots") or []
            assert pending
            assert all(
                item.get("briefId") and item.get("poolId") and item.get("planningSlotId") and item.get("dayNumber")
                for item in pending
            )
            assistant = connection.execute(
                """SELECT id, agent_response_json FROM conversation_turns
                WHERE session_id = ? AND role = 'assistant'
                ORDER BY turn_index DESC LIMIT 1""",
                (result.session_id,),
            ).fetchone()
            payload = json.loads(assistant["agent_response_json"])
            assert payload["versionDelta"] == 1
            assert payload["patchDelta"] == 1
            assert payload["routeWriteDelta"] == 0
            assert "portfolio_partial_anchor_grounding_evidence_missing" not in json.dumps(
                payload,
                ensure_ascii=False,
            )
            assert "goal_occurrence_identity_reused" not in json.dumps(
                payload,
                ensure_ascii=False,
            )
            fallback_decisions = [
                step
                for step in payload.get("planningSteps") or []
                if isinstance(step, dict) and step.get("type") == "agent_decision" and step.get("fallbackUsed")
            ]
            assert all(step.get("failureReason") != "controller_unavailable" for step in fallback_decisions)
            executable = [
                option
                for option in payload.get("choiceOptions") or []
                if option.get("kind") == "portfolio_partial_more_plans"
            ]
            assert len(executable) == 1
            assert executable[0]["action"] == "retry_model_planning"
            assert executable[0]["expectedBaseVersionId"] == result.active_version_id
            assert executable[0]["id"]
            assert all(
                option.get("expectedBaseVersionId") == result.active_version_id
                and option.get("planningSelectionRootTurnId")
                and option.get("rootPortfolioId")
                and option.get("requestContractFingerprint")
                for option in executable
            )
            night_view_count = sum(
                str((segment.get("semanticMetadata") or {}).get("sourceGoalId") or "") == "goal_night_view"
                for day in snapshot.get("days") or []
                for segment in day.get("segments") or []
                if isinstance(segment, dict)
            ) + sum(
                str(item.get("sourceGoalId") or "") == "goal_night_view" for item in pending if isinstance(item, dict)
            )
            assert night_view_count == 2
            print(
                "TRIP_PUBLIC_PARTIAL_STABILITY_METRICS="
                + json.dumps(
                    {
                        "timelineCreated": bool(result.active_version_id),
                        "partialTimelineCreated": snapshot.get("status") == "partial",
                        "structuredChoiceDispatched": True,
                        "continuationCapabilityEmitted": True,
                        "initialWriteDelta": [1, 1, 0],
                        "partialTimelineMissingFailureCount": 0,
                        "scopeDriftCount": 0,
                        "fakeOrNonAmapAnchorCount": sum(
                            not (
                                (segment.get("poi") or {}).get("source") == "amap-place-search"
                                and (segment.get("poi") or {}).get("amapId")
                                and (segment.get("poi") or {}).get("longitude") is not None
                                and (segment.get("poi") or {}).get("latitude") is not None
                            )
                            for segment in anchors
                        ),
                        "cardinalityMismatchCount": int(night_view_count != 2),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        finally:
            connection.close()
            get_settings.cache_clear()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_artifact_has_no_local_absolute_paths(artifact: Path) -> None:
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)

    for path in artifact.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        payloads = (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else [json.loads(path.read_text(encoding="utf-8"))]
        )
        for payload in payloads:
            assert not any(re.match(r"(?i)(?:^[a-z]:[\\/]|^\\\\)", item) for item in strings(payload)), path.name


def _itinerary_identity(snapshot: dict) -> list[tuple]:
    return [
        (
            day.get("dayNumber"),
            segment.get("startTime"),
            segment.get("endTime"),
            (segment.get("poi") or {}).get("amapId"),
            (segment.get("semanticMetadata") or {}).get("goalId"),
            (segment.get("semanticMetadata") or {}).get("intentType"),
        )
        for day in snapshot.get("days") or []
        for segment in day.get("segments") or []
    ]


def _itinerary_content_identity(snapshot: dict) -> list[tuple]:
    return [
        (day_number, amap_id, goal_id, intent_type)
        for day_number, _start_time, _end_time, amap_id, goal_id, intent_type in _itinerary_identity(snapshot)
    ]


def _route_anchor_counts(snapshot: dict) -> dict[int, int]:
    return {
        int(day.get("dayNumber") or 0): sum(
            1 for segment in day.get("segments") or [] if (segment.get("semanticMetadata") or {}).get("routeAnchor")
        )
        for day in snapshot.get("days") or []
    }


def _selected_route_metrics(snapshot: dict) -> tuple[int, int, float]:
    selected = [route for route in snapshot.get("routeOptions") or [] if route.get("isSelected")]
    return (
        len({(route.get("fromSegmentId"), route.get("toSegmentId")) for route in selected}),
        sum(int(route.get("durationMinutes") or 0) for route in selected),
        sum(float(route.get("distanceMeters") or 0) for route in selected),
    )


def test_creative_portfolio_runtime_turn_one_emits_proposal_artifacts(monkeypatch, tmp_path):
    def recorded_985_campus_candidates(_runtime, _city, _keyword):
        return [
            ("北京大学", "科教文化服务;学校;高等院校", "education"),
            ("清华大学", "科教文化服务;学校;高等院校", "education"),
        ]

    evidence_root = Path(os.environ.get("TRIP_CREATIVE_GOLDEN_ROOT") or tmp_path)
    evidence_root.mkdir(parents=True, exist_ok=True)
    database_path = evidence_root / "creative-runtime.sqlite3"
    state_dir = evidence_root / "runs"
    with monkeypatch.context() as scoped:
        scoped.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
        scoped.setenv("PROVIDER_MODE", "mock")
        scoped.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
        scoped.setattr(
            TripAgentRuntime,
            "_mock_city_campus_candidates",
            recorded_985_campus_candidates,
        )
        get_settings.cache_clear()
        clear_map_poi_runtime_state()
        initialize_database()
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            runtime = TripAgentRuntime(connection)
            result, exit_code = runtime.run_once(
                RuntimeRunOptions(
                    input=TURN_1,
                    city="北京",
                    stateDir=str(state_dir / "turn_1"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider={
                        "enabled": True,
                        "trustedPoiFixtures": True,
                        "mockRouteRefresh": True,
                        "forceDominantCandidates": True,
                        "exactPoiFixtures": [
                            {
                                "city": "北京",
                                "name": "海棠风味馆",
                                "type": "餐饮服务;中餐厅;地方菜",
                                "category": "food",
                                "matchKeywords": [
                                    "当地特色美食",
                                    "地方风味餐厅",
                                    "北京本地菜餐厅",
                                    "北京特色小吃馆",
                                    "北京老字号餐厅",
                                ],
                            }
                        ],
                    },
                ),
                argv=["trip-agent", "run", "--creative-portfolio-golden", "--turn", "1"],
            )
            assert exit_code == 3, result.warnings
            assert result.status == "needs_confirmation"
            assert result.active_version_id is None
            artifact = Path(result.artifact_path_absolute)
            for filename in (
                "portfolio.json",
                "plan_proposals.jsonl",
                "portfolio_scores.jsonl",
                "portfolio_verifier.jsonl",
                "portfolio_selection.json",
            ):
                assert (artifact / filename).exists(), filename
            portfolio = json.loads((artifact / "portfolio.json").read_text(encoding="utf-8"))
            final_response = json.loads((artifact / "final_response.json").read_text(encoding="utf-8"))
            assert final_response["artifactPath"].startswith("artifact://run_")
            assert "artifactPathAbsolute" not in final_response
            assert str(tmp_path) not in (artifact / "final_response.json").read_text(encoding="utf-8")
            _assert_artifact_has_no_local_absolute_paths(artifact)
            proposals = _jsonl(artifact / "plan_proposals.jsonl")
            selection = json.loads((artifact / "portfolio_selection.json").read_text(encoding="utf-8"))
            assert portfolio["status"] == "awaiting_selection"
            assert len(proposals) == 1
            assert all((proposal["verifier_json"] or {}).get("passed") is False for proposal in proposals)
            assert all((proposal["verifier_json"] or {}).get("draftPassed") is True for proposal in proposals)
            assert all(not (proposal["verifier_json"] or {}).get("hardFailures") for proposal in proposals)
            for proposal in proposals:
                snapshot = proposal["snapshot_json"] or {}
                removed_ids = set(
                    (snapshot.get("portfolioPartialProjection") or {}).get(
                        "removedSegmentIds"
                    )
                    or []
                )
                assert removed_ids
                assert snapshot.get("portfolioVerifier") == proposal["verifier_json"]
                assert not any(
                    removed_id in str(failure)
                    for failure in (proposal["verifier_json"] or {}).get("hardFailures")
                    or []
                    for removed_id in removed_ids
                )
            assert all(
                (proposal["verifier_json"] or {}).get("pendingHardSlotCount") == 0
                and (proposal["verifier_json"] or {}).get("pendingSoftSlotCount", 0) > 0
                for proposal in proposals
            )
            portfolio_summary = portfolio["summary_json"] or {}
            visible_ids = set(portfolio_summary.get("visibleProposalIds") or [])
            visible = [proposal for proposal in proposals if proposal["id"] in visible_ids]
            assert len(visible) == 1
            brief_generation_state = portfolio_summary["briefGenerationState"]
            assert len(brief_generation_state) == 4
            brief_ids = [item["briefId"] for item in brief_generation_state]
            assert all(isinstance(brief_id, str) and brief_id for brief_id in brief_ids)
            assert len(set(brief_ids)) == len(brief_ids)
            assert [item["order"] for item in brief_generation_state] == list(range(4))
            assert [item["status"] for item in brief_generation_state] == [
                "completed",
                "remaining",
                "remaining",
                "remaining",
            ]
            assert portfolio_summary["completedBriefIds"] == brief_ids[:1]
            assert portfolio_summary["remainingBriefIds"] == brief_ids[1:]
            assert portfolio_summary["failedBriefIds"] == []
            assert portfolio_summary["focusBriefId"] == brief_ids[0]
            assert portfolio_summary["nextBriefId"] == brief_ids[1]
            frontier = portfolio_summary["creativeExplorationFrontier"]
            assert frontier["currentFocusBriefId"] == brief_ids[0]
            assert frontier["nextBriefId"] == brief_ids[1]
            visible_candidates = [
                PlanCandidate.model_validate(
                    {
                        "proposalId": proposal["id"],
                        "portfolioId": proposal["portfolio_id"],
                        "brief": proposal["brief_json"],
                        "itinerarySnapshot": proposal["snapshot_json"],
                        "groundedEvidence": proposal["evidence_json"].get("groundedEvidence") or [],
                        "score": proposal["score_json"],
                        "verifier": proposal["verifier_json"],
                        "canonicalSignature": proposal["canonical_signature"],
                        "generationLineage": proposal["generation_lineage_json"],
                    }
                )
                for proposal in visible
            ]
            assert ParetoPortfolioSelector().min_pairwise_distance(visible_candidates) >= 0.25
            assert all(
                all((proposal["verifier_json"] or {}).get("requiredGoalCoverage", {}).values()) for proposal in visible
            )
            assert selection["versionDelta"] == 0
            assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0
            metrics = evaluate_round2(connection=connection, artifact=artifact, portfolio_id=portfolio["id"])
            assert metrics["portfolioGenerationCallCount"] == 1
            assert metrics["portfolioGeneratedProposalCount"] == 1
            assert metrics["portfolioVisibleProposalCount"] == 1
            assert metrics["briefDrivenProposalCount"] == 0
            assert metrics["visibleDistinctExperienceFamilySetCount"] == 1
            assert metrics["visibleDistinctDayRoleSignatureCount"] == 1
            assert metrics["minPairwiseStructuralDistanceWithoutAxis"] == 1.0
            assert metrics["scoreEvidenceMissingCount"] == metrics["nonEvidenceScoreFieldCount"] == 0
            assert metrics["repairAttemptCount"] == 0
            assert metrics["repairExternalCallCount"] == metrics["repairTouchedRequiredSegmentCount"] == 0
            assert (
                metrics["routeCoverageFailureCount"]
                == metrics["pacingViolationCount"]
                == metrics["hardBudgetViolationCount"]
                == 0
            )

            assistant = connection.execute(
                """SELECT id, agent_request_json, agent_response_json FROM conversation_turns
                WHERE session_id = ? AND role = 'assistant' ORDER BY turn_index DESC LIMIT 1""",
                (result.session_id,),
            ).fetchone()
            request_envelope = json.loads(assistant["agent_request_json"])
            request_intent_contract = request_envelope["requestIntentContract"]
            route_decision_contract = request_intent_contract["routeDecisionContract"]
            assert route_decision_contract["status"] == "ready"
            assert route_decision_contract["missingFields"] == []
            assert route_decision_contract["detourTolerance"] == {
                "maxGeneralizedCostDelta": 35.0,
                "maxDetourRatio": 0.35,
            }
            response_payload = json.loads(assistant["agent_response_json"])
            assert response_payload["initialPlan"]["mode"] == "initial_plan"
            assert response_payload["pipelineContext"]["requestIntentContract"]
            assert response_payload["pipelineContext"]["resolvedTripDates"]
            choice_options = [
                item
                for item in response_payload["choiceOptions"]
                if item.get("action") == "select_plan_proposal"
            ]
            assert len(choice_options) == 1
            assert "可编辑草案" in choice_options[0]["description"]
            assert "待补体验" in choice_options[0]["description"]
            assert all(
                all(
                    int((proposal["verifier_json"] or {}).get("dayAnchorActuals", {}).get(day, 0)) <= int(target)
                    for day, target in (proposal["verifier_json"] or {}).get("dayAnchorTargets", {}).items()
                )
                for proposal in visible
            )
            target_signatures = {
                tuple(sorted((proposal["verifier_json"] or {}).get("dayAnchorTargets", {}).items()))
                for proposal in visible
            }
            assert len(target_signatures) == 1
            assert all(
                sum((proposal["verifier_json"] or {}).get("dayAnchorTargets", {}).values()) >= 5 for proposal in visible
            )
            assert all(
                max((proposal["verifier_json"] or {}).get("dayAnchorTargets", {}).values()) >= 3 for proposal in visible
            )
            for proposal in visible:
                lineage = (proposal["generation_lineage_json"] or {}).get("briefPlanningProjection") or {}
                density_decision_source = lineage.get("densityDecisionSource")
                assert density_decision_source == "deterministic_fallback"
                assert lineage.get("transportPreference") == "public_transit"
                assert (proposal["score_json"].get("evidence") or {}).get("densityDecisionSource") == [
                    density_decision_source
                ]
                pareto = (proposal["generation_lineage_json"] or {}).get("paretoSelection") or {}
                assert pareto == {}
                partial_timeline = (
                    (proposal["generation_lineage_json"] or {}).get("partialTimeline")
                    or {}
                )
                partial_projection = (
                    (proposal["generation_lineage_json"] or {}).get(
                        "partialProjection"
                    )
                    or {}
                )
                assert partial_timeline.get("eligible") is True
                assert partial_projection.get("status") == "sanitized"
                evidence_by_day = lineage.get("dayEvidence") or {}
                assert set(evidence_by_day) == {"1", "2"}
                assert all(
                    set(values)
                    >= {
                        "requiredGoalCount",
                        "explicitSoftGoalCount",
                        "briefOptionalCount",
                    }
                    for values in evidence_by_day.values()
                )
                semantics = [
                    segment.get("semanticMetadata") or {}
                    for day in (proposal["snapshot_json"] or {}).get("days") or []
                    for segment in day.get("segments") or []
                ]
                assert any(item.get("goalId") and item.get("intentType") == "campus_visit" for item in semantics)
                assert any(item.get("goalId") and item.get("intentType") == "museum" for item in semantics)
                pending_slots = (proposal["snapshot_json"] or {}).get("portfolioPendingSlots") or []
                assert any(item.get("intentType") in {"meal", "local_food"} for item in pending_slots)
                assert any(item.get("poolId") and item.get("requirementLevel") == "soft" for item in pending_slots)
                optional_semantics = {
                    ((item.get("poiIdentity") or ""), item.get("optionalExperienceFamily")): item
                    for item in semantics
                    if item.get("portfolioOptional")
                }
                for grounded in (proposal["evidence_json"] or {}).get("groundedEvidence") or []:
                    family = grounded.get("optionalExperienceFamily")
                    if not family:
                        continue
                    identity = str(grounded.get("amapId") or grounded.get("id") or "")
                    semantic = optional_semantics.get((identity, family))
                    assert semantic is not None
                    assert grounded.get("briefId") == proposal["brief_json"]["briefId"]
                    assert grounded.get("planningSlotId") == semantic.get("slotId")
            selected_choice = choice_options[0]
            selected_projection = selected_choice["comparisonProjection"]
            assert selected_projection["comparisonRole"] == "candidate_proposal"
            assert selected_projection["originProjectionMode"] == "partial_preview"
            assert selected_projection["adoptionReady"] is True
            assert selected_projection["nextAction"] == "adopt_proposal"
            selected_proposal = next(proposal for proposal in visible if proposal["choice_id"] == selected_choice["id"])

            versions_before = connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0]
            patches_before = connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0]
            routes_before = connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0]

            committed, commit_exit_code = runtime.run_once(
                RuntimeRunOptions(
                    input="",
                    city="北京",
                    sessionId=result.session_id,
                    stateDir=str(state_dir / "turn_2"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider={
                        "enabled": True,
                        "trustedPoiFixtures": True,
                        "mockRouteRefresh": True,
                        "forceDominantCandidates": True,
                    },
                    selectedAgentChoice={
                        "sourceAssistantTurnId": assistant["id"],
                        "choiceId": selected_choice["id"],
                    },
                ),
                argv=["trip-agent", "run", "--creative-portfolio-golden", "--turn", "2"],
            )
            assert commit_exit_code == 0, committed.warnings
            assert committed.status == "success"
            assert committed.active_version_id
            assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] - versions_before == 1
            assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] - patches_before == 1
            statuses = connection.execute(
                "SELECT id, status FROM agent_plan_proposals WHERE portfolio_id = ?", (portfolio["id"],)
            ).fetchall()
            assert {row["status"] for row in statuses} == {"committed"}
            assert next(row["status"] for row in statuses if row["id"] == selected_proposal["id"]) == "committed"
            assert sum(1 for row in statuses if row["status"] == "committed") == 1

            commit_artifact = Path(committed.artifact_path_absolute)
            commit_input = json.loads((commit_artifact / "input.json").read_text(encoding="utf-8"))
            assert commit_input["input"] == ""
            assert set(commit_input["selectedAgentChoice"]) == {"sourceAssistantTurnId", "choiceId"}
            commit_selection = json.loads((commit_artifact / "portfolio_selection.json").read_text(encoding="utf-8"))
            assert commit_selection["selectedProposalId"] == selected_proposal["id"]
            assert commit_selection["activeVersionId"] == committed.active_version_id
            assert commit_selection["versionDelta"] == 1
            _assert_artifact_has_no_local_absolute_paths(commit_artifact)
            exported = runtime.export_state(result.session_id)
            assert exported["readOnly"] is True
            assert exported["session"]["active_version_id"] == committed.active_version_id
            assert _itinerary_identity(exported["active_itinerary_snapshot"]) == _itinerary_identity(
                selected_proposal["snapshot_json"]
            )
            for key in (
                "creativeBrief",
                "portfolioDayAnchorTargets",
                "portfolioDensityDecisionSource",
                "portfolioDensityEvidence",
                "portfolioPlanningProjection",
                "portfolioGoalOccurrencePlan",
                "portfolioRequiredCandidateBindings",
                "portfolioTransportPreference",
            ):
                assert exported["active_itinerary_snapshot"][key] == selected_proposal["snapshot_json"][key]
            anchor_counts = _route_anchor_counts(exported["active_itinerary_snapshot"])
            expected_route_pairs = sum(max(count - 1, 0) for count in anchor_counts.values())
            actual_route_pairs, known_travel_minutes, known_route_distance = _selected_route_metrics(
                exported["active_itinerary_snapshot"]
            )
            assert expected_route_pairs == sum(
                max(int(value) - 1, 0)
                for value in (selected_proposal["verifier_json"] or {}).get("dayAnchorActuals", {}).values()
            )
            assert actual_route_pairs == expected_route_pairs
            assert known_travel_minutes > 0
            assert known_route_distance > 0
            routes_after_commit = connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0]
            assert routes_after_commit > routes_before
            replay = TripAgentRuntime.replay_artifact(committed.artifact_path)
            assert replay["status"] == "success"
            assert replay["activeVersionId"] == committed.active_version_id
            assert replay["artifactPath"].startswith("artifact://run_")
            assert "artifactPathAbsolute" not in replay
            commit_verifier = json.loads((commit_artifact / "verifier_report.json").read_text(encoding="utf-8"))
            assert commit_verifier["passed"] is False
            assert commit_verifier["draftPassed"] is True
            assert commit_verifier["hardFailures"] == []
            commit_events = _jsonl(commit_artifact / "tool_events.jsonl")
            assert {event.get("type") for event in commit_events} >= {"apply_patch", "verify"}

            duplicate, duplicate_exit_code = runtime.run_once(
                RuntimeRunOptions(
                    input="",
                    city="北京",
                    sessionId=result.session_id,
                    stateDir=str(state_dir / "turn_3_duplicate"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider={
                        "enabled": True,
                        "trustedPoiFixtures": True,
                        "mockRouteRefresh": True,
                        "forceDominantCandidates": True,
                    },
                    selectedAgentChoice={
                        "sourceAssistantTurnId": assistant["id"],
                        "choiceId": selected_choice["id"],
                    },
                ),
                argv=["trip-agent", "run", "--creative-portfolio-golden", "--turn", "3"],
            )
            assert duplicate_exit_code == 0, duplicate.warnings
            assert duplicate.status == "success", (
                duplicate.status,
                duplicate.terminal_status,
                duplicate.assistant_reply,
            )
            assert duplicate.terminal_status == "success"
            assert duplicate.assistant_reply == "所选方案已提交；重复选择未创建新版本。"
            duplicate_artifact = Path(duplicate.artifact_path_absolute)
            _assert_artifact_has_no_local_absolute_paths(duplicate_artifact)
            duplicate_input = json.loads((duplicate_artifact / "input.json").read_text(encoding="utf-8"))
            assert duplicate_input["input"] == ""
            assert set(duplicate_input["selectedAgentChoice"]) == {"sourceAssistantTurnId", "choiceId"}
            duplicate_events = _jsonl(duplicate_artifact / "tool_events.jsonl")
            duplicate_patch_event = next(event for event in duplicate_events if event.get("type") == "apply_patch")
            assert {
                key: duplicate_patch_event["metadata"].get(key)
                for key in ("versionDelta", "patchDelta", "routeWriteDelta")
            } == {"versionDelta": 0, "patchDelta": 0, "routeWriteDelta": 0}
            duplicate_verifier = json.loads((duplicate_artifact / "verifier_report.json").read_text(encoding="utf-8"))
            assert duplicate_verifier["passed"] is False
            assert duplicate_verifier["draftPassed"] is True
            assert duplicate_verifier["hardFailures"] == []
            assert duplicate.active_version_id == committed.active_version_id
            assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == routes_after_commit
            (evidence_root / "export-state.json").write_text(
                json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (evidence_root / "replay.json").write_text(
                json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        finally:
            connection.close()
            get_settings.cache_clear()
