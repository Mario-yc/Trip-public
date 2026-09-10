import sqlite3
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.creative_exploration_frontier_service import CreativeExplorationFrontierService
from src.services.creative_planning_models import CreativeBrief, PlanCandidate, PlanPortfolio, PlanScoreVector
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.plan_proposal_commit_service import PlanProposalCommitService


def _candidate():
    return PlanCandidate(
        proposalId="proposal_a",
        portfolioId="portfolio_a",
        brief=CreativeBrief(briefId="brief_a", title="A", primaryAxis="classic", requiredGoalIds=[]),
        itinerarySnapshot={"days": []},
        score=PlanScoreVector(
            hardConstraintPassed=True,
            preferenceFit=80,
            thematicCoherence=80,
            experienceDiversity=80,
            routeEfficiency=80,
            pacingQuality=80,
            novelty=80,
            robustness=80,
            uncertaintyPenalty=10,
        ),
        verifier={"passed": True},
        canonicalSignature="a" * 16,
    )


@pytest.mark.parametrize("max_visible", [5, 6, 12])
def test_store_uses_configured_visible_and_generated_bounds(monkeypatch, max_visible):
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_MAX_VISIBLE_PROPOSALS", str(max_visible))
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_MAX_GENERATED_PROPOSALS", "12")
    get_settings.cache_clear()
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId=f"portfolio_configured_{max_visible}",
        sessionId=f"session_configured_{max_visible}",
        sourceUserTurnId=f"turn_configured_{max_visible}",
        sourceAssistantTurnId=f"assistant_configured_{max_visible}",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])

    for index in range(12):
        brief = CreativeBrief(
            briefId=f"brief_configured_{max_visible}_{index}",
            title=f"方向 {index + 1}",
            primaryAxis="classic",
            requiredGoalIds=[],
        )
        proposal_id = f"proposal_configured_{max_visible}_{index}"
        store.upsert_partial_preview(
            portfolio_id=portfolio.portfolio_id,
            proposal_id=proposal_id,
            choice_id=f"choice_configured_{max_visible}_{index}",
            snapshot={
                "title": brief.title,
                "creativeBrief": brief.model_dump(by_alias=True),
                "days": [{"dayNumber": 1, "segments": []}],
            },
            brief=brief,
            verifier={"passed": False, "hardFailures": ["pending_slot"]},
            score={"hardConstraintPassed": False},
            generation_lineage={"round": index + 1},
            status="blocked",
        )

    summary = store.summary(portfolio_id=portfolio.portfolio_id)
    assert len(summary["proposalIds"]) == 12
    assert len(summary["visibleProposalIds"]) == max_visible
    with pytest.raises(ValueError, match="portfolio_proposal_limit_exceeded"):
        overflow_brief = CreativeBrief(
            briefId=f"brief_configured_{max_visible}_overflow",
            title="超出预算方向",
            primaryAxis="classic",
            requiredGoalIds=[],
        )
        store.upsert_partial_preview(
            portfolio_id=portfolio.portfolio_id,
            proposal_id=f"proposal_configured_{max_visible}_overflow",
            choice_id=f"choice_configured_{max_visible}_overflow",
            snapshot={
                "title": overflow_brief.title,
                "creativeBrief": overflow_brief.model_dump(by_alias=True),
                "days": [{"dayNumber": 1, "segments": []}],
            },
            brief=overflow_brief,
            verifier={"passed": False, "hardFailures": ["pending_slot"]},
            score={"hardConstraintPassed": False},
            generation_lineage={"round": 13},
            status="blocked",
        )


@pytest.mark.parametrize("max_visible, expected_visible", [(6, 6), (5, 5)])
def test_store_separates_initial_generation_from_visible_bound(
    monkeypatch,
    max_visible,
    expected_visible,
):
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_INITIAL_BATCH_SIZE", "6")
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_MAX_VISIBLE_PROPOSALS", str(max_visible))
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_MAX_GENERATED_PROPOSALS", "12")
    get_settings.cache_clear()
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio_id = f"portfolio_six_initial_visible_{max_visible}"
    portfolio = PlanPortfolio(
        portfolioId=portfolio_id,
        sessionId="session_six_initial",
        sourceUserTurnId="turn_six_initial",
        sourceAssistantTurnId="assistant_six_initial",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=[f"proposal_initial_{index}" for index in range(6)],
        visibleProposalIds=[f"proposal_initial_{index}" for index in range(6)],
    )
    proposals = [
        _candidate().model_copy(
            update={
                "proposal_id": f"proposal_initial_{index}",
                "portfolio_id": portfolio_id,
                "brief": CreativeBrief(
                    briefId=f"brief_initial_{index}",
                    title=f"初始方向 {index + 1}",
                    primaryAxis="classic",
                    requiredGoalIds=[],
                ),
                "canonical_signature": f"signature-initial-{index}",
            }
        )
        for index in range(6)
    ]

    store.create(portfolio, proposals)

    summary = store.summary(portfolio_id=portfolio_id)
    assert len(summary["proposalIds"]) == 6
    assert len(summary["visibleProposalIds"]) == expected_visible
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()[0]
        == 6
    )


