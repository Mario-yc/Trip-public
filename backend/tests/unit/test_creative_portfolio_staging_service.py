import copy
import json
import threading
import time
from types import SimpleNamespace

from src.services.constraint_ledger_compiler import ConstraintLedgerCompiler
from src.services.amap_call_budget import (
    AmapCallBudget,
    amap_call_budget_scope,
    current_amap_call_budget,
)
from src.services.creative_planning_models import PlanCandidate, canonical_fingerprint
from src.services.creative_portfolio_provider_service import InitialCreativePortfolio
from src.services.creative_portfolio_staging_service import CreativePortfolioStagingService
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.goal_occurrence_compiler import GoalOccurrencePlan
from src.services.shared_candidate_universe_service import SharedCandidateUniverse
from src.services.bounded_portfolio_optimizer import PortfolioAssignment
from src.services.portfolio_route_feasibility_service import PortfolioRouteFeasibilityResult
from src.services.proposal_readiness_service import ProposalReadinessService


class FakeStore:
    def __init__(self, prior_projections=None):
        self.saved = None
        self.appended = []
        self.prior_projections = list(prior_projections or [])

    def create(self, portfolio, proposals, **_):
        self.saved = (portfolio, proposals)

    def offer_repaired_proposal(self, *, portfolio_id, proposal):
        self.appended.append((portfolio_id, proposal))
        return f"choice_{proposal.proposal_id}", proposal.proposal_id, True

    def visible_comparison_projections(self, *, portfolio_id):
        return copy.deepcopy(self.prior_projections)


def test_agent_title_candidates_reserve_titles_from_prior_continuation_cards():
    segments = []
    for index, name in enumerate(
        ["清华大学", "四季民福烤鸭店", "三源里菜市场", "模式口历史文化街区", "景山公园", "中央电视塔"],
        start=1,
    ):
        segments.append(
            {
                "id": f"segment-{index}",
                "startTime": f"{8 + index:02d}:00",
                "poi": {
                    "amapId": f"B{index:09d}",
                    "name": name,
                    "city": "北京",
                    "source": "amap-place-search",
                    "latitude": 39.9 + index / 1000,
                    "longitude": 116.3 + index / 1000,
                },
                "semanticMetadata": {},
            }
        )
    snapshot = {
        "city": "北京",
        "days": [{"dayNumber": 1, "segments": segments}],
    }
    evidence_ids = [segment["poi"]["amapId"] for segment in segments]
    projected = CreativeProposalTitleService.select_agent_candidate(
        snapshot,
        {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华书香与京城风物", "evidenceAmapIds": evidence_ids},
                {"title": "燕园清华书影漫行", "evidenceAmapIds": evidence_ids},
                {"title": "街巷清华风物慢游", "evidenceAmapIds": evidence_ids},
                {"title": "园林清华城景拾光", "evidenceAmapIds": evidence_ids},
            ],
        },
        reserved_titles={"清华书香与京城风物"},
    )

    assert projected["title"] == "燕园清华书影漫行"
    assert projected["selectedAmapIds"] == evidence_ids
    assert projected["candidateCount"] == 4


def test_staging_accepts_preferred_extra_of_hard_goal_as_explicit_soft_occurrence():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "requiredMin": 1,
                        "preferredCount": 2,
                        "maxCount": 2,
                        "allowedDayNumbers": [1, 2],
                    }
                ]
            },
        }
    )
    occurrence_plan = GoalOccurrencePlan.model_validate(
        {
            "schemaVersion": "goal-occurrence-plan-v1",
            "occurrences": [
                {
                    "occurrenceId": "occ:goal_campus_visit:day:1",
                    "sourceGoalId": "goal_campus_visit",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:goal_campus_visit:day:2",
                    "sourceGoalId": "goal_campus_visit",
                    "intentType": "campus_visit",
                    "dayNumber": 2,
                    "requirementLevel": "explicit_soft",
                },
            ],
            "avoidRecentEntities": True,
            "sourceFingerprint": "f" * 64,
        }
    )

    assert (
        CreativePortfolioStagingService._occurrence_contract_failure(
            ledger,
            occurrence_plan,
        )
        is None
    )


def _candidate(identity, score):
    if str(identity).startswith("m") and not str(identity).startswith("meal"):
        name = f"{identity} museum 博物馆"
        provider_type = "科教文化服务;博物馆;博物馆"
        category = "museum"
    elif str(identity).startswith("meal"):
        name = f"{identity} 北京餐厅"
        provider_type = "餐饮服务;中餐厅;中餐厅"
        category = "food"
    elif identity in {"tower", "square"}:
        name = f"{identity} 夜景观景台"
        provider_type = "风景名胜;风景名胜;观景点"
        category = "night_view"
    else:
        name = "清华大学" if str(identity) in {"c1", "thu", "shared-campus"} else "北京大学"
        provider_type = "科教文化服务;学校;高等院校"
        category = "campus"
    return {
        "amapId": identity,
        "name": name,
        "city": "北京",
        "address": f"北京市{name}测试地址",
        "longitude": 116.3,
        "latitude": 39.9,
        "type": provider_type,
        "providerType": provider_type,
        "category": category,
        "source": "amap-place-search",
        "sourcePrecheck": {"passed": True, "reasonCodes": []},
        "semanticPassed": True,
        "localRouteScore": score,
    }


def _required_semantic(poi):
    return {
        "goalId": poi["goalId"],
        "occurrenceId": poi["occurrenceId"],
        "intentType": poi["intentType"],
        "poolId": poi["poolId"],
        "planningSlotId": poi["planningSlotId"],
        "creativeBriefId": poi["briefId"],
        "required": True,
        "routeAnchor": True,
        "groundingStatus": "selected",
    }


def _strict_brief(brief_id, title, axis, goals):
    return {
        "brief": {
            "briefId": brief_id,
            "title": title,
            "primaryAxis": axis,
            "dayRoles": [
                {"dayNumber": 1, "role": title, "targetRouteAnchors": len(goals), "densityEvidence": ["pace=standard"]}
            ],
            "requiredGoalIds": goals,
        },
        "daySlots": [
            {
                "slotId": f"{brief_id}-{goal}",
                "dayNumber": 1,
                "timeWindow": "morning",
                "durationMinutes": 60,
                "kind": "visit",
                "rawNeed": goal,
                "routeAnchor": True,
                "requiredGoalId": goal,
            }
            for goal in goals
        ],
        "intentPools": [
            {
                "poolId": f"{brief_id}-{goal}-pool",
                "briefId": brief_id,
                "rawNeed": goal,
                "city": "北京",
                "intentType": "campus_visit" if goal == "campus" else "museum",
                "targetCount": 1,
                "requirementLevel": "required",
                "goalId": goal,
                "assignToSlots": [f"{brief_id}-{goal}"],
            }
            for goal in goals
        ],
    }


def test_staging_output_quality_preserves_successful_agent_title():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("food", "市井市场方向", "food_led", ["campus"])],
        }
    )
    brief = generated.proposals[0].brief
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "startTime": "09:00",
                        "poi": {
                            "amapId": "B000A8UIN8",
                            "name": "清华大学",
                            "source": "amap-place-search",
                            "latitude": 40.0,
                            "longitude": 116.3,
                        },
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "startTime": "12:00",
                        "poi": {
                            "amapId": "B000A8UIN9",
                            "name": "四季民福烤鸭店(故宫店)",
                            "source": "amap-place-search",
                            "latitude": 39.91,
                            "longitude": 116.4,
                        },
                        "semanticMetadata": {"intentType": "meal"},
                    },
                    {
                        "startTime": "14:30",
                        "poi": {
                            "amapId": "B000A8UIN7",
                            "name": "三源里菜市场",
                            "source": "amap-place-search",
                            "latitude": 39.95,
                            "longitude": 116.47,
                        },
                        "semanticMetadata": {
                            "optionalExperienceFamily": "market_walk",
                        },
                    },
                ],
            }
        ],
    }
    service = CreativePortfolioStagingService(
        FakeStore(),
        creative_output_quality_v2_mode="enforce",
    )
    snapshot["portfolioVerifier"] = {"passed": True}
    evidence_ids = [segment["poi"]["amapId"] for day in snapshot["days"] for segment in day["segments"]]
    titled = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华学府寻味漫游", "evidenceAmapIds": evidence_ids},
                {"title": "清华书香烟火街巷", "evidenceAmapIds": evidence_ids},
                {"title": "清华校园风味慢行", "evidenceAmapIds": evidence_ids},
            ],
        },
        context={},
    )
    titled_brief = brief.model_copy(update={"title": titled["title"]})
    evaluated = service._with_output_quality(titled, titled_brief)

    assert evaluated["title"] == titled["title"]
    assert evaluated["title"] != "市井市场方向｜北京真实地点草案"
    assert evaluated["portfolioTitleEvidence"]["generationSource"] == "agent_generated_title_candidates"
    assert evaluated["portfolioOutputQuality"]["displayTitle"] == titled["title"]
    assert titled_brief.title == titled["title"]


def test_verified_candidate_uses_agent_title_candidates_before_visibility():
    snapshot = {
        "city": "北京",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "campus",
                        "startTime": "09:00",
                        "poi": {
                            "amapId": "B00000001",
                            "name": "清华大学",
                            "city": "北京",
                            "source": "amap-place-search",
                            "latitude": 40.0,
                            "longitude": 116.3,
                        },
                        "semanticMetadata": {"intentType": "campus_visit"},
                    },
                    {
                        "id": "night",
                        "startTime": "19:00",
                        "poi": {
                            "amapId": "B00000002",
                            "name": "中央电视塔",
                            "city": "北京",
                            "source": "amap-place-search",
                            "latitude": 39.9,
                            "longitude": 116.31,
                        },
                        "semanticMetadata": {"intentType": "night_view"},
                    },
                ],
            }
        ],
    }
    candidate = PlanCandidate.model_validate(
        {
            "proposalId": "proposal_titled",
            "portfolioId": "portfolio_titled",
            "brief": {
                "briefId": "brief_titled",
                "title": "内部方向名",
                "primaryAxis": "photo_night",
                "requiredGoalIds": [],
            },
            "itinerarySnapshot": snapshot,
            "score": {
                "hardConstraintPassed": True,
                "preferenceFit": 80,
                "thematicCoherence": 80,
                "experienceDiversity": 75,
                "routeEfficiency": 75,
                "pacingQuality": 75,
                "novelty": 70,
                "robustness": 75,
                "uncertaintyPenalty": 0,
            },
            "verifier": {"passed": True, "draftPassed": True},
            "canonicalSignature": "title-signature-verified",
        }
    )
    calls: list[dict] = []

    def generate(context):
        calls.append(context)
        ids = context["evidenceAmapIds"]
        return json.dumps(
            {
                "schemaVersion": "creative-proposal-title-candidates-v1",
                "candidates": [
                    {"title": "清华书声映京城灯火", "evidenceAmapIds": ids},
                    {"title": "清华校园清韵漫游", "evidenceAmapIds": ids},
                    {"title": "清华书香一路京城", "evidenceAmapIds": ids},
                ],
            },
            ensure_ascii=False,
        )

    service = CreativePortfolioStagingService(FakeStore(), title_candidate_generator=generate)
    [titled] = service._ensure_agent_generated_titles(
        [candidate],
        city="北京",
        reserved_titles=set(),
    )

    assert len(calls) == 1
    assert calls[0]["evidenceAmapIds"] == ["B00000001", "B00000002"]
    assert titled.itinerary_snapshot["title"] == "清华书声映京城灯火"
    assert titled.itinerary_snapshot["portfolioTitleEvidence"]["candidateCount"] == 3
    assert service.last_staging_metrics["agentTitleGeneration"]["accepted"] == 1


