import pytest
from types import SimpleNamespace
from fastapi import HTTPException

from src.services.agent_run_control import (
    acquire_session_run,
    begin_session_run_write,
    release_session_run,
    request_session_run_cancel,
)
from src.services.plan_proposal_commit_service import PlanProposalCommitService
import src.services.plan_proposal_commit_service as commit_module


def test_commit_service_requires_server_store_and_marks_only_selected_proposal():
    # The store connection is supplied by the fixture-backed unit tested in
    # test_plan_portfolio_store; this test only verifies the ownership boundary.
    class FakeStore:
        def __init__(self):
            self.claimed = False
            self.committed = None

        def load_choice(self, **_):
            return (
                {"id": "portfolio", "status": "awaiting_selection", "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {
                    "id": "proposal",
                    "status": "offered",
                    "snapshot_json": '{"days": []}',
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            self.claimed = True
            return True

        def mark_committed(self, **kwargs):
            self.committed = kwargs

        def release_selection(self, **kwargs):
            raise AssertionError(f"unexpected release: {kwargs}")

    store = FakeStore()
    result = PlanProposalCommitService(store).commit(
        session_id="session",
        source_user_turn_id="turn",
        choice_id="opaque",
        active_version_id=None,
        verify=lambda snapshot: snapshot == {"days": []},
        persist=lambda snapshot: "version_1",
    )
    assert result == "version_1"
    assert store.committed == {
        "portfolio_id": "portfolio",
        "proposal_id": "proposal",
        "result_version_id": "version_1",
    }


def test_commit_rewrites_preview_only_rationale_before_active_version_persist():
    persisted = []

    class FakeStore:
        def load_choice(self, **_):
            return (
                {
                    "id": "portfolio",
                    "status": "awaiting_selection",
                    "expected_base_version_id": None,
                    "visible_proposal_ids": ["proposal"],
                },
                {
                    "id": "proposal",
                    "status": "offered",
                    "snapshot_json": (
                        '{"days":[],"status":"draft","decisionRationale":'
                        '"地点身份已核验；本方案尚未写入正式行程。"}'
                    ),
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            return True

        def mark_committed(self, **_):
            return None

        def release_selection(self, **kwargs):
            raise AssertionError(f"unexpected release: {kwargs}")

    def verify(snapshot):
        assert snapshot["status"] == "draft"
        assert "尚未写入正式行程" not in snapshot["decisionRationale"]
        assert "已采用为当前可编辑行程" in snapshot["decisionRationale"]
        return True

    def persist(snapshot):
        persisted.append(snapshot)
        return "version_adopted"

    result = PlanProposalCommitService(FakeStore()).commit(
        session_id="session",
        source_user_turn_id="turn",
        choice_id="opaque",
        active_version_id=None,
        verify=verify,
        persist=persist,
    )

    assert result == "version_adopted"
    assert persisted[0]["decisionRationale"] == (
        "地点身份已核验；已采用为当前可编辑行程，后续修改继续通过版本事务保存。"
    )


def test_commit_rejects_proposal_without_persisted_verifier_pass():
    class FakeStore:
        def load_choice(self, **_):
            return (
                {"id": "portfolio", "status": "awaiting_selection", "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {"id": "proposal", "status": "offered", "snapshot_json": "{}", "verifier_json": '{"passed": false}'},
            )

        def claim_selection(self, **_):
            raise AssertionError("must not claim")

    try:
        PlanProposalCommitService(FakeStore()).commit(
            session_id="session",
            source_user_turn_id="turn",
            choice_id="opaque",
            active_version_id=None,
            verify=lambda _: True,
            persist=lambda _: "version",
        )
    except ValueError as error:
        assert str(error) == "plan_proposal_verifier_not_passed"
    else:
        raise AssertionError("proposal without verifier pass was accepted")


def test_editable_draft_uses_same_verify_and_writer_exactly_once(monkeypatch):
    monkeypatch.setattr(
        commit_module,
        "get_settings",
        lambda: SimpleNamespace(agent_soft_slot_draft_adoption_enabled=True),
    )
    calls = {"verify": 0, "persist": 0}

    class FakeStore:
        def __init__(self):
            self.committed = False

        def load_choice(self, **_):
            status = "committed" if self.committed else "awaiting_selection"
            proposal_status = "committed" if self.committed else "offered"
            return (
                {"id": "portfolio", "status": status, "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {
                    "id": "proposal",
                    "status": proposal_status,
                    "snapshot_json": '{"days":[{"dayNumber":1,"segments":[]}],"portfolioPendingSlots":[{"planningSlotId":"slot_local","requirementLevel":"soft"}]}',
                    "verifier_json": '{"passed":false,"draftPassed":true,"pendingHardSlotCount":0,"pendingSoftSlotCount":1}',
                },
            )

        def claim_selection(self, **_):
            return not self.committed

        def mark_committed(self, **_):
            self.committed = True

        def release_selection(self, **kwargs):
            raise AssertionError(f"unexpected release: {kwargs}")

    store = FakeStore()

    def verify(snapshot):
        calls["verify"] += 1
        assert snapshot["portfolioPendingSlots"][0]["requirementLevel"] == "soft"
        return True

    def persist(_snapshot):
        calls["persist"] += 1
        return "version_draft"

    assert PlanProposalCommitService(store).commit(session_id="session", source_user_turn_id="turn", choice_id="opaque", active_version_id=None, verify=verify, persist=persist) == "version_draft"
    with pytest.raises(ValueError, match="committed_result_must_be_replayed"):
        PlanProposalCommitService(store).commit(session_id="session", source_user_turn_id="turn", choice_id="opaque", active_version_id=None, verify=verify, persist=persist)
    assert calls == {"verify": 1, "persist": 1}


def test_route_only_partial_is_reverified_before_single_writer_claim():
    events = []

    class FakeStore:
        def load_choice(self, **_):
            return (
                {
                    "id": "portfolio",
                    "status": "awaiting_selection",
                    "expected_base_version_id": "ver_base",
                    "visible_proposal_ids": ["proposal"],
                },
                {
                    "id": "proposal",
                    "status": "route_provider_failed",
                    "snapshot_json": '{"originProjectionMode":"partial_preview","portfolioRouteEvidence":[]}',
                    "verifier_json": '{"passed":false,"hardFailures":["portfolio_route_quality:route_evidence_incomplete"]}',
                },
            )

        def claim_selection(self, **_):
            events.append("claim")
            return True

        def mark_committed(self, **_):
            events.append("mark_committed")

        def release_selection(self, **_):
            raise AssertionError("route recheck succeeded; release is unexpected")

    def verify(snapshot):
        events.append("verify")
        assert snapshot["originProjectionMode"] == "partial_preview"
        snapshot["portfolioRouteEvidence"] = [{"id": "fresh-route"}]
        return True

    def persist(snapshot):
        events.append("persist")
        assert snapshot["portfolioRouteEvidence"] == [{"id": "fresh-route"}]
        return "ver_promoted"

    assert PlanProposalCommitService(FakeStore()).commit(
        session_id="session_partial_route",
        source_user_turn_id="turn",
        choice_id="opaque",
        active_version_id="ver_base",
        verify=verify,
        persist=persist,
    ) == "ver_promoted"
    assert events == ["verify", "claim", "persist", "mark_committed"]


def test_commit_preflight_error_does_not_claim_or_release_selection():
    class FakeStore:
        def __init__(self):
            self.claim_calls = 0
            self.release_calls = 0

        def load_choice(self, **_):
            return (
                {"id": "portfolio", "status": "awaiting_selection", "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {"id": "proposal", "status": "offered", "snapshot_json": "{}", "verifier_json": '{"passed": true}'},
            )

        def claim_selection(self, **_):
            self.claim_calls += 1
            return True

        def release_selection(self, **_):
            self.release_calls += 1

    store = FakeStore()

    def verify(_snapshot):
        raise RuntimeError("plan_proposal_route_quality_failed")

    with pytest.raises(RuntimeError, match="plan_proposal_route_quality_failed"):
        PlanProposalCommitService(store).commit(
            session_id="session",
            source_user_turn_id="turn",
            choice_id="opaque",
            active_version_id=None,
            verify=verify,
            persist=lambda _: "must_not_write",
        )
    assert store.claim_calls == 0
    assert store.release_calls == 0


def test_commit_cancel_after_preflight_before_claim_is_zero_write(monkeypatch):
    session_id = "session_cancel_after_preflight"

    class FakeStore:
        def __init__(self):
            self.claim_calls = 0
            self.release_calls = 0
            self.commit_calls = 0

        def load_choice(self, **_):
            return (
                {"id": "portfolio", "status": "awaiting_selection", "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {
                    "id": "proposal",
                    "status": "offered",
                    "snapshot_json": '{"days": []}',
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            self.claim_calls += 1
            return True

        def release_selection(self, **_):
            self.release_calls += 1

        def mark_committed(self, **_):
            self.commit_calls += 1

    store = FakeStore()
    persist_calls = 0
    verify_calls = 0

    def verify(_snapshot):
        nonlocal verify_calls
        verify_calls += 1
        return True

    def persist(_snapshot):
        nonlocal persist_calls
        persist_calls += 1
        return "must_not_write"

    def cancel_before_write_fence(received_session_id):
        assert received_session_id == session_id
        assert verify_calls == 1
        assert store.claim_calls == 0
        assert request_session_run_cancel(session_id) is True
        return begin_session_run_write(session_id)

    monkeypatch.setattr(
        "src.services.plan_proposal_commit_service.begin_session_run_write",
        cancel_before_write_fence,
    )

    assert acquire_session_run(session_id) is True
    try:
        with pytest.raises(HTTPException) as error:
            PlanProposalCommitService(store).commit(
                session_id=session_id,
                source_user_turn_id="turn",
                choice_id="opaque",
                active_version_id=None,
                verify=verify,
                persist=persist,
            )
        assert error.value.status_code == 499
        assert error.value.detail == "agent_run_cancelled"
    finally:
        release_session_run(session_id)

    assert store.claim_calls == 0
    assert store.release_calls == 0
    assert persist_calls == 0
    assert store.commit_calls == 0


def test_commit_fences_active_run_before_claim_and_writer_refence_is_idempotent():
    session_id = "session_commit_write_fence"
    events = []

    class FakeStore:
        def load_choice(self, **_):
            return (
                {"id": "portfolio", "status": "awaiting_selection", "expected_base_version_id": None, "visible_proposal_ids": ["proposal"]},
                {
                    "id": "proposal",
                    "status": "offered",
                    "snapshot_json": '{"days": []}',
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            assert request_session_run_cancel(session_id) is False
            events.append("claim")
            return True

        def release_selection(self, **kwargs):
            raise AssertionError(f"unexpected release: {kwargs}")

        def mark_committed(self, **_):
            events.append("commit")

    def verify(_snapshot):
        events.append("verify")
        return True

    def persist(_snapshot):
        assert begin_session_run_write(session_id) is True
        events.append("persist")
        return "version_1"

    assert acquire_session_run(session_id) is True
    try:
        result = PlanProposalCommitService(FakeStore()).commit(
            session_id=session_id,
            source_user_turn_id="turn",
            choice_id="opaque",
            active_version_id=None,
            verify=verify,
            persist=persist,
        )
    finally:
        release_session_run(session_id)

    assert result == "version_1"
    assert events == ["verify", "claim", "persist", "commit"]


def test_commit_reports_expired_portfolio_before_claiming():
    class FakeStore:
        def load_choice(self, **_):
            return (
                {
                    "id": "portfolio",
                    "status": "expired",
                    "failure_reason": "plan_portfolio_expired",
                    "expected_base_version_id": None,
                    "visible_proposal_ids": ["proposal"],
                },
                {
                    "id": "proposal",
                    "status": "expired",
                    "snapshot_json": "{}",
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            raise AssertionError("expired proposal must not be claimed")

    try:
        PlanProposalCommitService(FakeStore()).commit(
            session_id="session",
            source_user_turn_id="turn",
            choice_id="opaque",
            active_version_id=None,
            verify=lambda _: True,
            persist=lambda _: "must_not_write",
        )
    except ValueError as error:
        assert str(error) == "plan_proposal_expired"
    else:
        raise AssertionError("expired proposal was accepted")


def test_commit_rejects_persisted_proposal_not_in_visible_pareto_set():
    class FakeStore:
        def load_choice(self, **_):
            return (
                {
                    "id": "portfolio",
                    "status": "awaiting_selection",
                    "expected_base_version_id": None,
                    "visible_proposal_ids": ["proposal_visible"],
                },
                {
                    "id": "proposal_hidden",
                    "status": "offered",
                    "snapshot_json": "{}",
                    "verifier_json": '{"passed": true}',
                },
            )

        def claim_selection(self, **_):
            raise AssertionError("hidden proposal must not be claimed")

    try:
        PlanProposalCommitService(FakeStore()).commit(
            session_id="session",
            source_user_turn_id="turn",
            choice_id="opaque_hidden",
            active_version_id=None,
            verify=lambda _: True,
            persist=lambda _: "must_not_write",
        )
    except ValueError as error:
        assert str(error) == "plan_proposal_not_visible"
    else:
        raise AssertionError("non-visible proposal was accepted")
