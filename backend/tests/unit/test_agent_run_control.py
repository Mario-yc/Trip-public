from threading import Event, Thread

import pytest
from fastapi import HTTPException

from src.services.agent_run_control import (
    acquire_session_run,
    active_session_run_turn,
    assert_session_run_active,
    begin_session_run_write,
    bind_session_run_turn,
    execute_session_run_prewrite,
    release_session_run,
    request_session_run_cancel,
)


def test_active_run_turn_binding_is_single_run_scoped_and_cleared_on_release() -> None:
    session_id = "sess_reasoning_recovery_binding"
    assert acquire_session_run(session_id) is True
    try:
        assert active_session_run_turn(session_id) == ""
        assert bind_session_run_turn(session_id, "turn_current_user") is True
        assert active_session_run_turn(session_id) == "turn_current_user"
        assert bind_session_run_turn(session_id, "turn_other_user") is False
        assert active_session_run_turn(session_id) == "turn_current_user"
    finally:
        release_session_run(session_id)

    assert active_session_run_turn(session_id) == ""


def test_cancelled_run_is_rejected_at_cooperative_boundaries() -> None:
    session_id = "sess_cancel_control"
    assert acquire_session_run(session_id) is True
    try:
        assert request_session_run_cancel(session_id) is True
        with pytest.raises(HTTPException) as error:
            assert_session_run_active(session_id)
        assert error.value.status_code == 499
    finally:
        release_session_run(session_id)

    assert request_session_run_cancel(session_id) is False


def test_irreversible_write_fence_rejects_late_cancel_without_poisoning_run() -> None:
    session_id = "sess_write_fence"
    assert acquire_session_run(session_id) is True
    try:
        assert begin_session_run_write(session_id) is True
        assert request_session_run_cancel(session_id) is False
        assert_session_run_active(session_id)
    finally:
        release_session_run(session_id)


def test_irreversible_write_fence_honors_cancel_requested_before_write() -> None:
    session_id = "sess_cancel_before_write"
    assert acquire_session_run(session_id) is True
    try:
        assert request_session_run_cancel(session_id) is True
        with pytest.raises(HTTPException) as error:
            begin_session_run_write(session_id)
        assert error.value.status_code == 499
    finally:
        release_session_run(session_id)


def test_prewrite_guard_linearizes_short_claim_before_later_cancel() -> None:
    session_id = "sess_prewrite_claim"
    claim_entered = Event()
    release_claim = Event()
    cancel_started = Event()
    cancel_finished = Event()
    claim_result: list[str] = []
    cancel_result: list[bool] = []

    def claim_action() -> str:
        claim_entered.set()
        assert release_claim.wait(timeout=2)
        return "claimed"

    def claim_worker() -> None:
        claim_result.append(execute_session_run_prewrite(session_id, claim_action))

    def cancel_worker() -> None:
        cancel_started.set()
        cancel_result.append(request_session_run_cancel(session_id))
        cancel_finished.set()

    assert acquire_session_run(session_id) is True
    try:
        writer = Thread(target=claim_worker)
        canceller = Thread(target=cancel_worker)
        writer.start()
        assert claim_entered.wait(timeout=2)
        canceller.start()
        assert cancel_started.wait(timeout=2)
        assert cancel_finished.wait(timeout=0.05) is False
        release_claim.set()
        writer.join(timeout=2)
        canceller.join(timeout=2)
        assert writer.is_alive() is False
        assert canceller.is_alive() is False
        with pytest.raises(HTTPException) as error:
            assert_session_run_active(session_id)
        assert error.value.status_code == 499
    finally:
        release_claim.set()
        release_session_run(session_id)

    assert claim_result == ["claimed"]
    assert cancel_result == [True]
