import pytest
from threading import Event, Thread

from src.services.amap_rate_limiter import (
    AMAP_BASIC_LBS_RATE_LIMITER,
    AMAP_BASIC_SEARCH_RATE_LIMITER,
    AMAP_BASIC_WEATHER_RATE_LIMITER,
    AMAP_BASIC_WEB_SERVICE_QPS,
    AMAP_MAP_LOCATION_QPS,
    AMAP_WEB_SERVICE_RATE_LIMITER,
    SlidingWindowRateLimiter,
)


def test_amap_basic_web_service_qps_is_three():
    assert AMAP_BASIC_WEB_SERVICE_QPS == 3


def test_amap_map_location_qps_is_ten():
    assert AMAP_MAP_LOCATION_QPS == 10


def test_sliding_window_rate_limiter_waits_after_three_calls_per_second():
    now = 100.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(3, window_seconds=1.0, time_fn=time_fn, sleep_fn=sleep_fn)

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == [1.0]
    assert now == 101.0


def test_amap_web_service_limiters_share_one_key_level_bucket():
    assert AMAP_BASIC_SEARCH_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER
    assert AMAP_BASIC_LBS_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER
    assert AMAP_BASIC_WEATHER_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER


def test_amap_web_service_call_sites_import_same_limiter():
    from src.providers import travel_tools
    from src.services import map_poi_service, route_service

    assert map_poi_service.AMAP_WEB_SERVICE_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER
    assert route_service.AMAP_WEB_SERVICE_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER
    assert travel_tools.AMAP_WEB_SERVICE_RATE_LIMITER is AMAP_WEB_SERVICE_RATE_LIMITER


def test_sliding_window_rate_limiter_enforces_minimum_interval_between_calls():
    now = 100.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(
        3,
        window_seconds=1.0,
        min_interval_seconds=0.35,
        time_fn=time_fn,
        sleep_fn=sleep_fn,
    )

    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    assert sleeps == pytest.approx([0.35, 0.35])
    assert now == pytest.approx(100.7)


def test_place_capture_gate_reuses_key_limiter_and_fails_closed_during_10021_cooldown():
    from src.services import amap_rate_limiter

    gate_type = getattr(amap_rate_limiter, "AmapPlaceCaptureCallGate", None)
    cooldown_error = getattr(amap_rate_limiter, "AmapRateLimitCooldownActive", None)
    assert callable(gate_type) and isinstance(cooldown_error, type), (
        "Place capture is missing the shared pacing and 10021 cooldown contract"
    )

    now = 100.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(
        3,
        window_seconds=1.0,
        min_interval_seconds=0.35,
        time_fn=time_fn,
        sleep_fn=sleep_fn,
    )
    gate = gate_type(
        limiter=limiter,
        cooldown_seconds=90.0,
        time_fn=time_fn,
        include_stub_calls=True,
    )

    for _ in range(7):
        gate.before_external_call(production_http_active=False)

    assert sleeps == pytest.approx([0.35] * 6)
    assert now == pytest.approx(102.1)

    gate.mark_provider_rate_limited(production_http_active=False)
    with pytest.raises(cooldown_error, match="amap_key_transport_cooldown_active"):
        gate.before_external_call(production_http_active=False)
    assert sleeps == pytest.approx([0.35] * 6)

    now += 90.0
    gate.before_external_call(production_http_active=False)
    assert now == pytest.approx(192.1)

    production_only_gate = gate_type(
        limiter=SlidingWindowRateLimiter(
            3,
            min_interval_seconds=0.35,
            time_fn=time_fn,
            sleep_fn=sleep_fn,
        ),
        time_fn=time_fn,
    )
    before_production = now
    production_only_gate.before_external_call(production_http_active=False)
    production_only_gate.before_external_call(production_http_active=True)
    production_only_gate.before_external_call(production_http_active=True)
    assert now == pytest.approx(before_production + 0.35)


def test_place_capture_gate_serializes_call_start_with_10021_cooldown():
    from src.services.amap_rate_limiter import (
        AmapPlaceCaptureCallGate,
        AmapRateLimitCooldownActive,
    )

    now = 100.0
    gate = AmapPlaceCaptureCallGate(
        limiter=SlidingWindowRateLimiter(
            100,
            time_fn=lambda: now,
            sleep_fn=lambda _seconds: pytest.fail("unexpected pacing sleep"),
        ),
        cooldown_seconds=90.0,
        time_fn=lambda: now,
        include_stub_calls=True,
    )
    first_started = Event()
    second_waiting = Event()
    release_first = Event()
    outcomes: list[str] = []

    def first_call() -> None:
        with gate.external_call_lease(production_http_active=False):
            first_started.set()
            assert release_first.wait(timeout=2.0)
            gate.mark_provider_rate_limited(production_http_active=False)

    def second_call() -> None:
        assert first_started.wait(timeout=2.0)
        second_waiting.set()
        try:
            with gate.external_call_lease(production_http_active=False):
                outcomes.append("opened")
        except AmapRateLimitCooldownActive:
            outcomes.append("cooldown")

    first = Thread(target=first_call)
    second = Thread(target=second_call)
    first.start()
    second.start()
    assert second_waiting.wait(timeout=2.0)
    release_first.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes == ["cooldown"]
