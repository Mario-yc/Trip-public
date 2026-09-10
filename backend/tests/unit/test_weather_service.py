from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from src.providers.travel_tools import AmapWeatherProvider
from src.services.amap_rate_limiter import SlidingWindowRateLimiter
from src.services.weather_service import AMAP_WEATHER_URL, WeatherService


class FakeAmapWeatherResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return (
            b'{"status":"1","forecasts":[{"city":"\\u5317\\u4eac\\u5e02","casts":[{"date":"2026-06-10",'
            b'"dayweather":"\\u6674","nightweather":"\\u6674","daytemp":"28","nighttemp":"20"}]}]}'
        )


def test_amap_weather_provider_uses_shared_webservice_limiter(monkeypatch):
    acquire_calls: list[str] = []

    class FakeLimiter:
        def acquire(self):
            acquire_calls.append("acquire")

    monkeypatch.setattr("src.providers.travel_tools.AMAP_WEB_SERVICE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr("src.providers.travel_tools.urlopen", lambda _url, timeout: FakeAmapWeatherResponse())

    response = AmapWeatherProvider(api_key="test-key").query("北京")

    assert response.weather == "晴"
    assert acquire_calls == ["acquire"]


def test_amap_weather_provider_waits_on_fourth_request_per_second(monkeypatch):
    now = 30.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(3, window_seconds=1.0, time_fn=time_fn, sleep_fn=sleep_fn)
    monkeypatch.setattr("src.providers.travel_tools.AMAP_WEB_SERVICE_RATE_LIMITER", limiter)
    monkeypatch.setattr("src.providers.travel_tools.urlopen", lambda _url, timeout: FakeAmapWeatherResponse())
    provider = AmapWeatherProvider(api_key="test-key")

    for _index in range(4):
        provider.query("北京")

    assert sleeps == [1.0]


def test_amap_weather_provider_limits_concurrent_amap_requests(monkeypatch):
    active_requests = 0
    max_active_requests = 0
    lock = Lock()
    limited_requests_started = Event()
    release_requests = Event()

    class FakeLimiter:
        def acquire(self):
            return None

    def fake_urlopen(_url: str, timeout: float):
        nonlocal active_requests, max_active_requests
        with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            if active_requests == 3:
                limited_requests_started.set()
        release_requests.wait(timeout=2)
        with lock:
            active_requests -= 1
        return FakeAmapWeatherResponse()

    monkeypatch.setattr("src.providers.travel_tools.AMAP_WEB_SERVICE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr("src.providers.travel_tools.urlopen", fake_urlopen)
    provider = AmapWeatherProvider(api_key="test-key")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(provider.query, "北京") for _index in range(5)]
        assert limited_requests_started.wait(timeout=2)
        assert max_active_requests == 3
        release_requests.set()
        assert [future.result(timeout=2).weather for future in futures] == ["晴"] * 5
        assert max_active_requests == 3


def test_weather_service_without_key_returns_visible_degraded_status_without_mock_weather():
    signal = WeatherService(weather_provider_key="").build_weather_signal("北京", ["拍照优先"])

    assert signal.source == "高德天气"
    assert signal.data_status == "degraded"
    assert signal.fallback_used is False
    assert signal.provider_name == "amap-weather-provider"
    assert signal.confidence == 0
    assert signal.hourly_forecast == []
    assert "configured" in signal.failure_reason
    assert "未使用 mock 天气数据" in signal.user_visible_caveat


def test_weather_service_parses_amap_forecast_and_classifies_purpose_impact():
    def fake_http_get(url: str, _timeout: float) -> dict:
        assert url.startswith(AMAP_WEATHER_URL)
        assert "city=110000" in url
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-06-10",
                            "dayweather": "小雨",
                            "nightweather": "阴",
                            "daytemp": "28",
                            "nighttemp": "20",
                            "daywind": "东",
                            "nightwind": "东",
                        }
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["拍照优先"],
    )

    assert signal.source == "高德天气"
    assert signal.data_status == "degraded"
    assert signal.provider_name == "amap-weather-provider"
    assert signal.fallback_used is False
    assert signal.confidence == 0.72
    assert signal.risk_level == "risky"
    assert signal.date == "2026-06-10"
    assert signal.hourly_forecast[0]["dayWeather"] == "小雨"
    assert "拍照" in signal.purpose_impact_reason
    assert signal.source_url == AMAP_WEATHER_URL