def test_incomplete_candidate_keeps_status_title_without_calling_title_agent():
    candidate = PlanCandidate.model_validate(
        {
            "proposalId": "proposal_incomplete_title",
            "portfolioId": "portfolio_incomplete_title",
            "brief": {
                "briefId": "brief_incomplete_title",
                "title": "内部营销方向",
                "primaryAxis": "photo_night",
                "requiredGoalIds": [],
            },
            "itinerarySnapshot": {
                "city": "北京",
                "title": "不应展示的营销标题",
                "portfolioTitleEvidence": {"generationSource": "legacy"},
                "portfolioPendingSlots": [{"intentType": "night_view"}],
                "days": [],
            },
            "score": {
                "hardConstraintPassed": True,
                "preferenceFit": 80,
                "thematicCoherence": 80,
                "experienceDiversity": 75,
                "routeEfficiency": 75,
                "pacingQuality": 75,
                "novelty": 70,
                "robustness": 75,
                "uncertaintyPenalty": 0,
            },
            "verifier": {"passed": False, "draftPassed": True},
            "canonicalSignature": "incomplete-title-signature",
        }
    )

    def must_not_run(_context):
        raise AssertionError("incomplete proposal must not call title agent")

    service = CreativePortfolioStagingService(FakeStore(), title_candidate_generator=must_not_run)
    [projected] = service._ensure_agent_generated_titles([candidate], city="北京", reserved_titles=set())

    assert projected.itinerary_snapshot["title"] == "夜景待确认"
    assert "portfolioTitleEvidence" not in projected.itinerary_snapshot
    assert service.last_staging_metrics["agentTitleGeneration"] == {
        "attempted": 0,
        "accepted": 0,
        "preservedIncomplete": 1,
        "failedProposalIds": [],
    }


def test_verified_candidate_uses_fact_bound_fallback_when_title_provider_fails():
    candidate = PlanCandidate.model_validate(
        {
            "proposalId": "proposal_title_retry",
            "portfolioId": "portfolio_title_retry",
            "brief": {
                "briefId": "brief_title_retry",
                "title": "内部方向名",
                "primaryAxis": "photo_night",
                "requiredGoalIds": [],
            },
            "itinerarySnapshot": {
                "city": "北京",
                "portfolioVerifier": {"passed": True},
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "night",
                                "startTime": "19:00",
                                "poi": {
                                    "amapId": "B00000002",
                                    "name": "中央电视塔",
                                    "city": "北京",
                                    "source": "amap-place-search",
                                    "latitude": 39.9,
                                    "longitude": 116.31,
                                },
                                "semanticMetadata": {"intentType": "night_view"},
                            }
                        ],
                    }
                ],
            },
            "score": {
                "hardConstraintPassed": True,
                "preferenceFit": 80,
                "thematicCoherence": 80,
                "experienceDiversity": 75,
                "routeEfficiency": 75,
                "pacingQuality": 75,
                "novelty": 70,
                "robustness": 75,
                "uncertaintyPenalty": 0,
            },
            "verifier": {"passed": True, "draftPassed": True},
            "canonicalSignature": "title-retry-signature",
        }
    )

    def fail(_context):
        raise TimeoutError("title provider timeout")

    service = CreativePortfolioStagingService(FakeStore(), title_candidate_generator=fail)
    [projected] = service._ensure_agent_generated_titles([candidate], city="北京", reserved_titles=set())

    assert "中央电视塔" in projected.itinerary_snapshot["title"]
    assert projected.itinerary_snapshot["portfolioTitleGeneration"]["status"] == "failed_non_blocking"
    assert projected.itinerary_snapshot["portfolioTitleGeneration"]["retryable"] is False
    assert service.last_staging_metrics["agentTitleGeneration"] == {
        "attempted": 1,
        "accepted": 0,
        "preservedIncomplete": 0,
        "failedProposalIds": ["proposal_title_retry"],
    }


def test_verified_candidate_without_title_provider_uses_fact_bound_fallback():
    candidate = PlanCandidate.model_validate(
        {
            "proposalId": "proposal_title_unavailable",
            "portfolioId": "portfolio_title_unavailable",
            "brief": {
                "briefId": "brief_title_unavailable",
                "title": "内部方向名",
                "primaryAxis": "photo_night",
                "requiredGoalIds": [],
            },
            "itinerarySnapshot": {
                "city": "北京",
                "portfolioVerifier": {"passed": True},
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "night",
                                "startTime": "19:00",
                                "poi": {
                                    "amapId": "B00000002",
                                    "name": "中央电视塔",
                                    "city": "北京",
                                    "source": "amap-place-search",
                                    "latitude": 39.9,
                                    "longitude": 116.31,
                                },
                                "semanticMetadata": {"intentType": "night_view"},
                            }
                        ],
                    }
                ],
            },
            "score": {
                "hardConstraintPassed": True,
                "preferenceFit": 80,
                "thematicCoherence": 80,
                "experienceDiversity": 75,
                "routeEfficiency": 75,
                "pacingQuality": 75,
                "novelty": 70,
                "robustness": 75,
                "uncertaintyPenalty": 0,
            },
            "verifier": {"passed": True, "draftPassed": True},
            "canonicalSignature": "title-unavailable-signature",
        }
    )

    service = CreativePortfolioStagingService(FakeStore())
    [projected] = service._ensure_agent_generated_titles([candidate], city="北京", reserved_titles=set())

    assert "中央电视塔" in projected.itinerary_snapshot["title"]
    assert projected.itinerary_snapshot["portfolioTitleGeneration"]["status"] == "failed_non_blocking"
    assert projected.itinerary_snapshot["portfolioTitleGeneration"]["retryable"] is False
    assert projected.itinerary_snapshot["portfolioTitleGeneration"]["reasonCode"] == "title_provider_unavailable"
    assert "portfolioTitleEvidence" not in projected.itinerary_snapshot
    assert service.last_staging_metrics["agentTitleGeneration"] == {
        "attempted": 0,
        "accepted": 0,
        "preservedIncomplete": 0,
        "failedProposalIds": ["proposal_title_unavailable"],
    }


def test_staging_creates_only_verified_proposals_and_never_invokes_itinerary_writer():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {"goalId": "museum", "intentType": "museum"},
                ]
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                _strict_brief("classic", "经典", "classic", ["campus", "museum"]),
                _strict_brief("local", "本地", "local_immersion", ["campus", "museum"]),
            ],
        }
    )
    universe = SharedCandidateUniverse(
        {"campus_visit": [_candidate("c1", 1), _candidate("c2", 3)], "museum": [_candidate("m1", 2)]}, 2, 0
    )
    store = FakeStore()

    def snapshot(required, _soft, _optional, brief_id):
        segments = []
        for offset, (goal, poi) in enumerate(required.items()):
            segments.append(
                {
                    "startTime": f"{9 + offset * 2:02d}:00",
                    "endTime": f"{10 + offset * 2:02d}:00",
                    "poi": poi,
                    "semanticMetadata": _required_semantic(poi),
                }
            )
        return {"id": "plan_1", "title": brief_id, "days": [{"dayNumber": 1, "segments": segments}]}

    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
    )
    assert portfolio.status == "awaiting_selection"
    assert visible
    assert store.saved is not None
    assert all(candidate.verifier["passed"] for candidate in store.saved[1])
    assert all(candidate.itinerary_snapshot["id"] == "plan_1" for candidate in store.saved[1])
    assert all(
        candidate.itinerary_snapshot["portfolioTitleGeneration"]["status"] == "failed_non_blocking"
        for candidate in store.saved[1]
    )
    assert all(
        candidate.itinerary_snapshot["portfolioTitleGeneration"]["reasonCode"] == "title_provider_unavailable"
        for candidate in store.saved[1]
    )
    assert all(
        "proposal_title_generation_pending"
        not in ProposalReadinessService.compute(
            candidate.itinerary_snapshot,
            verifier=candidate.verifier,
        )["blockingReasons"]
        for candidate in store.saved[1]
    )
    identities = {
        tuple(sorted(item["poi"]["amapId"] for day in candidate.itinerary_snapshot["days"] for item in day["segments"]))
        for candidate in visible
    }
    assert len(identities) == len(visible)
    assert all(
        candidate.generation_lineage["portfolioRequiredCandidateBindings"]
        == candidate.itinerary_snapshot["portfolioRequiredCandidateBindings"]
        for candidate in visible
    )
    assert all(candidate.verifier["hardCandidateCrossBriefLeakCount"] == 0 for candidate in visible)
    assert all(candidate.verifier["hardCandidateLineageMissingCount"] == 0 for candidate in visible)

    append_store = FakeStore()
    appended_portfolio, appended_visible = CreativePortfolioStagingService(append_store).stage(
        session_id="session",
        source_user_turn_id="user_retry",
        source_assistant_turn_id="assistant_retry",
        expected_base_version_id="version_partial",
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
        focus_brief_id="local",
        existing_portfolio_id="portfolio_root",
    )
    assert append_store.saved is None
    assert appended_visible
    assert appended_portfolio.portfolio_id == "portfolio_root"
    assert all(candidate.portfolio_id == "portfolio_root" for candidate in appended_visible)
    assert [candidate.brief.brief_id for candidate in appended_visible] == ["local"]
    assert [candidate.brief.brief_id for _, candidate in append_store.appended] == ["local"]
    assert [portfolio_id for portfolio_id, _candidate in append_store.appended] == ["portfolio_root"] * len(
        appended_visible
    )


def test_staging_offers_soft_pending_plan_as_zero_write_editable_draft():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "广州",
            "requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]},
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "local_draft",
                        "title": "本地生活优先",
                        "primaryAxis": "local_immersion",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "校园与社区",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["hard=1", "soft=1"],
                            }
                        ],
                        "optionalExperiences": [{"family": "local_life", "description": "社区日常"}],
                        "requiredGoalIds": ["campus"],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus_slot",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "startTime": "09:30",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "校园参观",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                            "requirementLevel": "required",
                            "experienceShape": "single_poi",
                        },
                        {
                            "slotId": "local_slot",
                            "dayNumber": 1,
                            "timeWindow": "afternoon",
                            "startTime": "14:00",
                            "durationMinutes": 90,
                            "kind": "area_walk",
                            "rawNeed": "社区日常生活",
                            "routeAnchor": True,
                            "requirementLevel": "soft",
                            "experienceShape": "area",
                            "experienceGoal": "观察居民日常活动",
                            "desiredSignals": ["社区服务", "日常交易"],
                            "avoidSignals": ["纯观光陈列"],
                            "evidenceRequirements": {"minimumIndependentSources": 2},
                            "groundingContract": {"allowPending": True},
                            "routeContract": {"maxDetourMinutes": 20},
                            "optionalExperienceFamily": "local_life",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus_pool",
                            "briefId": "local_draft",
                            "rawNeed": "校园参观",
                            "city": "广州",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus_slot"],
                        },
                        {
                            "poolId": "local_pool",
                            "briefId": "local_draft",
                            "rawNeed": "社区日常生活",
                            "city": "广州",
                            "intentType": "area_walk",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "optionalExperienceFamily": "local_life",
                            "assignToSlots": ["local_slot"],
                        },
                    ],
                }
            ],
        }
    )
    campus = {**_candidate("c1", 1), "city": "广州"}
    store = FakeStore()

    def snapshot(required, _soft, _optional, brief_id):
        poi = next(iter(required.values()))
        return {
            "id": "plan_draft",
            "city": "广州",
            "title": brief_id,
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "startTime": "09:30",
                            "endTime": "10:30",
                            "poi": poi,
                            "semanticMetadata": _required_semantic(poi),
                        }
                    ],
                }
            ],
        }

    portfolio, visible = CreativePortfolioStagingService(
        store,
        soft_slot_draft_adoption_enabled=True,
    ).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [campus]}, 1, 0),
        snapshot_builder=snapshot,
    )

    assert portfolio.status == "awaiting_selection"
    assert len(visible) == 1
    assert visible[0].verifier["passed"] is False
    assert visible[0].verifier["draftPassed"] is True
    assert visible[0].itinerary_snapshot["portfolioPendingSlots"] == [
        {
            **visible[0].itinerary_snapshot["portfolioPendingSlots"][0],
            "briefId": "local_draft",
            "poolId": "local_pool",
            "planningSlotId": "local_slot",
            "requirementLevel": "soft",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
            "experienceShape": "area",
        }
    ]
    assert store.saved is not None