def test_store_create_is_idempotent_for_the_exact_same_planning_root_material():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_idempotent_create",
        sessionId="session_idempotent_create",
        sourceUserTurnId="turn_idempotent_create",
        sourceAssistantTurnId="assistant_idempotent_create",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
        failureReason="portfolio_route_quality_unresolved:meal_detour_high",
    )

    store.create(portfolio, [])
    store.create(portfolio, [])

    assert (
        connection.execute(
            """SELECT COUNT(*) FROM agent_plan_portfolios
           WHERE session_id = ? AND source_user_turn_id = ?""",
            (portfolio.session_id, portfolio.source_user_turn_id),
        ).fetchone()[0]
        == 1
    )


def test_store_create_rejects_conflicting_material_for_an_existing_planning_root():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_root_conflict",
        sessionId="session_root_conflict",
        sourceUserTurnId="turn_root_conflict",
        sourceAssistantTurnId="assistant_root_conflict",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
        failureReason="portfolio_route_quality_unresolved:meal_detour_high",
    )
    store.create(portfolio, [])

    with pytest.raises(ValueError, match="portfolio_root_identity_conflict"):
        store.create(
            portfolio.model_copy(update={"portfolio_id": "portfolio_root_conflict_other"}),
            [],
        )


def test_store_persists_proposals_without_itinerary_version():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_a",
        sessionId="session_a",
        sourceUserTurnId="turn_a",
        sourceAssistantTurnId="assistant_a",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_a"],
        visibleProposalIds=["proposal_a"],
    )
    candidate = _candidate().model_copy(
        update={
            "itinerary_snapshot": {
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "poi": {
                                    "amapId": "B000A0001",
                                    "name": "清华大学",
                                    "source": "amap-place-search",
                                    "latitude": 40.0,
                                    "longitude": 116.3,
                                }
                            }
                        ],
                    }
                ]
            }
        }
    )
    store.create(portfolio, [candidate])
    assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
    counts = store.classification_counts(portfolio_id="portfolio_a")
    assert counts["visibleComparisonProposalCount"] == 1
    assert counts["verifiedComparisonProposalCount"] == 0
    loaded = store.load_choice(
        session_id="session_a", source_user_turn_id="turn_a", choice_id="portfolio_choice_proposal_a"
    )
    assert loaded is not None
    assert loaded[0]["source_assistant_turn_id"] == "assistant_a"
    assert store.visible_comparison_projections(portfolio_id="portfolio_a") == [
        {
            "proposalId": "proposal_a",
            "title": "方案待补全",
            "days": candidate.itinerary_snapshot["days"],
        }
    ]
    assert store.claim_selection(portfolio_id="portfolio_a", proposal_id="proposal_a") is True
    assert store.claim_selection(portfolio_id="portfolio_a", proposal_id="proposal_a") is False


def test_visible_route_pending_partial_is_not_counted_as_verified():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_partial_truth",
        sessionId="session_partial_truth",
        sourceUserTurnId="turn_partial_truth",
        sourceAssistantTurnId="assistant_partial_truth",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])
    candidate = _candidate().model_copy(
        update={"proposal_id": "partial:portfolio_partial_truth", "portfolio_id": portfolio.portfolio_id}
    )
    snapshot = {
        **candidate.itinerary_snapshot,
        "portfolioPendingSlots": [{"planningSlotId": "slot_missing"}],
        "portfolioRouteQuality": {"providerState": "pending"},
    }
    store.upsert_partial_preview(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=candidate.proposal_id,
        choice_id="choice_partial_truth",
        snapshot=snapshot,
        brief=candidate.brief,
        verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
        score=candidate.score,
        generation_lineage={},
        status="route_pending",
    )

    counts = store.classification_counts(portfolio_id=portfolio.portfolio_id)

    assert counts == {
        "visibleComparisonProposalCount": 1,
        "partialComparisonProposalCount": 1,
        "verifiedComparisonProposalCount": 0,
        "adoptionReadyProposalCount": 0,
    }
    summary = json.loads(
        connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio.portfolio_id,),
        ).fetchone()[0]
    )
    assert summary["verifiedComparisonProposalCount"] == 0


