import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.core.database import get_db
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.proposal_visit_facts_service import ProposalVisitFactsService


class RecordedResolver:
    def __init__(self, *, intervals):
        self.intervals = intervals
        self.calls = []

    def resolve_visit_facts(self, **kwargs):
        self.calls.append(kwargs)
        now = kwargs["queried_at"]
        expires = now + timedelta(hours=24)
        opening = {
            "status": "verified",
            "valueText": "周一至周日 09:00-17:00",
            "structuredValue": {"intervals": self.intervals},
            "effectiveForDate": kwargs["visit_date"],
            "sourceRefs": [{"sourceName": "高德地图"}],
            "queriedAt": now.isoformat(),
            "expiresAt": expires.isoformat(),
        }
        return {
            "amapPoiId": kwargs["amap_poi_id"],
            "visitDate": kwargs["visit_date"],
            "refreshStatus": "partial",
            "facts": {"openingHours": opening},
            "sourceRefs": opening["sourceRefs"],
            "evidenceFingerprint": "e" * 64,
            "queriedAt": now.isoformat(),
            "expiresAt": expires.isoformat(),
        }


class BlockingTwoSegmentResolver(RecordedResolver):
    """Expose both a long network phase and an overlapping refresh request."""

    def __init__(self):
        super().__init__(intervals=[{"start": "09:00", "end": "20:00"}])
        self.second_primary_call_started = threading.Event()
        self.release_primary = threading.Event()
        self.call_threads: list[str] = []
        self._guard = threading.Lock()

    def resolve_visit_facts(self, **kwargs):
        with self._guard:
            self.call_threads.append(threading.current_thread().name)
            primary_call_count = self.call_threads.count("proposal-refresh-primary")
        if threading.current_thread().name == "proposal-refresh-primary" and primary_call_count == 2:
            self.second_primary_call_started.set()
            assert self.release_primary.wait(timeout=3)
        return super().resolve_visit_facts(**kwargs)


def _persist_proposal(
    db,
    *,
    session_id: str,
    proposal_id: str = "proposal_visit",
    include_second_segment: bool = False,
):
    now = datetime.now(timezone.utc).isoformat()
    portfolio_id = "portfolio_visit"
    snapshot = {
        "title": "开放时间测试方案",
        "days": [
            {
                "id": "day_visit",
                "dayNumber": 1,
                "date": "2026-10-01",
                "segments": [
                    {
                        "id": "segment_visit",
                        "startTime": "18:00",
                        "endTime": "19:00",
                        "poi": {"name": "测试地点", "amapId": "B000TEST"},
                    }
                ],
            }
        ],
    }
    if include_second_segment:
        snapshot["days"][0]["segments"].append(
            {
                "id": "segment_visit_second",
                "startTime": "19:00",
                "endTime": "20:00",
                "poi": {"name": "第二测试地点", "amapId": "B000TEST2"},
            }
        )
    db.execute(
        """INSERT INTO agent_plan_portfolios (
          id, session_id, source_user_turn_id, source_assistant_turn_id,
          expected_base_version_id, source_observation_fingerprint,
          request_contract_fingerprint, status, summary_json, created_at, updated_at
        ) VALUES (?, ?, 'root_visit', 'assistant_visit', NULL, ?, ?, 'awaiting_selection', ?, ?, ?)""",
        (portfolio_id, session_id, "o" * 64, "r" * 64, json.dumps({"visibleProposalIds": [proposal_id]}), now, now),
    )
    db.execute(
        """INSERT INTO agent_plan_proposals (
          id, portfolio_id, choice_id, rank_index, status, brief_json, snapshot_json,
          score_json, verifier_json, evidence_json, canonical_signature,
          generation_lineage_json, created_at, updated_at
        ) VALUES (?, ?, 'choice_visit', 0, 'offered', '{}', ?, '{}', '{}', '{}', ?, '{}', ?, ?)""",
        (proposal_id, portfolio_id, json.dumps(snapshot), "c" * 64, now, now),
    )
    db.commit()
    return snapshot