def test_weather_service_selects_travel_date_from_context():
    calls = 0

    def fake_http_get(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-10-15",
                            "dayweather": "晴",
                            "nightweather": "晴",
                            "daytemp": "24",
                            "nighttemp": "15",
                        },
                        {
                            "date": "2026-10-16",
                            "dayweather": "雷阵雨",
                            "nightweather": "小雨",
                            "daytemp": "20",
                            "nighttemp": "13",
                        },
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {"travelDateRange": {"start": "2026-10-16", "end": "2026-10-17"}, "tripPurpose": "夜景拍照"},
    )

    assert signal.date == "2026-10-16"
    assert signal.data_status == "outside_forecast_window"
    assert signal.risk_level == "unknown"
    assert "未使用今日天气" in signal.daily_summary
    assert calls == 0


def test_weather_service_selects_travel_date_from_agent_understood_requirements():
    calls = 0

    def fake_http_get(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-10-15",
                            "dayweather": "晴",
                            "nightweather": "晴",
                            "daytemp": "24",
                            "nighttemp": "15",
                        },
                        {
                            "date": "2026-10-16",
                            "dayweather": "小雨",
                            "nightweather": "阴",
                            "daytemp": "20",
                            "nighttemp": "13",
                        },
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {"understoodRequirements": {"fields": {"travelDate": "2026-10-16"}}},
    )

    assert signal.date == "2026-10-16"
    assert signal.data_status == "outside_forecast_window"
    assert calls == 0


def test_weather_service_does_not_use_today_forecast_when_travel_date_is_not_covered():
    def fake_http_get(_url: str, _timeout: float) -> dict:
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-06-22",
                            "dayweather": "晴",
                            "nightweather": "晴",
                            "daytemp": "31",
                            "nighttemp": "22",
                        }
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {"understoodRequirements": {"fields": {"travelDate": "10月中旬"}}},
    )

    assert signal.date == "10月中旬"
    assert signal.daily_summary == "旅行日期对应天气预报尚不可用，待出行前刷新"
    assert signal.confidence == 0
    assert signal.data_status == "forecast_not_supported_yet"
    assert signal.risk_level == "unknown"
    assert signal.failure_reason == "forecast_not_supported_yet"
    assert "未使用今日天气" in signal.user_visible_caveat


def test_weather_service_uses_resolved_trip_dates_before_other_context():
    calls = 0

    def fake_http_get(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {"date": "2026-07-01", "dayweather": "晴", "nightweather": "晴", "daytemp": "31", "nighttemp": "22"},
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {
            "travelDateRange": {"start": "2026-07-01"},
            "resolvedTripDates": {"status": "resolved", "startDate": "2026-10-01", "weatherForecastSupported": False},
        },
    )

    assert signal.date == "2026-10-01"
    assert signal.data_status == "outside_forecast_window"
    assert calls == 0


def test_weather_service_uses_weather_sensitivity_even_without_purpose_tag():
    def fake_http_get(_url: str, _timeout: float) -> dict:
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-10-16",
                            "dayweather": "大风",
                            "nightweather": "晴",
                            "daytemp": "21",
                            "nighttemp": "12",
                        }
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {"weatherSensitivity": "高", "preferenceSummary": "对天气敏感，老人同行"},
    )

    assert signal.risk_level == "risky"
    assert "天气敏感" in signal.purpose_impact_reason


def test_weather_service_mentions_impacted_itinerary_time_window():
    def fake_http_get(_url: str, _timeout: float) -> dict:
        return {
            "status": "1",
            "forecasts": [
                {
                    "city": "北京市",
                    "casts": [
                        {
                            "date": "2026-10-16",
                            "dayweather": "雷阵雨",
                            "nightweather": "小雨",
                            "daytemp": "20",
                            "nighttemp": "13",
                        }
                    ],
                }
            ],
        }

    signal = WeatherService(weather_provider_key="test-key", http_get=fake_http_get).build_weather_signal(
        "北京",
        ["历史文化"],
        {
            "tripPurpose": "夜景拍照",
            "travelTimeWindows": [
                {"poiName": "故宫博物院", "startTime": "09:30", "endTime": "11:30"},
                {"poiName": "什刹海", "startTime": "18:30", "endTime": "20:30"},
            ],
        },
    )

    assert signal.risk_level == "risky"
    assert "09:30-11:30" in signal.purpose_impact_reason
    assert "18:30-20:30" in signal.purpose_impact_reason