def test_same_root_expansion_rejects_physically_repeated_full_proposal_before_offer():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {"goalId": "museum", "intentType": "museum"},
                ]
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("local", "本地", "local_immersion", ["campus", "museum"])],
        }
    )
    campus = _candidate("B000A0001", 3)
    museum = {
        **_candidate("m1", 2),
        "amapId": "B000A0002",
    }
    universe = SharedCandidateUniverse(
        {"campus_visit": [campus], "museum": [museum]},
        2,
        0,
    )

    def snapshot(required, _soft, _optional, brief_id):
        segments = []
        for offset, (_goal, poi) in enumerate(required.items()):
            segments.append(
                {
                    "startTime": f"{9 + offset * 2:02d}:00",
                    "endTime": f"{10 + offset * 2:02d}:00",
                    "poi": poi,
                    "semanticMetadata": _required_semantic(poi),
                }
            )
        return {
            "id": "plan_repeat",
            "title": brief_id,
            "days": [{"dayNumber": 1, "segments": segments}],
        }

    repeated_projection = {
        "proposalId": "proposal_existing",
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {"poi": copy.deepcopy(campus)},
                    {"poi": copy.deepcopy(museum)},
                ],
            }
        ],
    }
    repeated_store = FakeStore([repeated_projection])
    repeated_service = CreativePortfolioStagingService(repeated_store)

    repeated_portfolio, repeated_visible = repeated_service.stage(
        session_id="session",
        source_user_turn_id="user_retry",
        source_assistant_turn_id="assistant_retry",
        expected_base_version_id="version_partial",
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
        focus_brief_id="local",
        existing_portfolio_id="portfolio_root",
    )

    assert repeated_visible == []
    assert repeated_store.appended == []
    assert repeated_portfolio.status == "failed"
    diagnostic = repeated_service.last_staging_metrics["perBriefDiagnostics"][0]
    assert diagnostic["noveltyFailures"] == ["full_proposal_not_materially_distinct"]

    partial_baseline_store = FakeStore()
    partial_baseline_portfolio, partial_baseline_visible = CreativePortfolioStagingService(
        partial_baseline_store
    ).stage(
        session_id="session",
        source_user_turn_id="user_retry",
        source_assistant_turn_id="assistant_retry",
        expected_base_version_id="version_partial",
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
        focus_brief_id="local",
        existing_portfolio_id="portfolio_root",
        prior_comparison_projections=[repeated_projection],
    )
    assert partial_baseline_visible == []
    assert partial_baseline_store.appended == []
    assert partial_baseline_portfolio.status == "failed"

    distinct_store = FakeStore(
        [
            {
                **repeated_projection,
                "proposalId": "proposal_other",
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {"poi": copy.deepcopy(campus)},
                            {"poi": {**copy.deepcopy(museum), "amapId": "B000A0009"}},
                        ],
                    }
                ],
            }
        ]
    )
    distinct_portfolio, distinct_visible = CreativePortfolioStagingService(distinct_store).stage(
        session_id="session",
        source_user_turn_id="user_retry",
        source_assistant_turn_id="assistant_retry",
        expected_base_version_id="version_partial",
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
        focus_brief_id="local",
        existing_portfolio_id="portfolio_root",
    )

    assert distinct_portfolio.status == "awaiting_selection"
    assert len(distinct_visible) == 1
    assert len(distinct_store.appended) == 1


def test_staging_rejects_zero_coordinate_amap_from_proposals():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("classic", "经典", "classic", ["campus"])],
        }
    )
    zero_coordinate_candidate = {
        **_candidate("B000A8UIN8", 2),
        "name": "清华大学",
        "city": "北京市",
        "longitude": 0.0,
        "latitude": 0.0,
    }
    store = FakeStore()
    service = CreativePortfolioStagingService(store)

    portfolio, visible = service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {"campus_visit": [zero_coordinate_candidate]},
            1,
            0,
        ),
        snapshot_builder=lambda required, _soft, _optional, brief_id: {
            "id": "plan-zero-coordinate",
            "title": brief_id,
            "city": "北京",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": required["campus"],
                            "semanticMetadata": _required_semantic(required["campus"]),
                        }
                    ],
                }
            ],
        },
    )

    assert visible == []
    assert portfolio.status == "failed"
    assert store.saved == (portfolio, [])
    assert service.partial_timeline_candidate is None
    assert "required_goal_ungrounded:campus" in (service.last_staging_metrics["perBriefDiagnostics"][0]["hardFailures"])


def test_staging_route_preflight_uses_at_most_two_workers_and_preserves_brief_order():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief(f"brief-{index}", f"方案{index}", "classic", ["campus"]) for index in range(4)],
        }
    )

    class DelayedRoutePreflight:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.preview_ids = []
            self.allow_web_discovery_flags = []

        def prepare(self, snapshot, **kwargs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.preview_ids.append(kwargs["preview_id"])
                self.allow_web_discovery_flags.append(kwargs["allow_web_discovery"])
            try:
                time.sleep(0.2)
                return PortfolioRouteFeasibilityResult(status="passed", snapshot=copy.deepcopy(snapshot))
            finally:
                with self.lock:
                    self.active -= 1

    preflight = DelayedRoutePreflight()
    service = CreativePortfolioStagingService(FakeStore(), route_feasibility_service=preflight)
    started = time.perf_counter()
    portfolio, visible = service.stage(
        session_id="s",
        source_user_turn_id="u",
        source_assistant_turn_id="a",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [_candidate("c1", 1)]}, 1, 0),
        focus_brief_id="brief-2",
        snapshot_builder=lambda required, _soft, _optional, brief_id: {
            "city": "北京",
            "creativeBrief": {"briefId": brief_id},
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": f"segment-{brief_id}",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": required["campus"],
                            "semanticMetadata": _required_semantic(required["campus"]),
                        }
                    ],
                }
            ],
        },
    )
    elapsed = time.perf_counter() - started

    assert portfolio.failure_reason is None
    assert portfolio.status == "awaiting_selection"
    assert len(visible) == 4
    assert set(preflight.preview_ids) == {f"{portfolio.portfolio_id}_brief-{index}" for index in range(4)}
    assert preflight.preview_ids[0] == f"{portfolio.portfolio_id}_brief-2"
    assert preflight.max_active == 2
    assert preflight.allow_web_discovery_flags == [False, False, False, False]
    assert service.last_staging_metrics["perBriefDiagnostics"][0]["briefId"] == "brief-2"
    assert service.last_staging_metrics["perBriefDiagnostics"][0]["dayAnchorShortfalls"] == []
    assert service.last_staging_metrics["perBriefDiagnostics"][0]["dayAnchorActuals"] == {"1": 1}
    assert service.last_staging_metrics["maxConcurrentBriefWorkers"] == 2
    brief_metrics = service.last_staging_metrics["briefMetrics"]
    assert [item["briefId"] for item in brief_metrics] == [
        "brief-2",
        "brief-0",
        "brief-1",
        "brief-3",
    ]
    assert all(item["routePreflightInvocationCount"] == 1 for item in brief_metrics)
    assert all(item["routePreflightCallCount"] == 0 for item in brief_metrics)
    assert all(item["candidateCount"] == 1 for item in brief_metrics)
    assert all(item["durationMs"] >= item["routePreflightMs"] for item in brief_metrics)
    assert brief_metrics[0]["status"] == "completed"
    assert brief_metrics[0]["verifierPassed"] is True
    assert brief_metrics[0]["routeProviderState"] == "not_required"
    assert brief_metrics[0]["reasonCodes"] == []
    assert elapsed < 0.6


def test_staging_projects_repair_provider_calls_separately_from_completed_candidate_matrices():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("repair-counts", "修复调用计数", "classic", ["campus"])],
        }
    )

    class RepairCountingRouteFeasibility:
        @staticmethod
        def prepare(snapshot, **_kwargs):
            return PortfolioRouteFeasibilityResult(
                status="failed",
                snapshot=copy.deepcopy(snapshot),
                provider_state="failed",
                route_execution_ledger={
                    "providerCallCount": 6,
                    "expectedLegCount": 3,
                    "completedLegCount": 3,
                    "verifiedLegCount": 0,
                    "failedLegCount": 3,
                },
                repair_attempt_ledger={
                    # Two candidate matrices completed, requiring six
                    # Provider route legs in the bounded repair phase.
                    "candidateEvaluationCount": 2,
                    "repairCallCount": 6,
                    "providerCallCount": 6,
                    "acceptedReplacementCount": 0,
                },
            )

    service = CreativePortfolioStagingService(
        FakeStore(),
        route_feasibility_service=RepairCountingRouteFeasibility(),
    )
    service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [_candidate("c1", 1)]}, 1, 0),
        snapshot_builder=lambda required, _soft, _optional, brief_id: {
            "city": "北京",
            "creativeBrief": {"briefId": brief_id},
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_campus",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": required["campus"],
                            "semanticMetadata": _required_semantic(required["campus"]),
                        }
                    ],
                }
            ],
        },
    )

    metric = service.last_staging_metrics["briefMetrics"][0]
    assert metric["repairAttemptCount"] == 2
    assert metric["repairCallCount"] == 6
    assert metric["repairProviderCallCount"] == 6


def test_staging_assigns_explicit_soft_goal_before_brief_optional_candidates():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit"},
                    {
                        "goalId": "meal",
                        "intentType": "meal",
                        "requirementLevel": "soft_experience",
                    },
                ],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "culture",
                        "title": "文化",
                        "primaryAxis": "culture_deep_dive",
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校与京味",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["explicitSoftGoalCount=1"],
                            }
                        ],
                        "requiredGoalIds": ["campus"],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-slot",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 90,
                            "kind": "visit",
                            "rawNeed": "985高校",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "meal-slot",
                            "dayNumber": 1,
                            "timeWindow": "lunch",
                            "durationMinutes": 60,
                            "kind": "meal",
                            "rawNeed": "北京特色美食",
                            "routeAnchor": True,
                            "softGoalId": "meal",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-pool",
                            "briefId": "culture",
                            "rawNeed": "985高校",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-slot"],
                        },
                        {
                            "poolId": "meal-pool",
                            "briefId": "culture",
                            "rawNeed": "北京特色美食",
                            "city": "北京",
                            "intentType": "meal",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "softGoalId": "meal",
                            "assignToSlots": ["meal-slot"],
                        },
                    ],
                }
            ],
        }
    )
    captured = {}

    def snapshot(required, soft, _optional, brief_id):
        captured.update(soft)
        segments = []
        for offset, (goal, poi) in enumerate([*required.items(), *soft.items()]):
            segments.append(
                {
                    "startTime": f"{9 + offset * 2:02d}:00",
                    "endTime": f"{10 + offset * 2:02d}:00",
                    "poi": poi,
                    "semanticMetadata": _required_semantic(poi)
                    if goal == "campus"
                    else {"goalId": goal, "required": False, "routeAnchor": True, "groundingStatus": "selected"},
                }
            )
        return {"id": "plan", "title": brief_id, "days": [{"dayNumber": 1, "segments": segments}]}

    store = FakeStore()
    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {
                "campus_visit": [_candidate("campus", 2)],
                "meal": [_candidate("meal", 1)],
            },
            2,
            0,
        ),
        snapshot_builder=snapshot,
    )

    assert portfolio.status == "awaiting_selection"
    assert visible
    assert captured["meal"]["slotId"] == "meal-slot"
    assert captured["meal"]["dayNumber"] == 1
    assert captured["meal"]["dayRole"] == "高校与京味"
    assert visible[0].generation_lineage["softCandidateCount"] == 1