def test_partial_preview_proposal_id_cannot_be_rebound_to_another_brief():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_immutable_partial",
        sessionId="session_immutable_partial",
        sourceUserTurnId="turn_immutable_partial",
        sourceAssistantTurnId="assistant_immutable_partial",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])
    first_brief = CreativeBrief(
        briefId="fallback_1_food_led",
        title="地方饮食与街区",
        primaryAxis="food_led",
        requiredGoalIds=[],
    )
    second_brief = CreativeBrief(
        briefId="fallback_2_photo_night",
        title="光影夜游",
        primaryAxis="photo_night",
        requiredGoalIds=[],
    )
    proposal_id = f"partial:{portfolio.portfolio_id}"
    first_snapshot = {
        "city": "北京",
        "title": "地方饮食与街区｜北京行程",
        "creativeBrief": first_brief.model_dump(by_alias=True),
        "days": [{"dayNumber": 1, "segments": []}],
        "portfolioPendingSlots": [],
        "portfolioSelectionContext": {
            "planningSelectionRootTurnId": portfolio.source_user_turn_id,
            "rootPortfolioId": portfolio.portfolio_id,
            "focusBriefId": first_brief.brief_id,
            "requestContractFingerprint": portfolio.request_contract_fingerprint,
        },
    }
    store.upsert_partial_preview(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=proposal_id,
        choice_id="readonly_food_led",
        snapshot=first_snapshot,
        brief=first_brief,
        verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
        score={"hardConstraintPassed": False},
        generation_lineage={"turn": 1},
        status="route_pending",
    )
    second_proposal_id = f"partial-preview:{portfolio.portfolio_id}:{second_brief.brief_id}"
    store.upsert_partial_preview(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=second_proposal_id,
        choice_id="readonly_photo_night",
        snapshot={
            **first_snapshot,
            "title": "光影夜游｜北京行程",
            "creativeBrief": second_brief.model_dump(by_alias=True),
            "portfolioSelectionContext": {
                **first_snapshot["portfolioSelectionContext"],
                "focusBriefId": second_brief.brief_id,
            },
        },
        brief=second_brief,
        verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
        score={"hardConstraintPassed": False},
        generation_lineage={"turn": 2},
        status="route_pending",
    )
    loaded_first = store.load_visible_proposal_snapshot(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=proposal_id,
    )
    assert loaded_first is not None
    assert loaded_first["creativeBrief"]["briefId"] == first_brief.brief_id
    store.upsert_partial_preview(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=proposal_id,
        choice_id="readonly_food_led",
        snapshot=loaded_first,
        brief=first_brief,
        verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
        score={"hardConstraintPassed": False},
        generation_lineage={"turn": 1, "refreshed": True},
        status="route_pending",
    )

    summary = json.loads(
        connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio.portfolio_id,),
        ).fetchone()[0]
    )
    assert summary["visibleProposalIds"] == [proposal_id, second_proposal_id]
    assert summary["focusBriefId"] == second_brief.brief_id

    with pytest.raises(ValueError, match="partial_preview_brief_identity_conflict"):
        store.upsert_partial_preview(
            portfolio_id=portfolio.portfolio_id,
            proposal_id=proposal_id,
            choice_id="readonly_photo_night",
            snapshot={
                **first_snapshot,
                "title": "光影夜游｜北京行程",
                "creativeBrief": second_brief.model_dump(by_alias=True),
            },
            brief=second_brief,
            verifier={"passed": False, "hardFailures": ["route_evidence_incomplete"]},
            score={"hardConstraintPassed": False},
            generation_lineage={"turn": 2},
            status="route_pending",
        )

    persisted = connection.execute(
        "SELECT brief_json, snapshot_json FROM agent_plan_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    assert json.loads(persisted["brief_json"])["briefId"] == first_brief.brief_id
    assert json.loads(persisted["snapshot_json"])["creativeBrief"]["briefId"] == first_brief.brief_id


def test_mark_committed_clears_selected_proposal_adopt_action_in_authoritative_evidence():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_committed_evidence",
        sessionId="session_committed_evidence",
        sourceUserTurnId="turn_committed_evidence",
        sourceAssistantTurnId="assistant_committed_evidence",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_committed_evidence"],
        visibleProposalIds=["proposal_committed_evidence"],
    )
    candidate = _candidate().model_copy(
        update={
            "proposal_id": "proposal_committed_evidence",
            "portfolio_id": "portfolio_committed_evidence",
        }
    )
    store.create(portfolio, [candidate])
    connection.execute(
        "UPDATE agent_plan_proposals SET evidence_json = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "readiness": {
                        "adoptionReady": True,
                        "currentReadiness": "route_ready",
                        "nextAction": "adopt",
                        "nextActionLabel": "采用此方案",
                    }
                }
            ),
            candidate.proposal_id,
        ),
    )
    connection.commit()

    store.mark_committed(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=candidate.proposal_id,
    )

    row = connection.execute(
        "SELECT status, evidence_json FROM agent_plan_proposals WHERE id = ?",
        (candidate.proposal_id,),
    ).fetchone()
    evidence = json.loads(row["evidence_json"])
    assert row["status"] == "committed"
    assert evidence["readiness"]["isAdopted"] is True
    assert evidence["readiness"]["adoptionReady"] is True
    assert evidence["readiness"]["nextAction"] == "none"
    assert evidence["readiness"]["nextActionLabel"] == "已采用"


def test_mark_committed_keeps_unselected_visible_proposal_as_readonly_comparison():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_comparison_lifecycle",
        sessionId="session_comparison_lifecycle",
        sourceUserTurnId="turn_comparison_lifecycle",
        sourceAssistantTurnId="assistant_comparison_lifecycle",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_old", "proposal_selected"],
        visibleProposalIds=["proposal_old", "proposal_selected"],
    )
    old = _candidate().model_copy(
        update={
            "proposal_id": "proposal_old",
            "portfolio_id": portfolio.portfolio_id,
            "canonical_signature": "b" * 16,
            "itinerary_snapshot": {"days": [{"dayNumber": 1, "segments": []}]},
        }
    )
    selected = _candidate().model_copy(
        update={
            "proposal_id": "proposal_selected",
            "portfolio_id": portfolio.portfolio_id,
            "canonical_signature": "c" * 16,
            "itinerary_snapshot": {"days": [{"dayNumber": 2, "segments": []}]},
        }
    )
    store.create(portfolio, [old, selected])

    store.mark_committed(
        portfolio_id=portfolio.portfolio_id,
        proposal_id=selected.proposal_id,
    )

    statuses = dict(
        connection.execute(
            "SELECT id, status FROM agent_plan_proposals WHERE portfolio_id = ?",
            (portfolio.portfolio_id,),
        ).fetchall()
    )
    assert statuses == {
        "proposal_old": "comparison_only",
        "proposal_selected": "committed",
    }
    assert [
        item["proposalId"] for item in store.visible_comparison_projections(portfolio_id=portfolio.portfolio_id)
    ] == ["proposal_old", "proposal_selected"]


