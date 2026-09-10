from __future__ import annotations

from contextlib import contextmanager
from threading import Lock, RLock
from typing import Iterator


_registry_guard = Lock()
_plan_locks: dict[str, RLock] = {}


@contextmanager
def timeline_write_lease(plan_id: str) -> Iterator[None]:
    """Serialize every in-process write entry for one itinerary plan.

    The lock is re-entrant because the mutation transaction owns the lease for
    bind/patch/verify/compensate while ``ItineraryPatchService`` independently
    enforces the same lease for direct API and legacy Agent patch callers.
    """

    with _registry_guard:
        lease = _plan_locks.setdefault(str(plan_id), RLock())
    with lease:
        yield