def test_missing_required_candidate_is_persisted_as_retryable_density_shortfall():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]},
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("classic", "经典", "classic", ["campus"])],
        }
    )
    store = FakeStore()
    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": []}, 0, 0),
        snapshot_builder=lambda *_: {"days": []},
    )
    assert visible == []
    assert portfolio.status == "failed"
    assert portfolio.failure_reason == "portfolio_anchor_target_shortfall:campus:0/1"
    assert store.saved[1] == []


def test_hard_gap_retains_maximal_grounded_subset_as_read_only_partial():
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
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "campus-night",
                        "title": "高校与夜景",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["campus", "night"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校与夜景",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["requiredGoalCount=2"],
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-slot",
                            "dayNumber": 1,
                            "timeWindow": "09:00-11:00",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "kind": "visit",
                            "rawNeed": "北京高校",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "night-slot",
                            "dayNumber": 1,
                            "timeWindow": "19:00-20:30",
                            "startTime": "19:00",
                            "durationMinutes": 90,
                            "kind": "night_view",
                            "rawNeed": "北京夜景",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-pool",
                            "briefId": "campus-night",
                            "rawNeed": "北京高校",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-slot"],
                        },
                        {
                            "poolId": "night-pool",
                            "briefId": "campus-night",
                            "rawNeed": "北京夜景",
                            "city": "北京",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-slot"],
                        },
                    ],
                }
            ],
        }
    )
    occurrence_plan = {
        "schemaVersion": "goal-occurrence-plan-v1",
        "avoidRecentEntities": True,
        "sourceFingerprint": "o" * 16,
        "occurrences": [
            {
                "occurrenceId": "occ:campus:day:1",
                "sourceGoalId": "campus",
                "intentType": "campus_visit",
                "dayNumber": 1,
                "requirementLevel": "hard",
            },
            {
                "occurrenceId": "occ:night:day:1",
                "sourceGoalId": "night",
                "intentType": "night_view",
                "dayNumber": 1,
                "requirementLevel": "hard",
            },
        ],
    }
    slot_by_occurrence = {
        "occ:campus:day:1": {
            "briefId": "campus-night",
            "poolId": "campus-pool",
            "planningSlotId": "campus-slot",
            "dayNumber": 1,
            "occurrenceId": "occ:campus:day:1",
            "sourceGoalId": "campus",
            "requirementLevel": "hard",
        },
        "occ:night:day:1": {
            "briefId": "campus-night",
            "poolId": "night-pool",
            "planningSlotId": "night-slot",
            "dayNumber": 1,
            "occurrenceId": "occ:night:day:1",
            "sourceGoalId": "night",
            "requirementLevel": "hard",
        },
    }

    def snapshot(required, _soft, _optional, _brief_id):
        segments = []
        for occurrence_id, poi in required.items():
            segments.append(
                {
                    "id": f"seg-{occurrence_id}",
                    "startTime": "09:00",
                    "endTime": "11:00",
                    "poi": poi,
                    "semanticMetadata": _required_semantic(poi),
                }
            )
        return {
            "id": "plan-hard-gap",
            "days": [{"dayNumber": 1, "segments": segments}],
            "portfolioGoalOccurrencePlan": copy.deepcopy(occurrence_plan),
            "portfolioPendingSlots": [
                copy.deepcopy(slot)
                for occurrence_id, slot in slot_by_occurrence.items()
                if occurrence_id not in required
            ],
        }

    store = FakeStore()
    service = CreativePortfolioStagingService(store)
    portfolio, visible = service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {"campus_visit": [_candidate("c1", 2)], "night_view": []},
            1,
            0,
        ),
        snapshot_builder=snapshot,
        goal_occurrence_plan=occurrence_plan,
    )

    assert visible == []
    assert portfolio.status == "failed"
    assert store.saved == (portfolio, [])
    assert store.appended == []
    partial = service.partial_timeline_candidate
    assert partial is not None
    assert partial.verifier["passed"] is False
    assert [segment["poi"]["amapId"] for day in partial.itinerary_snapshot["days"] for segment in day["segments"]] == [
        "c1"
    ]
    assert partial.itinerary_snapshot["portfolioPendingSlots"] == [slot_by_occurrence["occ:night:day:1"]]
    assert partial.generation_lineage["partialTimeline"] == {
        "eligible": True,
        "reason": "density_slots_unresolved",
        "strictProposalVerifierPassed": False,
    }

    for field, value in (
        ("briefId", ""),
        ("poolId", ""),
        ("planningSlotId", ""),
        ("dayNumber", 2),
        ("occurrenceId", "occ:other:day:1"),
        ("sourceGoalId", "other"),
        ("requirementLevel", "optional"),
    ):
        invalid_snapshot = copy.deepcopy(partial.itinerary_snapshot)
        invalid_snapshot["portfolioPendingSlots"][0][field] = value
        invalid = partial.model_copy(update={"itinerary_snapshot": invalid_snapshot})
        assert service._partial_timeline_eligible(invalid) is False

    no_anchor_snapshot = copy.deepcopy(partial.itinerary_snapshot)
    no_anchor_snapshot["days"][0]["segments"] = []
    no_anchor = partial.model_copy(update={"itinerary_snapshot": no_anchor_snapshot})
    assert service._partial_timeline_eligible(no_anchor) is False

    for poi_mutation in (
        {"source": "agent-text-timeline"},
        {"longitude": "not-a-coordinate"},
    ):
        invalid_snapshot = copy.deepcopy(partial.itinerary_snapshot)
        invalid_snapshot["days"][0]["segments"][0]["poi"].update(poi_mutation)
        invalid = partial.model_copy(update={"itinerary_snapshot": invalid_snapshot})
        assert service._partial_timeline_eligible(invalid) is False

    non_subset_snapshot = copy.deepcopy(partial.itinerary_snapshot)
    non_subset_snapshot["portfolioRequiredCandidateBindings"].append(
        {
            **non_subset_snapshot["portfolioRequiredCandidateBindings"][0],
            "occurrenceId": "occ:outside:day:1",
        }
    )
    non_subset = partial.model_copy(update={"itinerary_snapshot": non_subset_snapshot})
    assert service._partial_timeline_eligible(non_subset) is False

    identity_reuse = partial.model_copy(
        update={
            "verifier": partial.verifier
            | {
                "hardFailures": [
                    *partial.verifier["hardFailures"],
                    "goal_occurrence_identity_reused:hard:campus:c1",
                ],
            }
        }
    )
    assert service._partial_timeline_eligible(identity_reuse) is False

    for invalid_anchor in (
        {**_candidate("c1", 2), "source": "agent-text-timeline"},
        {**_candidate("c1", 2), "longitude": "not-a-coordinate"},
        {**_candidate("B000A8UIN8", 2), "longitude": 0.0, "latitude": 0.0},
    ):
        invalid_store = FakeStore()
        invalid_service = CreativePortfolioStagingService(invalid_store)
        _, invalid_visible = invalid_service.stage(
            session_id="session",
            source_user_turn_id="invalid-user",
            source_assistant_turn_id="invalid-assistant",
            expected_base_version_id=None,
            observation_fingerprint="o" * 16,
            request_fingerprint="r" * 16,
            ledger=ledger,
            generated=generated,
            universe=SharedCandidateUniverse(
                {"campus_visit": [invalid_anchor], "night_view": []},
                1,
                0,
            ),
            snapshot_builder=snapshot,
            goal_occurrence_plan=occurrence_plan,
        )
        assert invalid_visible == []
        assert invalid_store.saved[1] == []
        assert invalid_service.partial_timeline_candidate is None

    empty_service = CreativePortfolioStagingService(FakeStore())
    empty_service.stage(
        session_id="session",
        source_user_turn_id="empty-user",
        source_assistant_turn_id="empty-assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {"campus_visit": [], "night_view": []},
            0,
            0,
        ),
        snapshot_builder=snapshot,
        goal_occurrence_plan=occurrence_plan,
    )
    assert empty_service.partial_timeline_candidate is None

    reused_night = {**_candidate("tower", 1), "amapId": "c1"}
    reused_service = CreativePortfolioStagingService(FakeStore())
    reused_service.stage(
        session_id="session",
        source_user_turn_id="reused-user",
        source_assistant_turn_id="reused-assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {
                "campus_visit": [_candidate("c1", 2)],
                "night_view": [reused_night],
            },
            2,
            0,
        ),
        snapshot_builder=snapshot,
        goal_occurrence_plan=occurrence_plan,
    )
    assert reused_service.partial_timeline_candidate is None


def test_density_shortfall_exposes_verified_partial_timeline_candidate_without_offering_it():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "partial",
                        "title": "部分时间轴",
                        "primaryAxis": "local_immersion",
                        "requiredGoalIds": ["campus"],
                        "optionalExperiences": [
                            {
                                "family": "neighborhood_walk",
                                "description": "街区漫步",
                            }
                        ],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "高校与街区",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["requiredGoalCount=1", "briefOptionalCount=1"],
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-slot",
                            "dayNumber": 1,
                            "timeWindow": "09:00-11:00",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "kind": "visit",
                            "rawNeed": "985高校",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "walk-slot",
                            "dayNumber": 1,
                            "timeWindow": "14:00-16:00",
                            "startTime": "14:00",
                            "durationMinutes": 120,
                            "kind": "activity",
                            "rawNeed": "街区漫步",
                            "routeAnchor": True,
                            "optionalExperienceFamily": "neighborhood_walk",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-pool",
                            "briefId": "partial",
                            "rawNeed": "985高校",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-slot"],
                        },
                        {
                            "poolId": "walk-pool",
                            "briefId": "partial",
                            "rawNeed": "街区漫步",
                            "city": "北京",
                            "intentType": "neighborhood_walk",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "optionalExperienceFamily": "neighborhood_walk",
                            "assignToSlots": ["walk-slot"],
                        },
                    ],
                }
            ],
        }
    )
    store = FakeStore()
    service = CreativePortfolioStagingService(store)

    def snapshot(required, _soft, _optional, brief_id):
        poi = required["campus"]
        return {
            "id": "plan_partial",
            "title": brief_id,
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_campus",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "kind": "visit",
                            "poi": poi,
                            "semanticMetadata": _required_semantic(poi),
                        }
                    ],
                }
            ],
        }

    portfolio, visible = service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse(
            {"campus_visit": [_candidate("c1", 2)], "neighborhood_walk": []},
            2,
            0,
        ),
        snapshot_builder=snapshot,
    )

    assert visible == []
    assert portfolio.status == "failed"
    assert store.saved[1] == []
    assert service.partial_timeline_candidate is not None
    assert service.partial_timeline_candidate.verifier["passed"] is False
    assert service.partial_timeline_candidate.itinerary_snapshot["days"][0]["segments"][0]["poi"]["amapId"] == "c1"
    assert service.partial_timeline_candidate.generation_lineage["partialTimeline"] == {
        "eligible": True,
        "reason": "density_slots_unresolved",
        "strictProposalVerifierPassed": False,
    }
    mixed_snapshot = copy.deepcopy(service.partial_timeline_candidate.itinerary_snapshot)
    mixed_snapshot["days"][0]["segments"].append(
        {
            "id": "seg_fake_anchor",
            "startTime": "14:00",
            "endTime": "16:00",
            "kind": "activity",
            "poi": {
                "amapId": "FAKE",
                "name": "未核验地点",
                "source": "agent-text-timeline",
                "longitude": 116.4,
                "latitude": 39.9,
            },
            "semanticMetadata": {"routeAnchor": True},
        }
    )
    mixed_candidate = service.partial_timeline_candidate.model_copy(update={"itinerary_snapshot": mixed_snapshot})
    assert service._partial_timeline_eligible(mixed_candidate) is False
    optional_only_snapshot = copy.deepcopy(service.partial_timeline_candidate.itinerary_snapshot)
    optional_semantic = optional_only_snapshot["days"][0]["segments"][0]["semanticMetadata"]
    optional_semantic["required"] = False
    optional_semantic.pop("goalId", None)
    optional_semantic.pop("sourceGoalId", None)
    optional_only_snapshot["portfolioRequiredCandidateBindings"] = []
    optional_only_candidate = service.partial_timeline_candidate.model_copy(
        update={"itinerary_snapshot": optional_only_snapshot}
    )
    assert service._partial_timeline_eligible(optional_only_candidate) is False
    stale_brief_snapshot = copy.deepcopy(service.partial_timeline_candidate.itinerary_snapshot)
    stale_semantic = stale_brief_snapshot["days"][0]["segments"][0]["semanticMetadata"]
    stale_semantic.pop("creativeBriefId", None)
    stale_brief_snapshot["portfolioRequiredCandidateBindings"][0]["briefId"] = "other-brief"
    stale_brief_candidate = service.partial_timeline_candidate.model_copy(
        update={"itinerary_snapshot": stale_brief_snapshot}
    )
    assert service._partial_timeline_eligible(stale_brief_candidate) is False