def test_full_three_turn_comparison_lifecycle_is_zero_write_until_opaque_selection():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_three_turn",
        sessionId="session_three_turn",
        sourceUserTurnId="turn_three_turn_root",
        sourceAssistantTurnId="assistant_three_turn_1",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=[],
        visibleProposalIds=[],
    )
    store.create(portfolio, [])
    template = _candidate()
    write_deltas = {"version": 0, "patch": 0, "route": 0}

    skeleton_snapshot = {
        "title": "光影夜游｜北京行程",
        "days": [{"dayNumber": 1, "segments": []}],
        "portfolioPendingSlots": [
            {
                "slotId": "slot_hard_night",
                "planningSlotId": "slot_hard_night",
                "briefId": "brief_three_turn",
                "poolId": "pool_three_turn",
                "dayNumber": 1,
                "requirementLevel": "hard",
                "state": "pending",
            }
        ],
        "portfolioThemeQuality": {
            "themeEligible": False,
            "displayTitle": "北京真实地点行程骨架",
        },
    }
    store.upsert_partial_preview(
        portfolio_id=portfolio.portfolio_id,
        proposal_id="proposal_turn_1_skeleton",
        choice_id="readonly_turn_1_skeleton",
        snapshot=skeleton_snapshot,
        brief=template.brief,
        verifier={
            "passed": False,
            "draftPassed": False,
            "pendingHardSlotCount": 1,
        },
        score=template.score,
        generation_lineage={"turn": 1},
        status="partial_preview",
    )
    turn_1_ids = [
        item["proposalId"] for item in store.visible_comparison_projections(portfolio_id=portfolio.portfolio_id)
    ]
    assert turn_1_ids == ["proposal_turn_1_skeleton"]
    assert write_deltas == {"version": 0, "patch": 0, "route": 0}

    selectable = template.model_copy(
        update={
            "proposal_id": "proposal_turn_2_selectable",
            "portfolio_id": portfolio.portfolio_id,
            "canonical_signature": "t" * 16,
            "itinerary_snapshot": {
                "title": "北京真实地点可编辑草案",
                "days": [{"dayNumber": 1, "segments": []}],
                "portfolioPendingSlots": [],
            },
            "generation_lineage": {"turn": 2},
        }
    )
    choice_id, proposal_id, created = store.offer_repaired_proposal(
        portfolio_id=portfolio.portfolio_id,
        proposal=selectable,
    )
    assert created is True
    assert proposal_id == selectable.proposal_id
    turn_2_ids = [
        item["proposalId"] for item in store.visible_comparison_projections(portfolio_id=portfolio.portfolio_id)
    ]
    assert turn_2_ids == [
        "proposal_turn_1_skeleton",
        "proposal_turn_2_selectable",
    ]
    assert write_deltas == {"version": 0, "patch": 0, "route": 0}

    def persist(_snapshot):
        write_deltas["version"] += 1
        write_deltas["patch"] += 1
        return "version_three_turn"

    result_version_id = PlanProposalCommitService(store).commit(
        session_id=portfolio.session_id,
        source_user_turn_id=portfolio.source_user_turn_id,
        choice_id=choice_id,
        active_version_id=None,
        verify=lambda _snapshot: True,
        persist=persist,
    )
    assert result_version_id == "version_three_turn"
    assert write_deltas == {"version": 1, "patch": 1, "route": 0}
    statuses = dict(
        connection.execute(
            "SELECT id, status FROM agent_plan_proposals WHERE portfolio_id = ?",
            (portfolio.portfolio_id,),
        ).fetchall()
    )
    assert statuses == {
        "proposal_turn_1_skeleton": "comparison_only",
        "proposal_turn_2_selectable": "committed",
    }
    refreshed_ids = [
        item["proposalId"] for item in store.visible_comparison_projections(portfolio_id=portfolio.portfolio_id)
    ]
    assert refreshed_ids == turn_2_ids


def test_store_persists_neutral_title_when_theme_is_not_evidence_eligible():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_neutral_title",
        sessionId="session_neutral_title",
        sourceUserTurnId="turn_neutral_title",
        sourceAssistantTurnId="assistant_neutral_title",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_neutral_title"],
        visibleProposalIds=["proposal_neutral_title"],
    )
    candidate = _candidate().model_copy(
        update={
            "proposal_id": "proposal_neutral_title",
            "portfolio_id": portfolio.portfolio_id,
            "canonical_signature": "d" * 16,
            "itinerary_snapshot": {
                "city": "北京",
                "title": "光影夜游｜北京高校与夜景规划草案行程",
                "days": [],
                "portfolioPendingSlots": [],
            },
            "verifier": {"passed": False, "draftPassed": True},
        }
    )

    store.create(portfolio, [candidate])

    snapshot = json.loads(
        connection.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
            (candidate.proposal_id,),
        ).fetchone()[0]
    )
    assert snapshot["title"] == "方案待补全"


