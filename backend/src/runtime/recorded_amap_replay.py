from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import src.services.map_poi_service as map_poi_module
import src.services.route_service as route_module
from src.services.map_poi_service import MapPoiService, clear_map_poi_runtime_state
from src.services.route_service import RouteService


class RecordedAmapReplayError(RuntimeError):
    """Raised when a no-network replay receives a request absent from its recording."""


class _RecordedResponse:
    def __init__(self, payload: dict[str, Any]):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_RecordedResponse":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class RecordedAmapTransport:
    """Exact request replay for sanitized raw AMap responses.

    Only the HTTP transport is replaced. MapPoiService and RouteService still
    perform their normal request construction, budget accounting, JSON parsing,
    ranking, caching, route parsing, and validation.
    """

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path).resolve()
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        if payload.get("schemaVersion") != "trip-recorded-amap-v1":
            raise RecordedAmapReplayError("unsupported recorded AMap fixture schema")
        if payload.get("recordingType") != "recorded/non-live":
            raise RecordedAmapReplayError("recorded AMap fixture must be marked recorded/non-live")
        recorded_at = str(payload.get("recordedAt") or "").strip()
        try:
            datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RecordedAmapReplayError("recorded AMap fixture has invalid recordedAt") from error
        self._records = list(payload.get("responses") or [])
        self._recording_metadata = {
            "recordingType": payload["recordingType"],
            "recordedAt": recorded_at,
            "fixture": self.fixture_path.name,
        }
        for record in self._records:
            response = record.get("response") if isinstance(record, dict) else None
            response_hash = str(record.get("responseSha256") or "") if isinstance(record, dict) else ""
            if not isinstance(response, dict) or len(response_hash) != 64:
                raise RecordedAmapReplayError("recorded AMap fixture response hash is missing")
            actual_hash = sha256(
                json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if actual_hash != response_hash:
                raise RecordedAmapReplayError("recorded AMap fixture response hash mismatch")
        self._coordinates_by_amap_id = self._recorded_coordinates_by_amap_id(self._records)
        for record in self._records:
            request = record.get("request") if isinstance(record, dict) else None
            endpoint = str(request.get("endpoint") or "") if isinstance(request, dict) else ""
            if endpoint.startswith("/v3/direction/"):
                self._validate_route_request_pair(record, endpoint)
        self.requests: list[dict[str, Any]] = []

    def urlopen(self, request: Any, timeout: float | None = None) -> _RecordedResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        parsed = urlparse(url)
        try:
            allowed_target = (
                parsed.scheme == "https"
                and parsed.hostname == "restapi.amap.com"
                and parsed.username is None
                and parsed.password is None
                and parsed.port is None
                and not parsed.fragment
            )
        except ValueError:
            allowed_target = False
        if not allowed_target:
            raise RecordedAmapReplayError("recorded AMap request target is not allowed")
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        params.pop("key", None)
        actual = {"endpoint": parsed.path, "params": params}
        self.requests.append({**actual, "timeout": timeout})
        for record in self._records:
            if self._matches(record.get("request") or {}, actual):
                response = record.get("response")
                if not isinstance(response, dict):
                    raise RecordedAmapReplayError("recorded AMap response must be an object")
                replayed = deepcopy(response)
                replay_metadata = {
                    **self._recording_metadata,
                    "responseSha256": record["responseSha256"],
                }
                if isinstance(record.get("requestPair"), dict):
                    replay_metadata["requestPair"] = deepcopy(record["requestPair"])
                replayed["_tripRecordedReplay"] = {
                    **replay_metadata,
                }
                return _RecordedResponse(replayed)
        raise RecordedAmapReplayError("recorded AMap request not found")

    @staticmethod
    def _matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
        if str(expected.get("endpoint") or "") != actual["endpoint"]:
            return False
        expected_params = expected.get("params") or {}
        if not isinstance(expected_params, dict):
            return False
        normalized_expected = {str(key): str(value) for key, value in expected_params.items()}
        if str(expected.get("endpoint") or "").startswith("/v3/direction/"):
            # Route evidence is consumed as a binding proof.  Unlike the older
            # compact Place recordings, it must not be replayed for a request
            # whose parameter surface has changed or grown.
            return actual["params"] == normalized_expected
        return all(actual["params"].get(key) == value for key, value in normalized_expected.items())

    def _validate_route_request_pair(self, record: dict[str, Any], endpoint: str) -> None:
        request = record.get("request")
        request_pair = record.get("requestPair")
        if (
            not isinstance(request, dict)
            or not isinstance(request_pair, dict)
            or set(request_pair) != {"fromAmapId", "toAmapId", "mode"}
            or not all(isinstance(request_pair.get(field), str) and request_pair[field] for field in request_pair)
            or request_pair["mode"] != self._mode_for_direction_endpoint(endpoint)
        ):
            raise RecordedAmapReplayError("recorded AMap route requestPair is missing or invalid")
        params = request.get("params")
        if not isinstance(params, dict):
            raise RecordedAmapReplayError("recorded AMap route requestPair is missing or invalid")
        expected_origin = self._coordinates_by_amap_id.get(request_pair["fromAmapId"])
        expected_destination = self._coordinates_by_amap_id.get(request_pair["toAmapId"])
        if (
            expected_origin is None
            or expected_destination is None
            or self._canonical_coordinate(params.get("origin")) != expected_origin
            or self._canonical_coordinate(params.get("destination")) != expected_destination
        ):
            raise RecordedAmapReplayError("recorded AMap route requestPair is missing or invalid")

    @classmethod
    def _recorded_coordinates_by_amap_id(cls, records: list[dict[str, Any]]) -> dict[str, str]:
        coordinates: dict[str, str] = {}
        for record in records:
            response = record.get("response") if isinstance(record, dict) else None
            pois = response.get("pois") if isinstance(response, dict) else None
            if not isinstance(pois, list):
                continue
            for poi in pois:
                amap_id = poi.get("id") if isinstance(poi, dict) else None
                coordinate = cls._canonical_coordinate(poi.get("location") if isinstance(poi, dict) else None)
                if not isinstance(amap_id, str) or not amap_id or coordinate is None:
                    continue
                existing = coordinates.get(amap_id)
                if existing is not None and existing != coordinate:
                    raise RecordedAmapReplayError("recorded AMap POI coordinate lineage is ambiguous")
                coordinates[amap_id] = coordinate
        return coordinates

    @staticmethod
    def _canonical_coordinate(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        parts = value.split(",")
        if len(parts) != 2:
            return None
        try:
            longitude = float(parts[0].strip())
            latitude = float(parts[1].strip())
        except ValueError:
            return None
        if not (-180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0):
            return None
        return f"{longitude:.6f},{latitude:.6f}"

    @staticmethod
    def _mode_for_direction_endpoint(endpoint: str) -> str:
        if endpoint == "/v3/direction/transit/integrated":
            return "transit"
        if endpoint == "/v3/direction/walking":
            return "walking"
        if endpoint == "/v3/direction/driving":
            return "driving"
        raise RecordedAmapReplayError("recorded AMap route endpoint is unsupported")


@contextmanager
def recorded_amap_replay_scope(fixture_path: str | Path) -> Iterator[RecordedAmapTransport]:
    transport = RecordedAmapTransport(fixture_path)
    original_map_init = MapPoiService.__init__
    original_route_init = RouteService.__init__
    original_map_urlopen = map_poi_module.urlopen
    original_route_urlopen = route_module.urlopen

    def replay_map_init(service, map_provider_key=None, timeout_seconds=5.0):
        original_map_init(service, map_provider_key="recorded-amap-replay", timeout_seconds=timeout_seconds)

    def replay_route_init(service, map_provider_key=None, timeout_seconds=5.0):
        original_route_init(service, map_provider_key="recorded-amap-replay", timeout_seconds=timeout_seconds)

    clear_map_poi_runtime_state()
    RouteService.clear_cache()
    MapPoiService.__init__ = replay_map_init
    RouteService.__init__ = replay_route_init
    map_poi_module.urlopen = transport.urlopen
    route_module.urlopen = transport.urlopen
    try:
        yield transport
    finally:
        MapPoiService.__init__ = original_map_init
        RouteService.__init__ = original_route_init
        map_poi_module.urlopen = original_map_urlopen
        route_module.urlopen = original_route_urlopen
        clear_map_poi_runtime_state()
        RouteService.clear_cache()