def test_explicit_soft_occurrence_gap_is_partial_eligible_only_with_exact_pending_slot():
    """A removed explicit-soft segment is recoverable only through exact slot proof."""

    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "seg_campus",
                        "poi": {
                            "amapId": "campus_amap",
                            "name": "北京大学",
                            "city": "北京市",
                            "type": "科教文化服务;学校;高等院校",
                            "source": "amap-place-search",
                            "longitude": 116.31,
                            "latitude": 39.99,
                        },
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "required": True,
                            "groundingStatus": "selected",
                            "sourceGoalId": "goal_campus",
                            "creativeBriefId": "brief_soft_gap",
                            "poolId": "campus_pool",
                            "planningSlotId": "campus_slot",
                        },
                    }
                ],
            }
        ],
        "portfolioRequiredCandidateBindings": [
            {
                "sourceGoalId": "goal_campus",
                "briefId": "brief_soft_gap",
                "planningSlotId": "campus_slot",
                "amapId": "campus_amap",
            }
        ],
        "portfolioGoalOccurrencePlan": {
            "occurrences": [
                {
                    "occurrenceId": "occ:goal_meal:day:1",
                    "sourceGoalId": "goal_meal",
                    "dayNumber": 1,
                    "requirementLevel": "explicit_soft",
                }
            ],
        },
        "portfolioPendingSlots": [
            {
                "briefId": "brief_soft_gap",
                "poolId": "meal_pool",
                "planningSlotId": "meal_slot",
                "dayNumber": 1,
                "sourceGoalId": "goal_meal",
                "occurrenceId": "occ:goal_meal:day:1",
                "intent": "local_food",
                "timeWindow": "12:10-13:20",
            }
        ],
    }
    verifier = {
        "hardFailures": [
            "route_anchor_target_mismatch:day_1:1/2",
            "goal_occurrence_missing:occ:goal_meal:day:1:day_1",
        ],
        "dayAnchorShortfalls": {"1": 1},
        "requiredCandidateBindingActualCount": 1,
        "hardCandidateLineageMissingCount": 0,
    }
    candidate = PlanCandidate.model_validate(
        {
            "proposalId": "proposal_soft_gap",
            "portfolioId": "portfolio_soft_gap",
            "brief": {
                "briefId": "brief_soft_gap",
                "title": "高校与当地午餐",
                "primaryAxis": "local_immersion",
                "requiredGoalIds": ["goal_campus"],
            },
            "itinerarySnapshot": snapshot,
            "score": {
                "hardConstraintPassed": False,
                "preferenceFit": 70,
                "thematicCoherence": 70,
                "experienceDiversity": 70,
                "routeEfficiency": 60,
                "pacingQuality": 60,
                "novelty": 50,
                "robustness": 50,
                "uncertaintyPenalty": 10,
            },
            "verifier": verifier,
            "canonicalSignature": "soft-gap-signature",
        }
    )

    assert CreativePortfolioStagingService._partial_timeline_eligible(candidate) is True

    for mutation in (
        lambda pending: pending.clear(),
        lambda pending: pending[0].update({"dayNumber": 2}),
        lambda pending: pending[0].update({"dayNumber": "invalid"}),
        lambda pending: pending[0].update({"briefId": "other_brief"}),
        lambda pending: pending[0].update({"poolId": ""}),
        lambda pending: pending[0].update({"planningSlotId": ""}),
        lambda pending: pending[0].update({"sourceGoalId": "other_goal"}),
        lambda pending: pending[0].update({"occurrenceId": "occ:other:day:1"}),
    ):
        invalid_snapshot = copy.deepcopy(snapshot)
        mutation(invalid_snapshot["portfolioPendingSlots"])
        invalid = candidate.model_copy(update={"itinerary_snapshot": invalid_snapshot})
        assert CreativePortfolioStagingService._partial_timeline_eligible(invalid) is False

    sanitizable_snapshot = copy.deepcopy(snapshot)
    sanitizable_snapshot["portfolioPendingSlots"] = []
    sanitizable_snapshot["days"][0]["segments"].append(
        {
            "id": "meal_detour_soft",
            "kind": "meal",
            "startTime": "12:10",
            "endTime": "13:20",
            "poi": {
                "name": "京味餐厅",
                "amapId": "meal_amap",
                "city": "北京市",
                "type": "餐饮服务;中餐厅",
                "address": "北京市测试路1号",
                "source": "amap-place-search",
                "longitude": 116.41,
                "latitude": 39.91,
            },
            "semanticMetadata": {
                "routeAnchor": True,
                "required": False,
                "requirementLevel": "explicit_soft",
                "groundingStatus": "selected",
                "intentType": "meal",
                "sourceGoalId": "goal_meal",
                "creativeBriefId": "brief_soft_gap",
                "poolId": "meal_pool",
                "planningSlotId": "meal_slot",
            },
        }
    )
    sanitizable_snapshot["portfolioRouteQuality"] = {
        "status": "failed",
        "routeQualityIssues": [
            {
                "code": "meal_detour_high",
                "mealSegmentId": "meal_detour_soft",
                "routePair": {
                    "fromSegmentId": "seg_campus",
                    "toSegmentId": "meal_detour_soft",
                },
            }
        ],
    }
    proof_failure = "portfolio_route_decision_proof:route_time_window_mismatch:meal_detour_soft"
    sanitizable = candidate.model_copy(
        update={
            "itinerary_snapshot": sanitizable_snapshot,
            "verifier": verifier
            | {
                "hardFailures": [
                    "route_anchor_target_mismatch:day_1:1/2",
                    proof_failure,
                ],
            },
        }
    )
    assert CreativePortfolioStagingService._blocking_partial_failures(sanitizable) == []
    assert CreativePortfolioStagingService._partial_timeline_eligible(sanitizable) is True

    unrelated_proof_failure = candidate.model_copy(
        update={
            "itinerary_snapshot": sanitizable_snapshot,
            "verifier": verifier
            | {
                "hardFailures": [
                    "route_anchor_target_mismatch:day_1:1/2",
                    "portfolio_route_decision_proof:route_time_window_mismatch:seg_campus",
                ],
            },
        }
    )
    assert CreativePortfolioStagingService._blocking_partial_failures(
        unrelated_proof_failure
    ) == ["portfolio_route_decision_proof:route_time_window_mismatch:seg_campus"]
    assert CreativePortfolioStagingService._partial_timeline_eligible(
        unrelated_proof_failure
    ) is False

    hard_snapshot = copy.deepcopy(snapshot)
    hard_snapshot["portfolioGoalOccurrencePlan"]["occurrences"][0]["requirementLevel"] = "hard"
    hard = candidate.model_copy(update={"itinerary_snapshot": hard_snapshot})
    assert CreativePortfolioStagingService._partial_timeline_eligible(hard) is False

    unknown = candidate.model_copy(
        update={
            "verifier": verifier
            | {
                "hardFailures": [
                    "route_anchor_target_mismatch:day_1:1/2",
                    "goal_occurrence_missing:occ:unknown:day:1:day_1",
                ],
            }
        }
    )
    assert CreativePortfolioStagingService._partial_timeline_eligible(unknown) is False

    identity_reuse = candidate.model_copy(
        update={
            "verifier": verifier
            | {
                "hardFailures": [
                    "route_anchor_target_mismatch:day_1:1/2",
                    "goal_occurrence_identity_reused:distinct:goal_campus:campus_amap",
                ],
            }
        }
    )
    assert CreativePortfolioStagingService._blocking_partial_failures(identity_reuse) == [
        "goal_occurrence_identity_reused:distinct:goal_campus:campus_amap"
    ]
    assert CreativePortfolioStagingService._priority_portfolio_failures(identity_reuse) == [
        "goal_occurrence_identity_reused:distinct:goal_campus:campus_amap"
    ]
    assert CreativePortfolioStagingService._partial_timeline_eligible(identity_reuse) is False


def test_route_provider_failure_does_not_create_partial_timeline_candidate():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("route", "路线待核验", "classic", ["campus"])],
        }
    )

    class ProviderUnavailableRouteFeasibility:
        @staticmethod
        def prepare(snapshot, **_kwargs):
            work = copy.deepcopy(snapshot)
            work["portfolioRouteVerificationRequired"] = True
            work["portfolioRouteEvidence"] = []
            work["routeEvidence"] = []
            work["portfolioRouteQuality"] = {
                "status": "provider_error",
                "routeQualityIssues": [
                    {"code": "route_provider_unavailable"},
                    {
                        "code": "route_schedule_projection_failed",
                        "workerFailureClass": "RuntimeError",
                        "sanitizedWorkerFailureMessage": "schedule projection failed",
                    },
                ],
                "warnings": ["route provider unavailable"],
            }
            return PortfolioRouteFeasibilityResult(
                status="provider_error",
                snapshot=work,
                route_quality_issues=work["portfolioRouteQuality"]["routeQualityIssues"],
                warnings=["route provider unavailable"],
                requires_route_verification=True,
                provider_state="provider_error",
            )

    def snapshot(required, _soft, _optional, brief_id):
        poi = required["campus"]
        return {
            "id": "plan_route_partial",
            "title": brief_id,
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_campus",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "kind": "visit",
                            "poi": poi,
                            "semanticMetadata": _required_semantic(poi),
                        }
                    ],
                }
            ],
        }

    store = FakeStore()
    service = CreativePortfolioStagingService(
        store,
        route_feasibility_service=ProviderUnavailableRouteFeasibility(),
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
        universe=SharedCandidateUniverse({"campus_visit": [_candidate("c1", 2)]}, 1, 0),
        snapshot_builder=snapshot,
    )

    assert visible == []
    assert portfolio.status == "failed"
    assert portfolio.failure_reason == (
        "portfolio_route_quality_unresolved:route:route_provider_unavailable;route:route_schedule_projection_failed"
    )
    assert store.saved[1] == []
    assert service.partial_timeline_candidate is None
    details = service.last_staging_metrics["briefMetrics"][0]["routeQualityIssueDetails"]
    assert details == [
        {"code": "route_provider_unavailable"},
        {
            "code": "route_schedule_projection_failed",
            "workerFailureClass": "RuntimeError",
            "sanitizedWorkerFailureMessage": "schedule projection failed",
        },
    ]
    assert service.last_staging_metrics["perBriefDiagnostics"][0]["routeQualityIssueDetails"] == details