def test_refresh_resolves_all_external_facts_before_one_short_write_transaction(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts transaction boundary")
    _persist_proposal(
        db_connection,
        session_id=session.session_id,
        proposal_id="proposal_transaction_boundary",
        include_second_segment=True,
    )

    transaction_states: list[bool] = []

    class TransactionObservingResolver(RecordedResolver):
        def resolve_visit_facts(self, **kwargs):
            transaction_states.append(db_connection.in_transaction)
            return super().resolve_visit_facts(**kwargs)

    try:
        ProposalVisitFactsService(
            db_connection,
            resolver=TransactionObservingResolver(intervals=[{"start": "09:00", "end": "20:00"}]),
        ).refresh(
            session_id=session.session_id,
            proposal_id="proposal_transaction_boundary",
        )

        assert transaction_states == [False, False]
    finally:
        database.close()


def test_concurrent_same_material_refresh_is_singleflight_and_does_not_lock_sqlite(isolated_test_database):
    primary_database = get_db()
    secondary_database = get_db()
    primary_db = next(primary_database)
    secondary_db = next(secondary_database)
    secondary_db.execute("PRAGMA busy_timeout = 100")
    session = ConversationService(primary_db).create_session("北京", "proposal facts singleflight")
    _persist_proposal(
        primary_db,
        session_id=session.session_id,
        proposal_id="proposal_singleflight",
        include_second_segment=True,
    )
    resolver = BlockingTwoSegmentResolver()
    results: list[dict] = []
    errors: list[BaseException] = []

    def run(connection):
        try:
            results.append(
                ProposalVisitFactsService(connection, resolver=resolver).refresh(
                    session_id=session.session_id,
                    proposal_id="proposal_singleflight",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports the captured worker failure
            errors.append(exc)

    primary = threading.Thread(target=run, args=(primary_db,), name="proposal-refresh-primary")
    secondary = threading.Thread(target=run, args=(secondary_db,), name="proposal-refresh-secondary")
    try:
        primary.start()
        assert resolver.second_primary_call_started.wait(timeout=2)
        secondary.start()
        time.sleep(0.2)
        resolver.release_primary.set()
        primary.join(timeout=3)
        secondary.join(timeout=3)

        assert not primary.is_alive()
        assert not secondary.is_alive()
        assert errors == []
        assert len(results) == 2
        assert results[0]["queriedAt"] == results[1]["queriedAt"]
        assert (
            results[0]["visitFactsBySegment"]["segment_visit"]["evidenceFingerprint"]
            == results[1]["visitFactsBySegment"]["segment_visit"]["evidenceFingerprint"]
        )
        assert resolver.call_threads == ["proposal-refresh-primary", "proposal-refresh-primary"]
        assert secondary_db.total_changes == 0
        assert primary_db.execute(
            "SELECT COUNT(*) FROM proposal_segment_visit_facts WHERE proposal_id = 'proposal_singleflight'"
        ).fetchone()[0] == 2
    finally:
        resolver.release_primary.set()
        primary.join(timeout=1)
        secondary.join(timeout=1)
        primary_database.close()
        secondary_database.close()


def test_locked_batch_persistence_returns_typed_retryable_failure(isolated_test_database):
    database = get_db()
    lock_database = get_db()
    db_connection = next(database)
    lock_connection = next(lock_database)
    db_connection.execute("PRAGMA busy_timeout = 50")
    session = ConversationService(db_connection).create_session("北京", "proposal facts typed busy")
    _persist_proposal(db_connection, session_id=session.session_id, proposal_id="proposal_typed_busy")
    service = ProposalVisitFactsService(
        db_connection,
        resolver=RecordedResolver(intervals=[{"start": "09:00", "end": "20:00"}]),
    )
    try:
        lock_connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(HTTPException) as caught:
            service.refresh(session_id=session.session_id, proposal_id="proposal_typed_busy")

        assert caught.value.status_code == 503
        assert caught.value.detail["code"] == "proposal_visit_facts_persist_busy"
        assert db_connection.in_transaction is False
        assert db_connection.execute(
            "SELECT COUNT(*) FROM proposal_segment_visit_facts WHERE proposal_id = 'proposal_typed_busy'"
        ).fetchone()[0] == 0
        lock_connection.rollback()

        retried = service.refresh(session_id=session.session_id, proposal_id="proposal_typed_busy")

        assert retried["proposalId"] == "proposal_typed_busy"
        assert db_connection.execute(
            "SELECT COUNT(*) FROM proposal_segment_visit_facts WHERE proposal_id = 'proposal_typed_busy'"
        ).fetchone()[0] == 1
    finally:
        lock_connection.rollback()
        database.close()
        lock_database.close()


def test_batch_persistence_is_atomic_when_a_later_row_fails(isolated_test_database, monkeypatch):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts atomic batch")
    _persist_proposal(
        db_connection,
        session_id=session.session_id,
        proposal_id="proposal_atomic_batch",
        include_second_segment=True,
    )
    service = ProposalVisitFactsService(
        db_connection,
        resolver=RecordedResolver(intervals=[{"start": "09:00", "end": "20:00"}]),
    )
    original_persist = service._persist
    persist_count = 0

    def fail_second_row(*args, **kwargs):
        nonlocal persist_count
        persist_count += 1
        if persist_count == 2:
            raise RuntimeError("second row failed")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(service, "_persist", fail_second_row)
    try:
        with pytest.raises(RuntimeError, match="second row failed"):
            service.refresh(session_id=session.session_id, proposal_id="proposal_atomic_batch")

        assert db_connection.in_transaction is False
        assert db_connection.execute(
            "SELECT COUNT(*) FROM proposal_segment_visit_facts WHERE proposal_id = 'proposal_atomic_batch'"
        ).fetchone()[0] == 0
    finally:
        database.close()


def test_sequential_fresh_refresh_reuses_exact_persisted_response_without_dml(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts sequential replay")
    _persist_proposal(
        db_connection,
        session_id=session.session_id,
        proposal_id="proposal_sequential_replay",
        include_second_segment=True,
    )
    resolver = RecordedResolver(intervals=[{"start": "09:00", "end": "20:00"}])
    service = ProposalVisitFactsService(db_connection, resolver=resolver)
    try:
        first = service.refresh(session_id=session.session_id, proposal_id="proposal_sequential_replay")
        changes_after_first = db_connection.total_changes

        second = service.refresh(session_id=session.session_id, proposal_id="proposal_sequential_replay")

        assert second == first
        assert len(resolver.calls) == 2
        assert db_connection.total_changes == changes_after_first
    finally:
        database.close()


def test_proposal_visit_facts_refresh_is_read_only_and_blocks_verified_conflict(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts")
    snapshot = _persist_proposal(db_connection, session_id=session.session_id)
    before = {
        table: db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("itinerary_versions", "itinerary_patches", "route_options", "agent_plan_proposals")
    }
    resolver = RecordedResolver(intervals=[{"start": "09:00", "end": "17:00"}])

    try:
        result = ProposalVisitFactsService(db_connection, resolver=resolver).refresh(
            session_id=session.session_id,
            proposal_id="proposal_visit",
        )

        after = {table: db_connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
        assert after == before
        assert len(resolver.calls) == 1
        assert result["materialFingerprint"] == PlanComparisonPreviewService.material_fingerprint(snapshot)
        assert result["visitFactsBySegment"]["segment_visit"]["scheduleCompatibility"] == "verified_conflict"
        with pytest.raises(HTTPException) as caught:
            ProposalVisitFactsService(db_connection).assert_adoption_allowed(proposal_id="proposal_visit")
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "proposal_opening_hours_conflict"
    finally:
        database.close()


def test_unknown_schedule_does_not_block_adoption(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts unknown")
    _persist_proposal(db_connection, session_id=session.session_id, proposal_id="proposal_unknown")
    resolver = RecordedResolver(intervals=[])
    service = ProposalVisitFactsService(db_connection, resolver=resolver)

    try:
        result = service.refresh(session_id=session.session_id, proposal_id="proposal_unknown")

        assert result["verifiedScheduleConflicts"] == []
        service.assert_adoption_allowed(proposal_id="proposal_unknown")
    finally:
        database.close()


def test_turn_projection_carriers_share_persisted_visit_facts(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts carrier replay")
    snapshot = _persist_proposal(
        db_connection,
        session_id=session.session_id,
        proposal_id="proposal_carrier_replay",
    )
    material_fingerprint = PlanComparisonPreviewService.material_fingerprint(snapshot)
    facts_service = ProposalVisitFactsService(
        db_connection,
        resolver=RecordedResolver(intervals=[{"start": "09:00", "end": "20:00"}]),
    )

    try:
        refreshed = facts_service.refresh(
            session_id=session.session_id,
            proposal_id="proposal_carrier_replay",
        )
        raw_projection = {
            "planningSelectionRootTurnId": "root_visit",
            "rootPortfolioId": "portfolio_visit",
            "proposalId": "proposal_carrier_replay",
            "sourceAssistantTurnId": "assistant_carrier",
            "choiceId": "choice_visit",
            "materialFingerprint": material_fingerprint,
            "status": "complete",
            "isPartial": False,
            "adoptionReady": True,
            "title": "开放时间测试方案",
            "days": snapshot["days"],
            "pendingSlots": [],
        }
        service = AgentService(db_connection, provider=SimpleNamespace())
        turn_id = service._insert_turn(
            session.session_id,
            "assistant",
            "方案可确认",
            "active",
            agent_response_json={
                "comparisonProjections": [raw_projection],
                "choiceOptions": [
                    {
                        "id": "choice_visit",
                        "action": "select_plan_proposal",
                        "kind": "plan_proposal",
                        "comparisonProjection": raw_projection,
                    }
                ],
            },
        )

        direct_turn = service._turn_response(turn_id)
        reloaded_turn = ConversationService(db_connection).get_session(session.session_id).turns[-1]

        for turn in (direct_turn, reloaded_turn):
            top_level = turn.comparison_projections[0]
            embedded = turn.choice_options[0]["comparisonProjection"]
            assert top_level["refreshStatus"] == refreshed["refreshStatus"]
            assert embedded["refreshStatus"] == refreshed["refreshStatus"]
            assert embedded["visitFactsBySegment"] == top_level["visitFactsBySegment"]
            assert (
                embedded["visitFactsBySegment"]["segment_visit"]["evidenceFingerprint"]
                == refreshed["visitFactsBySegment"]["segment_visit"]["evidenceFingerprint"]
            )
    finally:
        database.close()


def test_partial_overlap_with_verified_opening_interval_blocks_adoption(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts partial overlap")
    _persist_proposal(db_connection, session_id=session.session_id, proposal_id="proposal_partial_overlap")
    service = ProposalVisitFactsService(
        db_connection,
        resolver=RecordedResolver(intervals=[{"start": "09:00", "end": "18:30"}]),
    )

    try:
        result = service.refresh(session_id=session.session_id, proposal_id="proposal_partial_overlap")

        assert result["visitFactsBySegment"]["segment_visit"]["scheduleCompatibility"] == "verified_conflict"
        with pytest.raises(HTTPException) as caught:
            service.assert_adoption_allowed(proposal_id="proposal_partial_overlap")
        assert caught.value.detail["code"] == "proposal_opening_hours_conflict"
    finally:
        database.close()


def test_fresh_identity_matched_proposal_fact_can_be_reused_after_adoption(isolated_test_database):
    database = get_db()
    db_connection = next(database)
    session = ConversationService(db_connection).create_session("北京", "proposal facts reuse")
    _persist_proposal(db_connection, session_id=session.session_id, proposal_id="proposal_reuse")
    service = ProposalVisitFactsService(
        db_connection,
        resolver=RecordedResolver(intervals=[{"start": "09:00", "end": "20:00"}]),
    )

    try:
        service.refresh(session_id=session.session_id, proposal_id="proposal_reuse")
        now = datetime.now(timezone.utc).isoformat()
        db_connection.execute(
            """INSERT INTO itinerary_plans (
              id, user_id, inspiration_set_id, template_type, title, city,
              budget_target, budget_tier, budget_estimate, budget_delta_explanation,
              decision_rationale, status, created_at, updated_at
            ) VALUES ('plan_reuse', 'user', 'inspiration', 'ai', 'reuse', '北京',
                      NULL, 'unknown', 0, '', '', 'active', ?, ?)""",
            (now, now),
        )
        db_connection.execute(
            """INSERT INTO itinerary_days (
              id, plan_id, day_number, date, weather_summary, risk_summary, total_estimated_cost
            ) VALUES ('day_reuse', 'plan_reuse', 1, '2026-10-01', '', '', 0)"""
        )
        db_connection.execute(
            """INSERT INTO pois (
              id, plan_id, name, city, category, latitude, longitude, photo_url, source, confidence, amap_id
            ) VALUES ('poi_reuse', 'plan_reuse', '测试地点', '北京', 'attraction', NULL, NULL, NULL,
                      'amap', 1, 'B000TEST')"""
        )
        db_connection.execute(
            """INSERT INTO itinerary_segments (
              id, plan_id, day_id, segment_order, kind, start_time, end_time, poi_id,
              transport_mode, estimated_cost, notes
            ) VALUES ('segment_visit', 'plan_reuse', 'day_reuse', 1, 'visit', '18:00', '19:00',
                      'poi_reuse', 'walk', 0, '')"""
        )

        copied = service.copy_to_adopted_plan(proposal_id="proposal_reuse", plan_id="plan_reuse")

        assert copied == 1
        row = db_connection.execute(
            "SELECT * FROM segment_visit_facts WHERE plan_id = 'plan_reuse' AND segment_id = 'segment_visit'"
        ).fetchone()
        assert row is not None
        assert row["amap_poi_id"] == "B000TEST"
        assert row["visit_date"] == "2026-10-01"
        assert row["evidence_fingerprint"] == "e" * 64
        assert "_proposalSchedule" not in json.loads(row["facts_json"])
    finally:
        database.close()
