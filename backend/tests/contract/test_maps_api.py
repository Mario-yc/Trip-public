from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from threading import Event, Lock
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app
from src.services.amap_call_budget import AmapCallBudget, amap_call_budget_scope
from src.services.amap_rate_limiter import SlidingWindowRateLimiter
from src.services.map_poi_service import MAP_BASIC_SEARCH_PARALLELISM, MapPoiService, clear_map_poi_runtime_state

import sqlite3


@pytest.fixture(autouse=True)
def reset_map_poi_runtime_state():
    clear_map_poi_runtime_state()
    yield
    clear_map_poi_runtime_state()


def clear_poi_candidates() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM amap_poi_candidates")



def test_basic_map_search_service_uses_three_concurrent_requests():
    assert MAP_BASIC_SEARCH_PARALLELISM == 3


def test_map_poi_webservice_search_uses_shared_webservice_limiter(monkeypatch):
    acquire_calls: list[str] = []

    class FakeLimiter:
        def acquire(self):
            acquire_calls.append("acquire")

    monkeypatch.setattr("src.services.map_poi_service.AMAP_WEB_SERVICE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr("src.services.map_poi_service.urlopen", lambda _url, timeout: FakeAmapPlaceResponse())
    service = MapPoiService(map_provider_key="test-amap-key")

    service._fetch_with_limit("https://example.invalid/place-text", "place/text")
    service._fetch_with_limit("https://example.invalid/place-around", "place/around")

    assert acquire_calls == ["acquire", "acquire"]


def test_map_poi_webservice_search_waits_on_fourth_request_per_second(monkeypatch):
    now = 10.0
    sleeps: list[float] = []

    def time_fn() -> float:
        return now

    def sleep_fn(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    limiter = SlidingWindowRateLimiter(3, window_seconds=1.0, time_fn=time_fn, sleep_fn=sleep_fn)
    monkeypatch.setattr("src.services.map_poi_service.AMAP_WEB_SERVICE_RATE_LIMITER", limiter)
    monkeypatch.setattr("src.services.map_poi_service.urlopen", lambda _url, timeout: FakeAmapPlaceResponse())
    service = MapPoiService(map_provider_key="test-amap-key")

    for index in range(4):
        service._fetch_with_limit(f"https://example.invalid/place-text/{index}", "place/text")

    assert sleeps == [1.0]


class FakeAmapPlaceResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return b'{"status":"1","pois":[]}'


class FakeAmapJsonResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def test_basic_map_search_service_semaphore_limits_concurrent_requests(monkeypatch):
    active_requests = 0
    max_active_requests = 0
    lock = Lock()
    limited_requests_started = Event()
    release_requests = Event()

    def fake_urlopen(_url: str, timeout: float):
        nonlocal active_requests, max_active_requests
        with lock:
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            if active_requests == MAP_BASIC_SEARCH_PARALLELISM:
                limited_requests_started.set()
        release_requests.wait(timeout=2)
        with lock:
            active_requests -= 1
        return FakeAmapPlaceResponse()

    monkeypatch.setattr("src.services.map_poi_service.urlopen", fake_urlopen)
    service = MapPoiService(map_provider_key="test-amap-key")

    with ThreadPoolExecutor(max_workers=MAP_BASIC_SEARCH_PARALLELISM + 2) as executor:
        futures = [
            executor.submit(service._fetch_with_limit, f"https://example.invalid/{index}", "place/text")
            for index in range(MAP_BASIC_SEARCH_PARALLELISM + 2)
        ]
        assert limited_requests_started.wait(timeout=2)
        assert max_active_requests == MAP_BASIC_SEARCH_PARALLELISM
        release_requests.set()
        assert [future.result(timeout=2) for future in futures] == [{"status": "1", "pois": []}] * (MAP_BASIC_SEARCH_PARALLELISM + 2)
        assert max_active_requests == MAP_BASIC_SEARCH_PARALLELISM

def test_map_poi_search_returns_amap_results_with_photos(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, params):
        assert params["city"] == "110000"
        assert params["keywords"] == "故宫"
        assert params["citylimit"] == "true"
        assert params["extensions"] == "all"
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B000A8UIN8",
                    "name": "故宫博物院",
                    "type": "风景名胜;风景名胜;世界遗产",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "景山前街4号",
                    "location": "116.397026,39.918058",
                    "photos": [{"title": "故宫博物院", "url": "https://example.com/palace.jpg"}],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        response = client.get("/api/map/pois", params={"city": "北京", "keyword": "故宫", "category": "scenic"})

    assert response.status_code == 200
    body = response.json()
    assert body["providerName"] == "amap-place-search"
    assert body["keyword"] == "故宫"
    assert body["category"] == "scenic"
    assert body["pois"][0]["name"] == "故宫博物院"
    assert body["pois"][0]["longitude"] == 116.397026
    assert body["pois"][0]["photos"][0]["url"] == "https://example.com/palace.jpg"


def test_map_poi_search_returns_clear_error_when_key_missing(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/map/pois", params={"city": "北京", "keyword": "故宫"})

    assert response.status_code == 400
    assert "MAP_PROVIDER_KEY" in response.json()["detail"]


def test_map_poi_museum_category_uses_amap_museum_typecode(monkeypatch):
    captured: dict[str, str] = {}

    def fake_fetch(_service, params):
        captured.update(params)
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B0MUSEUM001",
                    "name": "中国美术馆",
                    "type": "科教文化服务;博物馆;美术馆",
                    "typecode": "140100",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "五四大街1号",
                    "location": "116.409201,39.923681",
                    "photos": [],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)
    result = MapPoiService(map_provider_key="test-amap-key").search(
        city="北京",
        keyword="博物馆或美术馆",
        category="museum",
        limit=8,
    )

    assert captured["types"] == "140100"
    assert result.category == "museum"
    assert result.pois[0].name == "中国美术馆"


def test_map_poi_search_rejects_empty_keyword(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/map/pois", params={"city": "北京", "keyword": ""})

    assert response.status_code == 400
    assert "keyword" in response.json()["detail"]


def test_nearby_map_poi_search_uses_given_coordinates(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, params):
        assert params["location"] == "116.397026,39.918058"
        assert params["keywords"] == "咖啡"
        assert params["radius"] == "1500"
        assert params["city"] == "110000"
        assert params["citylimit"] == "true"
        assert params["sortrule"] == "distance"
        return {
            "status": "1",
            "pois": [
                {
                    "id": "nearby_1",
                    "name": "附近咖啡店",
                    "type": "餐饮服务;咖啡厅",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "附近街道1号",
                    "location": "116.398000,39.919000",
                    "distance": "142",
                    "photos": [],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_around", fake_fetch)

    with TestClient(app) as client:
        response = client.get(
            "/api/map/pois/nearby",
            params={
                "city": "北京",
                "longitude": 116.397026,
                "latitude": 39.918058,
                "keyword": "咖啡",
                "category": "food",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["pois"][0]["name"] == "附近咖啡店"
    assert body["pois"][0]["source"] == "amap-place-search"
    assert body["pois"][0]["distanceMeters"] == 142


def test_map_poi_search_surfaces_amap_failure(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, _params):
        from src.services.map_poi_service import MapPoiProviderError

        raise MapPoiProviderError("INVALID_USER_KEY")

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        response = client.get("/api/map/pois", params={"city": "北京", "keyword": "故宫"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "AMap POI search failed" in detail["message"]
    assert detail["debug"]["classifiedReason"] == "provider_down"
    assert_no_key_in_debug(detail["debug"])


def test_map_poi_resolve_accepts_unique_high_confidence_match(monkeypatch):
    clear_poi_candidates()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, _params):
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B000A8UIN8",
                    "name": "故宫博物院",
                    "type": "风景名胜;风景名胜;世界遗产",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "景山前街4号",
                    "location": "116.397026,39.918058",
                    "photos": [{"title": "故宫博物院", "url": "https://example.com/palace.jpg"}],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        response = client.post(
            "/api/map/pois/resolve",
            json={
                "sessionId": "sess_contract",
                "turnId": "turn_contract",
                "city": "北京",
                "queries": [{"name": "故宫", "category": "scenic"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["resolved"][0]["status"] == "accepted"
    assert body["resolved"][0]["poi"]["id"] == "B000A8UIN8"
    assert body["pending"] == []


def test_map_poi_resolve_records_multiple_candidates_as_pending(monkeypatch):
    clear_poi_candidates()
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, _params):
        return {
            "status": "1",
            "pois": [
                {
                    "id": "POI_1",
                    "name": "胡同餐厅(东城店)",
                    "type": "餐饮服务",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "东城",
                    "location": "116.39,39.91",
                    "photos": [],
                },
                {
                    "id": "POI_2",
                    "name": "胡同餐厅(西城店)",
                    "type": "餐饮服务",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "东城",
                    "location": "116.40,39.92",
                    "photos": [],
                },
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        response = client.post(
            "/api/map/pois/resolve",
            json={
                "sessionId": "sess_contract",
                "city": "北京",
                "queries": [{"name": "胡同餐厅", "category": "food"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["resolved"] == []
    assert body["pending"][0]["reason"] == "material_tradeoff"
    assert body["pending"][0]["candidateRecordId"].startswith("cand_")
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as connection:
        row = connection.execute("SELECT * FROM amap_poi_candidates").fetchone()
    assert row is not None


def test_map_poi_resolve_surfaces_key_missing_and_provider_failure(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "")
    get_settings.cache_clear()
    payload = {"sessionId": "sess_contract", "city": "北京", "queries": [{"name": "故宫"}]}

    with TestClient(app) as client:
        missing_key = client.post("/api/map/pois/resolve", json=payload)

    assert missing_key.status_code == 400
    assert "MAP_PROVIDER_KEY" in missing_key.json()["detail"]

    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, _params):
        from src.services.map_poi_service import MapPoiProviderError

        raise MapPoiProviderError("INVALID_USER_KEY")

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        provider_failure = client.post("/api/map/pois/resolve", json=payload)

    assert provider_failure.status_code == 502
    detail = provider_failure.json()["detail"]
    assert "AMap POI search failed" in detail["message"]
    assert detail["debug"]["classifiedReason"] == "provider_down"
    assert_no_key_in_debug(detail["debug"])


@pytest.mark.parametrize(
    ("raw_error", "classified_reason"),
    [
        ("CUQPS_HAS_EXCEEDED_THE_LIMIT", "qps_limited"),
        ("DAILY_QUERY_OVER_LIMIT", "daily_quota_limited"),
        ("USER_DAILY_QUERY_OVER_LIMIT", "account_quota_limited"),
        ("AMAP_PROVIDER_OVER_QUOTA", "unknown_rate_limited"),
    ],
)
def test_map_poi_resolve_surfaces_quota_failure_with_friendly_message(monkeypatch, raw_error, classified_reason):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_fetch(_service, _params):
        from src.services.map_poi_service import MapPoiProviderError

        raise MapPoiProviderError(raw_error)

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)

    with TestClient(app) as client:
        response = client.post(
            "/api/map/pois/resolve",
            json={"sessionId": "sess_contract", "city": "北京", "queries": [{"name": "故宫博物院"}]},
        )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "provider_rate_limited"
    assert detail["retryAfterSeconds"] > 0
    assert detail["debug"]["classifiedReason"] == classified_reason
    assert detail["debug"]["endpoint"] == "place/text"
    assert detail["debug"]["source"] == "place/text"
    assert detail["debug"]["requestParams"]["keywords"] == "故宫博物院"
    assert detail["debug"]["requestParams"]["city"] == "110000"
    assert "key" not in detail["debug"]["requestParams"]
    assert_no_key_in_debug(detail["debug"])


def test_map_poi_amap_json_error_preserves_raw_debug_fields_and_redacts_key(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_urlopen(_url, timeout):
        return FakeAmapJsonResponse(
            {
                "status": "0",
                "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                "infocode": "10021",
                "errmsg": "查询频率超限",
                "errcode": "AMAP_CUQPS",
            }
        )

    monkeypatch.setattr("src.services.map_poi_service.urlopen", fake_urlopen)
    service = MapPoiService(map_provider_key="test-amap-key")

    with pytest.raises(HTTPException) as error:
        service.search("北京", "故宫博物院", "scenic")

    debug = error.value.detail["debug"]
    assert error.value.detail["code"] == "provider_rate_limited"
    assert debug["endpoint"] == "place/text"
    assert debug["source"] == "place/text"
    assert debug["classifiedReason"] == "qps_limited"
    assert debug["rawStatus"] == "0"
    assert debug["rawInfo"] == "CUQPS_HAS_EXCEEDED_THE_LIMIT"
    assert debug["rawInfocode"] == "10021"
    assert debug["rawErrmsg"] == "查询频率超限"
    assert debug["rawErrcode"] == "AMAP_CUQPS"
    assert isinstance(debug["processId"], int)
    assert debug["cacheHit"] is False
    assert debug["localCooldown"] is False
    assert debug["requestParams"]["keywords"] == "故宫博物院"
    assert debug["requestParams"]["city"] == "110000"
    assert "key" not in debug["requestParams"]
    assert_no_key_in_debug(debug)


def test_map_poi_debug_redacts_token_like_query_parameters(monkeypatch):
    service = MapPoiService(map_provider_key="test-amap-key")

    text = service._safe_debug_text(
        "https://example.invalid/path?key=test-amap-key&access_token=secret-access&refresh_token=secret-refresh&api_key=secret-api"
    )

    assert "test-amap-key" not in text
    assert "secret-access" not in text
    assert "secret-refresh" not in text
    assert "secret-api" not in text
    assert "key=" not in text.lower()
    assert "access_token=" not in text.lower()
    assert "<query-redacted>" in text


def test_map_poi_http_429_debug_is_sanitized(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_urlopen(_url, timeout):
        raise HTTPError(
            "https://restapi.amap.com/v3/place/text?key=test-amap-key&keywords=故宫",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=BytesIO(
                json.dumps(
                    {
                        "status": "0",
                        "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT",
                        "infocode": "10021",
                        "errmsg": "查询频率超限",
                        "errcode": "AMAP_CUQPS",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr("src.services.map_poi_service.urlopen", fake_urlopen)
    service = MapPoiService(map_provider_key="test-amap-key")

    with pytest.raises(HTTPException) as error:
        service.search("北京", "故宫博物院", "scenic")

    detail = error.value.detail
    assert detail["code"] == "provider_rate_limited"
    assert detail["debug"]["endpoint"] == "place/text"
    assert detail["debug"]["source"] == "place/text"
    assert detail["debug"]["classifiedReason"] == "http_429"
    assert detail["debug"]["httpStatusCode"] == 429
    assert detail["debug"]["rawStatus"] == "0"
    assert detail["debug"]["rawInfo"] == "CUQPS_HAS_EXCEEDED_THE_LIMIT"
    assert detail["debug"]["rawInfocode"] == "10021"
    assert detail["debug"]["rawErrmsg"] == "查询频率超限"
    assert detail["debug"]["rawErrcode"] == "AMAP_CUQPS"
    assert isinstance(detail["debug"]["processId"], int)
    assert detail["debug"]["cacheHit"] is False
    assert detail["debug"]["localCooldown"] is False
    assert detail["debug"]["requestParams"]["keywords"] == "故宫博物院"
    assert_no_key_in_debug(detail["debug"])


def test_map_poi_nearby_debug_rounds_location_and_redacts_key(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()

    def fake_urlopen(_url, timeout):
        raise HTTPError(
            "https://restapi.amap.com/v3/place/around?key=test-amap-key&keywords=咖啡&location=116.397026,39.918058",
            429,
            "Too Many Requests",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("src.services.map_poi_service.urlopen", fake_urlopen)
    service = MapPoiService(map_provider_key="test-amap-key")

    with pytest.raises(HTTPException) as error:
        service.search_nearby("北京", 116.397026, 39.918058, "咖啡", "food", radius=1500)

    debug = error.value.detail["debug"]
    assert debug["endpoint"] == "place/around"
    assert debug["source"] == "place/around"
    assert debug["classifiedReason"] == "http_429"
    assert debug["httpStatusCode"] == 429
    assert isinstance(debug["processId"], int)
    assert debug["cacheHit"] is False
    assert debug["localCooldown"] is False
    assert debug["requestParams"] == {
        "keywords": "咖啡",
        "city": "110000",
        "types": "050000",
        "radius": "1500",
        "offset": "12",
        "page": "1",
        "location": "116.3970,39.9181",
    }
    assert_no_key_in_debug(debug)


def test_map_poi_search_uses_short_ttl_cache_before_provider(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    calls = {"count": 0}

    def fake_fetch(_service, _params):
        calls["count"] += 1
        return {
            "status": "1",
            "pois": [
                {
                    "id": "B000PALACE",
                    "name": "故宫博物院",
                    "type": "风景名胜;风景名胜相关;旅游景点",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "景山前街",
                    "location": "116.397026,39.918058",
                    "photos": [],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)
    service = MapPoiService(map_provider_key="test-amap-key")

    first = service.search("北京", "故宫博物院", "scenic")
    second = service.search("北京", "故宫博物院", "scenic")

    assert calls["count"] == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.pois[0].name == "故宫博物院"


def test_map_poi_budget_exceeded_short_circuits_provider_calls(monkeypatch):
    monkeypatch.setenv("MAP_PROVIDER_KEY", "test-amap-key")
    get_settings.cache_clear()
    calls = {"count": 0}

    def fake_fetch(_service, params):
        calls["count"] += 1
        return {
            "status": "1",
            "pois": [
                {
                    "id": f"BUDGET_{calls['count']}",
                    "name": params["keywords"],
                    "type": "风景名胜;风景名胜相关;旅游景点",
                    "cityname": "北京市",
                    "adname": "东城区",
                    "address": "测试地址",
                    "location": "116.397026,39.918058",
                    "photos": [],
                }
            ],
        }

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", fake_fetch)
    service = MapPoiService(map_provider_key="test-amap-key")
    budget = AmapCallBudget(place_text_max=1, total_external_max=1, source="test_budget")

    with amap_call_budget_scope(budget):
        first = service.search("北京", "故宫博物院", "scenic")
        with pytest.raises(HTTPException) as error:
            service.search("北京", "颐和园", "scenic")

    assert first.cache_hit is False
    assert calls["count"] == 1
    assert error.value.status_code == 429
    assert error.value.detail["code"] == "amap_budget_exceeded"
    assert error.value.detail["debug"]["classifiedReason"] == "budget_exceeded"
    assert error.value.detail["debug"]["requestParams"]["keywords"] == "颐和园"
    assert budget.skipped_because_budget == 1
    assert_no_key_in_debug(error.value.detail["debug"])


def test_map_poi_rate_limit_cooldown_short_circuits_followup_provider_calls(monkeypatch):
    calls = {"count": 0}

    def rate_limited_fetch(_service, _params):
        from src.services.map_poi_service import MapPoiProviderError

        calls["count"] += 1
        raise MapPoiProviderError("CUQPS_HAS_EXCEEDED_THE_LIMIT")

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", rate_limited_fetch)
    service = MapPoiService(map_provider_key="test-amap-key")

    with pytest.raises(HTTPException) as first:
        service.search("北京", "故宫博物院", "scenic")
    with pytest.raises(HTTPException) as second:
        service.search("北京", "颐和园", "scenic")

    assert calls["count"] == 1
    assert first.value.detail["code"] == "provider_rate_limited"
    assert second.value.detail["code"] == "provider_rate_limited"
    assert first.value.detail["debug"]["classifiedReason"] == "qps_limited"
    assert second.value.detail["debug"]["classifiedReason"] == "local_cooldown"
    assert second.value.detail["debug"]["localCooldown"] is True
    assert isinstance(second.value.detail["debug"]["processId"], int)
    assert second.value.detail["debug"]["cacheHit"] is False
    assert second.value.detail["debug"]["requestParams"]["keywords"] == "颐和园"
    assert_no_key_in_debug(first.value.detail["debug"])
    assert_no_key_in_debug(second.value.detail["debug"])


def test_map_poi_generic_retry_later_error_does_not_start_rate_limit_cooldown(monkeypatch):
    from src.services.map_poi_service import MapPoiProviderError

    calls = {"count": 0}

    def transient_fetch(_service, _params):
        calls["count"] += 1
        if calls["count"] == 1:
            raise MapPoiProviderError("SERVICE_BUSY，请稍后重试")
        return {"status": "1", "pois": []}

    monkeypatch.setattr("src.services.map_poi_service.MapPoiService._fetch_amap_place", transient_fetch)
    service = MapPoiService(map_provider_key="test-amap-key")

    with pytest.raises(HTTPException) as first:
        service.search("北京", "故宫博物院", "scenic")
    second = service.search("北京", "颐和园", "scenic")

    assert calls["count"] == 2
    assert first.value.detail["code"] == "provider_down"
    assert first.value.detail["debug"]["classifiedReason"] == "provider_down"
    assert second.pois == []


def assert_no_key_in_debug(debug: dict) -> None:
    serialized = json.dumps(debug, ensure_ascii=False)
    assert "test-amap-key" not in serialized
    assert "key=" not in serialized.lower()