def test_route_worker_exception_does_not_create_partial_timeline_candidate():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("route", "路线 worker 异常", "classic", ["campus"])],
        }
    )

    class RaisingRouteFeasibility:
        @staticmethod
        def prepare(_snapshot, **_kwargs):
            assert _kwargs["transport_mode"] == "unspecified"
            budget = current_amap_call_budget()
            assert budget is not None
            assert budget.route_refresh_max == 0
            assert budget.snapshot()["derivation"]["routeWorkLeaseCount"] == 0
            assert budget.try_acquire(
                endpoint="route/transit",
                keyword="staging-worker-failure",
                source="staging-worker-test",
            ) is False
            raise RuntimeError("recorded route worker failure")

    def snapshot(required, _soft, _optional, brief_id):
        poi = required["campus"]
        return {
            "id": "plan_route_worker_partial",
            "title": brief_id,
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "seg_campus",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "kind": "visit",
                            "poi": poi,
                            "semanticMetadata": _required_semantic(poi),
                        }
                    ],
                }
            ],
        }

    store = FakeStore()
    service = CreativePortfolioStagingService(
        store,
        route_feasibility_service=RaisingRouteFeasibility(),
    )
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    with amap_call_budget_scope(budget):
        portfolio, visible = service.stage(
            session_id="session",
            source_user_turn_id="user",
            source_assistant_turn_id="assistant",
            expected_base_version_id=None,
            observation_fingerprint="o" * 16,
            request_fingerprint="r" * 16,
            ledger=ledger,
            generated=generated,
            universe=SharedCandidateUniverse(
                {"campus_visit": [_candidate("c1", 2)]},
                1,
                0,
            ),
            snapshot_builder=snapshot,
        )

    assert visible == []
    assert portfolio.status == "failed"
    assert service.partial_timeline_candidate is None
    metric = service.last_staging_metrics["briefMetrics"][0]
    # Route capacity starts at zero until production signs an exact lease.
    # The worker failure is still attributable, but it cannot imply a
    # Provider submission in this lease-less fixture.
    assert metric["routePreflightCallCount"] == 0
    assert metric["routeProviderCallCount"] == 0
    assert metric["providerCacheHitCount"] == 0
    ledger = metric["routeExecutionLedger"]
    assert ledger["submittedLegCount"] == 0
    assert ledger["providerCallCount"] == 0
    assert ledger["workerFailureClass"] == "RuntimeError"


def test_repeated_required_occurrence_with_one_physical_candidate_reports_missing_day():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "Beijing",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {"goalId": "night", "intentType": "night_view", "requiredMin": 2},
                ],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "night",
                        "title": "Night",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["night"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "night one",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            },
                            {
                                "dayNumber": 2,
                                "role": "night two",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            },
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "night-d1",
                            "dayNumber": 1,
                            "timeWindow": "night",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        },
                        {
                            "slotId": "night-d2",
                            "dayNumber": 2,
                            "timeWindow": "night",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "night-d1-pool",
                            "briefId": "night",
                            "rawNeed": "night",
                            "city": "Beijing",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-d1"],
                        },
                        {
                            "poolId": "night-d2-pool",
                            "briefId": "night",
                            "rawNeed": "night",
                            "city": "Beijing",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-d2"],
                        },
                    ],
                }
            ],
        }
    )
    store = FakeStore()

    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"night_view": [_candidate("tower", 1)]}, 1, 0),
        snapshot_builder=lambda *_: {"days": []},
        goal_occurrence_plan={
            "schemaVersion": "goal-occurrence-plan-v1",
            "avoidRecentEntities": True,
            "sourceFingerprint": "o" * 16,
            "occurrences": [
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:night:day:2",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                },
            ],
        },
    )

    assert visible == []
    assert "day_2:0/1" in str(portfolio.failure_reason)
    assert store.saved[1] == []


def test_staging_fails_closed_when_required_goal_cardinality_needs_new_timeline_slots():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [{"goalId": "museum", "intentType": "museum", "requiredMin": 2}]
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "classic",
                        "title": "经典",
                        "primaryAxis": "classic",
                        "requiredGoalIds": ["museum"],
                    }
                }
            ],
        }
    )
    store = FakeStore()
    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"museum": [_candidate("m1", 2), _candidate("m2", 3)]}, 1, 0),
        snapshot_builder=lambda *_: {"days": []},
    )
    assert visible == []
    assert portfolio.status == "failed"
    assert portfolio.failure_reason == "required_goal_cardinality_not_supported:museum"
    assert store.saved[1] == []


def test_staging_keeps_same_goal_on_two_controller_days_as_two_distinct_optimizer_slots():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "Beijing",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [{"goalId": "campus", "intentType": "campus_visit", "requiredMin": 2}],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "two-days",
                        "title": "Two day campus",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["campus"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "day one",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["pace=standard"],
                            },
                            {
                                "dayNumber": 2,
                                "role": "day two",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["pace=standard"],
                            },
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-d1",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "campus-d2",
                            "dayNumber": 2,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-d1-pool",
                            "briefId": "two-days",
                            "rawNeed": "campus",
                            "city": "Beijing",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-d1"],
                        },
                        {
                            "poolId": "campus-d2-pool",
                            "briefId": "two-days",
                            "rawNeed": "campus",
                            "city": "Beijing",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-d2"],
                        },
                    ],
                }
            ],
        }
    )
    captured = {}

    def snapshot(required, _soft, _optional, _brief_id):
        captured.update(required)
        days = []
        for _occurrence_id, candidate in sorted(required.items()):
            days.append(
                {
                    "dayNumber": int(candidate["dayNumber"]),
                    "segments": [
                        {
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": candidate,
                            "semanticMetadata": _required_semantic(candidate),
                        }
                    ],
                }
            )
        return {"id": "plan", "days": days}

    portfolio, visible = CreativePortfolioStagingService(FakeStore()).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [_candidate("pku", 2), _candidate("thu", 1)]}, 2, 0),
        snapshot_builder=snapshot,
        goal_occurrence_plan={
            "schemaVersion": "goal-occurrence-plan-v1",
            "avoidRecentEntities": True,
            "sourceFingerprint": "o" * 16,
            "occurrences": [
                {
                    "occurrenceId": "occ:campus:day:1",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:campus:day:2",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                },
            ],
        },
    )

    assert portfolio.status == "awaiting_selection"
    assert visible
    assert set(captured) == {"occ:campus:day:1", "occ:campus:day:2"}
    assert {value["amapId"] for value in captured.values()} == {"pku", "thu"}


def test_staging_rejects_occurrence_contract_that_does_not_cover_required_cardinality():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "Beijing",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {"goalId": "night", "intentType": "night_view", "requiredMin": 2},
                ],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "night",
                        "title": "Night",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["night"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "night",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["pace=standard"],
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "night-d1",
                            "dayNumber": 1,
                            "timeWindow": "night",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night view",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "night-d1-pool",
                            "briefId": "night",
                            "rawNeed": "night view",
                            "city": "Beijing",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-d1"],
                        }
                    ],
                }
            ],
        }
    )
    store = FakeStore()

    portfolio, visible = CreativePortfolioStagingService(store).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"night_view": [_candidate("tower", 1)]}, 1, 0),
        snapshot_builder=lambda *_: {"days": []},
        goal_occurrence_plan={
            "schemaVersion": "goal-occurrence-plan-v1",
            "avoidRecentEntities": True,
            "sourceFingerprint": "o" * 16,
            "occurrences": [
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                }
            ],
        },
    )

    assert visible == []
    assert portfolio.failure_reason == "portfolio_goal_occurrence_cardinality_mismatch:night:1/2"
    assert store.saved[1] == []


def test_staging_accepts_two_night_views_and_two_daily_meals_from_occurrence_contract():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "Beijing",
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {"goalId": "campus", "intentType": "campus_visit", "requiredMin": 1},
                    {"goalId": "night", "intentType": "night_view", "requiredMin": 2},
                    {"goalId": "meal", "intentType": "meal", "requirementLevel": "soft_experience"},
                ],
            },
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "daily",
                        "title": "Daily",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["campus", "night"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "campus lunch night",
                                "targetRouteAnchors": 3,
                                "densityEvidence": ["pace=standard"],
                            },
                            {
                                "dayNumber": 2,
                                "role": "lunch night",
                                "targetRouteAnchors": 2,
                                "densityEvidence": ["pace=standard"],
                            },
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-d1",
                            "dayNumber": 1,
                            "timeWindow": "09:00-11:00",
                            "startTime": "09:00",
                            "durationMinutes": 120,
                            "kind": "campus",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "meal-d1",
                            "dayNumber": 1,
                            "timeWindow": "12:00-13:00",
                            "startTime": "12:00",
                            "durationMinutes": 60,
                            "kind": "meal",
                            "rawNeed": "local lunch",
                            "routeAnchor": True,
                            "softGoalId": "meal",
                        },
                        {
                            "slotId": "night-d1",
                            "dayNumber": 1,
                            "timeWindow": "19:00-20:00",
                            "startTime": "19:00",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night view",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        },
                        {
                            "slotId": "meal-d2",
                            "dayNumber": 2,
                            "timeWindow": "12:00-13:00",
                            "startTime": "12:00",
                            "durationMinutes": 60,
                            "kind": "meal",
                            "rawNeed": "local lunch",
                            "routeAnchor": True,
                            "softGoalId": "meal",
                        },
                        {
                            "slotId": "night-d2",
                            "dayNumber": 2,
                            "timeWindow": "19:00-20:00",
                            "startTime": "19:00",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night view",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        },
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-d1-pool",
                            "briefId": "daily",
                            "rawNeed": "campus",
                            "city": "Beijing",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-d1"],
                        },
                        {
                            "poolId": "meal-d1-pool",
                            "briefId": "daily",
                            "rawNeed": "local lunch",
                            "city": "Beijing",
                            "intentType": "meal",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "softGoalId": "meal",
                            "assignToSlots": ["meal-d1"],
                        },
                        {
                            "poolId": "night-d1-pool",
                            "briefId": "daily",
                            "rawNeed": "night view",
                            "city": "Beijing",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-d1"],
                        },
                        {
                            "poolId": "meal-d2-pool",
                            "briefId": "daily",
                            "rawNeed": "local lunch",
                            "city": "Beijing",
                            "intentType": "meal",
                            "targetCount": 1,
                            "requirementLevel": "optional",
                            "softGoalId": "meal",
                            "assignToSlots": ["meal-d2"],
                        },
                        {
                            "poolId": "night-d2-pool",
                            "briefId": "daily",
                            "rawNeed": "night view",
                            "city": "Beijing",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-d2"],
                        },
                    ],
                }
            ],
        }
    )
    universe = SharedCandidateUniverse(
        {
            "campus_visit": [{**_candidate("campus", 1), "city": "Beijing"}],
            "night_view": [
                {**_candidate("tower", 1), "city": "Beijing"},
                {**_candidate("square", 2), "city": "Beijing"},
            ],
            "meal": [
                {**_candidate("meal-one", 1), "city": "Beijing"},
                {**_candidate("meal-two", 2), "city": "Beijing"},
            ],
        },
        3,
        0,
    )

    def snapshot(required, soft, _optional, _brief_id):
        by_day = {1: [], 2: []}
        for identity, candidate in [*required.items(), *soft.items()]:
            semantic = (
                _required_semantic(candidate)
                if candidate.get("requirementLevel") == "hard"
                else {
                    "goalId": candidate.get("softGoalId"),
                    "occurrenceId": candidate.get("occurrenceId"),
                    "intentType": candidate.get("intentType"),
                    "poolId": candidate.get("poolId"),
                    "planningSlotId": candidate.get("planningSlotId"),
                    "creativeBriefId": candidate.get("briefId"),
                    "required": False,
                    "routeAnchor": True,
                    "groundingStatus": "selected",
                }
            )
            start = candidate["startTime"]
            hour = int(start[:2])
            end = f"{hour + 1:02d}:{start[3:]}"
            by_day[int(candidate["dayNumber"])].append(
                {
                    "id": f"seg-{identity}",
                    "startTime": start,
                    "endTime": end,
                    "poi": candidate,
                    "semanticMetadata": semantic,
                }
            )
        return {
            "id": "plan",
            "city": "Beijing",
            "portfolioDailyCapacityPlan": {
                "1": {
                    "usableMinutes": 510,
                    "plannedMinutes": 120,
                    "routeReserveMinutes": 0,
                    "bufferMinutes": 10,
                    "intentionalFreeMinutes": 380,
                    "unexplainedGapMinutes": 0,
                    "targetRouteAnchors": 1,
                },
                "2": {
                    "usableMinutes": 510,
                    "plannedMinutes": 0,
                    "routeReserveMinutes": 0,
                    "bufferMinutes": 0,
                    "intentionalFreeMinutes": 510,
                    "unexplainedGapMinutes": 0,
                    "targetRouteAnchors": 0,
                },
            },
            "days": [
                {"dayNumber": day, "segments": sorted(items, key=lambda item: item["startTime"])}
                for day, items in sorted(by_day.items())
            ],
        }

    portfolio, visible = CreativePortfolioStagingService(FakeStore()).stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=universe,
        snapshot_builder=snapshot,
        goal_occurrence_plan={
            "schemaVersion": "goal-occurrence-plan-v1",
            "avoidRecentEntities": True,
            "sourceFingerprint": "o" * 16,
            "occurrences": [
                {
                    "occurrenceId": "occ:campus:day:1",
                    "sourceGoalId": "campus",
                    "intentType": "campus_visit",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:meal:day:1",
                    "sourceGoalId": "meal",
                    "intentType": "meal",
                    "dayNumber": 1,
                    "requirementLevel": "explicit_soft",
                },
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                },
                {
                    "occurrenceId": "occ:meal:day:2",
                    "sourceGoalId": "meal",
                    "intentType": "meal",
                    "dayNumber": 2,
                    "requirementLevel": "explicit_soft",
                },
                {
                    "occurrenceId": "occ:night:day:2",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 2,
                    "requirementLevel": "hard",
                },
            ],
        },
    )

    assert portfolio.status == "awaiting_selection", portfolio.failure_reason
    assert visible
    assert visible[0].verifier["dayAnchorActuals"] == {"1": 3, "2": 2}
    assert visible[0].verifier["dailyCapacityEvidence"]["1"]["targetRouteAnchors"] == 3
    assert visible[0].verifier["dailyCapacityEvidence"]["2"]["targetRouteAnchors"] == 2
    assert not visible[0].verifier["hardFailures"]