def test_store_persists_stable_per_brief_generation_state_without_new_tables():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_brief_state",
        sessionId="session_brief_state",
        sourceUserTurnId="turn_brief_state",
        sourceAssistantTurnId="assistant_brief_state",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])

    initial = store.update_brief_generation_state(
        portfolio_id="portfolio_brief_state",
        ordered_brief_ids=["brief_focus", "brief_second", "brief_third"],
        processed_brief_ids=["brief_focus"],
        completed_brief_ids=["brief_focus"],
        failure_reason_codes={},
        result_types={"brief_focus": "partial"},
    )
    resumed = store.update_brief_generation_state(
        portfolio_id="portfolio_brief_state",
        ordered_brief_ids=["brief_focus", "brief_second", "brief_third"],
        processed_brief_ids=["brief_second"],
        completed_brief_ids=[],
        failure_reason_codes={"brief_second": ["route_quality_unresolved"]},
    )

    assert [item["briefId"] for item in initial["briefGenerationState"]] == [
        "brief_focus",
        "brief_second",
        "brief_third",
    ]
    assert [item["status"] for item in initial["briefGenerationState"]] == [
        "completed",
        "remaining",
        "remaining",
    ]
    assert [item["status"] for item in resumed["briefGenerationState"]] == [
        "completed",
        "failed",
        "remaining",
    ]
    assert resumed["completedBriefIds"] == ["brief_focus"]
    assert resumed["remainingBriefIds"] == ["brief_third"]
    assert resumed["failedBriefIds"] == ["brief_second"]
    assert resumed["nextBriefId"] == "brief_third"
    persisted = json.loads(
        connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            ("portfolio_brief_state",),
        ).fetchone()[0]
    )
    assert persisted["briefGenerationState"] == resumed["briefGenerationState"]
    assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0


def test_route_degraded_frontier_with_next_brief_remains_continuable_without_writes():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_route_degraded",
        sessionId="session_route_degraded",
        sourceUserTurnId="turn_route_degraded",
        sourceAssistantTurnId="assistant_route_degraded",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
        failureReason="portfolio_route_quality_unresolved:meal_detour_high",
    )
    store.create(portfolio, [])
    summary = store.update_brief_generation_state(
        portfolio_id=portfolio.portfolio_id,
        ordered_brief_ids=["brief_first", "brief_second", "brief_third", "brief_fourth"],
        processed_brief_ids=["brief_first"],
        completed_brief_ids=[],
        failure_reason_codes={"brief_first": ["meal_detour_high"]},
    )
    frontier = {
        "frontierState": "has_more",
        "remainingBudget": 11,
        "continuationRound": 1,
    }
    store.update_exploration_frontier(
        portfolio_id=portfolio.portfolio_id,
        frontier=frontier,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )

    promoted = store.mark_route_degraded_continuable(
        portfolio_id=portfolio.portfolio_id,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
        expected_next_brief_id="brief_second",
    )

    row = connection.execute(
        "SELECT status, failure_reason, summary_json FROM agent_plan_portfolios WHERE id = ?",
        (portfolio.portfolio_id,),
    ).fetchone()
    persisted = json.loads(row["summary_json"])
    assert promoted["generationState"] == "route_degraded"
    assert row["status"] == "awaiting_selection"
    assert row["failure_reason"] == portfolio.failure_reason
    assert persisted["nextBriefId"] == summary["nextBriefId"] == "brief_second"
    assert persisted["creativeExplorationFrontier"] == {
        **frontier,
        "planningSelectionRootTurnId": portfolio.source_user_turn_id,
        "rootPortfolioId": portfolio.portfolio_id,
        "requestContractFingerprint": portfolio.request_contract_fingerprint,
        "nextBriefId": "brief_second",
    }
    assert persisted["status"] == "awaiting_selection"
    assert persisted["planningSelectionRootTurnId"] == portfolio.source_user_turn_id
    assert persisted["rootPortfolioId"] == portfolio.portfolio_id
    assert persisted["requestContractFingerprint"] == portfolio.request_contract_fingerprint
    assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0

    repeated = store.mark_route_degraded_continuable(
        portfolio_id=portfolio.portfolio_id,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
        expected_next_brief_id="brief_second",
    )
    assert repeated == promoted


