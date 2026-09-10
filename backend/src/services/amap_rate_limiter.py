from contextlib import contextmanager
from collections import deque
from threading import Lock, RLock
from time import monotonic, sleep
from typing import Callable, Deque, Iterator, Optional


AMAP_BASIC_WEB_SERVICE_QPS = 3
AMAP_MAP_LOCATION_QPS = 10
AMAP_WEB_SERVICE_MIN_INTERVAL_SECONDS = 0.35
AMAP_PLACE_CAPTURE_COOLDOWN_SECONDS = 90.0
_TIME_COMPARISON_EPSILON_SECONDS = 1e-9


class SlidingWindowRateLimiter:
    def __init__(
        self,
        max_calls: int,
        window_seconds: float = 1.0,
        min_interval_seconds: float = 0.0,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.max_calls = max(1, int(max_calls))
        self.window_seconds = max(0.001, float(window_seconds))
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._time_fn = time_fn or monotonic
        self._sleep_fn = sleep_fn or sleep
        self._lock = Lock()
        self._calls: Deque[float] = deque()

    def acquire(self) -> None:
        while True:
            wait_seconds = 0.0
            with self._lock:
                now = self._time_fn()
                threshold = now - self.window_seconds
                while self._calls and self._calls[0] <= threshold:
                    self._calls.popleft()
                if self._calls and self.min_interval_seconds > 0:
                    elapsed_since_last = now - self._calls[-1]
                    if elapsed_since_last + _TIME_COMPARISON_EPSILON_SECONDS < self.min_interval_seconds:
                        wait_seconds = max(0.001, self.min_interval_seconds - elapsed_since_last)
                    elif len(self._calls) < self.max_calls:
                        self._calls.append(now)
                        return
                    else:
                        wait_seconds = max(0.001, self.window_seconds - (now - self._calls[0]))
                elif len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                else:
                    wait_seconds = max(0.001, self.window_seconds - (now - self._calls[0]))
            self._sleep_fn(wait_seconds)

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


class AmapRateLimitCooldownActive(RuntimeError):
    """The key/transport scope is cooling down before any external effect."""


class AmapPlaceCaptureCallGate:
    """Apply the shared key limiter plus a capture-wide provider cooldown.

    Production HTTP always passes through the gate. Tests may explicitly opt
    stub calls into the same path so pacing can be verified with an injected
    monotonic clock and sleeper without waiting or using a socket.
    """

    def __init__(
        self,
        *,
        limiter: SlidingWindowRateLimiter,
        cooldown_seconds: float = AMAP_PLACE_CAPTURE_COOLDOWN_SECONDS,
        time_fn: Optional[Callable[[], float]] = None,
        include_stub_calls: bool = False,
    ) -> None:
        self._limiter = limiter
        self._cooldown_seconds = max(0.001, float(cooldown_seconds))
        self._time_fn = time_fn or monotonic
        self._include_stub_calls = bool(include_stub_calls)
        self._lock = RLock()
        self._cooldown_until = 0.0

    def before_external_call(self, *, production_http_active: bool) -> None:
        with self.external_call_lease(
            production_http_active=production_http_active
        ):
            return

    @contextmanager
    def external_call_lease(
        self,
        *,
        production_http_active: bool,
    ) -> Iterator[None]:
        if not production_http_active and not self._include_stub_calls:
            yield
            return
        # Keep the transport-scope mutex through the actual one-shot call.
        # A 10021 observed by that call is recorded reentrantly before the
        # lease is released, so no not-yet-started capture can pass the gap.
        with self._lock:
            self._raise_if_cooling_down()
            self._limiter.acquire()
            self._raise_if_cooling_down()
            yield

    def mark_provider_rate_limited(self, *, production_http_active: bool) -> None:
        if not production_http_active and not self._include_stub_calls:
            return
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until,
                self._time_fn() + self._cooldown_seconds,
            )

    def reset(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0

    def _raise_if_cooling_down(self) -> None:
        if self._time_fn() < self._cooldown_until:
            raise AmapRateLimitCooldownActive(
                "amap_key_transport_cooldown_active"
            )


# AMap WebService QPS is enforced by key, not by endpoint. This limiter is
# process-local; multi-worker deployments still need a shared token bucket
# (SQLite/file lock/Redis/etc.) to make the guarantee global across processes.
AMAP_WEB_SERVICE_RATE_LIMITER = SlidingWindowRateLimiter(
    AMAP_BASIC_WEB_SERVICE_QPS,
    min_interval_seconds=AMAP_WEB_SERVICE_MIN_INTERVAL_SECONDS,
)
AMAP_PLACE_CAPTURE_CALL_GATE = AmapPlaceCaptureCallGate(
    limiter=AMAP_WEB_SERVICE_RATE_LIMITER,
)

# Backwards-compatible aliases. They intentionally point at the same object so
# older imports do not recreate independent per-service QPS buckets.
AMAP_BASIC_SEARCH_RATE_LIMITER = AMAP_WEB_SERVICE_RATE_LIMITER
AMAP_BASIC_LBS_RATE_LIMITER = AMAP_WEB_SERVICE_RATE_LIMITER
AMAP_BASIC_WEATHER_RATE_LIMITER = AMAP_WEB_SERVICE_RATE_LIMITER