def test_hard_binding_clones_shared_candidate_for_each_brief_without_stale_lineage():
    service = CreativePortfolioStagingService(FakeStore())
    first = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                _strict_brief("first", "第一案", "classic", ["campus"]),
                _strict_brief("second", "第二案", "local_immersion", ["campus"]),
            ],
        }
    )
    shared = {
        **_candidate("shared-campus", 1),
        "briefId": "stale",
        "poolId": "stale-pool",
        "planningSlotId": "stale-slot",
    }
    first_bound = service._bind_required_to_brief({"campus": shared}, first.proposals[0], occurrence_contract=None)
    second_bound = service._bind_required_to_brief({"campus": shared}, first.proposals[1], occurrence_contract=None)
    assert shared["briefId"] == "stale"
    assert first_bound["campus"] is not shared and second_bound["campus"] is not shared
    assert first_bound["campus"]["briefId"] == "first"
    assert second_bound["campus"]["briefId"] == "second"
    assert first_bound["campus"]["poolId"] == "first-campus-pool"
    assert second_bound["campus"]["planningSlotId"] == "second-campus"


def test_required_occurrence_prioritizes_its_owned_pool_before_global_beam():
    service = CreativePortfolioStagingService(FakeStore())
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "night-brief",
                        "title": "夜景",
                        "primaryAxis": "photo_night",
                        "requiredGoalIds": ["night"],
                    },
                    "daySlots": [
                        {
                            "slotId": "night-day-2",
                            "dayNumber": 2,
                            "timeWindow": "evening",
                            "durationMinutes": 75,
                            "kind": "night_view",
                            "rawNeed": "城市夜景",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "night-day-2-pool",
                            "briefId": "night-brief",
                            "intentType": "night_view",
                            "city": "北京",
                            "rawNeed": "城市夜景",
                            "requirementLevel": "required",
                            "goalId": "night",
                            "targetCount": 1,
                            "assignToSlots": ["night-day-2"],
                        }
                    ],
                }
            ],
        }
    )
    global_candidates = [_candidate(f"global-{index}", 1) for index in range(7)]
    owned = _candidate("owned-day-2", 2)
    universe = SharedCandidateUniverse(
        pools={"night_view": global_candidates},
        unique_query_count=2,
        deduped_query_count=0,
        scoped_pool_candidates={("night-brief", "night-day-2-pool"): [owned]},
    )
    occurrence = SimpleNamespace(
        occurrence_id="occ:night:day:2",
        source_goal_id="night",
        intent_type="night_view",
        day_number=2,
        requirement_level="hard",
        distinct_group_id="distinct:night",
    )

    candidates = service._required_candidates_for_occurrence(
        universe=universe,
        skeleton=generated.proposals[0],
        occurrence=occurrence,
        fallback_candidates=global_candidates,
    )

    assert candidates[0]["amapId"] == owned["amapId"]
    assert candidates[0]["dayNumber"] == 2
    assert candidates[0]["occurrenceId"] == "occ:night:day:2"
    assert owned["amapId"] in {item["amapId"] for item in candidates[:6]}


def test_hard_binding_rejects_ambiguous_legacy_and_missing_required_pool():
    service = CreativePortfolioStagingService(FakeStore())
    ambiguous = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "ambiguous",
                        "title": "歧义",
                        "primaryAxis": "classic",
                        "requiredGoalIds": ["campus"],
                    },
                    "daySlots": [
                        {
                            "slotId": "d1",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                        {
                            "slotId": "d2",
                            "dayNumber": 2,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        },
                    ],
                    "intentPools": [],
                }
            ],
        }
    )
    try:
        service._bind_required_to_brief(
            {"campus": _candidate("c", 1)}, ambiguous.proposals[0], occurrence_contract=None
        )
    except ValueError as exc:
        assert str(exc) == "portfolio_required_legacy_binding_ambiguous"
    else:
        raise AssertionError("ambiguous legacy binding must fail closed")
    missing_pool = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "missing",
                        "title": "缺池",
                        "primaryAxis": "classic",
                        "requiredGoalIds": ["campus"],
                    },
                    "daySlots": [
                        {
                            "slotId": "d1",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "campus",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        }
                    ],
                    "intentPools": [],
                }
            ],
        }
    )
    try:
        service._bind_required_to_brief(
            {"campus": _candidate("c", 1)}, missing_pool.proposals[0], occurrence_contract=None
        )
    except ValueError as exc:
        assert str(exc) == "portfolio_required_occurrence_pool_missing"
    else:
        raise AssertionError("missing required pool must fail closed")


def test_staging_uses_assignment_brief_id_when_optimizer_order_is_reversed():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]},
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                _strict_brief("first", "第一案", "classic", ["campus"]),
                _strict_brief("second", "第二案", "local_immersion", ["campus"]),
            ],
        }
    )
    service = CreativePortfolioStagingService(FakeStore())
    shared = _candidate("shared-campus", 1)
    service.optimizer.solve = lambda **_: [
        PortfolioAssignment("second", {"campus": shared}, {}, [], 0.0, 0.0),
        PortfolioAssignment("first", {"campus": shared}, {}, [], 0.0, 0.0),
    ]

    def snapshot(required, _soft, _optional, brief_id):
        poi = required["campus"]
        return {
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": poi,
                            "semanticMetadata": _required_semantic(poi),
                        }
                    ],
                }
            ],
            "creativeBrief": {"briefId": brief_id},
        }

    portfolio, visible = service.stage(
        session_id="s",
        source_user_turn_id="u",
        source_assistant_turn_id="a",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [shared]}, 1, 0),
        snapshot_builder=snapshot,
    )
    assert portfolio.status == "awaiting_selection"
    assert {item.brief.brief_id for item in visible} == {"first", "second"}
    assert {
        item.itinerary_snapshot["days"][0]["segments"][0]["semanticMetadata"]["creativeBriefId"] for item in visible
    } == {"first", "second"}


def test_explicit_soft_candidate_is_rechecked_for_each_brief_contract():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {
                "requiredIntents": [
                    {
                        "goalId": "meal",
                        "intentType": "meal",
                        "requirementLevel": "soft_experience",
                    }
                ],
            },
        }
    )

    def proposal(brief_id, exact_entity):
        return {
            "brief": {
                "briefId": brief_id,
                "title": brief_id,
                "primaryAxis": "food_led",
                "requiredGoalIds": [],
                "dayRoles": [
                    {
                        "dayNumber": 1,
                        "role": "lunch",
                        "targetRouteAnchors": 1,
                        "densityEvidence": ["explicitSoftGoalCount=1"],
                    }
                ],
            },
            "daySlots": [
                {
                    "slotId": f"{brief_id}-meal",
                    "dayNumber": 1,
                    "timeWindow": "lunch",
                    "durationMinutes": 60,
                    "kind": "meal",
                    "rawNeed": exact_entity,
                    "routeAnchor": True,
                    "softGoalId": "meal",
                }
            ],
            "intentPools": [
                {
                    "poolId": "meal-pool",
                    "briefId": brief_id,
                    "rawNeed": exact_entity,
                    "city": "北京",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "optional",
                    "softGoalId": "meal",
                    "assignToSlots": [f"{brief_id}-meal"],
                    "entityBindingMode": "exact_entity",
                    "exactEntity": exact_entity,
                }
            ],
        }

    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                proposal("first", "餐厅甲"),
                proposal("second", "餐厅乙"),
            ],
        }
    )
    candidate = {
        "amapId": "meal-a",
        "name": "餐厅甲",
        "longitude": 116.3,
        "latitude": 39.9,
        "providerType": "餐饮服务;中餐厅;中餐厅",
        "source": "amap-place-search",
        "semanticPassed": True,
        "localRouteScore": 1,
    }
    captured = {}
    service = CreativePortfolioStagingService(FakeStore())

    def capture_solve(**kwargs):
        captured.update(kwargs)
        return []

    service.optimizer.solve = capture_solve
    service.stage(
        session_id="s",
        source_user_turn_id="u",
        source_assistant_turn_id="a",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"meal": [candidate]}, 1, 0),
        snapshot_builder=lambda *_: {"days": []},
    )

    assert [item["amapId"] for item in captured["soft_pools"]["first"]["meal"]] == ["meal-a"]
    assert captured["soft_pools"]["second"]["meal"] == []


def test_pending_theme_candidate_is_retained_bounded_and_without_query_or_url():
    target = {
        "schemaVersion": "portfolio-theme-completion-candidates-v1",
        "mealCandidatesBySlot": {},
        "areaCandidates": [],
    }
    candidate = {
        "amapId": "B000A0001",
        "name": "候选餐厅",
        "source": "amap-place-search",
        "rawQuery": "北京 私密原始查询",
        "sourceClaims": [{"url": "https://example.invalid/full", "claim": "待补证"}],
        "consumerAdmissionReport": {"classification": "pending_evidence"},
        "consumerAdmissionInput": {"briefId": "food", "planningSlotId": "meal_day_1"},
    }

    CreativePortfolioStagingService._capture_completion_candidate(
        target,
        candidate,
        family="local_food",
        slot_id="meal_day_1",
    )

    stored = target["mealCandidatesBySlot"]["meal_day_1"][0]
    assert stored["amapId"] == "B000A0001"
    assert "rawQuery" not in stored
    assert "url" not in stored["sourceClaims"][0]


def test_required_candidate_is_rechecked_against_consuming_brief_exact_entity():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "exact",
                        "title": "Exact",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["campus"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "campus",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-slot",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "北京大学",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-pool",
                            "briefId": "exact",
                            "rawNeed": "北京大学",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-slot"],
                            "entityBindingMode": "exact_entity",
                            "exactEntity": "北京大学",
                        }
                    ],
                }
            ],
        }
    )
    candidate = {
        **_candidate("thu", 1),
        "name": "清华大学",
        "providerType": "科教文化服务;学校;高等院校",
        "intentType": "campus_visit",
    }
    service = CreativePortfolioStagingService(FakeStore())

    try:
        service._bind_required_to_brief(
            {"campus": candidate},
            generated.proposals[0],
            occurrence_contract=None,
        )
    except ValueError as exc:
        assert str(exc) == "portfolio_required_candidate_semantic_mismatch:exact_entity_mismatch"
    else:
        raise AssertionError("cross-brief required evidence must be rechecked")