def test_store_reserves_and_settles_root_candidate_amap_budget_before_provider_work():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_candidate_budget",
        sessionId="session_candidate_budget",
        sourceUserTurnId="turn_candidate_budget",
        sourceAssistantTurnId="assistant_candidate_budget",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
    )
    store.create(portfolio, [])
    frontier_service = CreativeExplorationFrontierService()
    frontier = frontier_service.initial(
        planning_root_id=portfolio.source_user_turn_id,
        portfolio_id=portfolio.portfolio_id,
        fingerprint=portfolio.request_contract_fingerprint,
    )
    frontier["continuationRound"] = 1
    store.update_exploration_frontier(
        portfolio_id=portfolio.portfolio_id,
        frontier=frontier,
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )

    claim = store.reserve_candidate_amap_budget(
        portfolio_id=portfolio.portfolio_id,
        execution_id="assistant_candidate_budget_next",
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )
    contender = store.reserve_candidate_amap_budget(
        portfolio_id=portfolio.portfolio_id,
        execution_id="assistant_candidate_budget_parallel",
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )
    settled = store.settle_candidate_amap_budget(
        portfolio_id=portfolio.portfolio_id,
        execution_id="assistant_candidate_budget_next",
        actual_usage={
            "usedPlaceText": 3,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 1,
            "usedTotalExternal": 4,
        },
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )
    settled_replay = store.settle_candidate_amap_budget(
        portfolio_id=portfolio.portfolio_id,
        execution_id="assistant_candidate_budget_next",
        actual_usage={
            "usedPlaceText": 3,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 1,
            "usedTotalExternal": 4,
        },
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )

    assert claim["status"] == "CLAIMED"
    assert claim["providerCallAllowed"] is True
    assert claim["allocation"]["totalExternalMax"] == 8
    assert contender["status"] == "CLAIM_IN_FLIGHT"
    assert contender["providerCallAllowed"] is False
    assert settled["status"] == "SETTLED"
    assert settled["frontier"]["amapCandidateUsage"]["usedTotalExternal"] == 4
    assert settled_replay["status"] == "REPLAY_SETTLED"
    persisted = store.summary(portfolio_id=portfolio.portfolio_id)["creativeExplorationFrontier"]
    assert persisted["amapCandidateUsage"]["usedPlaceText"] == 3
    assert persisted["amapCandidateReservations"]["assistant_candidate_budget_next"]["state"] == "settled"
    assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_route_degraded_frontier_is_terminal_only_after_real_exhaustion():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_route_exhausted",
        sessionId="session_route_exhausted",
        sourceUserTurnId="turn_route_exhausted",
        sourceAssistantTurnId="assistant_route_exhausted",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
        failureReason="portfolio_route_quality_unresolved:route_schedule_projection_failed",
    )
    store.create(portfolio, [])
    store.update_brief_generation_state(
        portfolio_id=portfolio.portfolio_id,
        ordered_brief_ids=["brief_only"],
        processed_brief_ids=["brief_only"],
        completed_brief_ids=[],
        failure_reason_codes={"brief_only": ["route_schedule_projection_failed"]},
    )
    store.update_exploration_frontier(
        portfolio_id=portfolio.portfolio_id,
        frontier={
            "frontierState": "semantic_exhausted",
            "remainingBudget": 11,
            "exhaustionReason": "directions_exhausted",
        },
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )

    assert (
        store.mark_route_degraded_continuable(
            portfolio_id=portfolio.portfolio_id,
            expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
            expected_next_brief_id="",
        )
        is None
    )
    row = connection.execute(
        "SELECT status FROM agent_plan_portfolios WHERE id = ?",
        (portfolio.portfolio_id,),
    ).fetchone()
    assert row["status"] == "failed"


def test_expired_portfolio_choice_fails_closed_and_expires_its_proposals():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_expired",
        sessionId="session_expired",
        sourceUserTurnId="turn_expired",
        sourceAssistantTurnId="assistant_expired",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_expired"],
        visibleProposalIds=["proposal_expired"],
    )
    candidate = _candidate().model_copy(update={"proposal_id": "proposal_expired", "portfolio_id": "portfolio_expired"})
    store.create(
        portfolio,
        [candidate],
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    loaded = store.load_choice(
        session_id="session_expired",
        source_user_turn_id="turn_expired",
        choice_id="portfolio_choice_proposal_expired",
    )
    assert loaded is not None
    assert loaded[0]["status"] == "expired"
    assert loaded[1]["status"] == "expired"


@pytest.mark.parametrize("root_status", ["awaiting_selection", "failed"])
def test_expired_portfolio_cannot_accept_repaired_proposal(root_status):
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_expired_repair",
        sessionId="session_expired_repair",
        sourceUserTurnId="turn_expired_repair",
        sourceAssistantTurnId="assistant_expired_repair",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_original"],
        visibleProposalIds=["proposal_original"],
    )
    original = _candidate().model_copy(
        update={
            "proposal_id": "proposal_original",
            "portfolio_id": "portfolio_expired_repair",
        }
    )
    store.create(
        portfolio,
        [original],
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    if root_status == "failed":
        connection.execute(
            "UPDATE agent_plan_portfolios SET status = 'failed' WHERE id = ?",
            ("portfolio_expired_repair",),
        )
        connection.commit()
    before_summary = connection.execute(
        "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
        ("portfolio_expired_repair",),
    ).fetchone()[0]
    repaired = _candidate().model_copy(
        update={
            "proposal_id": "proposal_late_repair",
            "portfolio_id": "portfolio_expired_repair",
            "canonical_signature": "b" * 16,
        }
    )

    with pytest.raises(ValueError, match="plan_portfolio_expired"):
        store.offer_repaired_proposal(
            portfolio_id="portfolio_expired_repair",
            proposal=repaired,
        )

    root = connection.execute(
        "SELECT status, summary_json FROM agent_plan_portfolios WHERE id = ?",
        ("portfolio_expired_repair",),
    ).fetchone()
    assert root["status"] == "expired"
    assert root["summary_json"] == before_summary
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE id = ?",
            ("proposal_late_repair",),
        ).fetchone()[0]
        == 0
    )


