"""Process-local cooperative control for a single mutating Agent run.

The HTTP stream, provider loop and patch service run in the same worker.  Keeping
the control state here avoids a circular dependency between those layers while
making a user cancellation observable at every safe boundary.
"""

from threading import Lock
from typing import Callable, TypeVar

from fastapi import HTTPException


_GUARD = Lock()
_ACTIVE_SESSION_RUNS: set[str] = set()
_ACTIVE_SESSION_RUN_TURNS: dict[str, str] = {}
_CANCELLED_SESSION_RUNS: set[str] = set()
_WRITE_FENCED_SESSION_RUNS: set[str] = set()
_T = TypeVar("_T")


def acquire_session_run(session_id: str) -> bool:
    with _GUARD:
        if session_id in _ACTIVE_SESSION_RUNS:
            return False
        _ACTIVE_SESSION_RUNS.add(session_id)
        _ACTIVE_SESSION_RUN_TURNS[session_id] = ""
        return True


def bind_session_run_turn(session_id: str, source_user_turn_id: str) -> bool:
    """Bind the active lease to its durable user turn for GET-only recovery."""

    normalized = str(source_user_turn_id or "").strip()
    if not normalized:
        return False
    with _GUARD:
        if session_id not in _ACTIVE_SESSION_RUNS:
            return False
        current = _ACTIVE_SESSION_RUN_TURNS.get(session_id, "")
        if current and current != normalized:
            return False
        _ACTIVE_SESSION_RUN_TURNS[session_id] = normalized
        return True


def active_session_run_turn(session_id: str) -> str:
    with _GUARD:
        return str(_ACTIVE_SESSION_RUN_TURNS.get(session_id) or "")


def release_session_run(session_id: str) -> None:
    with _GUARD:
        _ACTIVE_SESSION_RUNS.discard(session_id)
        _ACTIVE_SESSION_RUN_TURNS.pop(session_id, None)
        _CANCELLED_SESSION_RUNS.discard(session_id)
        _WRITE_FENCED_SESSION_RUNS.discard(session_id)


def is_session_run_active(session_id: str) -> bool:
    """Expose only run liveness for the read-only reconnect status endpoint."""

    with _GUARD:
        return session_id in _ACTIVE_SESSION_RUNS


def request_session_run_cancel(session_id: str) -> bool:
    with _GUARD:
        if session_id not in _ACTIVE_SESSION_RUNS or session_id in _WRITE_FENCED_SESSION_RUNS:
            return False
        _CANCELLED_SESSION_RUNS.add(session_id)
        return True


def begin_session_run_write(session_id: str) -> bool:
    """Atomically cross the cooperative-cancel point of no return.

    Before this fence a requested cancellation raises 499 and the caller must
    remain zero-write. Once fenced, later cancellation requests are rejected so
    an authoritative Single-Writer commit cannot be reported as cancelled and
    then partially rolled back by an outer request wrapper.
    """
    with _GUARD:
        if session_id not in _ACTIVE_SESSION_RUNS:
            return False
        if session_id in _CANCELLED_SESSION_RUNS:
            raise HTTPException(status_code=499, detail="agent_run_cancelled")
        _WRITE_FENCED_SESSION_RUNS.add(session_id)
        return True


def execute_session_run_prewrite(session_id: str, action: Callable[[], _T]) -> _T:
    """Linearize a short reversible CAS against cooperative cancellation.

    This guard is for pre-writer execution claims, not itinerary writes. If
    cancellation wins first, the action is never called. If the CAS wins first,
    cancellation may still be accepted immediately afterwards and the existing
    outer rollback path can unconsume the reversible claim.
    """
    with _GUARD:
        if session_id in _CANCELLED_SESSION_RUNS:
            raise HTTPException(status_code=499, detail="agent_run_cancelled")
        return action()


def assert_session_run_active(session_id: str) -> None:
    with _GUARD:
        if session_id in _CANCELLED_SESSION_RUNS:
            raise HTTPException(status_code=499, detail="agent_run_cancelled")