def test_required_candidate_gets_fresh_consumer_admission_after_brief_binding():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("campus", "高校方案", "classic", ["campus"])],
        }
    )
    candidate = {
        **_candidate("B000A0001", 3),
        "city": "北京",
        "providerTypeCode": "141201",
        "consumerAdmissionReport": {
            "consumerFingerprint": "stale-consumer",
            "scoreEligible": True,
        },
    }
    service = CreativePortfolioStagingService(FakeStore(), experience_grounding_v2_mode="enforce")

    rebound = service._bind_required_to_brief(
        {"campus": candidate},
        generated.proposals[0],
        occurrence_contract=None,
    )

    report = rebound["campus"]["consumerAdmissionReport"]
    assert report["classification"] == "admitted_final_anchor"
    assert report["scoreEligible"] is True
    assert report["consumerFingerprint"] != "stale-consumer"
    assert report["sourceScope"]["briefId"] is None


def test_required_admission_filters_before_optimizer_and_uses_bounded_backup():
    ledger = ConstraintLedgerCompiler().compile(
        {
            "city": "北京",
            "requestIntentContract": {"requiredIntents": [{"goalId": "campus", "intentType": "campus_visit"}]},
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [_strict_brief("campus", "高校方案", "classic", ["campus"])],
        }
    )
    missing_detail = {
        **_candidate("B000BAD001", 10),
        "city": "北京",
    }
    admitted_backup = {
        **_candidate("B000GOOD01", 1),
        "city": "北京",
        "providerTypeCode": "141201",
    }

    def snapshot(required, _soft, _optional, _brief_id):
        poi = required["campus"]
        semantic = {
            **_required_semantic(poi),
            "consumerAdmissionReport": copy.deepcopy(poi["consumerAdmissionReport"]),
        }
        return {
            "id": "plan-required-backup",
            "city": "北京",
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": "campus-segment",
                            "startTime": "09:00",
                            "endTime": "10:00",
                            "poi": poi,
                            "semanticMetadata": semantic,
                        }
                    ],
                }
            ],
        }

    service = CreativePortfolioStagingService(FakeStore(), experience_grounding_v2_mode="enforce")
    _portfolio, visible = service.stage(
        session_id="session",
        source_user_turn_id="user",
        source_assistant_turn_id="assistant",
        expected_base_version_id=None,
        observation_fingerprint="o" * 16,
        request_fingerprint="r" * 16,
        ledger=ledger,
        generated=generated,
        universe=SharedCandidateUniverse({"campus_visit": [missing_detail, admitted_backup]}, 2, 0),
        snapshot_builder=snapshot,
    )

    assert visible
    assert visible[0].itinerary_snapshot["days"][0]["segments"][0]["poi"]["amapId"] == "B000GOOD01"
    assert service.last_staging_metrics["consumerAdmissionPendingCount"] >= 1
    assert service.last_staging_metrics["consumerAdmissionAdmittedCount"] >= 1


def test_required_candidate_does_not_inherit_source_semantic_approval():
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "exact",
                        "title": "Exact",
                        "primaryAxis": "culture_deep_dive",
                        "requiredGoalIds": ["campus"],
                        "dayRoles": [
                            {
                                "dayNumber": 1,
                                "role": "campus",
                                "targetRouteAnchors": 1,
                                "densityEvidence": ["requiredGoalCount=1"],
                            }
                        ],
                    },
                    "daySlots": [
                        {
                            "slotId": "campus-slot",
                            "dayNumber": 1,
                            "timeWindow": "morning",
                            "durationMinutes": 60,
                            "kind": "visit",
                            "rawNeed": "北京大学",
                            "routeAnchor": True,
                            "requiredGoalId": "campus",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "campus-pool",
                            "briefId": "exact",
                            "rawNeed": "北京大学",
                            "city": "北京",
                            "intentType": "campus_visit",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "campus",
                            "assignToSlots": ["campus-slot"],
                            "entityBindingMode": "exact_entity",
                            "exactEntity": "北京大学",
                        }
                    ],
                }
            ],
        }
    )
    candidate = {
        **_candidate("pku", 1),
        "name": "北京大学",
        "providerType": "科教文化服务;学校;高等院校",
        "intentType": "campus_visit",
    }
    candidate.pop("semanticPassed", None)

    rebound = CreativePortfolioStagingService(FakeStore())._bind_required_to_brief(
        {"campus": candidate},
        generated.proposals[0],
        occurrence_contract=None,
    )

    assert rebound["campus"]["semanticPassed"] is True
    assert rebound["campus"]["semanticDecision"]["passed"] is True


def test_formal_occurrence_policy_is_nested_on_required_soft_and_segment_route_contracts():
    policy = {
        "accessPolicy": "public_outdoor_or_verified_controlled_access",
        "distinctnessPolicy": "distinct_physical_identity_per_occurrence",
        "timeWindow": {"start": "18:30", "end": "22:00"},
        "detourTolerance": {
            "maxGeneralizedCostDelta": 35,
            "maxDetourRatio": 0.35,
        },
        "evidenceFreshness": {
            "maxAgeHours": 24,
            "requiredForControlledAccess": True,
        },
        "confidence": 0.95,
    }
    occurrence_plan = GoalOccurrencePlan.model_validate(
        {
            "schemaVersion": "goal-occurrence-plan-v1",
            "avoidRecentEntities": True,
            "sourceFingerprint": "o" * 64,
            "occurrences": [
                {
                    "occurrenceId": "occ:night:day:1",
                    "sourceGoalId": "night",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "hard",
                    **policy,
                },
                {
                    "occurrenceId": "occ:night-soft:day:1",
                    "sourceGoalId": "night-soft",
                    "intentType": "night_view",
                    "dayNumber": 1,
                    "requirementLevel": "explicit_soft",
                    **policy,
                },
            ],
        }
    )
    generated = InitialCreativePortfolio.model_validate(
        {
            "schemaVersion": "initial-creative-portfolio-v1",
            "proposals": [
                {
                    "brief": {
                        "briefId": "night",
                        "title": "Night",
                        "primaryAxis": "photo_night",
                        "requiredGoalIds": ["night"],
                    },
                    "daySlots": [
                        {
                            "slotId": "night-slot",
                            "dayNumber": 1,
                            "timeWindow": "18:30-22:00",
                            "startTime": "19:00",
                            "durationMinutes": 60,
                            "kind": "night_view",
                            "rawNeed": "night view",
                            "routeAnchor": True,
                            "requiredGoalId": "night",
                        }
                    ],
                    "intentPools": [
                        {
                            "poolId": "night-pool",
                            "briefId": "night",
                            "rawNeed": "night view",
                            "city": "北京",
                            "intentType": "night_view",
                            "targetCount": 1,
                            "requirementLevel": "required",
                            "goalId": "night",
                            "assignToSlots": ["night-slot"],
                        }
                    ],
                }
            ],
        }
    )
    service = CreativePortfolioStagingService(FakeStore())
    required = service._bind_required_to_brief(
        {"occ:night:day:1": _candidate("tower", 1)},
        generated.proposals[0],
        occurrence_contract=occurrence_plan,
    )["occ:night:day:1"]
    soft = service._with_experience_spec_policy(
        _candidate("square", 2),
        occurrence_plan.occurrences[1],
    )
    expected_fingerprint = canonical_fingerprint(policy)

    assert required["experienceSpecPolicy"] == policy
    assert soft["experienceSpecPolicy"] == policy
    assert required["specFingerprint"] == expected_fingerprint
    assert soft["specFingerprint"] == expected_fingerprint
    assert required["routeContract"] == {
        "experienceSpecPolicy": policy,
        "specFingerprint": expected_fingerprint,
        "requiresProviderInsertionDecision": True,
    }
    required["consumerAdmissionInput"] = {
        "briefId": "night",
        "poolId": "night-pool",
        "planningSlotId": "night-slot",
        "dayNumber": 1,
        "city": "北京",
        "family": "night_view",
    }

    snapshot = {
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "night-segment",
                        "poi": required,
                        "semanticMetadata": _required_semantic(required),
                    }
                ],
            }
        ]
    }
    projected = service._with_experience_spec_route_contracts(
        snapshot,
        candidates=[required, soft],
    )
    assert projected["days"][0]["segments"][0]["semanticMetadata"]["routeContract"] == required["routeContract"]
    assert projected["days"][0]["segments"][0]["semanticMetadata"]["consumerAdmissionInput"] == required[
        "consumerAdmissionInput"
    ]


def test_consumer_admission_receives_nested_experience_spec_policy_and_fingerprint():
    policy = {
        "accessPolicy": "public_outdoor",
        "evidenceFreshness": {"maxAgeHours": 24},
    }
    fingerprint = canonical_fingerprint(policy)
    captured = {}

    class RecordingAdmission:
        def build_consumer_context(self, **kwargs):
            captured.update(copy.deepcopy(kwargs))
            return {"family": kwargs["family"]}

        def evaluate(self, _candidate, _consumer):
            return {
                "classification": "admitted_anchor",
                "scoreEligible": True,
                "gateResults": [],
            }

    service = CreativePortfolioStagingService(FakeStore(), experience_grounding_v2_mode="shadow")
    service.consumer_admission = RecordingAdmission()
    candidate = {
        **_candidate("tower", 1),
        "experienceSpecPolicy": policy,
        "specFingerprint": fingerprint,
        "routeContract": {
            "experienceSpecPolicy": policy,
            "specFingerprint": fingerprint,
        },
    }
    admitted = service._apply_consumer_admission(
        candidate,
        brief_id="night",
        pool=SimpleNamespace(
            pool_id="night-pool",
            city="北京",
            intent_type="night_view",
            optional_experience_family=None,
            exact_entity=None,
            entity_binding_mode="semantic",
            requirement_level="required",
            route_preference={},
            preferred_types=[],
            rejected_types=[],
        ),
        slot=SimpleNamespace(
            slot_id="night-slot",
            day_number=1,
            raw_need="night view",
            requirement_level="required",
            experience_shape="single_poi",
            experience_goal="night view",
            desired_signals=[],
            avoid_signals=[],
            evidence_requirements={},
            grounding_contract={},
            route_contract={},
            intent_fingerprint="intent-fingerprint",
            assigned_meal_family=None,
            time_window="18:30-22:00",
        ),
        family="night_view",
        metrics=service._new_admission_metrics(),
    )

    assert admitted is True
    assert captured["experience_spec_policy"] == policy
    assert captured["spec_fingerprint"] == fingerprint


def test_experience_spec_without_route_fields_still_requires_provider_insertion_decision():
    policy = {
        "accessPolicy": "public_outdoor",
        "evidenceFreshness": {"maxAgeHours": 24},
    }
    candidate = CreativePortfolioStagingService._with_experience_spec_policy(
        _candidate("tower", 1),
        policy,
    )

    assert candidate["experienceSpecPolicy"] == policy
    assert candidate["routeContract"]["requiresProviderInsertionDecision"] is True
    assert candidate["routeContract"]["specFingerprint"] == canonical_fingerprint(policy)


def test_search_semantic_scope_alone_does_not_activate_consumer_access_policy():
    candidate = CreativePortfolioStagingService._with_experience_spec_policy(
        _candidate("tower", 1),
        {
            "allowedDayNumbers": [1, 2],
            "experienceFamilies": ["public_city_view"],
            "unresolvedDimensions": [],
        },
    )

    assert "experienceSpecPolicy" not in candidate
    assert "specFingerprint" not in candidate
    assert "routeContract" not in candidate