def test_timed_out_committing_selection_can_be_released_for_retry():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_lease",
        sessionId="session_lease",
        sourceUserTurnId="turn_lease",
        sourceAssistantTurnId="assistant_lease",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_lease"],
        visibleProposalIds=["proposal_lease"],
    )
    candidate = _candidate().model_copy(update={"proposal_id": "proposal_lease", "portfolio_id": "portfolio_lease"})
    store.create(portfolio, [candidate])
    assert store.claim_selection(portfolio_id="portfolio_lease", proposal_id="proposal_lease") is True
    old = (datetime.now(timezone.utc) - timedelta(seconds=store.SELECTION_LEASE_SECONDS + 1)).isoformat()
    connection.execute("UPDATE agent_plan_portfolios SET updated_at = ? WHERE id = ?", (old, "portfolio_lease"))
    connection.commit()
    assert store.release_stale_selection(portfolio_id="portfolio_lease", reason="crash_before_writer") is True
    loaded = store.load_choice(
        session_id="session_lease", source_user_turn_id="turn_lease", choice_id="portfolio_choice_proposal_lease"
    )
    assert loaded is not None
    assert loaded[0]["status"] == "awaiting_selection"
    assert store.claim_selection(portfolio_id="portfolio_lease", proposal_id="proposal_lease") is True


def test_default_portfolio_choice_window_is_one_hour():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_ttl",
        sessionId="session_ttl",
        sourceUserTurnId="turn_ttl",
        sourceAssistantTurnId="assistant_ttl",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_ttl"],
        visibleProposalIds=["proposal_ttl"],
    )
    candidate = _candidate().model_copy(update={"proposal_id": "proposal_ttl", "portfolio_id": "portfolio_ttl"})
    before = datetime.now(timezone.utc)
    store.create(portfolio, [candidate])
    expires_at = datetime.fromisoformat(
        connection.execute(
            "SELECT expires_at FROM agent_plan_portfolios WHERE id = ?",
            ("portfolio_ttl",),
        ).fetchone()[0]
    )
    assert timedelta(minutes=59) <= expires_at - before <= timedelta(minutes=61)


def test_expired_portfolio_exposes_failure_reason():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_expired_reason",
        sessionId="session_expired_reason",
        sourceUserTurnId="turn_expired_reason",
        sourceAssistantTurnId="assistant_expired_reason",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
        proposalIds=["proposal_expired_reason"],
        visibleProposalIds=["proposal_expired_reason"],
    )
    candidate = _candidate().model_copy(
        update={
            "proposal_id": "proposal_expired_reason",
            "portfolio_id": "portfolio_expired_reason",
        }
    )
    store.create(
        portfolio,
        [candidate],
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    loaded = store.load_choice(
        session_id="session_expired_reason",
        source_user_turn_id="turn_expired_reason",
        choice_id="portfolio_choice_proposal_expired_reason",
    )
    assert loaded is not None
    assert loaded[0]["failure_reason"] == "plan_portfolio_expired"


def test_create_rejects_duplicate_canonical_signature_before_any_write():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_duplicate_signature",
        sessionId="session_duplicate_signature",
        sourceUserTurnId="turn_duplicate_signature",
        sourceAssistantTurnId="assistant_duplicate_signature",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
    )
    first = _candidate().model_copy(
        update={"proposal_id": "proposal_duplicate_one", "portfolio_id": portfolio.portfolio_id}
    )
    second = first.model_copy(update={"proposal_id": "proposal_duplicate_two"})
    with pytest.raises(ValueError, match="duplicate_canonical_signature"):
        store.create(portfolio, [first, second])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM agent_plan_portfolios WHERE id = ?", (portfolio.portfolio_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?", (portfolio.portfolio_id,)
        ).fetchone()[0]
        == 0
    )


def test_offer_repaired_proposal_is_idempotent_for_duplicate_canonical_signature():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_repair_duplicate_signature",
        sessionId="session_repair_duplicate_signature",
        sourceUserTurnId="turn_repair_duplicate_signature",
        sourceAssistantTurnId="assistant_repair_duplicate_signature",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
    )
    original = _candidate().model_copy(
        update={"proposal_id": "proposal_repair_original", "portfolio_id": portfolio.portfolio_id}
    )
    store.create(portfolio, [original])
    duplicate = original.model_copy(update={"proposal_id": "proposal_repair_duplicate"})
    choice_id, proposal_id, inserted = store.offer_repaired_proposal(
        portfolio_id=portfolio.portfolio_id, proposal=duplicate
    )
    assert (choice_id, proposal_id, inserted) == (
        "portfolio_choice_proposal_repair_original",
        "proposal_repair_original",
        False,
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?", (portfolio.portfolio_id,)
        ).fetchone()[0]
        == 1
    )


def test_offer_repaired_proposal_accepts_readiness_verified_editable_draft(monkeypatch):
    monkeypatch.setenv("AGENT_SOFT_SLOT_DRAFT_ADOPTION_ENABLED", "true")
    get_settings.cache_clear()
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_editable_repair",
        sessionId="session_editable_repair",
        sourceUserTurnId="turn_editable_repair",
        sourceAssistantTurnId="assistant_editable_repair",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
    )
    store.create(portfolio, [])
    editable = _candidate().model_copy(
        update={
            "proposal_id": "proposal_editable_repair",
            "portfolio_id": portfolio.portfolio_id,
            "canonical_signature": "e" * 16,
            "itinerary_snapshot": {
                "days": [
                    {
                        "dayNumber": 1,
                        "segments": [
                            {
                                "id": "seg_anchor",
                                "poi": {
                                    "id": "poi_anchor",
                                    "amapId": "B0EDITABLE1",
                                    "source": "amap-place-search",
                                    "longitude": 116.4,
                                    "latitude": 39.9,
                                },
                                "semanticMetadata": {"routeAnchor": True},
                            }
                        ],
                    }
                ],
                "portfolioPendingSlots": [
                    {
                        "planningSlotId": "slot_soft",
                        "dayNumber": 1,
                        "requirementLevel": "soft",
                    }
                ],
            },
            "verifier": {
                "passed": False,
                "draftPassed": True,
                "hardFailures": [],
                "pendingHardSlotCount": 0,
                "pendingSoftSlotCount": 1,
            },
        }
    )

    choice_id, proposal_id, inserted = store.offer_repaired_proposal(
        portfolio_id=portfolio.portfolio_id,
        proposal=editable,
    )

    assert inserted is True
    assert proposal_id == "proposal_editable_repair"
    assert choice_id == "portfolio_choice_proposal_editable_repair"


def test_brief_generation_state_rejects_identity_status_expiry_and_payload_tampering():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_generation_guard",
        sessionId="session_generation_guard",
        sourceUserTurnId="turn_generation_guard",
        sourceAssistantTurnId="assistant_generation_guard",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])
    kwargs = dict(
        portfolio_id=portfolio.portfolio_id,
        ordered_brief_ids=["brief_guard"],
        processed_brief_ids=["brief_guard"],
        completed_brief_ids=["brief_guard"],
        failure_reason_codes={},
        expected_source_user_turn_id="wrong_turn",
        expected_request_contract_fingerprint=portfolio.request_contract_fingerprint,
    )
    with pytest.raises(ValueError, match="portfolio_source_user_turn_mismatch"):
        store.update_brief_generation_state(**kwargs)
    kwargs["expected_source_user_turn_id"] = portfolio.source_user_turn_id
    kwargs["expected_request_contract_fingerprint"] = "wrong_fingerprint"
    with pytest.raises(ValueError, match="portfolio_request_fingerprint_mismatch"):
        store.update_brief_generation_state(**kwargs)
    kwargs["expected_request_contract_fingerprint"] = portfolio.request_contract_fingerprint
    kwargs["processed_brief_ids"] = ["brief_guard", "brief_guard"]
    with pytest.raises(ValueError, match="portfolio_processed_briefs_duplicate"):
        store.update_brief_generation_state(**kwargs)
    kwargs["processed_brief_ids"] = ["brief_guard"]
    kwargs["failure_reason_codes"] = {"unknown_brief": ["tampered"]}
    with pytest.raises(ValueError, match="portfolio_failure_reason_scope_mismatch"):
        store.update_brief_generation_state(**kwargs)
    connection.execute("UPDATE agent_plan_portfolios SET status = 'committed' WHERE id = ?", (portfolio.portfolio_id,))
    connection.commit()
    kwargs["failure_reason_codes"] = {}
    with pytest.raises(ValueError, match="plan_portfolio_not_updatable"):
        store.update_brief_generation_state(**kwargs)


def test_brief_generation_state_preserves_root_order_and_clears_failed_result_type():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_generation_subset",
        sessionId="session_generation_subset",
        sourceUserTurnId="turn_generation_subset",
        sourceAssistantTurnId="assistant_generation_subset",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="failed",
    )
    store.create(portfolio, [])
    store.update_brief_generation_state(
        portfolio_id=portfolio.portfolio_id,
        ordered_brief_ids=["brief_a", "brief_b", "brief_c"],
        processed_brief_ids=["brief_a"],
        completed_brief_ids=["brief_a"],
        failure_reason_codes={},
        result_types={"brief_a": "proposal"},
    )
    resumed = store.update_brief_generation_state(
        portfolio_id=portfolio.portfolio_id,
        ordered_brief_ids=["brief_b"],
        processed_brief_ids=["brief_b"],
        completed_brief_ids=[],
        failure_reason_codes={"brief_b": ["route_quality_unresolved"]},
    )
    assert [item["briefId"] for item in resumed["briefGenerationState"]] == ["brief_a", "brief_b", "brief_c"]
    assert resumed["briefGenerationState"][0]["resultType"] == "proposal"
    assert resumed["briefGenerationState"][1]["status"] == "failed"
    assert "resultType" not in resumed["briefGenerationState"][1]
    assert resumed["nextBriefId"] == "brief_c"


def test_expired_brief_generation_state_is_rejected_and_expires_root():
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    store = PlanPortfolioStore(connection)
    portfolio = PlanPortfolio(
        portfolioId="portfolio_generation_expired",
        sessionId="session_generation_expired",
        sourceUserTurnId="turn_generation_expired",
        sourceAssistantTurnId="assistant_generation_expired",
        sourceObservationFingerprint="o" * 16,
        requestContractFingerprint="r" * 16,
        status="awaiting_selection",
    )
    store.create(
        portfolio,
        [],
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="plan_portfolio_expired"):
        store.update_brief_generation_state(
            portfolio_id=portfolio.portfolio_id,
            ordered_brief_ids=["brief_expired"],
            processed_brief_ids=[],
            completed_brief_ids=[],
            failure_reason_codes={},
        )
    assert (
        connection.execute(
            "SELECT status FROM agent_plan_portfolios WHERE id = ?", (portfolio.portfolio_id,)
        ).fetchone()[0]
        == "expired"
    )
