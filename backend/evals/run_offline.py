from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator, Optional, Union
from urllib.parse import parse_qsl, urlparse


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from metrics import count_failed_tool_calls, count_tool_calls, count_verifier_failures, summarize_results  # type: ignore  # noqa: E402
    from replay import replay_agent_response, replay_trace  # type: ignore  # noqa: E402
except ModuleNotFoundError:
    from backend.evals.metrics import (  # type: ignore  # noqa: E402
        count_failed_tool_calls,
        count_tool_calls,
        count_verifier_failures,
        summarize_results,
    )
    from backend.evals.replay import replay_agent_response, replay_trace  # type: ignore  # noqa: E402


CASES_DIR = Path(__file__).resolve().parent / "cases"
REQUIRED_SEARCH_PROFILE_CASE_ID = "creative_portfolio_search_profile_recorded"
_OFFLINE_ENV_KEYS = (
    "DATABASE_URL",
    "PROVIDER_MODE",
    "AGENT_INITIAL_PLANNING_MODE",
    "AGENT_INTENT_ROUTING_MODE",
    "AGENT_CREATIVE_PORTFOLIO_ENABLED",
    "AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT",
    "DEFAULT_USER_ID",
    "MAP_PROVIDER_KEY",
    "DEEPSEEK_API_KEY",
    "AMAP_WEB_SERVICE_KEY",
    "WEB_SEARCH_API_KEY",
    "SEARCH_PROVIDER_KEY",
    "TICKET_PROVIDER_KEY",
    "WEATHER_PROVIDER_KEY",
)


def _canonical_response_sha256(response: dict[str, Any]) -> str:
    """Return the fixture's stable response fingerprint without inventing evidence."""

    canonical = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_RECORDED_AMAP_ROUTE_PAIR: ContextVar[Optional[dict[str, str]]] = ContextVar(
    "offline_recorded_amap_route_pair",
    default=None,
)
_RECORDED_AMAP_RESPONSE_HASH_ALGORITHM = "sha256-canonical-json-v1"
_PHASE1_PROFILE_CONTEXT: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "offline_phase1_profile_context",
    default=None,
)
_PHASE1_REQUEST_CONTEXT: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "offline_phase1_request_context",
    default=None,
)
_PHASE1_LOW_LEVEL_FETCH_CONTEXT: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "offline_phase1_low_level_fetch_context",
    default=None,
)


@contextmanager
def _offline_external_network_sentinel(runtime_trace: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Fail closed on socket-level network attempts during an offline case."""

    attempts = runtime_trace.setdefault("realExternalCalls", [])
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_send = socket.socket.send
    original_sendall = socket.socket.sendall
    original_sendto = socket.socket.sendto

    def blocked(operation: str):
        def reject(*_args, **_kwargs):
            attempts.append({"provider": "network", "operation": operation})
            raise AssertionError(f"offline eval blocked real external network operation: {operation}")

        return reject

    def guarded_send(original: Any, operation: str):
        def send(sock: socket.socket, *args: Any, **kwargs: Any):
            if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", None):
                return original(sock, *args, **kwargs)
            try:
                peer = sock.getpeername()
            except OSError:
                peer = None
            host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if host == "::1" or host.startswith("127."):
                return original(sock, *args, **kwargs)
            attempts.append({"provider": "network", "operation": operation})
            raise AssertionError(f"offline eval blocked real external network operation: {operation}")

        return send

    socket.create_connection = blocked("socket.create_connection")
    socket.getaddrinfo = blocked("socket.getaddrinfo")
    socket.socket.connect = blocked("socket.connect")
    socket.socket.connect_ex = blocked("socket.connect_ex")
    socket.socket.send = guarded_send(original_send, "socket.send")
    socket.socket.sendall = guarded_send(original_sendall, "socket.sendall")
    socket.socket.sendto = blocked("socket.sendto")
    state = {
        "active": (
            socket.create_connection is not original_create_connection
            and socket.getaddrinfo is not original_getaddrinfo
            and socket.socket.connect is not original_connect
            and socket.socket.connect_ex is not original_connect_ex
            and socket.socket.send is not original_send
            and socket.socket.sendall is not original_sendall
            and socket.socket.sendto is not original_sendto
        ),
        "attempts": attempts,
    }
    try:
        yield state
    finally:
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.socket.send = original_send
        socket.socket.sendall = original_sendall
        socket.socket.sendto = original_sendto


class _OfflineRecordedAmapReplayError(RuntimeError):
    """A local offline-only replay miss; it never falls through to the network."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(f"recorded_amap_replay:{self.code}")


class _OfflineRecordedAmapResponse:
    def __init__(self, payload: dict[str, Any]):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_OfflineRecordedAmapResponse":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _OfflineStrictRecordedAmapTransport:
    """One-shot, exact local URL transport for a ``trip-recorded-amap-v1`` fixture.

    This intentionally lives in the eval runner rather than runtime.  It leaves
    production request construction/parsing intact, but makes a recorded case
    prove every non-secret endpoint/parameter request and every route identity
    pair.  A record is consumed exactly once, so duplicate external work cannot
    silently reuse a plausible-looking response.
    """

    def __init__(
        self,
        fixture_value: Any,
        *,
        case_id: str = "",
        expected_record_keys: Any = None,
    ):
        self.requests: list[dict[str, Any]] = []
        self.matched: list[dict[str, Any]] = []
        self.unmatched: list[dict[str, Any]] = []
        self._records: list[dict[str, Any]] = []
        self._load_failure: Optional[str] = None
        self.case_id = str(case_id or "").strip()
        try:
            self._expected_record_key_counts = self._normalize_expected_record_keys(expected_record_keys)
        except _OfflineRecordedAmapReplayError as error:
            self._expected_record_key_counts = Counter()
            self._load_failure = error.code
        self._selector_failure: Optional[str] = None
        self._selector: dict[str, Any] = {
            "caseId": self.case_id or None,
            "usesFixtureCaseIds": False,
            "expectedRecordKeyCount": sum(self._expected_record_key_counts.values()),
        }
        self.fixture = self._safe_fixture_label(fixture_value)
        self.metadata: dict[str, Any] = {"fixture": self.fixture}
        self._load(fixture_value)

    @staticmethod
    def _safe_fixture_label(fixture_value: Any) -> str:
        value = str(fixture_value or "").strip().replace("\\", "/")
        return value or "<missing>"

    @staticmethod
    def _is_route_endpoint(endpoint: str) -> bool:
        return endpoint.startswith("/v3/direction/") or endpoint.startswith("/v4/direction/")

    @staticmethod
    def _normalize_fixture_params(raw_params: Any) -> dict[str, tuple[str, ...]]:
        if not isinstance(raw_params, dict):
            raise _OfflineRecordedAmapReplayError("fixture_request_params_invalid")
        normalized: dict[str, tuple[str, ...]] = {}
        for raw_key, raw_value in raw_params.items():
            if not isinstance(raw_key, str) or not raw_key:
                raise _OfflineRecordedAmapReplayError("fixture_request_parameter_name_invalid")
            if raw_key == "key":
                continue
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            if not values or any(not isinstance(value, str) for value in values):
                raise _OfflineRecordedAmapReplayError("fixture_request_parameter_value_invalid")
            normalized[raw_key] = tuple(sorted(values))
        return normalized

    @staticmethod
    def _normalize_actual_params(query: str) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key == "key":
                continue
            grouped.setdefault(key, []).append(value)
        return {key: tuple(sorted(values)) for key, values in grouped.items()}

    @staticmethod
    def _render_params(params: dict[str, tuple[str, ...]]) -> dict[str, Any]:
        return {key: values[0] if len(values) == 1 else list(values) for key, values in sorted(params.items())}

    @staticmethod
    def _normalize_route_pair(raw_pair: Any) -> dict[str, str]:
        if not isinstance(raw_pair, dict):
            raise _OfflineRecordedAmapReplayError("fixture_route_request_pair_missing")
        pair = {
            "fromAmapId": raw_pair.get("fromAmapId"),
            "toAmapId": raw_pair.get("toAmapId"),
            "mode": raw_pair.get("mode"),
        }
        if any(not isinstance(value, str) or not value for value in pair.values()):
            raise _OfflineRecordedAmapReplayError("fixture_route_request_pair_invalid")
        return {key: str(value) for key, value in pair.items()}

    @classmethod
    def _record_key(
        cls,
        endpoint: str,
        params: dict[str, tuple[str, ...]],
        request_pair: Optional[dict[str, str]] = None,
    ) -> str:
        payload: dict[str, Any] = {
            "endpoint": endpoint,
            "params": {key: list(values) for key, values in sorted(params.items())},
        }
        if request_pair is not None:
            payload["requestPair"] = dict(request_pair)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _normalize_expected_record_keys(cls, raw_records: Any) -> Counter[str]:
        if raw_records is None:
            return Counter()
        if not isinstance(raw_records, list) or not raw_records:
            raise _OfflineRecordedAmapReplayError("expected_record_selector_invalid")
        keys: Counter[str] = Counter()
        for raw_record in raw_records:
            if not isinstance(raw_record, dict):
                raise _OfflineRecordedAmapReplayError("expected_record_selector_invalid")
            endpoint = raw_record.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint.startswith("/"):
                raise _OfflineRecordedAmapReplayError("expected_record_selector_invalid")
            params = cls._normalize_fixture_params(raw_record.get("params"))
            request_pair = raw_record.get("requestPair")
            normalized_pair = cls._normalize_route_pair(request_pair) if request_pair is not None else None
            keys[cls._record_key(endpoint, params, normalized_pair)] += 1
        return keys

    @staticmethod
    def _validate_recorded_at(value: Any) -> str:
        recorded_at = str(value or "").strip()
        if not recorded_at:
            raise _OfflineRecordedAmapReplayError("fixture_recorded_at_missing")
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise _OfflineRecordedAmapReplayError("fixture_recorded_at_invalid") from error
        if parsed.tzinfo is None:
            raise _OfflineRecordedAmapReplayError("fixture_recorded_at_timezone_missing")
        return recorded_at

    def _load(self, fixture_value: Any) -> None:
        if self._load_failure:
            return
        value = str(fixture_value or "").strip()
        if not value:
            self._load_failure = "fixture_missing"
            return
        try:
            fixture_path = (PROJECT_ROOT / value).resolve()
            fixture_path.relative_to(PROJECT_ROOT.resolve())
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            self._load_failure = "fixture_unreadable"
            return
        try:
            if not isinstance(payload, dict):
                raise _OfflineRecordedAmapReplayError("fixture_payload_invalid")
            if payload.get("schemaVersion") != "trip-recorded-amap-v1":
                raise _OfflineRecordedAmapReplayError("fixture_schema_invalid")
            if payload.get("recordingType") != "recorded/non-live":
                raise _OfflineRecordedAmapReplayError("fixture_recording_type_invalid")
            if payload.get("responseHashAlgorithm") != _RECORDED_AMAP_RESPONSE_HASH_ALGORITHM:
                raise _OfflineRecordedAmapReplayError("fixture_response_hash_algorithm_invalid")
            recorded_at = self._validate_recorded_at(payload.get("recordedAt"))
            recorded_provider = str(payload.get("recordedProvider") or "").strip()
            if not recorded_provider:
                raise _OfflineRecordedAmapReplayError("fixture_recorded_provider_missing")
            raw_records = payload.get("responses")
            if not isinstance(raw_records, list) or not raw_records:
                raise _OfflineRecordedAmapReplayError("fixture_responses_invalid")
            self.metadata = {
                "fixture": fixture_path.relative_to(PROJECT_ROOT).as_posix(),
                "recordingType": "recorded/non-live",
                "recordedAt": recorded_at,
                "recordedProvider": recorded_provider,
            }
            self.fixture = str(self.metadata["fixture"])
            for index, raw_record in enumerate(raw_records):
                if not isinstance(raw_record, dict):
                    raise _OfflineRecordedAmapReplayError("fixture_response_record_invalid")
                raw_request = raw_record.get("request")
                if not isinstance(raw_request, dict):
                    raise _OfflineRecordedAmapReplayError("fixture_request_missing")
                endpoint = raw_request.get("endpoint")
                if not isinstance(endpoint, str) or not endpoint.startswith("/"):
                    raise _OfflineRecordedAmapReplayError("fixture_request_endpoint_invalid")
                response = raw_record.get("response")
                response_hash = raw_record.get("responseSha256")
                if not isinstance(response, dict):
                    raise _OfflineRecordedAmapReplayError("fixture_response_payload_invalid")
                if not isinstance(response_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", response_hash):
                    raise _OfflineRecordedAmapReplayError("fixture_response_hash_invalid")
                if _canonical_response_sha256(response).upper() != response_hash.upper():
                    raise _OfflineRecordedAmapReplayError("fixture_response_hash_mismatch")
                record: dict[str, Any] = {
                    "index": index,
                    "endpoint": endpoint,
                    "params": self._normalize_fixture_params(raw_request.get("params")),
                    "response": deepcopy(response),
                    "responseSha256": response_hash,
                    "used": False,
                }
                raw_case_id = raw_record.get("caseId")
                if raw_case_id is not None:
                    if not isinstance(raw_case_id, str) or not raw_case_id.strip():
                        raise _OfflineRecordedAmapReplayError("fixture_record_case_id_invalid")
                    record["caseId"] = raw_case_id.strip()
                if self._is_route_endpoint(endpoint):
                    record["requestPair"] = self._normalize_route_pair(raw_record.get("requestPair"))
                record["recordKey"] = self._record_key(
                    endpoint,
                    record["params"],
                    record.get("requestPair"),
                )
                self._records.append(record)
            self._apply_record_selector()
        except _OfflineRecordedAmapReplayError as error:
            self._records = []
            self._load_failure = error.code

    def _apply_record_selector(self) -> None:
        fixture_has_case_ids = any(record.get("caseId") for record in self._records)
        self._selector["usesFixtureCaseIds"] = fixture_has_case_ids
        eligible_records = list(self._records)
        if fixture_has_case_ids:
            eligible_records = [
                record for record in eligible_records if bool(self.case_id) and record.get("caseId") == self.case_id
            ]
        for record in self._records:
            record["allowed"] = False
        if self._expected_record_key_counts:
            selected_counts: Counter[str] = Counter()
            for record in eligible_records:
                key = record["recordKey"]
                if selected_counts[key] >= self._expected_record_key_counts[key]:
                    continue
                record["allowed"] = True
                selected_counts[key] += 1
            if selected_counts != self._expected_record_key_counts:
                self._selector_failure = "expected_record_selector_unmatched"
        else:
            for record in eligible_records:
                record["allowed"] = True
        self._selector["allowedRecordCount"] = sum(1 for record in self._records if record.get("allowed"))
        self._selector["fixtureRecordCount"] = len(self._records)
        if fixture_has_case_ids and not eligible_records:
            self._selector_failure = "case_record_selector_unmatched"
        if self._selector_failure:
            self._selector["failure"] = self._selector_failure

    def _record_unmatched(self, request: dict[str, Any], reason: str) -> None:
        self.unmatched.append({**request, "reason": str(reason)})

    def urlopen(self, request: Any, timeout: float | None = None) -> _OfflineRecordedAmapResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        parsed = urlparse(url)
        actual: dict[str, Any] = {
            "endpoint": parsed.path,
            "params": self._render_params(self._normalize_actual_params(parsed.query)),
            "timeoutSeconds": timeout,
        }
        route_pair = _RECORDED_AMAP_ROUTE_PAIR.get()
        if route_pair is not None:
            actual["requestPair"] = dict(route_pair)
        self.requests.append(actual)
        if self._load_failure:
            self._record_unmatched(actual, self._load_failure)
            raise _OfflineRecordedAmapReplayError(self._load_failure)

        actual_params = self._normalize_actual_params(parsed.query)
        candidates = [
            record
            for record in self._records
            if not record["used"]
            and record.get("allowed")
            and record["endpoint"] == parsed.path
            and record["params"] == actual_params
        ]
        if not candidates:
            self._record_unmatched(actual, "request_not_found")
            raise _OfflineRecordedAmapReplayError("request_not_found")
        if self._is_route_endpoint(parsed.path):
            if route_pair is None:
                self._record_unmatched(actual, "route_pair_missing")
                raise _OfflineRecordedAmapReplayError("route_pair_missing")
            candidates = [record for record in candidates if record.get("requestPair") == route_pair]
            if not candidates:
                self._record_unmatched(actual, "route_pair_not_found")
                raise _OfflineRecordedAmapReplayError("route_pair_not_found")

        record = candidates[0]
        record["used"] = True
        matched = {
            "recordIndex": record["index"],
            "endpoint": parsed.path,
            "params": actual["params"],
            "responseSha256": record["responseSha256"],
        }
        if route_pair is not None:
            matched["requestPair"] = dict(route_pair)
        self.matched.append(matched)
        replay_metadata = {
            **self.metadata,
            "responseSha256": record["responseSha256"],
        }
        if record.get("requestPair") is not None:
            replay_metadata["requestPair"] = dict(record["requestPair"])
        payload = deepcopy(record["response"])
        payload["_tripRecordedReplay"] = replay_metadata
        return _OfflineRecordedAmapResponse(payload)

    def _unused_records(self) -> list[dict[str, Any]]:
        unused: list[dict[str, Any]] = []
        for record in self._records:
            if record["used"] or not record.get("allowed"):
                continue
            item = {
                "recordIndex": record["index"],
                "endpoint": record["endpoint"],
                "params": self._render_params(record["params"]),
                "reason": "fixture_record_unused",
            }
            if record.get("requestPair") is not None:
                item["requestPair"] = dict(record["requestPair"])
            unused.append(item)
        return unused

    def failure_events(self) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        if self._load_failure:
            failures.append({"fixture": self.fixture, "reason": self._load_failure})
        if self._selector_failure:
            failures.append({"fixture": self.fixture, "reason": self._selector_failure})
        failures.extend(deepcopy(self.unmatched))
        failures.extend(self._unused_records())
        return failures

    def trace(self) -> dict[str, Any]:
        failures = self.failure_events()
        return {
            "enabled": True,
            "mode": "strict_recorded_amap_replay",
            "fixture": self.fixture,
            "metadata": dict(self.metadata),
            "selector": dict(self._selector),
            "requests": deepcopy(self.requests),
            "matched": deepcopy(self.matched),
            "unmatched": deepcopy(self.unmatched),
            "unusedFixtureRecords": self._unused_records(),
            "failureCount": len(failures),
            "failures": failures,
        }


@contextmanager
def _offline_strict_recorded_amap_replay_scope(
    fixture_value: Any,
    *,
    case_id: str = "",
    expected_record_keys: Any = None,
    production_map_search: Any,
    production_map_fetch_place: Any,
    production_map_fetch_around: Any,
    production_build_routes: Any,
) -> Iterator[_OfflineStrictRecordedAmapTransport]:
    """Patch only eval-local transports while keeping production services intact."""

    import src.services.map_poi_service as map_poi_service_module
    import src.services.route_service as route_service_module
    from src.models.route_option import normalize_route_mode
    from src.services.map_poi_service import MapPoiService, clear_map_poi_runtime_state
    from src.services.route_service import RouteService

    transport = _OfflineStrictRecordedAmapTransport(
        fixture_value,
        case_id=case_id,
        expected_record_keys=expected_record_keys,
    )
    original_map_init = MapPoiService.__init__
    original_route_init = RouteService.__init__
    original_route_fetch = RouteService._fetch_amap_route
    original_map_search = MapPoiService.search
    original_map_fetch_place = MapPoiService._fetch_amap_place
    original_map_fetch_around = MapPoiService._fetch_amap_around
    original_build_routes = RouteService.build_routes
    original_map_urlopen = map_poi_service_module.urlopen
    original_route_urlopen = route_service_module.urlopen

    def replay_map_init(service, map_provider_key=None, timeout_seconds=5.0):
        original_map_init(service, map_provider_key="offline-recorded-amap-replay", timeout_seconds=timeout_seconds)

    def replay_route_init(service, map_provider_key=None, timeout_seconds=5.0):
        original_route_init(service, map_provider_key="offline-recorded-amap-replay", timeout_seconds=timeout_seconds)

    def replay_route_fetch(service, from_poi, to_poi, transport_mode):
        pair = {
            "fromAmapId": str(getattr(from_poi, "amap_id", "") or ""),
            "toAmapId": str(getattr(to_poi, "amap_id", "") or ""),
            "mode": normalize_route_mode(transport_mode),
        }
        token = _RECORDED_AMAP_ROUTE_PAIR.set(pair)
        try:
            return original_route_fetch(service, from_poi, to_poi, transport_mode)
        finally:
            _RECORDED_AMAP_ROUTE_PAIR.reset(token)

    def strict_build_routes(service, *args, **kwargs):
        """Run the real matrix builder, then close its documented error swallow."""

        unmatched_before = len(transport.unmatched)
        routes = production_build_routes(service, *args, **kwargs)
        swallowed = transport.unmatched[unmatched_before:]
        if not swallowed:
            return routes
        codes = sorted({str(item.get("reason") or "recorded_amap_replay_failed") for item in swallowed})
        service.warnings.append("recorded_amap_replay_failed:" + ",".join(codes))
        return []

    clear_map_poi_runtime_state()
    RouteService.clear_cache()
    MapPoiService.__init__ = replay_map_init
    RouteService.__init__ = replay_route_init
    # The legacy runner has already installed mock wrappers globally.  Swap
    # those out inside this scope so this branch uses the original production
    # search/fetch/matrix methods rather than merely delegating through them.
    MapPoiService.search = production_map_search
    MapPoiService._fetch_amap_place = production_map_fetch_place
    MapPoiService._fetch_amap_around = production_map_fetch_around
    RouteService._fetch_amap_route = replay_route_fetch
    RouteService.build_routes = strict_build_routes
    map_poi_service_module.urlopen = transport.urlopen
    route_service_module.urlopen = transport.urlopen
    try:
        yield transport
    finally:
        MapPoiService.__init__ = original_map_init
        RouteService.__init__ = original_route_init
        MapPoiService.search = original_map_search
        MapPoiService._fetch_amap_place = original_map_fetch_place
        MapPoiService._fetch_amap_around = original_map_fetch_around
        RouteService._fetch_amap_route = original_route_fetch
        RouteService.build_routes = original_build_routes
        map_poi_service_module.urlopen = original_map_urlopen
        route_service_module.urlopen = original_route_urlopen
        clear_map_poi_runtime_state()
        RouteService.clear_cache()


def _strict_replay_result_failure(result: dict[str, Any], transport: _OfflineStrictRecordedAmapTransport) -> None:
    """Make a replay mismatch a case result, never a runner-wide exception."""

    trace = transport.trace()
    result["recordedAmapReplay"] = trace
    if not trace["failures"]:
        return
    result["passed"] = False
    reasons = sorted({str(item.get("reason") or "recorded_amap_replay_failed") for item in trace["failures"]})
    replay_reason = "recorded AMap replay failed: " + ", ".join(reasons)
    prior_reason = str(result.get("failureReason") or "")
    result["failureReason"] = f"{prior_reason}; {replay_reason}" if prior_reason else replay_reason


def _recorded_route_fixture(case: dict[str, Any]) -> dict[str, Any]:
    """Load an explicitly non-live AMap recording for provider-route replay.

    A boolean ``recordProviderRoutes`` used to make the harness synthesize a
    route for every pair.  That is not provider evidence.  A case now has to
    name a recorded fixture; absent, malformed, or tampered recordings produce
    no route rather than a plausible-looking success.
    """

    fixture_value = str(case.get("recordedProviderRouteFixture") or "").strip()
    if not fixture_value:
        return {"records": [], "failure": "recorded_route_fixture_missing"}
    fixture_path = (PROJECT_ROOT / fixture_value).resolve()
    try:
        fixture_path.relative_to(PROJECT_ROOT.resolve())
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"records": [], "failure": "recorded_route_fixture_unreadable"}
    if (
        payload.get("schemaVersion") != "trip-recorded-amap-v1"
        or payload.get("recordingType") != "recorded/non-live"
        or not str(payload.get("recordedAt") or "").strip()
    ):
        return {"records": [], "failure": "recorded_route_fixture_metadata_invalid"}
    records = [
        item
        for item in (payload.get("responses") or [])
        if isinstance(item, dict) and isinstance(item.get("requestPair"), dict)
    ]
    return {
        "records": records,
        "fixture": fixture_path.relative_to(PROJECT_ROOT).as_posix(),
        "recordedAt": str(payload["recordedAt"]),
        "recordingType": str(payload["recordingType"]),
        "failure": None,
    }


def _recorded_route_response(
    records: list[dict[str, Any]],
    *,
    from_amap_id: str,
    to_amap_id: str,
    mode: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return one hash-bound raw response for the exact provider request pair."""

    for record in records:
        pair = record.get("requestPair") or {}
        if (
            str(pair.get("fromAmapId") or "") != from_amap_id
            or str(pair.get("toAmapId") or "") != to_amap_id
            or str(pair.get("mode") or "") != mode
        ):
            continue
        response = record.get("response")
        expected_hash = str(record.get("responseSha256") or "")
        if not isinstance(response, dict) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None:
            return None, "recorded_route_response_metadata_invalid"
        if _canonical_response_sha256(response).upper() != expected_hash.upper():
            return None, "recorded_route_response_hash_mismatch"
        return response, None
    return None, "recorded_route_pair_not_found"


_CANONICAL_AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")


def _route_discovery_entry(
    *,
    case_id: str,
    plan_id: str,
    from_segment_id: str,
    to_segment_id: str,
    from_poi: Any,
    to_poi: Any,
    mode: str,
    route_budget: Any,
    uses_offline_mock_amap: bool,
    repair_scope_certificate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Record an attempted route request without turning it into route evidence.

    The offline harness normally observes this request only after a hash-bound
    replay record is available.  Discovery deliberately records the same
    request *before* fixture lookup, so a capture allowlist can be reviewed
    without manufacturing a RouteOption or calling a network provider.
    """

    from src.services.amap_call_budget import AmapCallBudget

    from_amap_id = str(getattr(from_poi, "amap_id", "") or "").strip().upper()
    to_amap_id = str(getattr(to_poi, "amap_id", "") or "").strip().upper()
    from_source = str(getattr(from_poi, "source", "") or "")
    to_source = str(getattr(to_poi, "source", "") or "")
    normalized_mode = str(mode or "").strip()
    blocked_reasons: list[str] = []
    if from_source != "amap-place-search" or to_source != "amap-place-search":
        blocked_reasons.append("poi_source_not_amap_place_search")
    if not _CANONICAL_AMAP_ID_RE.fullmatch(from_amap_id) or not _CANONICAL_AMAP_ID_RE.fullmatch(to_amap_id):
        blocked_reasons.append("non_canonical_amap_identity")
    # The four legacy eval cases inject POIs through mockAmapResponses.  An ID
    # that happens to look like an AMap ID is not sufficient provenance for a
    # real capture request, so keep the actual request trace but block capture.
    if uses_offline_mock_amap:
        blocked_reasons.append("offline_mock_amap_poi_provenance")
    if not normalized_mode:
        blocked_reasons.append("route_mode_missing")

    budget_limit = getattr(route_budget, "route_refresh_max", None) if route_budget is not None else None
    budget_used = getattr(route_budget, "used_route", None) if route_budget is not None else None
    budget_source = str(getattr(route_budget, "source", "") or "") if route_budget is not None else ""
    if budget_limit is None or budget_used is None:
        blocked_reasons.append("route_budget_unavailable")
    elif int(budget_limit) <= int(budget_used):
        blocked_reasons.append("route_budget_exhausted")
    if route_budget is not None and not isinstance(route_budget, AmapCallBudget):
        blocked_reasons.append("route_budget_not_production_exact_lease")
    elif route_budget is not None and budget_source != "creative_portfolio_route_preflight":
        blocked_reasons.append("route_budget_source_not_exact_lease")
    elif route_budget is not None:
        validate_route_work = getattr(route_budget, "validate_route_work", None)
        if (
            not callable(validate_route_work)
            or getattr(validate_route_work, "__func__", None) is not AmapCallBudget.validate_route_work
        ):
            blocked_reasons.append("route_budget_validation_unavailable")
        elif not validate_route_work(
            from_amap_id=from_amap_id,
            to_amap_id=to_amap_id,
            mode=normalized_mode,
            endpoint=f"route/{normalized_mode}",
            source="route_service",
            repair_scope_certificate=repair_scope_certificate,
        ):
            blocked_reasons.append(str(getattr(route_budget, "last_denial_reason", "") or "route_work_unauthorized"))

    return {
        "caseId": str(case_id),
        "planId": str(plan_id),
        "fromSegmentId": str(from_segment_id),
        "toSegmentId": str(to_segment_id),
        # Keep the exact request IDs alongside the capture-only canonical view;
        # a capture must never substitute a parent, label, or nearby POI.
        "fromAmapId": from_amap_id,
        "toAmapId": to_amap_id,
        "fromCanonicalAmapId": from_amap_id if _CANONICAL_AMAP_ID_RE.fullmatch(from_amap_id) else "",
        "toCanonicalAmapId": to_amap_id if _CANONICAL_AMAP_ID_RE.fullmatch(to_amap_id) else "",
        "mode": normalized_mode,
        "fromPoiSource": from_source,
        "toPoiSource": to_source,
        "routeBudget": {
            "known": route_budget is not None,
            "routeRefreshMax": budget_limit,
            "usedRoute": budget_used,
            "source": budget_source,
        },
        "captureEligible": not blocked_reasons,
        "captureBlockedReasons": sorted(set(blocked_reasons)),
        "discoveryOnly": True,
    }


def _recorded_route_replay_failure(
    from_poi: Any,
    to_poi: Any,
    *,
    dry_route_discovery: bool,
) -> str | None:
    """Return a fail-closed reason before replay can produce route evidence."""

    if dry_route_discovery:
        return "dry_route_discovery_no_replay"
    for poi in (from_poi, to_poi):
        source = str(getattr(poi, "source", "") or "")
        amap_id = str(getattr(poi, "amap_id", "") or "").strip().upper()
        if source != "amap-place-search" or not _CANONICAL_AMAP_ID_RE.fullmatch(amap_id):
            return "recorded_route_endpoint_not_canonical_amap"
    return None


def _summarize_route_discovery(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a stable, auditable capture manifest without hiding bad pairs."""

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (
            str(entry.get("fromAmapId") or ""),
            str(entry.get("toAmapId") or ""),
            str(entry.get("mode") or ""),
        )
        pair = grouped.setdefault(
            key,
            {
                "fromAmapId": key[0],
                "toAmapId": key[1],
                "fromCanonicalAmapId": str(entry.get("fromCanonicalAmapId") or ""),
                "toCanonicalAmapId": str(entry.get("toCanonicalAmapId") or ""),
                "mode": key[2],
                "captureEligible": True,
                "captureBlockedReasons": [],
                "lineage": [],
                "routeBudgets": [],
            },
        )
        pair["captureEligible"] = bool(pair["captureEligible"]) and bool(entry.get("captureEligible"))
        pair["captureBlockedReasons"] = sorted(
            set(pair["captureBlockedReasons"]) | set(entry.get("captureBlockedReasons") or [])
        )
        pair["lineage"].append(
            {
                "planId": str(entry.get("planId") or ""),
                "fromSegmentId": str(entry.get("fromSegmentId") or ""),
                "toSegmentId": str(entry.get("toSegmentId") or ""),
            }
        )
        route_budget = entry.get("routeBudget")
        if isinstance(route_budget, dict) and route_budget not in pair["routeBudgets"]:
            pair["routeBudgets"].append(route_budget)

    pairs = [grouped[key] for key in sorted(grouped)]
    capture_eligible_pairs = [item for item in pairs if item.get("captureEligible")]
    blocked_reasons = sorted({reason for item in pairs for reason in (item.get("captureBlockedReasons") or [])})
    # A budget scope is intentionally conservative: when the harness cannot
    # prove that two entries used separate ledgers, it treats them as sharing
    # one.  That can stop a capture early, but can never permit an over-budget
    # capture by hiding repeated runtime requests behind pair de-duplication.
    budget_scopes: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    budget_unknown = False
    for entry in entries:
        budget = entry.get("routeBudget") if isinstance(entry.get("routeBudget"), dict) else {}
        limit = budget.get("routeRefreshMax")
        used = budget.get("usedRoute")
        if limit is None or used is None:
            budget_unknown = True
            continue
        scope_key = (
            str(entry.get("caseId") or ""),
            str(budget.get("source") or ""),
            int(limit),
            int(used),
        )
        scope = budget_scopes.setdefault(
            scope_key,
            {
                "caseId": scope_key[0],
                "source": scope_key[1],
                "routeRefreshMax": scope_key[2],
                "usedRoute": scope_key[3],
                "remainingRouteCapacity": max(0, scope_key[2] - scope_key[3]),
                "actualRequestCount": 0,
                "uniqueRequestKeys": set(),
            },
        )
        scope["actualRequestCount"] += 1
        scope["uniqueRequestKeys"].add(
            (
                str(entry.get("fromAmapId") or ""),
                str(entry.get("toAmapId") or ""),
                str(entry.get("mode") or ""),
            )
        )
    rendered_scopes = []
    for scope in budget_scopes.values():
        unique_request_count = len(scope.pop("uniqueRequestKeys"))
        scope["uniqueRequestCount"] = unique_request_count
        scope["actualRequestCountExceeded"] = int(scope["actualRequestCount"]) > int(scope["remainingRouteCapacity"])
        scope["deduplicatedRequestCountExceeded"] = unique_request_count > int(scope["remainingRouteCapacity"])
        rendered_scopes.append(scope)
    rendered_scopes.sort(key=lambda item: (item["caseId"], item["source"], item["routeRefreshMax"], item["usedRoute"]))
    budget_known = bool(pairs) and not budget_unknown
    budget_exceeded = any(
        item["actualRequestCountExceeded"] or item["deduplicatedRequestCountExceeded"] for item in rendered_scopes
    )
    return {
        "mode": "dry_route_discovery",
        "networkCalls": 0,
        "actualRouteRequests": entries,
        "uniqueRoutePairs": pairs,
        "captureEligiblePairs": capture_eligible_pairs,
        "captureBlockedReasons": blocked_reasons,
        "routeBudget": {
            "known": budget_known,
            "scopes": rendered_scopes,
            "uniquePairCount": len(pairs),
            "exceeded": budget_exceeded,
        },
        # A single blocked request means the case cannot be faithfully captured:
        # silently capturing a subset would reintroduce the missing-leg gap.
        "captureReady": bool(pairs)
        and len(capture_eligible_pairs) == len(pairs)
        and budget_known
        and not budget_exceeded,
        "incomplete": not bool(pairs),
    }


def _capture_nonnegative_int(
    value: Any,
    *,
    field: str,
    errors: list[str],
    required: bool = False,
) -> int:
    """Parse capture metadata without letting malformed runtime evidence escape.

    The capture manifest is an authorization boundary.  A malformed integer is
    not a reason to infer a value from a case fixture or to continue with a
    partially trusted trace; callers receive a harmless zero plus an explicit
    fail-closed error instead.
    """

    if value is None or value == "":
        if required:
            errors.append(f"capture_metadata_missing:{field}")
        return 0
    if isinstance(value, bool):
        errors.append(f"capture_metadata_invalid:{field}")
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"capture_metadata_invalid:{field}")
        return 0
    if parsed < 0 or (required and parsed == 0):
        errors.append(f"capture_metadata_invalid:{field}")
        return 0
    return parsed


def _logical_route_authorization(
    persisted_initial_plan: Any,
) -> dict[str, Any]:
    """Project only an already-sealed route authority for capture preflight.

    A request's natural-language transport preference is useful diagnostic
    context, but it is not permission to make a Provider route request.  This
    projector deliberately refuses to infer a preferred mode or a budget from
    an awaiting/malformed contract.  Even a ready contract remains
    ``budgetState=not_derived`` until a later, server-owned pair×mode lease is
    available; the initial identity-free trace has no canonical endpoints from
    which to derive that lease.
    """

    blocked = {
        "status": "missing",
        "preferredMode": "",
        "modeSource": "",
        "contractFingerprint": "",
        "budgetState": "not_derived",
        "budgetReason": "canonical_adjacent_pairs_not_materialized",
    }
    if not isinstance(persisted_initial_plan, dict):
        return blocked
    pipeline_context = persisted_initial_plan.get("pipelineContext")
    if not isinstance(pipeline_context, dict):
        return {**blocked, "status": "pipeline_context_missing"}
    request_contract = pipeline_context.get("requestIntentContract")
    if not isinstance(request_contract, dict):
        return {**blocked, "status": "request_intent_contract_missing"}
    raw_contract = request_contract.get("routeDecisionContract")
    if not isinstance(raw_contract, dict):
        return {**blocked, "status": "route_decision_contract_missing"}
    if str(raw_contract.get("status") or "") != "ready":
        return {
            **blocked,
            "status": "route_decision_contract_not_ready",
            "missingFields": [str(item) for item in raw_contract.get("missingFields") or []],
        }

    from src.models.route_option import normalize_route_mode
    from src.services.route_insertion_scorer import RouteInsertionScorer

    normalized = RouteInsertionScorer.normalized_route_decision_contract(raw_contract)
    if normalized is None:
        return {**blocked, "status": "route_decision_contract_invalid"}
    provenance = normalized.get("provenance") if isinstance(normalized.get("provenance"), dict) else {}
    preferred_mode = normalize_route_mode(str(provenance.get("transportMode") or ""))
    fingerprint = str(normalized.get("fingerprint") or "")
    if not preferred_mode or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return {**blocked, "status": "route_decision_contract_incomplete"}
    return {
        "status": "ready",
        "preferredMode": preferred_mode,
        "modeSource": "request_intent_contract.route_decision_contract",
        "contractFingerprint": fingerprint,
        "budgetState": "not_derived",
        "budgetReason": "canonical_adjacent_pairs_not_materialized",
    }


def _capture_semantic_trace(
    *,
    case: dict[str, Any],
    runtime_trace: dict[str, Any],
    route_discovery: Any,
    case_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Expose only server-compiled capture facts for the pre-network manifest.

    This trace deliberately excludes provider identities.  A route discovery
    run may have traversed legacy mock POIs, but those values are useful only
    to derive *logical* segment topology; they must never become an AMap
    capture allowlist.
    """

    from src.services.consumer_candidate_admission_service import (
        ConsumerCandidateAdmissionService,
    )

    if not isinstance(runtime_trace, dict):
        runtime_trace = {}
    trace_errors: list[str] = []
    raw_adapter_calls = runtime_trace.get("adapterCalls")
    if raw_adapter_calls is None:
        raw_adapter_calls = []
    if not isinstance(raw_adapter_calls, list):
        trace_errors.append("capture_metadata_invalid:adapterCalls")
        raw_adapter_calls = []
    adapter_calls = [item for item in raw_adapter_calls if isinstance(item, dict)]
    if len(adapter_calls) != len(raw_adapter_calls):
        trace_errors.append("capture_metadata_invalid:adapterCalls.item")
    profile_runs: list[dict[str, Any]] = []
    for call in adapter_calls:
        profile = call.get("profile") if isinstance(call.get("profile"), dict) else {}
        if not profile:
            trace_errors.append("capture_metadata_invalid:adapterCalls.profile")
        adapted_raw = call.get("adaptedPlans")
        if adapted_raw is None:
            adapted_raw = []
        if not isinstance(adapted_raw, list):
            trace_errors.append("capture_metadata_invalid:adapterCalls.adaptedPlans")
            adapted_raw = []
        adapted = [item for item in adapted_raw if isinstance(item, dict)]
        if len(adapted) != len(adapted_raw):
            trace_errors.append("capture_metadata_invalid:adapterCalls.adaptedPlans.item")
        source_evidence = profile.get("sourceEvidence")
        if not isinstance(source_evidence, dict):
            trace_errors.append("capture_metadata_invalid:profile.sourceEvidence")
            source_evidence = {}
        budget_policy = profile.get("budgetPolicy")
        if not isinstance(budget_policy, dict):
            trace_errors.append("capture_metadata_invalid:profile.budgetPolicy")
            budget_policy = {}
        profile_id = str(profile.get("profileId") or "")
        profile_fingerprint = str(profile.get("profileFingerprint") or "")
        execution_fingerprint = str(profile.get("executionFingerprint") or "")
        day_number = _capture_nonnegative_int(
            profile.get("dayNumber"),
            field="profile.dayNumber",
            errors=trace_errors,
            required=True,
        )
        if not profile_id or not profile_fingerprint or not execution_fingerprint:
            trace_errors.append("capture_profile_identity_missing")
        scope_payload = {
            "briefId": str(profile.get("briefId") or ""),
            "poolId": str(profile.get("poolId") or ""),
            "planningSlotId": str(profile.get("planningSlotId") or ""),
            "dayNumber": day_number,
        }
        occurrence_payload = {
            "profileId": profile_id,
            "profileFingerprint": profile_fingerprint,
            "executionFingerprint": execution_fingerprint,
            **scope_payload,
        }
        occurrence_fingerprint = _json_fingerprint(occurrence_payload).upper()
        consumer_admission_input = ConsumerCandidateAdmissionService.build_consumer_context(
            brief_id=scope_payload["briefId"],
            pool_id=scope_payload["poolId"],
            planning_slot_id=scope_payload["planningSlotId"],
            day_number=scope_payload["dayNumber"],
            city=str(profile.get("city") or ""),
            family=str(profile.get("experienceFamily") or ""),
            activity_mode=str(profile.get("intentType") or ""),
            requirement_level=str(profile.get("requirementLevel") or ""),
            experience_shape=str(profile.get("experienceShape") or "single_poi"),
            experience_goal=str(profile.get("experienceGoal") or ""),
            optional_experience_family=(str(profile.get("originalExperienceFamily") or "") or None),
            assigned_meal_family=(str(profile.get("assignedMealFamily") or "") or None),
            desired_signals=list(profile.get("desiredSignals") or []),
            avoid_signals=list(profile.get("avoidSignals") or []),
            evidence_requirements=dict(profile.get("evidencePolicy") or {}),
            grounding_policy=dict(profile.get("groundingPolicy") or {}),
            route_context=dict(profile.get("routeContext") or {}),
            experience_spec_policy=dict(profile.get("experienceSpecPolicy") or {}),
            spec_fingerprint=str(
                (profile.get("experienceSpecPolicy") or {}).get("specFingerprint")
                if isinstance(profile.get("experienceSpecPolicy"), dict)
                else ""
            ),
            intent_fingerprint=str(profile.get("intentFingerprint") or ""),
            exact_entity=(str(profile.get("exactEntity") or "") or None),
            preferred_types=list(profile.get("preferredPlaceFacets") or []),
            rejected_types=list(profile.get("rejectedPlaceFacets") or []),
        )
        raw_source_plans = [item for item in profile.get("queryPlans") or [] if isinstance(item, dict)]
        source_plans_by_id = {
            str(item.get("planId") or ""): item for item in raw_source_plans if str(item.get("planId") or "")
        }
        profile_runs.append(
            {
                "profileId": profile_id,
                "profileFingerprint": profile_fingerprint,
                "executionFingerprint": execution_fingerprint,
                "occurrenceFingerprint": occurrence_fingerprint,
                "scope": scope_payload,
                "semanticRole": {
                    "experienceFamily": str(profile.get("experienceFamily") or ""),
                    "intentType": str(profile.get("intentType") or ""),
                    "requirementLevel": str(profile.get("requirementLevel") or ""),
                    "entityBindingMode": str(profile.get("entityBindingMode") or ""),
                },
                "city": str(profile.get("city") or ""),
                "consumerAdmissionInput": consumer_admission_input,
                "sourceEvidence": {
                    "source": str(source_evidence.get("source") or ""),
                    "candidateHintsUsedAsSemanticEvidence": bool(
                        source_evidence.get("candidateHintsUsedAsSemanticEvidence")
                    ),
                },
                "queryPlans": [
                    {
                        "planId": str(item.get("planId") or ""),
                        "sourcePlanId": str(item.get("sourcePlanId") or ""),
                        "sourcePlanFingerprint": _json_fingerprint(
                            source_plans_by_id.get(str(item.get("sourcePlanId") or ""), {})
                        ).upper(),
                        "providerPlanFingerprint": _json_fingerprint(item).upper(),
                        "endpoint": str(item.get("endpoint") or ""),
                        "mode": str(item.get("mode") or ""),
                        "city": str(item.get("city") or ""),
                        "keyword": str(item.get("keyword") or ""),
                        "category": str(item.get("category") or ""),
                        "providerCategoryKey": str(item.get("providerCategoryKey") or ""),
                        "anchorPolicy": str(item.get("anchorPolicy") or ""),
                        "radiusMeters": _capture_nonnegative_int(
                            item.get("radiusMeters"),
                            field="queryPlan.radiusMeters",
                            errors=trace_errors,
                        ),
                        "resultLimit": _capture_nonnegative_int(
                            item.get("resultLimit"),
                            field="queryPlan.resultLimit",
                            errors=trace_errors,
                        ),
                    }
                    for item in adapted
                    if isinstance(item, dict)
                ],
                "budget": dict(budget_policy),
            }
        )

    observed_amap_adcode = _capture_observed_amap_adcode(
        case=case,
        runtime_trace=runtime_trace,
        profile_runs=profile_runs,
        errors=trace_errors,
    )
    persisted_initial_plan = runtime_trace.get("serverInitialPlan")
    persisted_payload = (
        persisted_initial_plan.get("initialPlan")
        if isinstance(persisted_initial_plan, dict) and isinstance(persisted_initial_plan.get("initialPlan"), dict)
        else {}
    )
    persisted_initial_plan_status = "missing"
    if isinstance(persisted_initial_plan, dict) and not persisted_payload:
        persisted_initial_plan_status = "invalid_payload"
    if str(persisted_payload.get("mode") or "") == "initial_plan":
        initial_payload = persisted_payload
        logical_plan_source = "persisted_server_initial_plan"
        persisted_initial_plan_status = "observed"
    else:
        if persisted_payload:
            persisted_initial_plan_status = "invalid_mode"
        initial_payloads = case.get("initialPlanPayloads") or []
        if not isinstance(initial_payloads, list):
            trace_errors.append("capture_case_invalid:initialPlanPayloads")
            initial_payloads = []
        initial_payload = next(
            (item for item in initial_payloads if isinstance(item, dict) and item.get("mode") == "day_slots"),
            {},
        )
        logical_plan_source = "legacy_case_initial_plan" if initial_payload else "not_observed"
    raw_pools = initial_payload.get("intentPools") if isinstance(initial_payload, dict) else []
    if raw_pools is None:
        raw_pools = []
    if not isinstance(raw_pools, list):
        trace_errors.append("capture_initial_plan_invalid:intentPools")
        raw_pools = []
    pools = {}
    for pool in raw_pools:
        if not isinstance(pool, dict):
            trace_errors.append("capture_initial_plan_invalid:intentPools.item")
            continue
        pool_id = str(pool.get("poolId") or "")
        if not pool_id:
            trace_errors.append("capture_initial_plan_invalid:poolId")
            continue
        if pool_id in pools:
            trace_errors.append("capture_initial_plan_duplicate:poolId")
            continue
        pools[pool_id] = pool
    slot_to_pools: dict[str, list[str]] = {}
    for pool_id, pool in pools.items():
        assigned_slots = pool.get("assignToSlots")
        if not isinstance(assigned_slots, list):
            trace_errors.append("capture_initial_plan_invalid:assignToSlots")
            continue
        for slot_id in assigned_slots:
            normalized_slot_id = str(slot_id or "")
            if normalized_slot_id:
                slot_to_pools.setdefault(normalized_slot_id, []).append(pool_id)

    profile_by_scope: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    profile_identity_scopes: dict[tuple[str, str, str], set[tuple[str, str, str, int]]] = {}
    profile_identity_contents: dict[tuple[str, str, str], set[str]] = {}
    for profile in profile_runs:
        scope = profile.get("scope") if isinstance(profile.get("scope"), dict) else {}
        scope_key = (
            str(scope.get("briefId") or ""),
            str(scope.get("poolId") or ""),
            str(scope.get("planningSlotId") or ""),
            _capture_nonnegative_int(
                scope.get("dayNumber"),
                field="profile.scope.dayNumber",
                errors=trace_errors,
                required=True,
            ),
        )
        identity = (
            str(profile.get("profileId") or ""),
            str(profile.get("profileFingerprint") or ""),
            str(profile.get("executionFingerprint") or ""),
        )
        if not all(scope_key) or not all(identity):
            trace_errors.append("capture_profile_scope_invalid")
            continue
        profile_identity_scopes.setdefault(identity, set()).add(scope_key)
        profile_identity_contents.setdefault(identity, set()).add(
            _json_fingerprint(
                {
                    "scope": scope_key,
                    "semanticRole": profile.get("semanticRole"),
                    "city": profile.get("city"),
                    "sourceEvidence": profile.get("sourceEvidence"),
                    "queryPlans": profile.get("queryPlans"),
                    "budget": profile.get("budget"),
                }
            )
        )
        if all(scope_key):
            profile_by_scope.setdefault(scope_key, []).append(profile)

    invalid_profile_identities: set[tuple[str, str, str]] = set()
    profile_scope_integrity_errors: list[str] = []
    for identity, scopes in profile_identity_scopes.items():
        if len(scopes) != 1:
            invalid_profile_identities.add(identity)
            profile_scope_integrity_errors.append("profile_identity_reused_across_scopes")
        if len(profile_identity_contents.get(identity) or set()) != 1:
            invalid_profile_identities.add(identity)
            profile_scope_integrity_errors.append("profile_identity_content_conflict")

    logical_slots: list[dict[str, Any]] = []
    topology_errors: list[str] = []
    raw_slots = initial_payload.get("daySlots") if isinstance(initial_payload, dict) else []
    if raw_slots is None:
        raw_slots = []
    if not isinstance(raw_slots, list):
        trace_errors.append("capture_initial_plan_invalid:daySlots")
        raw_slots = []
    for slot in raw_slots:
        if not isinstance(slot, dict):
            trace_errors.append("capture_initial_plan_invalid:daySlots.item")
            continue
        slot_id = str(slot.get("slotId") or "")
        day_number = _capture_nonnegative_int(
            slot.get("dayNumber"),
            field="daySlot.dayNumber",
            errors=trace_errors,
            required=True,
        )
        if not slot_id:
            trace_errors.append("capture_initial_plan_invalid:slotId")
        pool_ids = list(dict.fromkeys(slot_to_pools.get(slot_id, [])))
        pool = pools.get(pool_ids[0], {}) if len(pool_ids) == 1 else {}
        brief_id = str(pool.get("briefId") or "")
        scope_key = (brief_id, str(pool_ids[0] or "") if len(pool_ids) == 1 else "", slot_id, day_number)
        matched_profiles: list[dict[str, Any]] = []
        seen_profile_identity: set[tuple[str, str, str]] = set()
        invalid_profile_match = False
        for profile in profile_by_scope.get(scope_key, []):
            identity = (
                str(profile.get("profileId") or ""),
                str(profile.get("profileFingerprint") or ""),
                str(profile.get("executionFingerprint") or ""),
            )
            if identity in invalid_profile_identities:
                invalid_profile_match = True
                continue
            if identity not in seen_profile_identity:
                seen_profile_identity.add(identity)
                matched_profiles.append(profile)
        if len(pool_ids) != 1:
            binding_status = "ambiguous_pool" if pool_ids else "missing_pool"
        elif invalid_profile_match:
            binding_status = "invalid_profile_identity"
        elif len(matched_profiles) != 1:
            binding_status = "ambiguous_profile" if matched_profiles else "missing_profile"
        else:
            binding_status = "matched"
        matched_profile = matched_profiles[0] if binding_status == "matched" else {}
        semantic_role = (
            matched_profile.get("semanticRole")
            if isinstance(matched_profile, dict) and isinstance(matched_profile.get("semanticRole"), dict)
            else {}
        )
        time_window = str(slot.get("timeWindow") or "")
        start_time = str(slot.get("startTime") or "")
        if not start_time and re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", time_window):
            start_time = time_window.split("-", 1)[0]
        logical_slot = {
            "briefId": brief_id,
            "dayNumber": day_number,
            "planningSlotId": slot_id,
            "poolId": str(pool_ids[0] or "") if len(pool_ids) == 1 else "",
            "semanticRole": str(slot.get("kind") or ""),
            "experienceFamily": str(semantic_role.get("experienceFamily") or ""),
            "intentType": str(semantic_role.get("intentType") or pool.get("intentType") or ""),
            "routeAnchor": bool(slot.get("routeAnchor")),
            "startTime": start_time,
            "profileId": str(matched_profile.get("profileId") or ""),
            "profileBindingStatus": binding_status,
        }
        logical_slots.append(logical_slot)

    anchors_by_day: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for slot in logical_slots:
        if not slot["routeAnchor"]:
            continue
        if slot["profileBindingStatus"] != "matched":
            topology_errors.append(
                f"route_anchor_profile_binding_{slot['profileBindingStatus']}:{slot['planningSlotId']}"
            )
            continue
        if not slot["briefId"] or not re.fullmatch(r"\d{2}:\d{2}", str(slot["startTime"])):
            topology_errors.append(f"route_anchor_schedule_unresolved:{slot['planningSlotId']}")
            continue
        anchors_by_day.setdefault((str(slot["briefId"]), int(slot["dayNumber"])), []).append(slot)

    logical_edges: list[dict[str, Any]] = []
    for (brief_id, day_number), anchors in sorted(anchors_by_day.items()):
        ordered = sorted(anchors, key=lambda item: (str(item["startTime"]), str(item["planningSlotId"])))
        for left, right in zip(ordered, ordered[1:]):
            logical_edges.append(
                {
                    "briefId": brief_id,
                    "dayNumber": day_number,
                    "fromLogicalNode": f"{brief_id}::{left['planningSlotId']}",
                    "toLogicalNode": f"{brief_id}::{right['planningSlotId']}",
                    "mode": "",
                    "modeReason": "server_initial_plan_route_anchor_adjacency",
                }
            )

    actual_requests = (
        route_discovery.get("actualRouteRequests")
        if isinstance(route_discovery, dict) and isinstance(route_discovery.get("actualRouteRequests"), list)
        else []
    )
    logical_route_budget = {
        "plannedAdjacentPairCount": len(logical_edges),
        "actualLedgerKnown": bool(
            isinstance(route_discovery, dict)
            and isinstance(route_discovery.get("routeBudget"), dict)
            and route_discovery["routeBudget"].get("known")
        ),
        "state": "not_instantiated" if not actual_requests else "observed",
        "reason": "portfolio_staging_not_reached" if not actual_requests else "runtime_route_requests_observed",
        "scopes": [],
    }
    logical_route_authorization = _logical_route_authorization(persisted_initial_plan)

    user_messages = [
        str(step.get("content") or "")
        for step in case.get("steps") or []
        if isinstance(step, dict) and step.get("action") == "send_agent_message"
    ]
    profile_execution_status = "executed" if profile_runs else "not_executed"
    persisted_choice = (
        case_result.get("persistedClarificationChoiceReplay")
        if isinstance(case_result, dict) and isinstance(case_result.get("persistedClarificationChoiceReplay"), dict)
        else {}
    )
    route_contract = (
        persisted_choice.get("routeDecisionContract")
        if isinstance(persisted_choice.get("routeDecisionContract"), dict)
        else {}
    )
    raw_web_calls = [item for item in runtime_trace.get("webCalls") or [] if isinstance(item, dict)]
    deferred_dependencies = []
    for call_index, call in enumerate(raw_web_calls, start=1):
        seeds = [str(item) for item in call.get("seedNames") or [] if str(item)]
        deferred_dependencies.append(
            {
                "dependencyId": f"web-seed-{call_index}",
                "type": "web_seed_derived_place_requests",
                "status": "blocked" if seeds else "deferred",
                "queryFingerprint": _json_fingerprint({"query": str(call.get("query") or "")}),
                "seedCount": len(seeds),
                "reason": (
                    "web_seed_dynamic_query_lineage_unclosed" if seeds else "offline_web_result_has_no_entity_seed"
                ),
            }
        )
    phase1_errors = [str(item) for item in runtime_trace.get("phase1PlaceRequestErrors") or [] if str(item)]
    try:
        from backend.evals.recorded_fixture_capture import compute_source_fingerprint

        source_fingerprint = compute_source_fingerprint(project_root=PROJECT_ROOT)
    except (ImportError, OSError, RuntimeError, ValueError):
        source_fingerprint = ""
        phase1_errors.append("source_fingerprint_unavailable")
    trace = {
        "schemaVersion": "recorded-capture-semantic-trace-v1",
        "caseId": str(case.get("id") or ""),
        "originalUserMessages": user_messages,
        "profileExecutionStatus": profile_execution_status,
        "profileRuns": profile_runs,
        "logicalPlanSource": logical_plan_source,
        "persistedInitialPlanStatus": persisted_initial_plan_status,
        "logicalSlots": logical_slots,
        "logicalRouteTopology": logical_edges,
        "logicalRouteTopologyErrors": sorted(set(topology_errors)),
        "profileScopeIntegrityErrors": sorted(set(profile_scope_integrity_errors)),
        "semanticTraceErrors": sorted(set(trace_errors)),
        "observedAmapAdcode": observed_amap_adcode,
        "logicalRouteBudget": logical_route_budget,
        "logicalRouteAuthorization": logical_route_authorization,
        "sourceFingerprint": source_fingerprint,
        "dedicatedCaseSha256": str(case.get("_offlineCaseSha256") or ""),
        "opaqueChoiceCheckpointFingerprint": str(persisted_choice.get("checkpointFingerprint") or ""),
        "routeContractFingerprint": str(route_contract.get("fingerprint") or ""),
        "phase1PlaceRequestEvidence": [
            deepcopy(item) for item in runtime_trace.get("phase1PlaceRequestEvidence") or [] if isinstance(item, dict)
        ],
        "phase1PlaceRequestErrors": sorted(set(phase1_errors)),
        "phase1DeferredDependencies": deferred_dependencies,
        "phase1WebInvocationCount": len(raw_web_calls),
        "phase1WebSeedCount": sum(int(item.get("seedCount") or 0) for item in deferred_dependencies),
        "legacyMockAmapPresent": bool(case.get("mockAmapResponses")),
        "identityCapturePrerequisite": (
            "production_search_profile_executed" if profile_runs else "missing_production_search_profile"
        ),
    }
    trace["runtimeEvidenceSha256"] = (
        hashlib.sha256(
            json.dumps(
                trace,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        .hexdigest()
        .upper()
    )
    return trace


def issue_production_dry_route_trace(
    *,
    capture_semantic_trace: dict[str, Any],
    anchor_binding_certificate_fingerprint: str,
    planning_root: dict[str, str],
) -> dict[str, Any]:
    """Issue the only dry Route-topology input accepted by capture tooling.

    This remains entirely offline.  It turns the evaluator's already emitted
    logical route topology into a sealed, source-bound selection trace after
    canonical Place identities exist.  The function deliberately does not
    invent physical endpoints, modes, or walking fallbacks: endpoint binding
    remains the responsibility of the downstream certificate validator, while
    walking can only be considered after real preferred-transit evidence.
    """

    trace = deepcopy(capture_semantic_trace) if isinstance(capture_semantic_trace, dict) else None
    if not isinstance(trace, dict):
        raise ValueError("production_dry_route_trace_invalid")
    trace_fingerprint = trace.pop("runtimeEvidenceSha256", None)
    if (
        not isinstance(trace_fingerprint, str)
        or not re.fullmatch(r"[0-9A-F]{64}", trace_fingerprint)
        or _json_fingerprint(trace).upper() != trace_fingerprint
    ):
        raise ValueError("production_dry_route_trace_fingerprint_invalid")
    if (
        not isinstance(anchor_binding_certificate_fingerprint, str)
        or not re.fullmatch(r"[0-9A-F]{64}", anchor_binding_certificate_fingerprint)
        or not isinstance(planning_root, dict)
        or set(planning_root) != {"rootPortfolioId", "planningRoot", "briefId"}
        or not all(isinstance(value, str) and value for value in planning_root.values())
    ):
        raise ValueError("production_dry_route_trace_scope_invalid")
    route_contract = trace.get("routeContractFingerprint")
    logical_edges = trace.get("logicalRouteTopology")
    route_authorization = trace.get("logicalRouteAuthorization")
    if (
        not isinstance(route_contract, str)
        or not re.fullmatch(r"[0-9A-F]{64}", route_contract)
        or not isinstance(logical_edges, list)
        or not logical_edges
        or trace.get("logicalRouteTopologyErrors") != []
        or not isinstance(route_authorization, dict)
        or route_authorization.get("status") != "ready"
        or route_authorization.get("preferredMode") != "transit"
        or route_authorization.get("contractFingerprint") != route_contract
    ):
        raise ValueError("production_dry_route_trace_topology_unavailable")
    preferred: list[dict[str, Any]] = []
    for edge in logical_edges:
        if not isinstance(edge, dict) or set(edge) != {
            "briefId",
            "dayNumber",
            "fromLogicalNode",
            "toLogicalNode",
            "mode",
            "modeReason",
        }:
            raise ValueError("production_dry_route_trace_topology_invalid")
        if (
            not isinstance(edge["briefId"], str)
            or not edge["briefId"]
            or not isinstance(edge["dayNumber"], int)
            or edge["briefId"] != planning_root["briefId"]
            or edge["mode"] != ""
            or edge["modeReason"] != "server_initial_plan_route_anchor_adjacency"
            or edge["fromLogicalNode"] == edge["toLogicalNode"]
        ):
            raise ValueError("production_dry_route_trace_topology_invalid")
        preferred.append(
            {
                "briefId": edge["briefId"],
                "dayNumber": edge["dayNumber"],
                "fromLogicalNode": edge["fromLogicalNode"],
                "toLogicalNode": edge["toLogicalNode"],
                "mode": "transit",
                "condition": "preferred",
                "reason": "production_selected_adjacent",
            }
        )
    issued = {
        "schemaVersion": "trip-production-issued-dry-route-trace-v1",
        "captureSemanticTraceFingerprint": trace_fingerprint,
        "anchorBindingCertificateFingerprint": anchor_binding_certificate_fingerprint,
        "sourceFingerprint": trace.get("sourceFingerprint"),
        "dedicatedCaseSha256": trace.get("dedicatedCaseSha256"),
        "routeContractFingerprint": route_contract,
        "planningRoot": deepcopy(planning_root),
        "preferredTransitLegs": preferred,
        "conditionalWalkingFallbacks": [],
    }
    if not all(
        isinstance(issued[key], str) and re.fullmatch(r"[0-9A-F]{64}", issued[key])
        for key in ("sourceFingerprint", "dedicatedCaseSha256", "routeContractFingerprint")
    ):
        raise ValueError("production_dry_route_trace_binding_invalid")
    issued["productionDryRouteTraceFingerprint"] = _json_fingerprint(issued).upper()
    return issued


def _capture_observed_amap_adcode(
    *,
    case: dict[str, Any],
    runtime_trace: dict[str, Any],
    profile_runs: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    """Project only an already-built production AMap city parameter.

    The eval harness must not translate a profile city into an adcode itself.
    It can only preserve the six-digit value emitted by ``MapPoiService`` for
    an actual place request, and only for a non-mock case whose profile city
    facts are internally consistent.
    """

    unavailable = {
        "adcode": "",
        "source": "not_observed",
        "observedRequestCount": 0,
        "status": "not_observed",
    }
    if bool(case.get("mockAmapResponses")):
        return {**unavailable, "status": "mock_provenance"}

    raw_calls = runtime_trace.get("amapCalls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        errors.append("capture_metadata_invalid:amapCalls")
        return {**unavailable, "status": "invalid"}

    observed: set[str] = set()
    request_count = 0
    invalid = False
    for call in raw_calls:
        if not isinstance(call, dict):
            errors.append("capture_metadata_invalid:amapCalls.item")
            invalid = True
            continue
        if str(call.get("endpoint") or "") not in {"place/text", "place/around"}:
            continue
        request_count += 1
        params = call.get("sanitizedParams") if isinstance(call.get("sanitizedParams"), dict) else call.get("params")
        city = params.get("city") if isinstance(params, dict) else None
        if not isinstance(city, str) or not re.fullmatch(r"\d{6}", city):
            invalid = True
            continue
        observed.add(city)

    profile_cities = {str(item.get("city") or "").strip() for item in profile_runs}
    profile_cities.discard("")
    profiles_are_consistent = len(profile_cities) == 1 and all(
        all(
            str(plan.get("city") or "").strip() == str(profile.get("city") or "").strip()
            for plan in profile.get("queryPlans") or []
            if isinstance(plan, dict)
        )
        for profile in profile_runs
    )
    if invalid:
        return {**unavailable, "observedRequestCount": request_count, "status": "invalid"}
    if len(observed) != 1:
        return {
            **unavailable,
            "observedRequestCount": request_count,
            "status": "ambiguous" if observed else "not_observed",
        }
    if not profiles_are_consistent:
        return {
            **unavailable,
            "observedRequestCount": request_count,
            "status": "profile_city_inconsistent",
        }
    return {
        "adcode": next(iter(observed)),
        "source": "server_observed_amap_request_param",
        "observedRequestCount": request_count,
        "status": "observed",
    }


def _offline_intent_contract(context: dict[str, Any]) -> dict[str, Any]:
    """Keep the deterministic eval provider explicit about the intent seam."""

    message = str(context.get("message") or "")
    if context.get("schemaVersion") == "conversation-intent-context-v2":
        guide_grounded = "攻略" in message and any(marker in message for marker in ("方案", "行程", "规划"))
        intent = "continue_plan_expansion" if guide_grounded else "create_itinerary"
        requested_scope = "planning_root" if guide_grounded else "new_itinerary"
        return {
            "schemaVersion": "conversation-intent-hypothesis-v2",
            "primary": {
                "intent": intent,
                "confidence": 0.99,
                "requestedScope": requested_scope,
                "isQuestion": False,
                "isNegated": False,
                "continuationMode": "guide_grounded" if guide_grounded else None,
                "targetReference": {"kind": "latest_guide" if guide_grounded else "none"},
            },
            "alternatives": [],
            "semanticSignals": {
                "quotedCommand": False,
                "hypothetical": False,
                "correctionAfterNegation": False,
            },
        }
    state = context.get("state") if isinstance(context.get("state"), dict) else {}
    if state.get("hasUnansweredClarification"):
        intent = "clarification_answer"
        requested_scope = "clarification"
    elif state.get("hasActiveVersion") and any(
        marker in message for marker in ("修改为", "改成", "换成", "调整为", "改到")
    ):
        intent = "modify_itinerary"
        requested_scope = "active_itinerary"
    elif any(marker in message for marker in ("重试", "再试", "重新来")):
        intent = "retry_current_stage"
        requested_scope = "current_stage"
    elif state.get("hasActiveVersion"):
        intent = "modify_itinerary"
        requested_scope = "active_itinerary"
    else:
        intent = "create_itinerary"
        requested_scope = "new_itinerary"
    return {
        "intent": intent,
        "confidence": 0.99,
        "requestedScope": requested_scope,
        "isQuestion": False,
        "isNegated": "不要" in message or "别" in message,
    }


def _offline_strict_draft_decision(
    context: dict[str, Any],
    *,
    theme: str,
    hard_only: bool = False,
    optional_experience_budget: Optional[int] = None,
) -> dict[str, Any]:
    """Build one deterministic Controller fixture that satisfies the live V3 draft contract."""

    requirements = [
        item
        for item in context.get("goalRequirements") or []
        if isinstance(item, dict) and str(item.get("goalId") or "").strip()
    ]
    available_days = [
        int(item)
        for item in context.get("availableDayNumbers") or []
        if isinstance(item, int) and int(item) > 0
    ]
    if not available_days:
        dates = (context.get("resolvedTripDates") or {}).get("dates") or []
        available_days = list(range(1, len(dates) + 1)) or [1]
    available_days = sorted(set(available_days))

    hard_requirements = [
        item
        for item in requirements
        if str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
    ]
    soft_requirements = [] if hard_only else [item for item in requirements if item not in hard_requirements]
    scheduled_hard: dict[int, list[str]] = {day_number: [] for day_number in available_days}
    scheduled_soft: dict[int, list[str]] = {day_number: [] for day_number in available_days}

    def schedule(requirement: dict[str, Any], destination: dict[int, list[str]]) -> None:
        goal_id = str(requirement["goalId"])
        allowed = [
            int(item)
            for item in requirement.get("allowedDayNumbers") or available_days
            if isinstance(item, int) and int(item) in destination
        ]
        allowed = list(dict.fromkeys(allowed)) or available_days
        requested = int(
            requirement.get("requiredMin")
            or requirement.get("preferredCount")
            or requirement.get("target")
            or 1
        )
        for day_number in allowed[: max(1, min(requested, len(allowed)))]:
            destination[day_number].append(goal_id)

    for requirement in hard_requirements:
        schedule(requirement, scheduled_hard)
    for requirement in soft_requirements:
        schedule(requirement, scheduled_soft)

    requirement_by_goal = {str(item["goalId"]): item for item in requirements}
    day_part_defaults = {
        "campus_visit": "morning",
        "meal": "noon",
        "local_food": "noon",
        "museum": "afternoon",
        "night_view": "night",
    }
    start_defaults = {
        "morning": "09:00",
        "noon": "12:00",
        "afternoon": "14:00",
        "evening": "19:00",
        "night": "19:00",
        "flexible": "10:00",
    }
    duration_defaults = {
        "campus_visit": 120,
        "meal": 60,
        "local_food": 60,
        "museum": 120,
        "night_view": 90,
    }
    occurrence_schedule_hints: list[dict[str, Any]] = []
    for day_number in available_days:
        goal_ids = [*scheduled_hard[day_number], *scheduled_soft[day_number]]
        ordered = sorted(
            goal_ids,
            key=lambda goal_id: (
                {
                    "morning": 1,
                    "noon": 2,
                    "afternoon": 3,
                    "evening": 4,
                    "night": 5,
                    "flexible": 6,
                }.get(
                    str((requirement_by_goal[goal_id].get("schedulePreference") or {}).get("dayPart") or ""),
                    {
                        "campus_visit": 1,
                        "meal": 2,
                        "local_food": 2,
                        "museum": 3,
                        "night_view": 5,
                    }.get(str(requirement_by_goal[goal_id].get("intentType") or ""), 6),
                ),
                goal_id,
            ),
        )
        for sequence, goal_id in enumerate(ordered, start=1):
            requirement = requirement_by_goal[goal_id]
            intent_type = str(requirement.get("intentType") or "")
            day_part = str((requirement.get("schedulePreference") or {}).get("dayPart") or "")
            if day_part not in {"morning", "noon", "afternoon", "evening", "night", "flexible"}:
                day_part = day_part_defaults.get(intent_type, "flexible")
            duration = duration_defaults.get(intent_type, 90)
            occurrence_schedule_hints.append(
                {
                    "goalId": goal_id,
                    "dayNumber": day_number,
                    "dayPart": day_part,
                    "sequence": sequence,
                    "preferredStartTime": start_defaults[day_part],
                    "durationEstimate": {
                        "min": max(1, duration - 30),
                        "preferred": duration,
                        "max": duration + 30,
                    },
                    "estimateSource": "controller_estimate",
                    "confidence": 0.95,
                }
            )

    route_requirement = (
        context.get("routePlanningPolicyRequirement")
        if isinstance(context.get("routePlanningPolicyRequirement"), dict)
        else {}
    )
    request_contract = (
        context.get("requestIntentContract")
        if isinstance(context.get("requestIntentContract"), dict)
        else {}
    )
    route_contract = (
        request_contract.get("routeDecisionContract")
        if isinstance(request_contract.get("routeDecisionContract"), dict)
        else {}
    )
    projected_envelope = (
        route_requirement.get("detourEnvelope")
        if isinstance(route_requirement.get("detourEnvelope"), dict)
        else {}
    )
    if {"maxGeneralizedCostDelta", "maxDetourRatio"}.issubset(projected_envelope):
        detour_envelope = deepcopy(projected_envelope)
    elif isinstance(route_contract.get("detourTolerance"), dict):
        detour_envelope = deepcopy(route_contract["detourTolerance"])
    else:
        detour_envelope = {"maxGeneralizedCostDelta": 30.0, "maxDetourRatio": 0.35}
    source = str(route_requirement.get("source") or "controller_estimate")
    if source not in {"user_explicit", "clarification_answer", "controller_estimate"}:
        source = "controller_estimate"
    optional_occurrence_count = sum(len(goal_ids) for goal_ids in scheduled_soft.values())
    budget = optional_occurrence_count if optional_experience_budget is None else int(optional_experience_budget)
    budget = max(optional_occurrence_count, min(3, max(0, budget)))
    transport_mode = str((route_contract.get("provenance") or {}).get("transportMode") or "transit")
    if transport_mode not in {"transit", "walking", "bicycling", "driving"}:
        transport_mode = "transit"

    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary",
            "goalPriority": [str(item["goalId"]) for item in [*hard_requirements, *soft_requirements]],
            "dayStrategies": [
                {
                    "dayNumber": day_number,
                    "theme": theme,
                    "requiredGoalIds": scheduled_hard[day_number],
                    "requiredGoalCounts": {goal_id: 1 for goal_id in scheduled_hard[day_number]},
                    "optionalGoalIds": scheduled_soft[day_number],
                    "pace": "standard",
                    "maxRouteAnchors": 6,
                }
                for day_number in available_days
            ],
            "optionalExperienceBudget": budget,
            "routePlanningPolicy": {
                "objective": "least_generalized_cost",
                "source": source,
                "allowExperienceDetour": budget > 0,
                "mobilityProfile": {
                    "transportMode": transport_mode,
                    "paceClass": "standard",
                },
                "detourEnvelope": detour_envelope,
            },
            "occurrenceScheduleHints": occurrence_schedule_hints,
            "searchPriority": ["exact_entity", "required", "semantic_family"],
            "candidateSelectionPolicy": {
                "autoSelectWhenDominant": True,
                "askWhenMaterialTradeoff": True,
                "preferLowDetour": True,
                "avoidRecentEntities": True,
            },
            "schedulePolicy": {
                "respectOpeningWindowsWhenKnown": True,
                "allowProvisionalWhenUnknown": True,
            },
        },
    }


class OfflineAgentProvider:
    def __init__(
        self,
        payloads: list[Union[dict[str, Any], str]],
        initial_plan_payloads: Optional[list[Union[dict[str, Any], str]]] = None,
        candidate_hint_payloads: Optional[list[Union[dict[str, Any], str]]] = None,
        controller_mode: str = "",
        semantic_action_payloads: Optional[list[dict[str, Any]]] = None,
        request_coverage_payloads: Optional[list[list[dict[str, Any]]]] = None,
    ):
        self.payloads = list(payloads)
        self.initial_plan_payloads = list(initial_plan_payloads or [])
        self.candidate_hint_payloads = list(candidate_hint_payloads or [])
        self.controller_mode = str(controller_mode or "")
        self.contexts: list[dict[str, Any]] = []
        self.controller_invocation_count = 0
        self.semantic_action_payloads = list(semantic_action_payloads or [])
        self.request_coverage_payloads = list(request_coverage_payloads or [])
        self.semantic_performance_sink: Optional[dict[str, Any]] = None

    def prepare_controller_performance(self, context: dict[str, Any], sink: dict[str, Any], *, call_kind: str) -> None:
        self.semantic_performance_sink = sink

    def decide_autonomy_lite(self, context: dict[str, Any], *, timeout_seconds: float) -> str:
        assert timeout_seconds > 0
        if context.get("schemaVersion") == "conversation-action-context-v1":
            if self.semantic_performance_sink is not None:
                self.semantic_performance_sink.update(providerInvoked=True, instrumentationStatus="offline_stub")
            return self._pop_payload(self.semantic_action_payloads, context, "semantic action fixture")
        return json.dumps(_offline_intent_contract(context), ensure_ascii=False)

    def decide_autonomy(self, context: dict[str, Any], *, timeout_seconds: float, repair_feedback: str = "") -> str:
        self.controller_invocation_count += 1
        if context.get("requestActivityClauses") is not None:
            if not self.request_coverage_payloads:
                raise AssertionError("offline Full requires an explicit source-clause coverage fixture")
            coverage = deepcopy(self.request_coverage_payloads.pop(0))
            assert [row["clauseId"] for row in coverage] == [row["clauseId"] for row in context["requestActivityClauses"]]
            requirements = []
            for row in coverage:
                for activity in row.get("activities") or []:
                    if activity["polarity"] != "required":
                        continue
                    old = next((item for item in context.get("goalRequirements") or []
                                if item.get("intentType") == activity["intentType"]), {})
                    requirements.append({**deepcopy(old), "goalId": activity["goalId"],
                        "intentType": activity["intentType"], "requiredMin": activity["minCount"],
                        "allowedDayNumbers": activity["allowedDayNumbers"], "requirementLevel": "required",
                        "schedulePreference": {"dayPart": activity["dayPart"]}})
            decision = _offline_strict_draft_decision({**context, "goalRequirements": requirements}, theme="高校与公园")
            decision["actionDirective"]["requestCoverage"] = coverage
            return json.dumps(decision, ensure_ascii=False)
        if self.controller_mode == "creative_search_profile_route_detour":
            return json.dumps(
                _creative_search_profile_route_detour_controller_decision(context),
                ensure_ascii=False,
            )
        if self.controller_mode == "creative_search_profile":
            return json.dumps(
                _creative_search_profile_controller_decision(context),
                ensure_ascii=False,
            )
        observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
        if int(observation.get("cycleIndex") or 0) > 0:
            return json.dumps(
                {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "finish",
                    "actionDirective": {"type": "finish", "assistantReply": "离线门禁执行完成。"},
                },
                ensure_ascii=False,
            )
        return json.dumps(
            _offline_strict_draft_decision(context, theme="北京地标"),
            ensure_ascii=False,
        )

    def generate(self, context: dict[str, Any]) -> str:
        return self._pop_payload(self.payloads, context, "Agent call")

    def generate_initial_plan(self, context: dict[str, Any]) -> str:
        if not self.initial_plan_payloads:
            raise AssertionError("offline eval provider received an unexpected initial plan call")
        raw = self._pop_payload(self.initial_plan_payloads, context, "initial plan call")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return json.dumps(_align_offline_initial_plan_payload(payload, context), ensure_ascii=False)

    def generate_candidate_hints(self, context: dict[str, Any], intent_pools: list[dict[str, Any]]) -> str:
        if not self.candidate_hint_payloads:
            raise AssertionError("offline eval provider received an unexpected candidate hints call")
        payload_context = {**context, "intentPools": intent_pools}
        return self._pop_payload(self.candidate_hint_payloads, payload_context, "candidate hints call")

    def _pop_payload(self, payloads: list[Union[dict[str, Any], str]], context: dict[str, Any], label: str) -> str:
        self.contexts.append(context)
        if not payloads:
            raise AssertionError(f"offline eval provider received an unexpected {label}")
        payload = payloads.pop(0)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)


def _offline_goal_requirements(context: dict[str, Any]) -> list[dict[str, Any]]:
    direct = context.get("goalRequirements") if isinstance(context.get("goalRequirements"), list) else []
    if direct:
        return [item for item in direct if isinstance(item, dict) and str(item.get("goalId") or "").strip()]
    contract = context.get("requestIntentContract") if isinstance(context.get("requestIntentContract"), dict) else {}
    return [
        item
        for item in contract.get("requiredIntents") or []
        if isinstance(item, dict) and str(item.get("goalId") or "").strip()
    ]


def _offline_goal_match_score(goal: dict[str, Any], text: str) -> int:
    normalized = str(text or "").replace("（", "(").replace("）", ")").lower()
    exact_entity = str(goal.get("exactEntity") or "").strip()
    intent_type = str(goal.get("intentType") or "").strip()
    score = 0
    if exact_entity and exact_entity.lower() in normalized:
        score += 100
    if intent_type == "museum" and any(marker in normalized for marker in ("博物院", "博物馆", "美术馆", "展览馆", "故宫")):
        score += 20
    if intent_type == "park" and any(marker in normalized for marker in ("公园", "园林", "景山", "颐和园", "圆明园")):
        score += 20
    if intent_type == "campus_visit" and any(marker in normalized for marker in ("大学", "学院", "高校", "校园", "校区")):
        score += 20
    if intent_type == "meal" and any(marker in normalized for marker in ("餐", "饭", "小吃", "美食", "菜")):
        score += 20
    if exact_entity:
        exact_tokens = [token for token in (exact_entity, exact_entity.replace("博物院", ""), exact_entity.replace("公园", "")) if token]
        if any(token.lower() and token.lower() in normalized for token in exact_tokens):
            score += 10
    return score


def _align_offline_initial_plan_payload(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("mode") or "") != "day_slots":
        return payload
    goal_requirements = _offline_goal_requirements(context)
    if not goal_requirements:
        return payload
    aligned = deepcopy(payload)
    slots = [item for item in aligned.get("daySlots") or [] if isinstance(item, dict)]
    pools = [item for item in aligned.get("intentPools") or [] if isinstance(item, dict)]
    slots_by_id = {
        str(item.get("slotId") or "").strip(): item
        for item in slots
        if str(item.get("slotId") or "").strip()
    }
    used_goal_ids: set[str] = set()
    kind_by_intent = {
        "campus_visit": "campus",
        "meal": "meal",
        "museum": "museum",
        "park": "park",
        "night_view": "night_view",
    }
    for pool in pools:
        assigned_slot_ids = [str(item) for item in pool.get("assignToSlots") or [] if str(item)]
        assigned_slots = [slots_by_id[slot_id] for slot_id in assigned_slot_ids if slot_id in slots_by_id]
        day_number = int((assigned_slots[0].get("dayNumber") if assigned_slots else 0) or 0)
        text = " ".join(
            str(part or "")
            for part in [
                pool.get("rawNeed"),
                *(pool.get("candidateHints") or []),
                *[slot.get("rawNeed") for slot in assigned_slots],
            ]
            if str(part or "").strip()
        )
        best_goal = None
        best_key = None
        for goal in goal_requirements:
            goal_id = str(goal.get("goalId") or "").strip()
            if not goal_id or goal_id in used_goal_ids:
                continue
            allowed_days = {
                int(item)
                for item in goal.get("allowedDayNumbers") or []
                if isinstance(item, int) and not isinstance(item, bool) and int(item) > 0
            }
            if allowed_days and day_number not in allowed_days:
                continue
            score = _offline_goal_match_score(goal, text)
            if score <= 0:
                continue
            key = (
                score,
                int(goal.get("requiredMin") or 0),
                len(str(goal.get("exactEntity") or "")),
                -len(goal_id),
            )
            if best_key is None or key > best_key:
                best_goal = goal
                best_key = key
        if best_goal is None:
            continue
        goal_id = str(best_goal.get("goalId") or "")
        intent_type = str(best_goal.get("intentType") or "")
        exact_entity = str(best_goal.get("exactEntity") or "").strip()
        used_goal_ids.add(goal_id)
        pool["goalId"] = goal_id
        pool["softGoalId"] = None
        pool["intentType"] = intent_type or str(pool.get("intentType") or "")
        pool["requirementLevel"] = "required"
        if exact_entity:
            pool["candidateHints"] = list(dict.fromkeys([exact_entity, *(pool.get("candidateHints") or [])]))
        for slot in assigned_slots:
            slot["kind"] = kind_by_intent.get(intent_type, slot.get("kind"))
            if exact_entity:
                slot["rawNeed"] = exact_entity
    return aligned


class OfflineToolLoopProvider:
    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.contexts: list[dict[str, Any]] = []

    def decide_autonomy_lite(self, context: dict[str, Any], *, timeout_seconds: float) -> str:
        assert timeout_seconds > 0
        return json.dumps(_offline_intent_contract(context), ensure_ascii=False)

    def run_tool_loop(self, context: dict[str, Any], registry) -> Any:
        from src.services.deepseek_agent_provider import AgentToolLoopResult

        self.contexts.append(context)
        raw_messages: list[dict[str, Any]] = []
        final_reply = ""
        for round_item in self.script:
            tool_calls = round_item.get("toolCalls") or []
            if tool_calls:
                raw_messages.append({"role": "assistant", "tool_calls": tool_calls})
                for index, call in enumerate(tool_calls):
                    tool_name = str(call.get("name") or call.get("toolName") or "")
                    arguments = call.get("arguments") or {}
                    registry.execute(str(call.get("id") or f"offline_call_{index}"), tool_name, arguments)
                continue
            final_reply = str(round_item.get("reply") or round_item.get("content") or "").strip()
            raw_messages.append({"role": "assistant", "content": final_reply})
        return AgentToolLoopResult(
            reply=final_reply or "工具调用已完成。",
            tool_events=registry.events,
            raw_messages=raw_messages,
        )


def _creative_search_profile_controller_decision(
    context: dict[str, Any],
) -> dict[str, Any]:
    requirements = [
        item
        for item in context.get("goalRequirements") or []
        if isinstance(item, dict) and str(item.get("goalId") or "").strip()
    ]
    hard_requirements = [
        item
        for item in requirements
        if str(item.get("requirementLevel") or "") == "hard" or int(item.get("requiredMin") or 0) > 0
    ]
    if not hard_requirements:
        hard_requirements = [
            item for item in requirements if str(item.get("intentType") or "") not in {"meal", "local_food"}
        ]
    hard_goal_ids = list(dict.fromkeys(str(item["goalId"]) for item in hard_requirements))
    if not hard_goal_ids:
        raise AssertionError("creative Search Profile offline case requires a real hard goal")
    decision = _offline_strict_draft_decision(
        context,
        theme="文化场馆与街区语义体验",
        hard_only=True,
        optional_experience_budget=2,
    )
    decision["actionDirective"]["goalPriority"] = hard_goal_ids
    decision["actionDirective"]["searchPriority"] = ["required", "semantic_family", "low_detour"]
    return decision


def _creative_search_profile_route_detour_controller_decision(
    context: dict[str, Any],
) -> dict[str, Any]:
    """Offline-only Controller stub for the dedicated opaque-choice case.

    The typed values originate here, as Controller output.  The case and the
    replay action know only a canonical fingerprint and never reconstruct a
    value from a label, ordinal, schema example, or server-side default.
    """

    checkpoint = context.get("clarificationCheckpoint")
    resolved_answers = checkpoint.get("resolvedAnswers") or [] if isinstance(checkpoint, dict) else []
    if any(
        isinstance(item, dict)
        and str(item.get("dimensionId") or "") == "route_decision.detour_tolerance"
        and str(item.get("source") or "") == "structured_option"
        for item in resolved_answers
    ):
        return _creative_search_profile_controller_decision(context)

    unresolved = [
        item
        for item in context.get("clarificationDimensions") or []
        if isinstance(item, dict) and str(item.get("status") or "unresolved") == "unresolved"
    ]
    if len(unresolved) != 1 or str(unresolved[0].get("dimensionId") or "") != ("route_decision.detour_tolerance"):
        raise AssertionError("dedicated offline Controller requires exactly one route-detour dimension")
    if unresolved[0].get("allowedSemanticFields") != [
        "detourTolerance",
        "adjacentLegConstraint",
    ]:
        raise AssertionError("dedicated route-detour dimension has an unexpected semantic schema")
    return {
        "schemaVersion": "agent-decision-v3",
        "primaryAction": "ask_user",
        "actionDirective": {
            "type": "ask_user",
            "question": "这次行程可接受多大的路线绕行？",
            "dimensionId": "route_decision.detour_tolerance",
            "whyItMatters": "该数值只用于比较录制 Provider 路线矩阵，不会直接修改时间轴。",
            "allowFreeText": False,
            "options": [
                {
                    "id": "bounded_detour",
                    "label": "较少绕行",
                    "semanticValue": {
                        "detourTolerance": {
                            "maxGeneralizedCostDelta": 27.5,
                            "maxDetourRatio": 0.23,
                        }
                    },
                    "allowsManualInput": False,
                },
                {
                    "id": "wider_detour",
                    "label": "可接受更多绕行",
                    "semanticValue": {
                        "detourTolerance": {
                            "maxGeneralizedCostDelta": 41.25,
                            "maxDetourRatio": 0.61,
                        }
                    },
                    "allowsManualInput": False,
                },
            ],
        },
    }


def run_offline_eval(
    cases_dir: Optional[Path] = None,
    database_url: Optional[str] = None,
    repeat: int = 1,
    dry_route_discovery: bool = False,
    capture_semantic_manifest: bool = False,
) -> dict[str, Any]:
    loaded_cases = _load_cases(cases_dir or CASES_DIR)
    if database_url is not None:
        return _run_with_database(
            loaded_cases,
            database_url,
            repeat=max(1, repeat),
            dry_route_discovery=dry_route_discovery,
            capture_semantic_manifest=capture_semantic_manifest,
        )
    with tempfile.TemporaryDirectory(prefix="trip-agent-eval-") as tmp_dir:
        db_path = Path(tmp_dir) / "offline_eval.db"
        return _run_with_database(
            loaded_cases,
            f"sqlite:///{db_path}",
            repeat=max(1, repeat),
            dry_route_discovery=dry_route_discovery,
            capture_semantic_manifest=capture_semantic_manifest,
        )


def _run_with_database(
    loaded_cases: list[dict[str, Any]],
    database_url: str,
    repeat: int,
    dry_route_discovery: bool = False,
    capture_semantic_manifest: bool = False,
) -> dict[str, Any]:
    offline_env_before = {key: os.environ.get(key) for key in _OFFLINE_ENV_KEYS}
    try:
        _configure_offline_env(database_url)
        return _run_with_database_configured(
            loaded_cases,
            repeat,
            dry_route_discovery=dry_route_discovery,
            capture_semantic_manifest=capture_semantic_manifest,
        )
    finally:
        for key, value in offline_env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_module = sys.modules.get("src.core.config")
        cached_settings = getattr(config_module, "get_settings", None)
        if cached_settings is not None:
            cached_settings.cache_clear()


def _run_with_database_configured(
    loaded_cases: list[dict[str, Any]],
    repeat: int,
    dry_route_discovery: bool = False,
    capture_semantic_manifest: bool = False,
) -> dict[str, Any]:

    from fastapi.testclient import TestClient

    from src.core.config import get_settings
    from src.core.database import sqlite_path_from_url
    from src.core.schema import initialize_database
    from src.main import app
    import src.providers.travel_tools as travel_tools_module
    import src.services.agent_service as agent_service_module
    import src.services.amap_poi_search_plan_adapter as amap_adapter_module
    import src.services.itinerary_service as itinerary_service_module
    import src.services.map_poi_service as map_poi_service_module
    import src.services.poi_discovery_service as poi_discovery_service_module
    from src.services.public_source_reader import PublicSourceReader
    from src.services.amap_call_budget import (
        current_amap_call_budget,
        current_amap_route_repair_scope,
    )
    from src.services.map_poi_service import clear_map_poi_runtime_state
    from src.services.route_service import RouteService

    get_settings.cache_clear()
    resolved_db = sqlite_path_from_url(get_settings().database_url)
    if resolved_db == PROJECT_ROOT / "trip_demo.db":
        raise RuntimeError("Offline eval refuses to use trip_demo.db")
    initialize_database()

    original_create_agent_provider = agent_service_module.create_agent_provider
    original_fetch_amap_place = map_poi_service_module.MapPoiService._fetch_amap_place
    original_fetch_amap_around = map_poi_service_module.MapPoiService._fetch_amap_around
    original_map_search = map_poi_service_module.MapPoiService.search
    original_map_search_nearby = map_poi_service_module.MapPoiService.search_nearby
    original_map_consume_budget = map_poi_service_module.MapPoiService._consume_budget_or_raise
    original_map_urlopen = map_poi_service_module.urlopen
    original_build_routes = RouteService.build_routes
    original_web_search = travel_tools_module.ResilientWebSearchProvider.search
    original_public_source_read = PublicSourceReader.read
    original_adapt_profile = amap_adapter_module.AmapPoiSearchPlanAdapter.adapt
    original_collect_profile_candidates = itinerary_service_module.ItineraryService._collect_search_profile_candidates
    original_poi_discover = poi_discovery_service_module.PoiDiscoveryService.discover
    provider_holder: dict[str, Any] = {}
    amap_holder: dict[str, Any] = {}
    web_holder: dict[str, Any] = {}
    adapter_holder: dict[str, Any] = {}
    phase1_holder: dict[str, Any] = {}

    phase1_param_allowlist = {
        "place/text": {
            "keywords",
            "city",
            "citylimit",
            "offset",
            "page",
            "extensions",
            "output",
            "types",
        },
        "place/around": {
            "keywords",
            "location",
            "city",
            "citylimit",
            "radius",
            "sortrule",
            "offset",
            "page",
            "extensions",
            "output",
            "types",
        },
    }
    phase1_secret_keys = {
        "key",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "proxy",
        "token",
    }

    def phase1_enabled() -> bool:
        return bool(phase1_holder.get("enabled"))

    def phase1_error(code: str) -> None:
        phase1_holder.setdefault("errors", []).append(str(code))

    def phase1_fingerprint(value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()

    def phase1_budget_used(snapshot: dict[str, Any], field: str) -> int:
        used = snapshot.get("used") if isinstance(snapshot.get("used"), dict) else {}
        return int(used.get(field, snapshot.get(field, 0)) or 0)

    def phase1_budget_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Project the complete non-secret acquisition state used by a receipt."""

        return {
            "source": str(snapshot.get("source") or ""),
            "budget": deepcopy(snapshot.get("budget") or {}),
            "used": deepcopy(snapshot.get("used") or {}),
            "cacheHitCount": int(snapshot.get("cacheHitCount") or 0),
            "duplicateExternalQueryCount": int(snapshot.get("duplicateExternalQueryCount") or 0),
            "reusedQueryCount": int(snapshot.get("reusedQueryCount") or 0),
            "newQueryCount": int(snapshot.get("newQueryCount") or 0),
            "skippedBecauseBudget": int(snapshot.get("skippedBecauseBudget") or 0),
        }

    def phase1_profile_occurrence(profile: Any) -> dict[str, Any]:
        occurrence = {
            "profileId": str(getattr(profile, "profileId", "") or ""),
            "profileFingerprint": str(getattr(profile, "profileFingerprint", "") or ""),
            "executionFingerprint": str(getattr(profile, "executionFingerprint", "") or ""),
            "briefId": str(getattr(profile, "briefId", "") or ""),
            "poolId": str(getattr(profile, "poolId", "") or ""),
            "planningSlotId": str(getattr(profile, "planningSlotId", "") or ""),
            "dayNumber": int(getattr(profile, "dayNumber", 0) or 0),
        }
        occurrence["occurrenceFingerprint"] = phase1_fingerprint(occurrence)
        return occurrence

    def phase1_anchor_payload(poi: Any) -> Optional[dict[str, Any]]:
        if poi is None:
            return None
        amap_id = str(getattr(poi, "amap_id", "") or "").strip().upper()
        source = str(getattr(poi, "source", "") or "")
        if source != "amap-place-search" or re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id) is None:
            return None
        try:
            longitude = round(float(getattr(poi, "longitude")), 6)
            latitude = round(float(getattr(poi, "latitude")), 6)
        except (TypeError, ValueError):
            return None
        return {"amapId": amap_id, "longitude": longitude, "latitude": latitude}

    def phase1_profile_scope(service: Any, profile: Any, slot_context: Any) -> dict[str, Any]:
        adapted = original_adapt_profile(amap_adapter_module.AmapPoiSearchPlanAdapter(), profile)
        route_scope = service._verified_profile_route_scope(profile, slot_context)
        route_plans = (
            service._route_scoped_profile_plans(adapted, route_scope, slot_context, profile=profile)
            if route_scope is not None and slot_context is not None
            else []
        )
        plans = [*route_plans, *adapted] if route_plans else list(adapted)
        return {
            "kind": "profile_execution",
            "profileOccurrence": phase1_profile_occurrence(profile),
            "plans": [item.model_dump(by_alias=True) for item in plans],
            "sourcePlans": [item.model_dump(by_alias=True) for item in profile.queryPlans],
            "routeScope": deepcopy(route_scope),
            "anchorLineage": {
                "slotId": str(getattr(slot_context, "slot_id", "") or "") if slot_context is not None else "",
                "previous": phase1_anchor_payload(getattr(slot_context, "previous_anchor", None)),
                "next": phase1_anchor_payload(getattr(slot_context, "next_anchor", None)),
                "queryScopeFingerprint": str((route_scope or {}).get("queryFingerprint") or ""),
            },
        }

    def phase1_collect_profile_candidates(service: Any, intent: Any, **kwargs: Any) -> Any:
        if not phase1_enabled() or getattr(intent, "search_profile", None) is None:
            return original_collect_profile_candidates(service, intent, **kwargs)
        context = phase1_profile_scope(
            service,
            intent.search_profile,
            kwargs.get("slot_context"),
        )
        token = _PHASE1_PROFILE_CONTEXT.set(context)
        try:
            return original_collect_profile_candidates(service, intent, **kwargs)
        except Exception as error:
            phase1_error("profile_candidate_collection_" + type(error).__name__.strip().casefold())
            raise
        finally:
            _PHASE1_PROFILE_CONTEXT.reset(token)

    def phase1_discover(service: Any, **kwargs: Any) -> Any:
        if not phase1_enabled() or kwargs.get("search_profile") is None:
            return original_poi_discover(service, **kwargs)
        profile = kwargs["search_profile"]
        if not hasattr(profile, "model_dump"):
            from src.models.poi_search_profile import PoiSearchProfile

            profile = PoiSearchProfile.model_validate(profile)
        plans = original_adapt_profile(amap_adapter_module.AmapPoiSearchPlanAdapter(), profile)
        source_plan = next(
            (item for item in profile.queryPlans if item.mode == "web_seed_then_amap"),
            profile.queryPlans[0],
        )
        provider_plan = next(
            (item for item in plans if item.sourcePlanId == source_plan.planId),
            None,
        )
        context = {
            "kind": "web_seed_execution",
            "profileOccurrence": phase1_profile_occurrence(profile),
            "plans": [item.model_dump(by_alias=True) for item in plans],
            "sourcePlans": [item.model_dump(by_alias=True) for item in profile.queryPlans],
            "sourcePlan": source_plan.model_dump(by_alias=True),
            "providerPlan": provider_plan.model_dump(by_alias=True) if provider_plan is not None else None,
        }
        token = _PHASE1_PROFILE_CONTEXT.set(context)
        try:
            return original_poi_discover(service, **kwargs)
        finally:
            _PHASE1_PROFILE_CONTEXT.reset(token)

    def phase1_sanitized_request(endpoint: str, url: str) -> dict[str, str]:
        parsed = urlparse(url)
        expected_path = f"/v3/{endpoint}"
        if parsed.scheme != "https" or parsed.netloc != "restapi.amap.com" or parsed.path != expected_path:
            phase1_error("place_request_endpoint_invalid")
            raise AssertionError("Phase-1 Place request endpoint is not the production AMap endpoint")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        keys = [str(key).casefold() for key, _value in pairs]
        if len(keys) != len(set(keys)):
            phase1_error("place_request_duplicate_parameter")
            raise AssertionError("Phase-1 Place request contains duplicate parameters")
        unknown = set(keys) - phase1_param_allowlist[endpoint] - {"key"}
        if unknown:
            phase1_error("place_request_parameter_not_allowlisted")
            raise AssertionError("Phase-1 Place request contains a non-allowlisted parameter")
        secret_aliases = set(keys) & phase1_secret_keys
        if secret_aliases != {"key"}:
            phase1_error("place_request_secret_parameter_invalid")
            raise AssertionError("Phase-1 Place request key boundary is invalid")
        sanitized = {str(key): str(value) for key, value in pairs if str(key).casefold() != "key"}
        if set(sanitized) - phase1_param_allowlist[endpoint]:
            phase1_error("place_request_parameter_not_allowlisted")
            raise AssertionError("Phase-1 Place request sanitization failed closed")
        return dict(sorted(sanitized.items()))

    def phase1_local_urlopen(request: Any, timeout: float | None = None) -> _OfflineRecordedAmapResponse:
        del timeout
        if not phase1_enabled():
            raise AssertionError("offline eval blocked an unscoped AMap transport request")
        url = request.full_url if hasattr(request, "full_url") else str(request)
        parsed = urlparse(url)
        endpoint = {
            "/v3/place/text": "place/text",
            "/v3/place/around": "place/around",
        }.get(parsed.path)
        context = _PHASE1_REQUEST_CONTEXT.get()
        low_level_fetch = _PHASE1_LOW_LEVEL_FETCH_CONTEXT.get()
        if endpoint is None or not isinstance(context, dict) or context.get("endpoint") != endpoint:
            phase1_error("place_request_fetch_context_missing")
            raise AssertionError("Phase-1 low-level fetch has no exact high-level request binding")
        expected_method = "_fetch_amap_place" if endpoint == "place/text" else "_fetch_amap_around"
        if (
            not isinstance(low_level_fetch, dict)
            or low_level_fetch.get("consumed")
            or str(low_level_fetch.get("endpoint") or "") != endpoint
            or str(low_level_fetch.get("productionMethod") or "") != expected_method
        ):
            phase1_error("production_low_level_fetch_provenance_missing")
            raise AssertionError("Phase-1 request did not enter the production MapPoiService low-level fetch")
        marker_material = {
            key: value for key, value in low_level_fetch.items() if key not in {"markerFingerprint", "consumed"}
        }
        if str(low_level_fetch.get("markerFingerprint") or "") != phase1_fingerprint(marker_material):
            phase1_error("production_low_level_fetch_provenance_invalid")
            raise AssertionError("Phase-1 production low-level fetch marker is invalid")
        receipt = context.get("budgetReceipt")
        if not isinstance(receipt, dict) or receipt.get("consumed"):
            phase1_error("place_request_budget_receipt_missing_or_reused")
            raise AssertionError("Phase-1 low-level fetch lacks a fresh budget receipt")
        sanitized = phase1_sanitized_request(endpoint, url)
        if sanitized != context.get("expectedSanitizedParams"):
            phase1_error("place_request_budget_fetch_parameter_mismatch")
            raise AssertionError("Phase-1 low-level fetch differs from the budget-acquired request")
        if endpoint == "place/around":
            anchor = context.get("anchorLineage") or {}
            center = context.get("requestedCenter") or {}
            expected_location = str(center.get("location") or "")
            if (
                not sanitized.get("location")
                or sanitized.get("location") != expected_location
                or not sanitized.get("radius")
                or not anchor.get("queryScopeFingerprint")
            ):
                phase1_error("place_around_request_lineage_mismatch")
                raise AssertionError("Phase-1 place/around center, radius, or anchor lineage is invalid")
        phase1_holder["eventOrdinal"] = int(phase1_holder.get("eventOrdinal") or 0) + 1
        receipt["fetchOrdinal"] = int(phase1_holder["eventOrdinal"])
        if not (
            int(receipt.get("acquisitionOrdinal") or 0)
            < int(low_level_fetch.get("enteredOrdinal") or 0)
            < int(receipt["fetchOrdinal"])
        ):
            phase1_error("production_low_level_fetch_order_invalid")
            raise AssertionError("Phase-1 production low-level fetch order is invalid")
        request_material = {"endpoint": endpoint, "sanitizedParams": sanitized}
        request_fingerprint = phase1_fingerprint(request_material)
        occurrence = deepcopy(context.get("profileOccurrence") or {})
        query_plan_lineage = deepcopy(context.get("queryPlanLineage") or {})
        expected_receipt_binding = {
            "endpoint": endpoint,
            "requestFingerprint": request_fingerprint,
            "profileOccurrenceFingerprint": phase1_fingerprint(occurrence),
            "queryPlanLineageFingerprint": phase1_fingerprint(query_plan_lineage),
            "queryScopeFingerprint": str(query_plan_lineage.get("queryScopeFingerprint") or ""),
        }
        if any(str(receipt.get(key) or "") != str(value) for key, value in expected_receipt_binding.items()):
            phase1_error("place_request_budget_receipt_binding_mismatch")
            raise AssertionError("Phase-1 budget receipt binding changed before fetch")
        receipt_material = {
            key: value for key, value in receipt.items() if key not in {"receiptFingerprint", "consumed"}
        }
        receipt["receiptFingerprint"] = phase1_fingerprint(receipt_material)
        receipt["consumed"] = True
        low_level_fetch["consumed"] = True
        evidence: dict[str, Any] = {
            **request_material,
            "requestFingerprint": request_fingerprint,
            "profileOccurrence": occurrence,
            "queryPlanLineage": query_plan_lineage,
            "budgetReceipt": {key: value for key, value in receipt.items() if key != "consumed"},
            "productionLowLevelFetch": {key: value for key, value in low_level_fetch.items() if key != "consumed"},
        }
        if endpoint == "place/around":
            evidence["anchorLineage"] = deepcopy(context.get("anchorLineage") or {})
        evidence["auditFingerprint"] = phase1_fingerprint(evidence)
        phase1_holder.setdefault("requests", []).append(evidence)
        amap_holder.setdefault("calls", []).append(deepcopy(evidence))
        return _OfflineRecordedAmapResponse(_fake_amap_place(amap_holder.get("responses") or {}, sanitized))

    def phase1_budget_receipt(
        service: Any,
        source: str,
        params: dict[str, str],
        category: str,
        *,
        query_scope_fingerprint: Optional[str] = None,
        query_variant_fingerprint: Optional[str] = None,
    ) -> None:
        if not phase1_enabled() or source not in {"place/text", "place/around"}:
            return original_map_consume_budget(
                service,
                source,
                params,
                category,
                query_scope_fingerprint=query_scope_fingerprint,
                query_variant_fingerprint=query_variant_fingerprint,
            )
        context = _PHASE1_REQUEST_CONTEXT.get()
        budget = current_amap_call_budget()
        if not isinstance(context, dict) or context.get("endpoint") != source or budget is None:
            phase1_error("place_request_budget_context_missing")
            raise AssertionError("Phase-1 Place request has no authoritative budget context")
        parameter_names = {str(key).casefold() for key in params}
        unknown_parameters = parameter_names - phase1_param_allowlist[source] - {"key"}
        secret_parameters = parameter_names & phase1_secret_keys
        if unknown_parameters or secret_parameters != {"key"}:
            phase1_error("place_request_budget_parameters_invalid")
            raise AssertionError("Phase-1 budget acquisition received unsafe parameters")
        expected_sanitized_params = dict(
            sorted((str(key), str(value)) for key, value in params.items() if str(key).casefold() != "key")
        )
        context["expectedSanitizedParams"] = expected_sanitized_params
        before_snapshot = budget.snapshot()
        expected_query_scope = str((context.get("queryPlanLineage") or {}).get("queryScopeFingerprint") or "")
        if str(query_scope_fingerprint or "") != expected_query_scope:
            phase1_error("place_request_query_scope_mismatch")
            raise AssertionError("Phase-1 Place request query scope does not match its plan lineage")
        original_map_consume_budget(
            service,
            source,
            params,
            category,
            query_scope_fingerprint=query_scope_fingerprint,
            query_variant_fingerprint=query_variant_fingerprint,
        )
        after_snapshot = budget.snapshot()
        before = {
            "usedCalls": phase1_budget_used(before_snapshot, "usedTotalExternal"),
            "newQueryCalls": int(before_snapshot.get("newQueryCount") or 0),
            "textSearchCalls": phase1_budget_used(before_snapshot, "usedPlaceText"),
            "aroundSearchCalls": phase1_budget_used(before_snapshot, "usedPlaceAround"),
        }
        after = {
            "usedCalls": phase1_budget_used(after_snapshot, "usedTotalExternal"),
            "newQueryCalls": int(after_snapshot.get("newQueryCount") or 0),
            "textSearchCalls": phase1_budget_used(after_snapshot, "usedPlaceText"),
            "aroundSearchCalls": phase1_budget_used(after_snapshot, "usedPlaceAround"),
        }
        expected_field = "textSearchCalls" if source == "place/text" else "aroundSearchCalls"
        other_field = "aroundSearchCalls" if source == "place/text" else "textSearchCalls"
        if (
            after["usedCalls"] - before["usedCalls"] != 1
            or after["newQueryCalls"] - before["newQueryCalls"] != 1
            or after[expected_field] - before[expected_field] != 1
            or after[other_field] != before[other_field]
        ):
            phase1_error("place_request_budget_delta_invalid")
            raise AssertionError("Phase-1 Place request budget acquisition delta is invalid")
        object_key = id(budget)
        budget_objects = phase1_holder.setdefault("budgetObjects", {})
        budget_label = budget_objects.setdefault(object_key, f"budget-object-{len(budget_objects) + 1}")
        phase1_holder["eventOrdinal"] = int(phase1_holder.get("eventOrdinal") or 0) + 1
        context["budgetReceipt"] = {
            "budgetObjectId": budget_label,
            "acquired": True,
            "endpoint": source,
            "requestFingerprint": phase1_fingerprint(
                {
                    "endpoint": source,
                    "sanitizedParams": expected_sanitized_params,
                }
            ),
            "profileOccurrenceFingerprint": phase1_fingerprint(context.get("profileOccurrence") or {}),
            "queryPlanLineageFingerprint": phase1_fingerprint(context.get("queryPlanLineage") or {}),
            "queryScopeFingerprint": str((context.get("queryPlanLineage") or {}).get("queryScopeFingerprint") or ""),
            "queryVariantFingerprint": str(query_variant_fingerprint or "") or None,
            "before": before,
            "after": after,
            "beforeSnapshot": phase1_budget_snapshot(before_snapshot),
            "afterSnapshot": phase1_budget_snapshot(after_snapshot),
            "acquisitionOrdinal": int(phase1_holder["eventOrdinal"]),
            "fetchOrdinal": 0,
        }

    def phase1_match_plan(
        *,
        endpoint: str,
        city: str,
        keyword: str,
        category: str,
        limit: int,
        radius: Optional[int],
        query_scope_fingerprint: Optional[str],
    ) -> dict[str, Any]:
        profile_context = _PHASE1_PROFILE_CONTEXT.get()
        if not isinstance(profile_context, dict):
            phase1_error("place_request_profile_context_missing")
            raise AssertionError("Phase-1 Place request has no active Search Profile occurrence")
        if profile_context.get("kind") == "web_seed_execution":
            phase1_error("web_seed_dynamic_query_lineage_unclosed")
            raise AssertionError("Phase-1 Web-seed Place request cannot be frozen without seed lineage")
        matches = [
            item
            for item in profile_context.get("plans") or []
            if isinstance(item, dict)
            and str(item.get("endpoint") or "") == endpoint
            and str(item.get("city") or "") == str(city)
            and str(item.get("keyword") or "") == str(keyword)
            and str(item.get("category") or "") == str(category)
            and int(item.get("resultLimit") or 0) == int(limit)
            and (endpoint != "place/around" or int(item.get("radiusMeters") or 0) == int(radius or 0))
        ]
        if len(matches) != 1:
            phase1_error("place_request_query_plan_match_not_unique")
            raise AssertionError(f"Phase-1 Place request matched {len(matches)} provider query plans")
        plan = matches[0]
        source_plan = next(
            (
                item
                for item in profile_context.get("sourcePlans") or []
                if isinstance(item, dict) and str(item.get("planId") or "") == str(plan.get("sourcePlanId") or "")
            ),
            None,
        )
        if not isinstance(source_plan, dict):
            phase1_error("place_request_source_plan_missing")
            raise AssertionError("Phase-1 Place request has no exact source query-plan identity")
        query_scope = str(query_scope_fingerprint or "")
        occurrence = deepcopy(profile_context.get("profileOccurrence") or {})
        source_plan_fingerprint = phase1_fingerprint(source_plan)
        provider_plan_fingerprint = phase1_fingerprint(plan)
        lineage_scope = query_scope or phase1_fingerprint(
            {
                "occurrenceFingerprint": str(occurrence.get("occurrenceFingerprint") or ""),
                "sourcePlanFingerprint": source_plan_fingerprint,
                "providerPlanFingerprint": provider_plan_fingerprint,
                "requestShape": {
                    "endpoint": endpoint,
                    "city": str(city),
                    "keyword": str(keyword),
                    "category": str(category),
                    "limit": int(limit),
                    "radius": int(radius or 0),
                },
            }
        )
        anchor_lineage = deepcopy(profile_context.get("anchorLineage") or {})
        if endpoint == "place/around" and (
            not query_scope
            or query_scope != str(anchor_lineage.get("queryScopeFingerprint") or "")
            or not anchor_lineage.get("slotId")
            or not (anchor_lineage.get("previous") or anchor_lineage.get("next"))
        ):
            phase1_error("place_around_anchor_lineage_incomplete")
            raise AssertionError("Phase-1 place/around request lacks verified anchor/query-scope lineage")
        return {
            "profileOccurrence": occurrence,
            "queryPlanLineage": {
                "sourcePlanId": str(plan.get("sourcePlanId") or ""),
                "sourcePlanFingerprint": source_plan_fingerprint,
                "providerPlanId": str(plan.get("planId") or ""),
                "providerPlanFingerprint": provider_plan_fingerprint,
                "queryScopeFingerprint": lineage_scope,
            },
            "anchorLineage": anchor_lineage if endpoint == "place/around" else None,
        }

    def create_provider() -> Any:
        provider = provider_holder.get("provider")
        if provider is None:
            raise AssertionError("offline eval case did not configure a provider")
        return provider

    def phase1_production_fetch(
        *,
        endpoint: str,
        production_method: str,
        original_fetch: Any,
        service: Any,
        params: dict[str, str],
    ) -> dict[str, Any]:
        if not phase1_enabled():
            return original_fetch(service, params)
        context = _PHASE1_REQUEST_CONTEXT.get()
        receipt = context.get("budgetReceipt") if isinstance(context, dict) else None
        if not isinstance(receipt, dict) or str(receipt.get("endpoint") or "") != endpoint:
            phase1_error("production_low_level_fetch_budget_receipt_missing")
            raise AssertionError("Phase-1 production low-level fetch lacks the acquired budget receipt")
        phase1_holder["eventOrdinal"] = int(phase1_holder.get("eventOrdinal") or 0) + 1
        marker = {
            "endpoint": endpoint,
            "productionMethod": production_method,
            "acquisitionOrdinal": int(receipt.get("acquisitionOrdinal") or 0),
            "enteredOrdinal": int(phase1_holder["eventOrdinal"]),
            "profileOccurrenceFingerprint": str(receipt.get("profileOccurrenceFingerprint") or ""),
            "queryPlanLineageFingerprint": str(receipt.get("queryPlanLineageFingerprint") or ""),
        }
        marker["markerFingerprint"] = phase1_fingerprint(marker)
        marker["consumed"] = False
        token = _PHASE1_LOW_LEVEL_FETCH_CONTEXT.set(marker)
        try:
            response = original_fetch(service, params)
            if not marker.get("consumed"):
                phase1_error("production_low_level_fetch_transport_not_observed")
                raise AssertionError("Phase-1 production low-level fetch did not reach the local transport")
            return response
        finally:
            _PHASE1_LOW_LEVEL_FETCH_CONTEXT.reset(token)

    def fetch_amap_place(service, params: dict[str, str]) -> dict[str, Any]:
        # Phase 1 executes the production request constructor and parser while
        # ``map_poi_service.urlopen`` is atomically pinned to the local
        # fail-closed transport installed below. No socket can be reached.
        if phase1_enabled():
            return phase1_production_fetch(
                endpoint="place/text",
                production_method="_fetch_amap_place",
                original_fetch=original_fetch_amap_place,
                service=service,
                params=params,
            )
        amap_holder.setdefault("calls", []).append({"endpoint": "place/text", "params": dict(params)})
        return _fake_amap_place(amap_holder.get("responses") or {}, params)

    def fetch_amap_around(service, params: dict[str, str]) -> dict[str, Any]:
        if phase1_enabled():
            return phase1_production_fetch(
                endpoint="place/around",
                production_method="_fetch_amap_around",
                original_fetch=original_fetch_amap_around,
                service=service,
                params=params,
            )
        amap_holder.setdefault("calls", []).append({"endpoint": "place/around", "params": dict(params)})
        return _fake_amap_place(amap_holder.get("responses") or {}, params)

    def record_map_search(
        service,
        city: str,
        keyword: str = "",
        category: str = "all",
        limit: int = 12,
        bypass_cache: bool = False,
        *,
        query_scope_fingerprint: Optional[str] = None,
    ) -> Any:
        amap_holder.setdefault("searchCalls", []).append(
            {
                "city": str(city),
                "keyword": str(keyword),
                "category": str(category),
                "limit": int(limit),
                "bypassCache": bool(bypass_cache),
                "queryScopeFingerprint": str(query_scope_fingerprint or ""),
            }
        )
        token = None
        if phase1_enabled():
            token = _PHASE1_REQUEST_CONTEXT.set(
                {
                    "endpoint": "place/text",
                    **phase1_match_plan(
                        endpoint="place/text",
                        city=city,
                        keyword=keyword,
                        category=category,
                        limit=limit,
                        radius=None,
                        query_scope_fingerprint=query_scope_fingerprint,
                    ),
                }
            )
        try:
            return original_map_search(
                service,
                city,
                keyword,
                category,
                limit,
                bypass_cache,
                query_scope_fingerprint=query_scope_fingerprint,
            )
        finally:
            if token is not None:
                _PHASE1_REQUEST_CONTEXT.reset(token)

    def record_map_search_nearby(
        service: Any,
        city: str,
        longitude: float,
        latitude: float,
        keyword: str,
        category: str = "all",
        radius: int = 1500,
        limit: int = 12,
        bypass_cache: bool = False,
        *,
        query_scope_fingerprint: Optional[str] = None,
    ) -> Any:
        amap_holder.setdefault("searchCalls", []).append(
            {
                "endpoint": "place/around",
                "city": str(city),
                "keyword": str(keyword),
                "category": str(category),
                "radius": int(radius),
                "limit": int(limit),
                "bypassCache": bool(bypass_cache),
                "queryScopeFingerprint": str(query_scope_fingerprint or ""),
            }
        )
        token = None
        if phase1_enabled():
            binding = phase1_match_plan(
                endpoint="place/around",
                city=city,
                keyword=keyword,
                category=category,
                limit=limit,
                radius=radius,
                query_scope_fingerprint=query_scope_fingerprint,
            )
            binding["requestedCenter"] = {
                "longitude": round(float(longitude), 6),
                "latitude": round(float(latitude), 6),
                "location": f"{longitude},{latitude}",
            }
            token = _PHASE1_REQUEST_CONTEXT.set({"endpoint": "place/around", **binding})
        try:
            return original_map_search_nearby(
                service,
                city,
                longitude,
                latitude,
                keyword,
                category,
                radius,
                limit,
                bypass_cache,
                query_scope_fingerprint=query_scope_fingerprint,
            )
        finally:
            if token is not None:
                _PHASE1_REQUEST_CONTEXT.reset(token)

    def recorded_web_search(
        service,
        query: str,
        count: int = 5,
        freshness: str = "noLimit",
    ) -> Any:
        raw_results = _recorded_web_results(
            web_holder.get("responses") or {},
            query,
        )

        web_holder.setdefault("calls", []).append(
            {
                "query": str(query),
                "count": int(count),
                "freshness": str(freshness),
                "resultCount": len(raw_results),
                "seedNames": [
                    _recorded_web_seed_name(item.get("title"))
                    for item in raw_results
                    if _recorded_web_seed_name(item.get("title"))
                ],
            }
        )
        items = [
            travel_tools_module.WebSearchItem(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or ""),
                source_name=str(item.get("sourceName") or "recorded-fixture"),
                credibility_rank=str(item.get("credibilityRank") or "recorded"),
                provider_name="recorded-offline-web-search",
                confidence=float(item.get("confidence") or 0.95),
                published_at=(str(item.get("publishedAt") or "") or None),
            )
            for item in raw_results
            if isinstance(item, dict) and str(item.get("title") or "").strip() and str(item.get("url") or "").strip()
        ]
        return travel_tools_module.WebSearchResponse(
            query=str(query),
            results=items,
            confidence=0.95 if items else 0.0,
            provider_name="recorded-offline-web-search",
            failure_reason=None if items else "offline_recorded_web_no_match",
            user_visible_caveat="离线录制搜索边界；不是实时网页结果。",
            provider_diagnostics=[
                {
                    "provider": "recorded-offline-web-search",
                    "status": "success" if items else "failed",
                    "resultCount": len(items),
                }
            ],
            attempted_providers=["recorded-offline-web-search"],
            successful_providers=(["recorded-offline-web-search"] if items else []),
            failed_providers=([] if items else ["recorded-offline-web-search"]),
        )

    def recorded_source_read(service, url: str, *, deadline=None) -> dict[str, Any]:
        document = (web_holder.get("sourceDocuments") or {}).get(url)
        if not isinstance(document, dict):
            return {"status": "failed", "reason": "offline_source_document_missing"}
        body = str(document.get("bodyText") or "")
        return {"status": "succeeded", "reason": None, "canonicalUrl": url,
                "title": str(document.get("title") or "Offline document fixture"),
                "bodyText": body, "contentFingerprint": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "contentKind": "article", "fetchedAt": "2026-09-06T00:00:00+00:00"}

    def recorded_routes(
        service,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        recording = amap_holder.get("routeRecording") or {}
        records = recording.get("records") or []
        segment_by_id = {str(segment.id): segment for segment in segments or []}
        poi_by_id = {str(poi.id): poi for poi in pois or []}
        requested_pairs = set(route_pairs or [])
        if not requested_pairs:
            requested_pairs = {
                (str(left.id), str(right.id)) for left, right in zip(segments or [], (segments or [])[1:])
            }
        routes = []
        for left_id, right_id in sorted(requested_pairs):
            left = segment_by_id.get(str(left_id))
            right = segment_by_id.get(str(right_id))
            if left is None or right is None:
                continue
            left_poi = poi_by_id.get(str(left.poi_id))
            right_poi = poi_by_id.get(str(right.poi_id))
            if left_poi is None or right_poi is None:
                continue
            if not service._is_routeable_poi(left_poi) or not service._is_routeable_poi(right_poi):
                # Match the production RouteService boundary: a skipped,
                # unrouteable endpoint is not an actual external request.
                continue
            mode = str(transport_mode)
            from_amap_id = str(getattr(left_poi, "amap_id", "") or "")
            to_amap_id = str(getattr(right_poi, "amap_id", "") or "")
            dry_route_discovery = bool(amap_holder.get("dryRouteDiscovery"))
            if dry_route_discovery:
                amap_holder.setdefault("routeDiscovery", []).append(
                    _route_discovery_entry(
                        case_id=str(amap_holder.get("caseId") or ""),
                        plan_id=str(plan_id),
                        from_segment_id=str(left_id),
                        to_segment_id=str(right_id),
                        from_poi=left_poi,
                        to_poi=right_poi,
                        mode=mode,
                        route_budget=current_amap_call_budget(),
                        uses_offline_mock_amap=bool(amap_holder.get("usesOfflineMockAmap")),
                        repair_scope_certificate=current_amap_route_repair_scope(),
                    )
                )
                # A dry manifest must never replay a route response or emit
                # recordedProviderEvidence, even if the case names a fixture.
                continue
            replay_failure = _recorded_route_replay_failure(
                left_poi,
                right_poi,
                dry_route_discovery=False,
            )
            if replay_failure:
                amap_holder.setdefault("calls", []).append(
                    {
                        "endpoint": "route/recorded-real",
                        "params": {
                            "fromSegmentId": str(left_id),
                            "toSegmentId": str(right_id),
                            "fromAmapId": from_amap_id,
                            "toAmapId": to_amap_id,
                            "mode": mode,
                        },
                        "fixture": recording.get("fixture"),
                        "recordingType": recording.get("recordingType"),
                        "status": "failed",
                        "failure": replay_failure,
                    }
                )
                continue
            if not records:
                # Discovery above is not a replay hit.  Keep normal mode fail
                # closed and do not manufacture a route from the requested pair.
                amap_holder.setdefault("calls", []).append(
                    {
                        "endpoint": "route/recorded-real",
                        "params": {
                            "fromSegmentId": str(left_id),
                            "toSegmentId": str(right_id),
                            "fromAmapId": from_amap_id,
                            "toAmapId": to_amap_id,
                            "mode": mode,
                        },
                        "fixture": recording.get("fixture"),
                        "recordingType": recording.get("recordingType"),
                        "status": "failed",
                        "failure": recording.get("failure") or "recorded_route_pair_not_found",
                    }
                )
                continue
            response, failure = _recorded_route_response(
                records,
                from_amap_id=from_amap_id,
                to_amap_id=to_amap_id,
                mode=mode,
            )
            call = {
                "endpoint": "route/recorded-real",
                "params": {
                    "fromSegmentId": str(left_id),
                    "toSegmentId": str(right_id),
                    "fromAmapId": from_amap_id,
                    "toAmapId": to_amap_id,
                    "mode": mode,
                },
                "fixture": recording.get("fixture"),
                "recordingType": recording.get("recordingType"),
            }
            if response is None:
                call["status"] = "failed"
                call["failure"] = failure
                amap_holder.setdefault("calls", []).append(call)
                continue
            route = service._route_from_payload(
                str(plan_id),
                1,
                1,
                left_poi,
                right_poi,
                mode,
                response,
                from_segment_id=str(left_id),
                to_segment_id=str(right_id),
            )
            route.is_selected = True
            route.provider_payload = {
                **dict(route.provider_payload or {}),
                "recordedProviderEvidence": {
                    "fixture": recording.get("fixture"),
                    "recordingType": recording.get("recordingType"),
                    "recordedAt": recording.get("recordedAt"),
                    "requestPair": {
                        "fromAmapId": from_amap_id,
                        "toAmapId": to_amap_id,
                        "mode": mode,
                    },
                    "responseSha256": _canonical_response_sha256(response),
                },
            }
            call["status"] = "success"
            call["responseSha256"] = _canonical_response_sha256(response)
            amap_holder.setdefault("calls", []).append(call)
            routes.append(route)
        return routes

    def adapt_profile(adapter, profile):
        adapted = original_adapt_profile(adapter, profile)
        adapter_holder.setdefault("calls", []).append(
            {
                "profile": profile.model_dump(by_alias=True),
                "adaptedPlans": [item.model_dump(by_alias=True) for item in adapted],
            }
        )
        return adapted

    agent_service_module.create_agent_provider = create_provider
    # The production low-level methods used in Phase 1 can only reach this
    # local response transport; both installation and restoration are owned by
    # this eval scope.
    map_poi_service_module.urlopen = phase1_local_urlopen
    map_poi_service_module.MapPoiService._fetch_amap_place = fetch_amap_place
    map_poi_service_module.MapPoiService._fetch_amap_around = fetch_amap_around
    map_poi_service_module.MapPoiService.search = record_map_search
    map_poi_service_module.MapPoiService.search_nearby = record_map_search_nearby
    map_poi_service_module.MapPoiService._consume_budget_or_raise = phase1_budget_receipt
    itinerary_service_module.ItineraryService._collect_search_profile_candidates = phase1_collect_profile_candidates
    poi_discovery_service_module.PoiDiscoveryService.discover = phase1_discover
    RouteService.build_routes = recorded_routes
    travel_tools_module.ResilientWebSearchProvider.search = recorded_web_search
    PublicSourceReader.read = recorded_source_read
    amap_adapter_module.AmapPoiSearchPlanAdapter.adapt = adapt_profile
    try:
        results = []
        with TestClient(app) as client:
            for iteration in range(repeat):
                for case in loaded_cases:
                    _configure_case_env(case)
                    get_settings.cache_clear()
                    _clear_database(resolved_db)
                    clear_map_poi_runtime_state()
                    RouteService.clear_cache()
                    provider_holder["provider"] = _provider_from_case(case)
                    amap_holder["responses"] = case.get("mockAmapResponses") or {}
                    amap_holder["calls"] = []
                    amap_holder["searchCalls"] = []
                    amap_holder["routeDiscovery"] = []
                    amap_holder["dryRouteDiscovery"] = bool(dry_route_discovery)
                    amap_holder["caseId"] = str(case.get("id") or "")
                    amap_holder["usesOfflineMockAmap"] = bool(case.get("mockAmapResponses"))
                    amap_holder["routeRecording"] = _recorded_route_fixture(case)
                    web_holder["responses"] = case.get("recordedWebSearchResponses") or {}
                    web_holder["sourceDocuments"] = case.get("recordedSourceDocuments") or {}
                    web_holder["calls"] = []
                    adapter_holder["calls"] = []
                    phase1_holder.clear()
                    phase1_holder.update(
                        {
                            "enabled": bool(capture_semantic_manifest and dry_route_discovery),
                            "requests": [],
                            "errors": [],
                            "budgetObjects": {},
                            "eventOrdinal": 0,
                        }
                    )
                    runtime_trace = {
                        "webCalls": web_holder["calls"],
                        "amapCalls": amap_holder["calls"],
                        "amapSearchCalls": amap_holder["searchCalls"],
                        "adapterCalls": adapter_holder["calls"],
                        "realExternalCalls": [],
                        "recordedFixtureCaptureSessions": [],
                        "phase1PlaceRequestEvidence": phase1_holder["requests"],
                        "phase1PlaceRequestErrors": phase1_holder["errors"],
                    }
                    offline_isolation = {
                        "controllerFactory": agent_service_module.create_agent_provider is create_provider,
                        "amapTextTransport": map_poi_service_module.MapPoiService._fetch_amap_place is fetch_amap_place,
                        "amapAroundTransport": map_poi_service_module.MapPoiService._fetch_amap_around
                        is fetch_amap_around,
                        "routeTransport": RouteService.build_routes is recorded_routes,
                        "webSearchTransport": travel_tools_module.ResilientWebSearchProvider.search
                        is recorded_web_search,
                        "controllerProvider": isinstance(provider_holder["provider"], OfflineAgentProvider),
                        "phase1LocalAmapTransport": map_poi_service_module.urlopen is phase1_local_urlopen,
                    }
                    strict_fixture = case.get("recordedAmapReplayFixture")
                    strict_enabled = bool(str(strict_fixture or "").strip()) and not dry_route_discovery
                    replay_scope = (
                        _offline_strict_recorded_amap_replay_scope(
                            strict_fixture,
                            case_id=str(case.get("id") or ""),
                            expected_record_keys=case.get("recordedAmapReplayExpectedRecords"),
                            production_map_search=original_map_search,
                            production_map_fetch_place=original_fetch_amap_place,
                            production_map_fetch_around=original_fetch_amap_around,
                            production_build_routes=original_build_routes,
                        )
                        if strict_enabled
                        else nullcontext(None)
                    )
                    with (
                        _offline_external_network_sentinel(runtime_trace) as network_sentinel,
                        replay_scope as strict_transport,
                    ):
                        offline_isolation["globalNetworkSentinel"] = bool(network_sentinel["active"])
                        if not all(offline_isolation.values()):
                            raise AssertionError("offline eval external-provider isolation is incomplete")
                        runtime_trace["offlineProviderIsolation"] = offline_isolation
                        result = _run_case(
                            client,
                            case,
                            provider_holder["provider"],
                            resolved_db,
                            runtime_trace,
                        )
                        if strict_transport is not None:
                            _strict_replay_result_failure(result, strict_transport)
                    if dry_route_discovery:
                        result["recordedRouteDiscovery"] = _summarize_route_discovery(
                            list(amap_holder["routeDiscovery"])
                        )
                    if capture_semantic_manifest:
                        result["recordedCaptureSemanticTrace"] = _capture_semantic_trace(
                            case=case,
                            runtime_trace=runtime_trace,
                            route_discovery=result.get("recordedRouteDiscovery"),
                            case_result=result,
                        )
                    if repeat > 1:
                        result["iteration"] = iteration + 1
                    results.append(result)
        if not any(
            case.get("id") == REQUIRED_SEARCH_PROFILE_CASE_ID
            and isinstance(case.get("searchProfileEval"), dict)
            and case["searchProfileEval"].get("required") is True
            for case in loaded_cases
        ):
            results.append(
                _result(
                    REQUIRED_SEARCH_PROFILE_CASE_ID,
                    False,
                    failure_reason=("required non-scenarios Creative Portfolio Search Profile offline case is missing"),
                )
            )
        summary = summarize_results(results, repeat=repeat)
        return {"summary": summary, "cases": results}
    finally:
        agent_service_module.create_agent_provider = original_create_agent_provider
        map_poi_service_module.urlopen = original_map_urlopen
        map_poi_service_module.MapPoiService._fetch_amap_place = original_fetch_amap_place
        map_poi_service_module.MapPoiService._fetch_amap_around = original_fetch_amap_around
        map_poi_service_module.MapPoiService.search = original_map_search
        map_poi_service_module.MapPoiService.search_nearby = original_map_search_nearby
        map_poi_service_module.MapPoiService._consume_budget_or_raise = original_map_consume_budget
        itinerary_service_module.ItineraryService._collect_search_profile_candidates = (
            original_collect_profile_candidates
        )
        poi_discovery_service_module.PoiDiscoveryService.discover = original_poi_discover
        RouteService.build_routes = original_build_routes
        travel_tools_module.ResilientWebSearchProvider.search = original_web_search
        PublicSourceReader.read = original_public_source_read
        amap_adapter_module.AmapPoiSearchPlanAdapter.adapt = original_adapt_profile


def _provider_from_case(case: dict[str, Any]) -> Any:
    if case.get("providerMode") == "tool_loop":
        return OfflineToolLoopProvider(case.get("toolLoopScript") or [])
    return OfflineAgentProvider(
        case.get("agentProviderPayloads") or _provider_payloads(case["id"]),
        initial_plan_payloads=case.get("initialPlanPayloads") or [],
        candidate_hint_payloads=case.get("candidateHintPayloads") or [],
        request_coverage_payloads=case.get("requestCoveragePayloads") or [],
        controller_mode=str(case.get("controllerMode") or ""),
        semantic_action_payloads=case.get("semanticActionPayloads") or [],
    )


def _attach_offline_safety_evidence(
    result: dict[str, Any],
    context: dict[str, Any],
    provider: Any,
) -> dict[str, Any]:
    runtime_trace = context.get("runtimeTrace") if isinstance(context.get("runtimeTrace"), dict) else {}
    external_calls = [item for item in runtime_trace.get("realExternalCalls") or [] if isinstance(item, dict)]
    result["offlineControllerStubInvocationCount"] = int(getattr(provider, "controller_invocation_count", 0))
    result["offlineProviderIsolation"] = deepcopy(runtime_trace.get("offlineProviderIsolation") or {})
    result["realExternalCallLedger"] = {
        "network": len(external_calls),
        "amap": sum(str(item.get("provider") or "") == "amap" for item in external_calls),
        "web": sum(str(item.get("provider") or "") == "web" for item in external_calls),
        "controller": sum(str(item.get("provider") or "") == "controller" for item in external_calls),
    }
    result["globalNetworkSentinelAttemptCount"] = len(external_calls)
    result["realExternalControllerCallCount"] = result["realExternalCallLedger"]["controller"]
    return result


def _run_case(
    client,
    case: dict[str, Any],
    provider: Any,
    db_path: Path,
    runtime_trace: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    started = perf_counter()
    context: dict[str, Any] = {
        "case": case,
        "provider": provider,
        "runtimeTrace": runtime_trace if runtime_trace is not None else {},
        "_responses": [],
    }
    try:
        if not case.get("steps"):
            return _result(case["id"], False, failure_reason="offline eval case is missing steps")
        for step in case["steps"]:
            _run_step(client, step, context, db_path)
        session_id = str(_value(context, "session.sessionId") or "")
        server_initial_plan = _persisted_server_initial_plan(db_path, session_id)
        if server_initial_plan:
            context["runtimeTrace"]["serverInitialPlan"] = server_initial_plan
        passed, failure_reason = _run_assertions(case.get("assertions") or [], context)
        body = _last_response_body(context)
        planning_steps = _collect_planning_steps(context)
        replay_steps = planning_steps
        profile_contract = case.get("searchProfileEval")
        if isinstance(profile_contract, dict) and profile_contract.get("required"):
            session_id = str(_value(context, "session.sessionId") or "")
            replay_steps = _latest_profile_replay_steps(_persisted_profile_metric_events(db_path, session_id))
            if not replay_steps:
                replay_steps = planning_steps
        stability_metrics = _stability_metrics(
            case,
            body,
            planning_steps,
            context,
            db_path,
        )
        stability_metrics.update(
            _search_profile_eval_metrics(
                case,
                planning_steps,
                context,
                db_path,
            )
        )
        stability_failures = _stability_failures(stability_metrics, case)
        if stability_failures:
            passed = False
            failure_reason = "; ".join(stability_failures)
        result = _result(
            case["id"],
            passed,
            body=body,
            planning_steps=replay_steps,
            expected_stages=(case.get("expect") or {}).get("replayStages"),
            allowed_failed_stages=(case.get("expect") or {}).get("allowedFailedStages"),
            invalid_patch_count=int((case.get("expect") or {}).get("invalidPatchCount") or 0),
            failure_reason=failure_reason,
            latency_ms=int((perf_counter() - started) * 1000),
            stability_metrics=stability_metrics,
        )
        if isinstance(context.get("persistedClarificationChoiceReplay"), dict):
            result["persistedClarificationChoiceReplay"] = deepcopy(context["persistedClarificationChoiceReplay"])
        return _attach_offline_safety_evidence(result, context, provider)
    except Exception as error:
        result = _result(
            case["id"],
            False,
            failure_reason=str(error),
            latency_ms=int((perf_counter() - started) * 1000),
        )
        return _attach_offline_safety_evidence(result, context, provider)


def _opaque_clarification_wire_payload(source_assistant_turn_id: str, choice_id: str) -> dict[str, Any]:
    source_id = str(source_assistant_turn_id or "").strip()
    persisted_choice_id = str(choice_id or "").strip()
    if not source_id or not persisted_choice_id:
        raise AssertionError("opaque clarification wire identity is incomplete")
    payload = {
        "content": "",
        "context": {
            "selectedAgentChoice": {
                "sourceAssistantTurnId": source_id,
                "choiceId": persisted_choice_id,
            }
        },
    }
    if (
        set(payload) != {"content", "context"}
        or payload["content"] != ""
        or set(payload["context"]) != {"selectedAgentChoice"}
        or set(payload["context"]["selectedAgentChoice"]) != {"sourceAssistantTurnId", "choiceId"}
    ):
        raise AssertionError("opaque clarification wire payload contains non-identity material")
    return payload


def _assistant_turn_from_context(context: dict[str, Any], source_from: str) -> dict[str, Any]:
    source = _value(context, source_from, default={})
    assistant = source.get("assistantTurn") if isinstance(source, dict) else None
    if not isinstance(assistant, dict):
        raise AssertionError(f"offline eval source '{source_from}' does not contain an assistant turn")
    return assistant


def _resolve_choice_option(
    *,
    assistant: dict[str, Any],
    choice_action: str = "",
    choice_kind: str = "",
    choice_id: str = "",
) -> dict[str, Any]:
    options = [item for item in assistant.get("choiceOptions") or [] if isinstance(item, dict)]
    matches = []
    for option in options:
        option_id = str(option.get("id") or option.get("choiceId") or "")
        if choice_id and option_id != choice_id:
            continue
        if choice_action and str(option.get("action") or "") != choice_action:
            continue
        if choice_kind and str(option.get("kind") or "") != choice_kind:
            continue
        matches.append(option)
    if len(matches) != 1:
        available = [
            {
                "id": str(item.get("id") or item.get("choiceId") or ""),
                "action": str(item.get("action") or ""),
                "kind": str(item.get("kind") or ""),
            }
            for item in options
        ]
        raise AssertionError(
            "offline eval choice selection is ambiguous or missing: "
            f"action={choice_action or '<any>'}, kind={choice_kind or '<any>'}, id={choice_id or '<any>'}, "
            f"available={available}"
        )
    return matches[0]


def _run_step(client, step: dict[str, Any], context: dict[str, Any], db_path: Path) -> None:
    action = step["action"]
    if action == "create_session":
        response = client.post(
            "/api/agent/sessions", json=step.get("payload") or {"city": "北京", "title": "北京离线 Eval"}
        )
        _save_response(step, context, response)
        return
    if action == "send_agent_message":
        session_id = _value(context, step.get("sessionIdFrom") or "session.sessionId")
        _capture_profile_db_before(context, db_path, session_id)
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={
                "content": step["content"],
                "context": {
                    "travelDateRange": {"startDate": "2026-10-01", "endDate": "2026-10-01"},
                    **(step.get("context") or {}),
                },
            },
        )
        _save_response(step, context, response)
        return
    if action == "send_selected_agent_choice":
        session_id = _value(context, step.get("sessionIdFrom") or "session.sessionId")
        _capture_profile_db_before(context, db_path, session_id)
        assistant = _assistant_turn_from_context(context, step.get("sourceFrom") or "first")
        choice = _resolve_choice_option(
            assistant=assistant,
            choice_action=str(step.get("choiceAction") or ""),
            choice_kind=str(step.get("choiceKind") or ""),
            choice_id=str(step.get("choiceId") or ""),
        )
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={
                "content": step.get("content") or "选择 Agent 选项",
                "context": {
                    "travelDateRange": {"startDate": "2026-10-01", "endDate": "2026-10-01"},
                    **(step.get("context") or {}),
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": str(assistant.get("id") or ""),
                        "choiceId": str(choice.get("id") or choice.get("choiceId") or ""),
                    },
                },
            },
        )
        _save_response(step, context, response)
        return
    if action == "send_portfolio_more_plans_choice":
        session_id = _value(
            context,
            step.get("sessionIdFrom") or "session.sessionId",
        )
        _capture_profile_db_before(context, db_path, session_id)
        assistant = _assistant_turn_from_context(
            context,
            step.get("sourceFrom") or "first",
        )
        choice_kind = str(step.get("choiceKind") or "")
        if choice_kind:
            choice = _resolve_choice_option(
                assistant=assistant,
                choice_kind=choice_kind,
            )
        else:
            options = [item for item in assistant.get("choiceOptions") or [] if isinstance(item, dict)]
            matches = [
                item
                for item in options
                if str(item.get("kind") or "") in {"portfolio_more_plans", "portfolio_partial_more_plans"}
            ]
            if len(matches) != 1:
                available = [
                    {
                        "id": str(item.get("id") or item.get("choiceId") or ""),
                        "action": str(item.get("action") or ""),
                        "kind": str(item.get("kind") or ""),
                    }
                    for item in options
                ]
                raise AssertionError(
                    "offline profile case did not expose exactly one portfolio more-plans choice: "
                    f"available={available}"
                )
            choice = matches[0]
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={
                "content": "选择 Agent 选项",
                "context": {
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": str(assistant.get("id") or ""),
                        "choiceId": str(choice.get("id") or ""),
                    }
                },
            },
        )
        _save_response(step, context, response)
        return
    if action == "send_persisted_clarification_choice":
        session_id = str(
            _value(
                context,
                step.get("sessionIdFrom") or "session.sessionId",
            )
            or ""
        )
        assistant = _assistant_turn_from_context(context, step.get("sourceFrom") or "first")
        source_assistant_turn_id = str(assistant.get("id") or "")
        persisted = _persisted_clarification_choice_for_replay(
            db_path,
            session_id=session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            dimension_id=str(step.get("dimensionId") or ""),
            semantic_value_fingerprint=str(step.get("semanticValueFingerprint") or ""),
        )
        before_counts = _session_write_counts(db_path, session_id)
        wire_payload = _opaque_clarification_wire_payload(
            source_assistant_turn_id,
            persisted["choiceId"],
        )
        submitted_context = deepcopy(wire_payload["context"])
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json=wire_payload,
        )
        _save_response(step, context, response)
        if response.status_code != 200:
            raise AssertionError(
                f"persisted clarification choice POST failed before dry continuation: {response.status_code}"
            )
        response_body = context[step.get("saveAs") or "last"]
        response_assistant = (
            response_body.get("assistantTurn")
            if isinstance(response_body, dict) and isinstance(response_body.get("assistantTurn"), dict)
            else {}
        )
        replay_trace = _persisted_clarification_choice_result(
            db_path,
            session_id=session_id,
            source_assistant_turn_id=source_assistant_turn_id,
            response_assistant_turn_id=str(response_assistant.get("id") or ""),
            persisted=persisted,
            submitted_context=submitted_context,
            before_counts=before_counts,
        )
        runtime_trace = context.get("runtimeTrace") if isinstance(context.get("runtimeTrace"), dict) else {}
        real_external_calls = [item for item in runtime_trace.get("realExternalCalls") or [] if isinstance(item, dict)]
        replay_trace["realExternalNetworkCallCount"] = len(real_external_calls)
        replay_trace["recordedFixtureCaptureDelta"] = len(runtime_trace.get("recordedFixtureCaptureSessions") or [])
        context["persistedClarificationChoiceReplay"] = replay_trace
        return
    if action == "edit_user_message":
        session_id = _value(context, step.get("sessionIdFrom") or "session.sessionId")
        turn_id = _value(context, step["turnIdFrom"])
        response = client.patch(
            f"/api/agent/sessions/{session_id}/messages/{turn_id}",
            json={"content": step["content"], "regenerate": bool(step.get("regenerate", True))},
        )
        _save_response(step, context, response)
        return
    if action == "get_session":
        session_id = _value(context, step.get("sessionIdFrom") or "session.sessionId")
        response = client.get(f"/api/agent/sessions/{session_id}")
        _save_response(step, context, response)
        return
    if action == "patch_itinerary":
        plan_id = _value(context, step["planIdFrom"])
        payload = {
            "sourceType": step.get("sourceType") or "manual",
            "operations": step.get("operations") or [],
        }
        if step.get("baseVersionIdFrom"):
            payload["baseVersionId"] = _value(context, step["baseVersionIdFrom"])
        response = client.post(f"/api/itineraries/{plan_id}/patch", json=payload)
        _save_response(step, context, response)
        return
    if action == "refresh_tickets":
        plan_id = _value(context, step["planIdFrom"])
        response = client.post(f"/api/itineraries/{plan_id}/tickets/refresh")
        _save_response(step, context, response)
        return
    if action == "db_query":
        with _open_db(db_path) as connection:
            rows = connection.execute(step["query"]).fetchall()
        context[step["saveAs"]] = [dict(row) for row in rows]
        return
    raise ValueError(f"Unknown eval action: {action}")


def _save_response(step: dict[str, Any], context: dict[str, Any], response) -> None:
    try:
        body = response.json()
    except Exception:
        body = {"rawText": response.text}
    if isinstance(body, dict):
        body = {**body, "statusCode": response.status_code}
    else:
        body = {"body": body, "statusCode": response.status_code}
    context[step.get("saveAs") or "last"] = body
    context["last"] = body
    context.setdefault("_responses", []).append(body)


def _run_assertions(assertions: list[dict[str, Any]], context: dict[str, Any]) -> tuple[bool, str]:
    for assertion in assertions:
        passed, reason = _check_assertion(assertion, context)
        if not passed:
            return False, reason
    return True, ""


def _check_assertion(assertion: dict[str, Any], context: dict[str, Any]) -> tuple[bool, str]:
    values = _values(context, assertion["path"])
    if "equals" in assertion:
        return _ok(values == [assertion["equals"]], assertion, values)
    if "equalsPath" in assertion:
        return _ok(values == _values(context, assertion["equalsPath"]), assertion, values)
    if "exists" in assertion:
        exists = bool(values) and values[0] is not None
        return _ok(exists is bool(assertion["exists"]), assertion, values)
    if "isNull" in assertion:
        is_null = (not values) or values[0] is None
        return _ok(is_null is bool(assertion["isNull"]), assertion, values)
    if "lengthEquals" in assertion:
        target = _value(context, assertion["path"], default=[])
        return _ok(len(target or []) == int(assertion["lengthEquals"]), assertion, values)
    if "lengthAtLeast" in assertion:
        target = _value(context, assertion["path"], default=[])
        return _ok(len(target or []) >= int(assertion["lengthAtLeast"]), assertion, values)
    if "lengthAtMost" in assertion:
        target = _value(context, assertion["path"], default=[])
        return _ok(len(target or []) <= int(assertion["lengthAtMost"]), assertion, values)
    if "contains" in assertion:
        candidates = _membership_values(values)
        return _ok(assertion["contains"] in candidates, assertion, candidates)
    if "notContains" in assertion:
        candidates = _membership_values(values)
        return _ok(assertion["notContains"] not in candidates, assertion, candidates)
    if "notContainsAny" in assertion:
        candidates = [str(item) for item in _membership_values(values)]
        blocked = [str(item) for item in assertion["notContainsAny"]]
        passed = not any(blocked_item in candidate for candidate in candidates for blocked_item in blocked)
        return _ok(passed, assertion, candidates)
    if "containsPath" in assertion:
        candidates = _membership_values(values)
        return _ok(
            any(item in candidates for item in _values(context, assertion["containsPath"])), assertion, candidates
        )
    if "equalsList" in assertion:
        return _ok(values == assertion["equalsList"], assertion, values)
    if "allFieldEquals" in assertion:
        rows = _value(context, assertion["path"], default=[])
        expected = assertion["allFieldEquals"]
        passed = all(row.get(field) == value for row in rows for field, value in expected.items())
        return _ok(bool(rows) and passed, assertion, values)
    if "allFieldContains" in assertion:
        rows = _value(context, assertion["path"], default=[])
        expected = assertion["allFieldContains"]
        passed = all(str(value) in str(row.get(field) or "") for row in rows for field, value in expected.items())
        return _ok(bool(rows) and passed, assertion, values)
    if "noneFieldContainsAny" in assertion:
        rows = _value(context, assertion["path"], default=[])
        expected = assertion["noneFieldContainsAny"]
        blocked_pairs = [
            (field, str(value)) for field, values_for_field in expected.items() for value in values_for_field
        ]
        passed = all(blocked not in str(row.get(field) or "") for row in rows for field, blocked in blocked_pairs)
        return _ok(bool(rows) and passed, assertion, values)
    if "uniqueNormalized" in assertion:
        normalizer = str(assertion.get("normalizer") or "simple")
        ignored = {str(item) for item in assertion.get("ignoreValues") or []}
        normalized = [
            _normalize_assertion_value(item, normalizer)
            for item in _membership_values(values)
            if str(item) not in ignored
        ]
        normalized = [item for item in normalized if item]
        return _ok(len(normalized) == len(set(normalized)), assertion, normalized)
    raise ValueError(f"Unsupported assertion: {assertion}")


def _ok(passed: bool, assertion: dict[str, Any], actual: list[Any]) -> tuple[bool, str]:
    if passed:
        return True, ""
    return False, f"assertion failed for {assertion.get('path')}: expected {assertion}, actual={actual}"


def _value(context: dict[str, Any], path: str, default: Any = None) -> Any:
    values = _values(context, path)
    return values[0] if values else default


def _values(context: dict[str, Any], path: str) -> list[Any]:
    current: list[Any] = [context]
    for part in path.split("."):
        expand = part.endswith("[]")
        key = part[:-2] if expand else part
        next_values: list[Any] = []
        for item in current:
            value = _item_value(item, key)
            if expand:
                if isinstance(value, list):
                    next_values.extend(value)
            elif value is not None:
                next_values.append(value)
        current = next_values
    return current


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    if isinstance(item, list) and key.isdigit():
        index = int(key)
        return item[index] if index < len(item) else None
    return getattr(item, key, None)


def _membership_values(values: list[Any]) -> list[Any]:
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    return values


def _normalize_assertion_value(value: Any, normalizer: str) -> str:
    import re

    text = str(value or "")
    if normalizer == "canonical_entity":
        text = re.sub(r"[\(（【\[].*?[\)）】\]]", "", text)
        text = re.sub(r"[\s\-_,.()（）·・，。]+", "", text).casefold()
        positions = [(text.find(anchor), anchor) for anchor in ("大学", "高等院校", "学院") if text.find(anchor) >= 0]
        if positions:
            index, anchor = min(positions, key=lambda item: item[0])
            return text[: index + len(anchor)]
        return re.sub(r"(校区|分校区|分馆|入口|出入口|售票处|游客中心|服务中心|停车场)$", "", text)
    return re.sub(r"\s+", "", text).casefold()


def _collect_planning_steps(context: dict[str, Any]) -> list[dict[str, Any]]:
    responses = context.get("_responses")
    if not isinstance(responses, list):
        responses = [value for value in context.values() if isinstance(value, dict)]
    for value in reversed(responses):
        if not isinstance(value, dict):
            continue
        event_sources: list[Any] = []
        for payload in (
            value,
            value.get("assistantTurn") if isinstance(value.get("assistantTurn"), dict) else {},
        ):
            for key in ("planningSteps", "toolEvents"):
                event_sources.append(payload.get(key))
        planning_run = value.get("planningRun")
        if isinstance(planning_run, dict) and isinstance(planning_run.get("toolCalls"), list):
            event_sources.append(planning_run["toolCalls"])
        for candidate_events in event_sources:
            if not isinstance(candidate_events, list) or not candidate_events:
                continue
            events = [event for event in candidate_events if isinstance(event, dict)]
            if events:
                return _dedupe_events(events)
    return []


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for index, event in enumerate(events):
        marker = json.dumps(
            {
                "i": index if not event.get("id") and not event.get("type") else None,
                "id": event.get("id"),
                "type": event.get("type"),
                "label": event.get("label") or event.get("toolName"),
                "timestamp": event.get("timestamp") or event.get("startedAt"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(event)
    return deduped


def _last_response_body(context: dict[str, Any]) -> Optional[dict[str, Any]]:
    value = context.get("last")
    return value if isinstance(value, dict) else None


def _provider_payloads(case_id: str) -> list[Union[dict[str, Any], str]]:
    if case_id == "pending_poi_multiple_candidates":
        return [_full_itinerary_output("北京胡同餐厅 1 日", ["胡同餐厅"])]
    if case_id == "edit_message_rollback":
        return [
            _full_itinerary_output("北京初版", ["故宫"]),
            _patch_output("北京第二版"),
            _full_itinerary_output("北京重新分支", ["故宫"]),
        ]
    if case_id == "invalid_agent_patch":
        return [
            _full_itinerary_output("北京故宫 1 日游", ["故宫"]),
            {
                "reply": "尝试执行不支持操作。",
                "mode": "patch",
                "operations": [{"op": "teleport_segment"}],
                "fullItinerary": None,
                "poiResolutionRequests": [],
                "warnings": [],
            },
        ]
    return [_full_itinerary_output("北京故宫 1 日游", ["故宫"])]


def _full_itinerary_output(title: str, poi_names: list[str]) -> dict[str, Any]:
    segments = [
        {
            "poiName": name,
            "category": "food" if "餐厅" in name else "scenic",
            "startTime": "09:00" if index == 0 else "12:00",
            "durationMinutes": 90,
            "notes": f"离线 eval 安排：{name}",
            "estimatedCost": 60 if index == 0 else 0,
        }
        for index, name in enumerate(poi_names)
    ]
    return {
        "reply": f"已生成{title}。",
        "mode": "full_itinerary",
        "operations": [],
        "fullItinerary": {
            "title": title,
            "city": "北京",
            "days": [{"dayNumber": 1, "title": "离线 Eval Day 1", "segments": segments}],
        },
        "poiResolutionRequests": [
            {"name": name, "category": segment["category"]} for name, segment in zip(poi_names, segments)
        ],
        "warnings": [],
    }


def _patch_output(title: str) -> dict[str, Any]:
    return {
        "reply": f"已改为{title}。",
        "mode": "patch",
        "operations": [{"op": "replace_trip_title", "value": title}],
        "fullItinerary": None,
        "poiResolutionRequests": [],
        "warnings": [],
    }


def _fake_amap_place(responses: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
    keyword = params.get("keywords", "")
    if responses.get("__familyAwareFoodFixture"):
        family_markers = [
            ("护国寺|炒肝|豆汁", "护国寺北京小吃店", "小吃"),
            ("卤煮", "门框胡同卤煮店", "卤煮"),
            ("爆肚", "老北京爆肚店", "爆肚"),
            ("炸酱面", "京味炸酱面馆", "炸酱面"),
            ("牛街|清真", "牛街清真餐厅", "清真"),
            ("涮肉|铜锅", "老北京铜锅涮肉馆", "涮肉"),
            ("烤鸭", "便宜坊老字号烤鸭店", "烤鸭"),
            ("北京菜|京味", "京味北京菜馆", "北京菜"),
            ("宫廷点心|奶酪", "老北京宫廷点心铺", "宫廷点心"),
        ]
        for family_index, (pattern, name, subtype) in enumerate(family_markers, start=1):
            if re.search(pattern, keyword):
                return {
                    "status": "1",
                    "pois": [
                        {
                            "id": "AMAP_LOCAL_FOOD_LUNCH" if family_index <= 5 else "AMAP_LOCAL_FOOD_DINNER",
                            "name": name,
                            "type": f"餐饮服务;中餐厅;{subtype}",
                            "cityname": "北京市",
                            "adname": "东城区",
                            "address": "离线 family fixture",
                            "location": "116.397500,39.918500",
                            "photos": [],
                        }
                    ],
                }
    aliases = responses.get("__aliases") if isinstance(responses.get("__aliases"), dict) else {}
    response = responses.get(str(aliases.get(keyword) or keyword))
    if response is None:
        return {"status": "1", "pois": []}
    if isinstance(response, dict) and "pois" in response:
        return response
    pois = response if isinstance(response, list) else [response]
    return {"status": "1", "pois": pois}


def _recorded_web_results(
    responses: dict[str, Any],
    query: str,
) -> list[dict[str, Any]]:
    for marker, payload in responses.items():
        if str(marker) and str(marker) in str(query):
            return [item for item in (payload if isinstance(payload, list) else [payload]) if isinstance(item, dict)]
    return []


def _recorded_web_seed_name(title: Any) -> str:
    return re.split(
        r"\s*[-—_|｜]\s*",
        str(title or "").strip(),
        maxsplit=1,
    )[0].strip()


def _configure_offline_env(database_url: str) -> None:
    os.environ["DATABASE_URL"] = database_url
    os.environ["PROVIDER_MODE"] = "mock"
    os.environ["AGENT_INITIAL_PLANNING_MODE"] = "strict_portfolio"
    os.environ["AGENT_INTENT_ROUTING_MODE"] = "legacy-only"
    # This recorded harness supplies a DaySlot-only provider; Portfolio is tested by dedicated fixtures.
    os.environ["AGENT_CREATIVE_PORTFOLIO_ENABLED"] = "false"
    os.environ["DEFAULT_USER_ID"] = "offline-eval-user"
    os.environ["MAP_PROVIDER_KEY"] = "offline-amap-key"
    for key in (
        "DEEPSEEK_API_KEY",
        "AMAP_WEB_SERVICE_KEY",
        "WEB_SEARCH_API_KEY",
        "SEARCH_PROVIDER_KEY",
        "TICKET_PROVIDER_KEY",
        "WEATHER_PROVIDER_KEY",
    ):
        os.environ[key] = ""


def _configure_case_env(case: dict[str, Any]) -> None:
    initial_planning_mode = str(case.get("initialPlanningMode") or "strict_portfolio").strip()
    if initial_planning_mode not in {"strict_portfolio", "simple_open_v1"}:
        raise AssertionError("offline eval case has an unsupported initial planning mode")
    os.environ["AGENT_INITIAL_PLANNING_MODE"] = initial_planning_mode
    intent_routing_mode = str(case.get("intentRoutingMode") or "legacy-only").strip()
    if intent_routing_mode not in {
        "legacy-only",
        "shadow",
        "active-read",
        "active-all",
        "kill-switch",
    }:
        raise AssertionError("offline eval case has an unsupported intent routing mode")
    os.environ["AGENT_INTENT_ROUTING_MODE"] = intent_routing_mode
    portfolio_enabled = bool(case.get("creativePortfolioEnabled"))
    os.environ["AGENT_CREATIVE_PORTFOLIO_ENABLED"] = "true" if portfolio_enabled else "false"
    os.environ["AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT"] = str(
        max(1, min(4, int(case.get("creativePortfolioTargetCount") or 4)))
    )


def _clear_database(db_path: Path) -> None:
    with _open_db(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM agent_choice_executions;
            DELETE FROM timeline_mutation_transactions;
            DELETE FROM agent_plan_proposals;
            DELETE FROM agent_plan_portfolios;
            DELETE FROM amap_poi_candidates;
            DELETE FROM travel_preference_memories;
            DELETE FROM planning_runs;
            DELETE FROM itinerary_patches;
            DELETE FROM itinerary_versions;
            DELETE FROM conversation_turns;
            DELETE FROM conversation_sessions;
            DELETE FROM itinerary_segments;
            DELETE FROM itinerary_days;
            DELETE FROM itinerary_plans;
            DELETE FROM pois;
            DELETE FROM ticket_lookup_results;
            DELETE FROM route_options;
            DELETE FROM weather_signals;
            DELETE FROM traffic_crowding_signals;
            DELETE FROM poi_risk_alerts;
            """
        )


def _open_db(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def _capture_profile_db_before(
    context: dict[str, Any],
    db_path: Path,
    session_id: str,
) -> None:
    contract = (context.get("case") or {}).get("searchProfileEval")
    if not isinstance(contract, dict) or not contract.get("required"):
        return
    if "profileDbBefore" not in context:
        context["profileDbBefore"] = _session_write_counts(
            db_path,
            str(session_id),
        )


def _session_write_counts(db_path: Path, session_id: str) -> dict[str, int]:
    with _open_db(db_path) as connection:
        return {
            "version": int(
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            ),
            "patch": int(
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            ),
            "route": int(
                connection.execute(
                    "SELECT COUNT(*) FROM route_options",
                ).fetchone()[0]
            ),
        }


def _proposal_only_stability_material(
    db_path: Path,
    session_id: str,
) -> dict[str, Any]:
    """Load the visible, persisted Simple Direction proposal truth.

    Proposal-first cases deliberately have no itinerary version yet.  Their
    stability evidence therefore comes from the authoritative portfolio rows,
    not from a response-only card or a prematurely committed timeline.
    """

    with _open_db(db_path) as connection:
        roots = connection.execute(
            """SELECT id, summary_json
               FROM agent_plan_portfolios
               WHERE session_id = ?
               ORDER BY created_at DESC""",
            (session_id,),
        ).fetchall()
        for root in roots:
            try:
                summary = json.loads(str(root["summary_json"] or "{}"))
            except (TypeError, ValueError):
                summary = {}
            if str(summary.get("workflowMode") or "") != "simple_direction_v1":
                continue
            visible_ids = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]
            if not visible_ids:
                return {
                    "visibleProposalCount": 0,
                    "adoptionReadyProposalCount": 0,
                    "persistedSelectionCapabilityCount": 0,
                    "snapshot": {},
                    "verifier": {},
                    "routeProviderAttemptCount": 0,
                    "routeCoverageComplete": False,
                    "routeEvidenceFingerprintBound": False,
                }
            placeholders = ",".join("?" for _ in visible_ids)
            rows = connection.execute(
                f"""SELECT id, status, snapshot_json, verifier_json
                    FROM agent_plan_proposals
                    WHERE portfolio_id = ? AND id IN ({placeholders})
                    ORDER BY rank_index ASC""",
                (str(root["id"]), *visible_ids),
            ).fetchall()
            proposals: list[dict[str, Any]] = []
            for row in rows:
                try:
                    snapshot = json.loads(str(row["snapshot_json"] or "{}"))
                except (TypeError, ValueError):
                    snapshot = {}
                try:
                    verifier = json.loads(str(row["verifier_json"] or "{}"))
                except (TypeError, ValueError):
                    verifier = {}
                proposals.append(
                    {
                        "id": str(row["id"]),
                        "status": str(row["status"] or ""),
                        "snapshot": snapshot if isinstance(snapshot, dict) else {},
                        "verifier": verifier if isinstance(verifier, dict) else {},
                    }
                )
            ordered = sorted(
                proposals,
                key=lambda item: visible_ids.index(item["id"]) if item["id"] in visible_ids else len(visible_ids),
            )
            adoption_ready = [
                item
                for item in ordered
                if item["status"] == "adoption_ready" and item["verifier"].get("confirmationPassed") is True
            ]
            representative = (adoption_ready or ordered or [{}])[0]
            snapshot = representative.get("snapshot") if isinstance(representative, dict) else {}
            if not isinstance(snapshot, dict):
                snapshot = {}
            route_audit = (
                snapshot.get("simpleOpenRouteAssignment")
                if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
                else {}
            )
            route_contract = (
                snapshot.get("routeDecisionContract") if isinstance(snapshot.get("routeDecisionContract"), dict) else {}
            )
            route_fingerprint = str(route_audit.get("routeContractFingerprint") or "")
            contract_fingerprint = str(route_contract.get("fingerprint") or "")
            assistant_rows = connection.execute(
                """SELECT agent_response_json FROM conversation_turns
                   WHERE session_id = ? AND role = 'assistant'
                     AND agent_response_json IS NOT NULL
                   ORDER BY turn_index, created_at""",
                (session_id,),
            ).fetchall()
            persisted_selection_capability_count = 0
            for assistant_row in assistant_rows:
                try:
                    response_payload = json.loads(str(assistant_row["agent_response_json"] or "{}"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(response_payload, dict):
                    continue
                persisted_selection_capability_count += sum(
                    1
                    for option in response_payload.get("choiceOptions") or []
                    if isinstance(option, dict)
                    and str(option.get("action") or "") == "select_plan_proposal"
                    and str(option.get("rootPortfolioId") or "") == str(root["id"])
                )
            return {
                "visibleProposalCount": len(ordered),
                "adoptionReadyProposalCount": len(adoption_ready),
                "persistedSelectionCapabilityCount": persisted_selection_capability_count,
                "snapshot": snapshot,
                "verifier": representative.get("verifier") if isinstance(representative, dict) else {},
                "routeProviderAttemptCount": int(route_audit.get("routeProviderAttemptCount") or 0),
                "routeCoverageComplete": route_audit.get("routeCoverageComplete") is True,
                "routeEvidenceFingerprintBound": bool(
                    route_fingerprint and contract_fingerprint and route_fingerprint == contract_fingerprint
                ),
            }
    return {
        "visibleProposalCount": 0,
        "adoptionReadyProposalCount": 0,
        "persistedSelectionCapabilityCount": 0,
        "snapshot": {},
        "verifier": {},
        "routeProviderAttemptCount": 0,
        "routeCoverageComplete": False,
        "routeEvidenceFingerprintBound": False,
    }


def _stability_metrics(
    case: dict[str, Any],
    body: dict[str, Any],
    planning_steps: list[dict[str, Any]],
    context: dict[str, Any],
    db_path: Path,
) -> dict[str, Any]:
    contract = case.get("stabilityMetrics")
    if not isinstance(contract, dict):
        return {}
    expected_lifecycle = str(contract.get("expectedLifecycle") or "committed_timeline")
    if expected_lifecycle not in {"committed_timeline", "proposal_only"}:
        raise AssertionError("offline stability case has an unsupported expected lifecycle")
    expected_night_count = int(contract["expectedNightViewCount"])
    expected_night_days = sorted(int(day) for day in contract.get("expectedNightViewDayNumbers") or [])
    scheduled_by_day = _scheduled_goal_counts(planning_steps, "goal_night_view")
    session_id = _value(context, "session.sessionId")
    proposal_material = (
        _proposal_only_stability_material(db_path, str(session_id)) if expected_lifecycle == "proposal_only" else {}
    )
    proposal_snapshot = proposal_material.get("snapshot") if isinstance(proposal_material.get("snapshot"), dict) else {}
    persisted_by_day = _persisted_intent_counts(
        {"itinerary": proposal_snapshot} if expected_lifecycle == "proposal_only" else body,
        "night_view",
    )
    draft_decision = _draft_decision_metadata(planning_steps)
    partial_timeline = any(
        step.get("type") == "agent_action_outcome"
        and str(((step.get("metadata") or {}).get("resultPreview") or {}).get("status") or "") == "partial"
        for step in planning_steps
    )
    itinerary = body.get("itinerary") if isinstance(body.get("itinerary"), dict) else {}
    timeline_created = bool(body.get("version") and itinerary.get("days"))
    with _open_db(db_path) as connection:
        version_write_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )
        patch_write_count = int(
            connection.execute("SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session_id,)).fetchone()[
                0
            ]
        )
        route_write_count = int(connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0])
        latest_version = connection.execute(
            """SELECT snapshot_json FROM itinerary_versions
               WHERE session_id = ? ORDER BY created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
    latest_snapshot = json.loads(str(latest_version[0])) if latest_version and latest_version[0] else {}
    pending_slots = latest_snapshot.get("portfolioPendingSlots") or []
    pending_scope_parts: list[str] = []
    continuation_scope_drift = False
    for slot in pending_slots:
        if not isinstance(slot, dict):
            continuation_scope_drift = True
            continue
        brief_id = str(slot.get("briefId") or "")
        stable_brief_id = brief_id.split(":turn_", 1)[0] if ":turn_" in brief_id else brief_id
        values = (
            stable_brief_id,
            str(slot.get("poolId") or ""),
            str(slot.get("planningSlotId") or ""),
            str(slot.get("dayNumber") or ""),
        )
        if not all(values):
            continuation_scope_drift = True
        pending_scope_parts.append("|".join(values))
    pending_scope_fingerprint = ";".join(sorted(pending_scope_parts))
    serialized_runtime = json.dumps({"body": body, "planningSteps": planning_steps}, ensure_ascii=False, default=str)
    grounding_terminal_failure = "portfolio_partial_anchor_grounding_evidence_missing" in serialized_runtime
    scheduled_days = sorted(day for day, count in scheduled_by_day.items() if count > 0)
    persisted_days = sorted(day for day, count in persisted_by_day.items() if count > 0)
    scheduled_count = sum(scheduled_by_day.values())
    persisted_count = sum(persisted_by_day.values())
    cardinality_mismatch = (
        scheduled_count != expected_night_count
        or scheduled_days != expected_night_days
        or any(count > 1 for count in scheduled_by_day.values())
    )
    persisted_over_allocation = (
        persisted_count > expected_night_count
        or any(count > 1 for count in persisted_by_day.values())
        or any(day not in expected_night_days for day in persisted_days)
    )
    persistence_mismatch = not partial_timeline and (
        persisted_count != expected_night_count or persisted_days != expected_night_days
    )
    expected_version_writes = int(contract["expectedVersionWriteCount"])
    expected_patch_writes = int(contract["expectedPatchWriteCount"])
    expected_route_writes = int(contract["expectedRouteWriteCount"])
    unexpected_version_write = version_write_count != expected_version_writes
    unexpected_patch_write = patch_write_count != expected_patch_writes
    unexpected_route_write = route_write_count != expected_route_writes
    unexpected_write = unexpected_version_write or unexpected_patch_write or unexpected_route_write
    directive_repair_count = int(draft_decision.get("schemaRepairAttempts") or 0)
    reason_codes = draft_decision.get("reasonCodes") or []
    deterministic_fallback_count = int("deterministic_cardinality_fallback" in reason_codes)
    proposal_material_created = bool(proposal_material.get("visibleProposalCount"))
    expected_material_created = (
        timeline_created if expected_lifecycle == "committed_timeline" else proposal_material_created
    )
    cardinality_terminal_failure = bool(
        not expected_material_created
        and (
            cardinality_mismatch
            or "controller_cardinality_invalid" in reason_codes
            or "deterministic_cardinality_fallback" in reason_codes
        )
    )
    fake_or_non_amap_anchor_count = _fake_or_non_amap_route_anchor_count(
        {"itinerary": proposal_snapshot} if expected_lifecycle == "proposal_only" else body
    )
    discovery = _planning_step_preview(planning_steps, "portfolio_candidate_discovery")
    staging = _planning_step_preview(planning_steps, "portfolio_staging_performance")
    return {
        "stabilityMetricEligible": True,
        "expectedLifecycle": expected_lifecycle,
        "timelineCreated": timeline_created,
        "proposalMaterialCreated": proposal_material_created,
        "partialTimeline": partial_timeline,
        "fullTimeline": timeline_created and not partial_timeline,
        "nightViewExpectedCount": expected_night_count,
        "nightViewExpectedDayNumbers": expected_night_days,
        "nightViewScheduledCount": scheduled_count,
        "nightViewScheduledByDay": scheduled_by_day,
        "nightViewPersistedCount": persisted_count,
        "nightViewPersistedByDay": persisted_by_day,
        "nightViewCardinalityMismatch": cardinality_mismatch,
        "nightViewPersistedOverAllocation": persisted_over_allocation,
        "nightViewPersistenceMismatch": persistence_mismatch,
        "directiveRepairCount": directive_repair_count,
        "deterministicFallbackCount": deterministic_fallback_count,
        "cardinalityTerminalFailure": cardinality_terminal_failure,
        "groundingTerminalFailure": grounding_terminal_failure,
        "continuationScopeDrift": continuation_scope_drift,
        "pendingSlotScopeFingerprint": pending_scope_fingerprint,
        "versionWriteCount": version_write_count,
        "patchWriteCount": patch_write_count,
        "routeWriteCount": route_write_count,
        "unexpectedVersionWrite": unexpected_version_write,
        "unexpectedPatchWrite": unexpected_patch_write,
        "unexpectedRouteWrite": unexpected_route_write,
        "fakeOrNonAmapRouteAnchorCount": fake_or_non_amap_anchor_count,
        "unexpectedWrite": unexpected_write,
        "proposalVisibleCount": int(proposal_material.get("visibleProposalCount") or 0),
        "proposalAdoptionReadyCount": int(proposal_material.get("adoptionReadyProposalCount") or 0),
        "proposalPersistedSelectionCapabilityCount": int(
            proposal_material.get("persistedSelectionCapabilityCount") or 0
        ),
        "proposalRouteProviderAttemptCount": int(proposal_material.get("routeProviderAttemptCount") or 0),
        "proposalRouteCoverageComplete": bool(proposal_material.get("routeCoverageComplete")),
        "proposalRouteEvidenceFingerprintBound": bool(proposal_material.get("routeEvidenceFingerprintBound")),
        "webDiscoveryAttempted": int(discovery.get("webQueryCount") or 0) > 0,
        "webQueryCount": int(discovery.get("webQueryCount") or 0),
        "webSeedCount": int(discovery.get("webSeedCount") or 0),
        "webSeedAmapGroundingCount": int(discovery.get("webSeedAmapGroundingCount") or 0),
        "webOnlyFinalPoiCount": int(discovery.get("webOnlyFinalPoiCount") or 0),
        "fakeCoordinateCount": int(discovery.get("fakeCoordinateCount") or 0),
        "candidateBudgetUsed": int(discovery.get("candidateBudgetUsed") or 0),
        "candidateBudgetLimit": int(discovery.get("candidateBudgetLimit") or 24),
        "candidateBudgetExceeded": bool(
            int(discovery.get("candidateBudgetDeniedCount") or 0) > 0
            or int(discovery.get("candidateBudgetUsed") or 0) > int(discovery.get("candidateBudgetLimit") or 24)
        ),
        "requiredBudgetExceededCount": int(discovery.get("requiredBudgetExceededCount") or 0),
        "explicitMealBudgetExceededCount": int(discovery.get("explicitMealBudgetExceededCount") or 0),
        "maxConcurrentBriefWorkers": int(staging.get("maxConcurrentBriefWorkers") or 0),
        "visibleProposalCount": int(staging.get("visibleProposalCount") or 0),
        "visibleProposalBriefIds": list(staging.get("visibleProposalBriefIds") or []),
        "sameDayDuplicatePoiCount": int(staging.get("sameDayDuplicatePoiCount") or 0),
        "crossDayReuseCount": int(staging.get("crossDayReuseCount") or 0),
        "webDiscoveryMs": float(discovery.get("webDiscoveryMs") or 0.0),
        "routePreflightMs": float(staging.get("routePreflightMs") or 0.0),
        "stagingTotalMs": float(staging.get("stagingTotalMs") or 0.0),
        **_search_profile_stability_metrics(planning_steps),
    }


def _planning_step_preview(planning_steps: list[dict[str, Any]], step_type: str) -> dict[str, Any]:
    for step in planning_steps:
        if step.get("type") != step_type:
            continue
        preview = (step.get("metadata") or {}).get("resultPreview")
        if isinstance(preview, dict):
            return preview
    return {}


def _search_profile_stability_metrics(
    planning_steps: list[dict[str, Any]],
) -> dict[str, int]:
    compile_preview = _planning_step_preview(
        planning_steps,
        "compile_experience_search_profile",
    )
    discovery_preview = _planning_step_preview(
        planning_steps,
        "portfolio_candidate_discovery",
    )
    compile_keys = (
        "compiledSearchProfileCount",
        "distinctSearchProfileFingerprintCount",
        "familySearchProfileCoverageCount",
        "familySearchSemanticMismatchCount",
        "genericScenicCollapseCount",
    )
    discovery_keys = (
        "profileCoverageShortcutHitCount",
        "invalidCoverageShortcutCount",
        "consumerAdmissionCoverageRejectedCount",
        "semanticCandidateAcceptedCount",
        "semanticCandidateRejectedCount",
        "familySpecificAmapCandidateCount",
        "webSeedGroundedCandidateCount",
        "duplicateExcludedBeforeRouteCount",
        "routePreflightAvoidedBySemanticFilterCount",
    )
    return {
        **{key: int(compile_preview.get(key) or 0) for key in compile_keys},
        **{key: int(discovery_preview.get(key) or 0) for key in discovery_keys},
    }


def _search_profile_eval_metrics(
    case: dict[str, Any],
    planning_steps: list[dict[str, Any]],
    context: dict[str, Any],
    db_path: Path,
) -> dict[str, Any]:
    contract = case.get("searchProfileEval")
    if not isinstance(contract, dict) or not contract.get("required"):
        return {}

    session_id = str(_value(context, "session.sessionId") or "")
    planning_run_ids, planning_run_event_batches = _persisted_planning_run_event_batches(
        db_path,
        session_id,
    )
    persisted_run_events = [event for batch in planning_run_event_batches for event in batch]
    persisted_metric_events = _profile_metric_events(persisted_run_events)
    metric_cycle_count = _profile_metric_cycle_count(planning_run_event_batches)
    staged_verifier_cycle_count = _profile_staged_verifier_cycle_count(planning_run_event_batches)
    persisted_stage_events = [
        (
            str(event.get("type") or event.get("toolName") or event.get("id") or ""),
            event,
        )
        for event in persisted_run_events
    ]
    portfolio_generate_count = sum(stage == "creative_portfolio_provider" for stage, _event in persisted_stage_events)
    portfolio_stage_count = sum(
        stage == "stage_plan_portfolio" and str(event.get("status") or "") in {"completed", "needs_confirmation"}
        for stage, event in persisted_stage_events
    )
    verifier_rows = [
        row
        for stage, event in persisted_stage_events
        if stage == "portfolio_staging_performance"
        for row in _profile_verifier_rows(event)
    ]
    portfolio_verifier_count = len(verifier_rows)
    portfolio_verifier_failure_count = sum(
        not isinstance(row, dict)
        or (row.get("verifierPassed") is not True and row.get("draftVerifierPassed") is not True)
        for row in verifier_rows
    )
    portfolio_verifier_incomplete_evidence_count = sum(
        not isinstance(row, dict)
        or not isinstance(row.get("verifierPassed"), bool)
        for row in verifier_rows
    )
    metric_events = persisted_metric_events or planning_steps
    compile_previews = _planning_step_previews(
        metric_events,
        "compile_experience_search_profile",
    )
    discovery_previews = _planning_step_previews(
        metric_events,
        "portfolio_candidate_discovery",
    )
    compiled_profiles = [
        item for preview in compile_previews for item in preview.get("profileMetrics") or [] if isinstance(item, dict)
    ]
    profile_fingerprints = {
        str(item.get("searchProfileFingerprint") or "")
        for item in compiled_profiles
        if re.fullmatch(
            r"[0-9a-f]{64}",
            str(item.get("searchProfileFingerprint") or ""),
        )
    }
    compiled_count = len(compiled_profiles)
    distinct_count = len(profile_fingerprints)
    profile_ids = {
        str(item.get("searchProfileId") or "") for item in compiled_profiles if str(item.get("searchProfileId") or "")
    }
    profile_scopes = {
        (
            str(item.get("briefId") or ""),
            str(item.get("poolId") or ""),
            str(item.get("planningSlotId") or ""),
        )
        for item in compiled_profiles
        if all(str(item.get(key) or "") for key in ("briefId", "poolId", "planningSlotId"))
    }
    family_activity_contracts = {
        (
            str(item.get("experienceFamily") or ""),
            str(item.get("activityMode") or ""),
        )
        for item in compiled_profiles
        if str(item.get("experienceFamily") or "") and str(item.get("activityMode") or "")
    }
    family_activity_contract_coverage_count = sum(
        1
        for item in compiled_profiles
        if str(item.get("experienceFamily") or "") and str(item.get("activityMode") or "")
    )
    fingerprint_contracts: dict[str, set[tuple[str, str]]] = {}
    for item in compiled_profiles:
        fingerprint = str(item.get("searchProfileFingerprint") or "")
        contract_key = (
            str(item.get("experienceFamily") or ""),
            str(item.get("activityMode") or ""),
        )
        if fingerprint and all(contract_key):
            fingerprint_contracts.setdefault(fingerprint, set()).add(contract_key)
    fingerprint_contract_collisions = sum(max(len(contracts) - 1, 0) for contracts in fingerprint_contracts.values())
    family_coverage_count = sum(
        1
        for item in compiled_profiles
        if all(
            str(item.get(key) or "").strip()
            for key in (
                "searchProfileId",
                "searchProfileFingerprint",
                "experienceFamily",
                "briefId",
                "poolId",
                "planningSlotId",
            )
        )
    )
    compile_event_mismatches = sum(
        int(preview.get("compiledSearchProfileCount") or 0)
        != len([item for item in preview.get("profileMetrics") or [] if isinstance(item, dict)])
        or int(preview.get("distinctSearchProfileFingerprintCount") or 0)
        != len(
            {
                str(item.get("searchProfileFingerprint") or "")
                for item in preview.get("profileMetrics") or []
                if isinstance(item, dict)
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(item.get("searchProfileFingerprint") or ""),
                )
            }
        )
        for preview in compile_previews
    )
    semantic_contract_mismatches = 0
    exclusion_mismatches = 0
    compiled_checkpoint_mismatches = 0
    candidate_lineage_mismatches = 0

    checkpoints = _persisted_profile_checkpoints(db_path, session_id)
    pool_reports = [
        report
        for checkpoint in checkpoints
        for report in checkpoint.get("poolReports") or []
        if isinstance(report, dict)
    ]
    trace_reports = [report for report in pool_reports if isinstance(report.get("searchProfileTrace"), dict)]
    report_index: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for report in trace_reports:
        trace = report["searchProfileTrace"]
        key = (
            str(trace.get("profileId") or ""),
            str(trace.get("profileFingerprint") or ""),
            str(trace.get("briefId") or ""),
            str(trace.get("poolId") or ""),
            str(trace.get("planningSlotId") or ""),
        )
        report_index.setdefault(key, []).append(report)

        semantic_evidence = trace.get("semanticContractEvidence")
        semantic_fingerprint = str(trace.get("semanticContractFingerprint") or "")
        if not isinstance(semantic_evidence, dict) or semantic_fingerprint != _json_fingerprint(semantic_evidence):
            semantic_contract_mismatches += 1
            continue
        evidence_profile = semantic_evidence.get("profile") or {}
        exclusion = semantic_evidence.get("exclusion") or {}
        excluded_ids = [str(item) for item in exclusion.get("excludedPhysicalPoiIds") or [] if str(item)]
        canonical_excluded_ids = _canonical_profile_ids(excluded_ids)
        expected_exclusion_fingerprint = _compiler_fingerprint(
            {
                "schemaVersion": "poi-search-exclusions-v1",
                "excludedPhysicalPoiIds": canonical_excluded_ids,
            }
        )
        if (
            str(evidence_profile.get("profileId") or "") != str(trace.get("profileId") or "")
            or str(evidence_profile.get("profileFingerprint") or "") != str(trace.get("profileFingerprint") or "")
            or str(exclusion.get("exclusionFingerprint") or "") != str(trace.get("exclusionFingerprint") or "")
            or str(exclusion.get("exclusionFingerprint") or "") != expected_exclusion_fingerprint
            or int(exclusion.get("excludedPhysicalPoiCount") or 0) != len(excluded_ids)
            or excluded_ids != canonical_excluded_ids
            or str(semantic_evidence.get("executionFingerprint") or "") != str(trace.get("executionFingerprint") or "")
        ):
            exclusion_mismatches += 1

    for item in compiled_profiles:
        key = (
            str(item.get("searchProfileId") or ""),
            str(item.get("searchProfileFingerprint") or ""),
            str(item.get("briefId") or ""),
            str(item.get("poolId") or ""),
            str(item.get("planningSlotId") or ""),
        )
        if not report_index.get(key):
            compiled_checkpoint_mismatches += 1

    invalid_shortcuts = 0
    shortcut_evidence_count = 0
    noncanonical_candidate_count = 0
    excluded_physical_ids: set[str] = set()
    for report in trace_reports:
        trace = report["searchProfileTrace"]
        excluded_ids = {str(item) for item in trace.get("excludedPhysicalPoiIds") or [] if str(item)}
        excluded_physical_ids.update(excluded_ids)
        selected_candidates = [item for item in report.get("selectedCandidates") or [] if isinstance(item, dict)]
        for candidate in selected_candidates:
            longitude = candidate.get("longitude")
            latitude = candidate.get("latitude")
            canonical = bool(
                str(candidate.get("source") or "") == "amap-place-search"
                and re.fullmatch(
                    r"B[0-9A-Z]{8,31}",
                    str(candidate.get("amapId") or candidate.get("id") or ""),
                )
                and isinstance(longitude, (int, float))
                and not isinstance(longitude, bool)
                and math.isfinite(float(longitude))
                and -180 <= float(longitude) <= 180
                and isinstance(latitude, (int, float))
                and not isinstance(latitude, bool)
                and math.isfinite(float(latitude))
                and -90 <= float(latitude) <= 90
                and not (float(longitude) == 0 and float(latitude) == 0)
            )
            profile_lineage = bool(
                str(candidate.get("searchProfileId") or "") == str(trace.get("profileId") or "")
                and str(candidate.get("searchProfileFingerprint") or "") == str(trace.get("profileFingerprint") or "")
                and str(candidate.get("briefId") or "") == str(trace.get("briefId") or "")
                and str(candidate.get("poolId") or "") == str(trace.get("poolId") or "")
                and str(candidate.get("planningSlotId") or "") == str(trace.get("planningSlotId") or "")
            )
            if not canonical:
                noncanonical_candidate_count += 1
            if not profile_lineage:
                candidate_lineage_mismatches += 1
        shortcut = report.get("coverageShortcutEvidence")
        if isinstance(shortcut, dict):
            shortcut_evidence_count += 1
            target_count = int(
                ((trace.get("coveragePolicy") or {}).get("targetCount")) or report.get("profileTargetCount") or 0
            )
            evidence_target_count = int(
                ((trace.get("coveragePolicy") or {}).get("evidenceTargetCount"))
                or report.get("profileEvidenceTargetCount")
                or 0
            )
            if (
                str(shortcut.get("profileFingerprint") or "") != str(trace.get("profileFingerprint") or "")
                or int(shortcut.get("candidateCount") or 0) != len(selected_candidates)
                or int(shortcut.get("targetCount") or 0) != target_count
                or int(shortcut.get("evidenceTargetCount") or 0) != evidence_target_count
                or int(shortcut.get("excludedPhysicalPoiCount") or 0) != len(excluded_ids)
            ):
                invalid_shortcuts += 1

    discovery_totals = {
        key: sum(int(preview.get(key) or 0) for preview in discovery_previews)
        for key in (
            "profileCoverageShortcutHitCount",
            "invalidCoverageShortcutCount",
            "consumerAdmissionCoverageRejectedCount",
            "semanticCandidateAcceptedCount",
            "semanticCandidateRejectedCount",
            "familySpecificAmapCandidateCount",
            "webSeedGroundedCandidateCount",
            "duplicateExcludedBeforeRouteCount",
            "routePreflightAvoidedBySemanticFilterCount",
        )
    }
    checkpoint_semantic_rejections = sum(int(report.get("semanticRejectedCount") or 0) for report in trace_reports)
    checkpoint_duplicate_exclusions = sum(int(report.get("duplicateExcludedCount") or 0) for report in trace_reports)
    checkpoint_family_candidates = sum(
        int(report.get("familySpecificAmapCandidateCount") or 0) for report in trace_reports
    )
    checkpoint_web_seed_candidates = sum(
        int(report.get("webSeedGroundedCandidateCount") or 0) for report in trace_reports
    )
    discovery_totals["semanticCandidateRejectedCount"] = max(
        discovery_totals["semanticCandidateRejectedCount"],
        checkpoint_semantic_rejections,
    )
    discovery_totals["routePreflightAvoidedBySemanticFilterCount"] = max(
        discovery_totals["routePreflightAvoidedBySemanticFilterCount"],
        checkpoint_semantic_rejections,
    )
    discovery_totals["duplicateExcludedBeforeRouteCount"] = max(
        discovery_totals["duplicateExcludedBeforeRouteCount"],
        checkpoint_duplicate_exclusions,
    )
    discovery_totals["familySpecificAmapCandidateCount"] = max(
        discovery_totals["familySpecificAmapCandidateCount"],
        checkpoint_family_candidates,
    )
    discovery_totals["webSeedGroundedCandidateCount"] = max(
        discovery_totals["webSeedGroundedCandidateCount"],
        checkpoint_web_seed_candidates,
    )
    invalid_shortcuts += abs(discovery_totals["profileCoverageShortcutHitCount"] - shortcut_evidence_count)
    invalid_shortcuts += discovery_totals["invalidCoverageShortcutCount"]

    runtime_trace = context.get("runtimeTrace") or {}
    adapter_adoption_count, adapter_mismatch_count = _adapter_plan_adoption_counts(runtime_trace)
    before_counts = context.get("profileDbBefore")
    db_baseline_mismatch_count = 0
    after_counts = _session_write_counts(db_path, session_id)
    if not isinstance(before_counts, dict):
        before_counts = {"version": -1, "patch": -1, "route": -1}
        db_baseline_mismatch_count = 1
    lineage_mismatches = sum(
        (
            compile_event_mismatches,
            semantic_contract_mismatches,
            exclusion_mismatches,
            compiled_checkpoint_mismatches,
            candidate_lineage_mismatches,
            adapter_mismatch_count,
            db_baseline_mismatch_count,
        )
    )

    return {
        "searchProfileMetricEligible": True,
        "compiledSearchProfileCount": compiled_count,
        "distinctSearchProfileFingerprintCount": distinct_count,
        "uniqueSearchProfileIdCount": len(profile_ids),
        "uniqueSearchProfileScopeCount": len(profile_scopes),
        "familyActivitySearchContractCount": len(family_activity_contracts),
        "familyActivitySearchContractCoverageCount": (family_activity_contract_coverage_count),
        "profileFingerprintContractCollisionCount": (fingerprint_contract_collisions),
        "familySearchProfileCoverageCount": family_coverage_count,
        "familySearchSemanticMismatchCount": sum(
            int(preview.get("familySearchSemanticMismatchCount") or 0) for preview in compile_previews
        ),
        "genericScenicCollapseCount": sum(
            int(preview.get("genericScenicCollapseCount") or 0) for preview in compile_previews
        ),
        **discovery_totals,
        "invalidCoverageShortcutCount": invalid_shortcuts,
        "profileLineageMismatchCount": lineage_mismatches,
        "profileCompileEventMismatchCount": compile_event_mismatches,
        "profileSemanticContractMismatchCount": semantic_contract_mismatches,
        "profileExclusionMismatchCount": exclusion_mismatches,
        "profileCompiledCheckpointMismatchCount": (compiled_checkpoint_mismatches),
        "profileCandidateLineageMismatchCount": candidate_lineage_mismatches,
        "profileAdapterMismatchCount": adapter_mismatch_count,
        "profileDbBaselineMismatchCount": db_baseline_mismatch_count,
        "nonCanonicalAmapProfileCandidateCount": noncanonical_candidate_count,
        "profileCheckpointCount": len(checkpoints),
        "profileCheckpointReportCount": len(trace_reports),
        "profilePlanningRunReferenceCount": len(planning_run_ids),
        "profilePlanningRunLoadedCount": len(planning_run_event_batches),
        "profilePlanningRunMetricEventCount": len(persisted_metric_events),
        "profilePlanningRunMetricCycleCount": metric_cycle_count,
        "profilePlanningRunStagedVerifierCycleCount": (staged_verifier_cycle_count),
        "profileExcludedPhysicalPoiCount": len(excluded_physical_ids),
        "profileCoverageShortcutEvidenceCount": shortcut_evidence_count,
        "profileAdapterCallCount": len(runtime_trace.get("adapterCalls") or []),
        "profileAdapterPlanAdoptionCount": adapter_adoption_count,
        "profileWebProviderCallCount": len(runtime_trace.get("webCalls") or []),
        "profileAmapProviderCallCount": len(runtime_trace.get("amapCalls") or []),
        "profilePortfolioGenerateCallCount": portfolio_generate_count,
        "profilePortfolioStageCallCount": portfolio_stage_count,
        "profilePortfolioVerifierCallCount": portfolio_verifier_count,
        "profilePortfolioVerifierFailureCount": (portfolio_verifier_failure_count),
        "profilePortfolioVerifierIncompleteEvidenceCount": portfolio_verifier_incomplete_evidence_count,
        "preAdoptionVersionWriteCount": int(after_counts["version"]) - int(before_counts["version"]),
        "preAdoptionPatchWriteCount": int(after_counts["patch"]) - int(before_counts["patch"]),
        "preAdoptionRouteWriteCount": int(after_counts["route"]) - int(before_counts["route"]),
    }


def _planning_step_previews(
    planning_steps: list[dict[str, Any]],
    step_type: str,
) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for step in planning_steps:
        stage = str(step.get("type") or step.get("toolName") or step.get("id") or "")
        if stage != step_type:
            continue
        metadata = step.get("metadata") or {}
        preview = metadata.get("resultPreview")
        if not isinstance(preview, dict):
            preview = step.get("traceSummary")
        if isinstance(preview, dict):
            previews.append(preview)
    return previews


def _persisted_profile_metric_events(
    db_path: Path,
    session_id: str,
) -> list[dict[str, Any]]:
    _run_ids, event_batches = _persisted_planning_run_event_batches(
        db_path,
        session_id,
    )
    return _profile_metric_events([event for batch in event_batches for event in batch])


def _persisted_planning_run_event_batches(
    db_path: Path,
    session_id: str,
) -> tuple[list[str], list[list[dict[str, Any]]]]:
    with _open_db(db_path) as connection:
        turn_rows = connection.execute(
            """SELECT planning_run_id FROM conversation_turns
               WHERE session_id = ? AND role = 'assistant'
                 AND planning_run_id IS NOT NULL
               ORDER BY turn_index, created_at""",
            (session_id,),
        ).fetchall()
        run_ids = list(
            dict.fromkeys(str(row["planning_run_id"] or "") for row in turn_rows if str(row["planning_run_id"] or ""))
        )
        event_batches: list[list[dict[str, Any]]] = []
        for run_id in run_ids:
            row = connection.execute(
                "SELECT tool_calls_json FROM planning_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                continue
            try:
                payload = json.loads(str(row["tool_calls_json"] or "[]"))
            except json.JSONDecodeError:
                payload = []
            event_batches.append(
                [event for event in (payload if isinstance(payload, list) else []) if isinstance(event, dict)]
            )
    return run_ids, event_batches


def _profile_metric_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stages = {
        "compile_experience_search_profile",
        "portfolio_candidate_discovery",
    }
    return [
        event for event in events if str(event.get("type") or event.get("toolName") or event.get("id") or "") in stages
    ]


def _profile_metric_cycle_count(
    event_batches: list[list[dict[str, Any]]],
) -> int:
    complete = 0
    for batch in event_batches:
        stages = [str(event.get("type") or event.get("toolName") or event.get("id") or "") for event in batch]
        try:
            compile_index = stages.index("compile_experience_search_profile")
            discovery_index = stages.index("portfolio_candidate_discovery")
        except ValueError:
            continue
        complete += int(compile_index < discovery_index)
    return complete


def _profile_verifier_rows(event: dict[str, Any]) -> list[Any]:
    trace_summary = event.get("traceSummary")
    if not isinstance(trace_summary, dict):
        return []
    rows = trace_summary.get("briefMetrics")
    return rows if isinstance(rows, list) else []


def _profile_staged_verifier_cycle_count(
    event_batches: list[list[dict[str, Any]]],
) -> int:
    complete = 0
    for batch in event_batches:
        stages = [str(event.get("type") or event.get("toolName") or event.get("id") or "") for event in batch]
        try:
            compile_index = stages.index("compile_experience_search_profile")
            discovery_index = stages.index("portfolio_candidate_discovery")
            verifier_index = stages.index("portfolio_staging_performance")
            stage_index = stages.index("stage_plan_portfolio")
        except ValueError:
            continue
        verifier_rows = _profile_verifier_rows(batch[verifier_index])
        stage_event = batch[stage_index]
        complete += int(
            compile_index < discovery_index < verifier_index < stage_index
            and str(stage_event.get("status") or "") in {"completed", "needs_confirmation"}
            and bool(verifier_rows)
            and all(isinstance(row, dict) and isinstance(row.get("verifierPassed"), bool) for row in verifier_rows)
        )
    return complete


def _latest_profile_replay_steps(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        stage = str(event.get("type") or event.get("toolName") or event.get("id") or "")
        if stage == "compile_experience_search_profile":
            return events[index:]
    return events


def _persisted_profile_checkpoints(
    db_path: Path,
    session_id: str,
) -> list[dict[str, Any]]:
    with _open_db(db_path) as connection:
        rows = connection.execute(
            """SELECT agent_response_json FROM conversation_turns
               WHERE session_id = ? AND role = 'assistant'
               ORDER BY turn_index, created_at""",
            (session_id,),
        ).fetchall()
    checkpoints: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["agent_response_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        checkpoint = payload.get("groundingCheckpoint")
        if isinstance(checkpoint, dict):
            checkpoints.append(checkpoint)
    return checkpoints


def _persisted_server_initial_plan(
    db_path: Path,
    session_id: str,
) -> dict[str, Any]:
    """Read the latest server-persisted creative initial plan without replaying it.

    This is capture-preflight evidence only.  The helper never promotes a plan
    into selected POIs, route pairs, or a transport mode; those remain blocked
    until the recorded Place replay has produced canonical identities.
    """

    if not session_id:
        return {}
    with _open_db(db_path) as connection:
        rows = connection.execute(
            """SELECT id, turn_index, agent_response_json FROM conversation_turns
               WHERE session_id = ? AND role = 'assistant' AND status = 'active'
                 AND agent_response_json IS NOT NULL
               ORDER BY turn_index, created_at""",
            (session_id,),
        ).fetchall()
    for row in reversed(rows):
        try:
            payload = json.loads(str(row["agent_response_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        initial_plan = payload.get("initialPlan") if isinstance(payload, dict) else None
        if not isinstance(initial_plan, dict) or str(initial_plan.get("mode") or "") != "initial_plan":
            continue
        pipeline_context = payload.get("pipelineContext")
        return {
            "assistantTurnId": str(row["id"] or ""),
            "turnIndex": int(row["turn_index"] or 0),
            "initialPlan": deepcopy(initial_plan),
            "pipelineContext": deepcopy(pipeline_context) if isinstance(pipeline_context, dict) else {},
        }
    return {}


def _persisted_clarification_choice_for_replay(
    db_path: Path,
    *,
    session_id: str,
    source_assistant_turn_id: str,
    dimension_id: str,
    semantic_value_fingerprint: str,
) -> dict[str, Any]:
    """Select one current persisted option by semantic hash before any POST."""

    if not session_id or not source_assistant_turn_id:
        raise AssertionError("persisted clarification replay requires a current source assistant turn")
    if dimension_id != "route_decision.detour_tolerance":
        raise AssertionError("persisted clarification replay dimension is outside the dedicated contract")
    requested_fingerprint = semantic_value_fingerprint.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", requested_fingerprint) is None:
        raise AssertionError("persisted clarification replay requires a canonical SHA-256 selector")

    from src.services.clarification_checkpoint_service import ClarificationCheckpointService

    with _open_db(db_path) as connection:
        row = connection.execute(
            """SELECT id, session_id, role, status, turn_index,
                      agent_request_json, agent_response_json
               FROM conversation_turns WHERE id = ? AND session_id = ?""",
            (source_assistant_turn_id, session_id),
        ).fetchone()
        latest_active_assistant = connection.execute(
            """SELECT id FROM conversation_turns
               WHERE session_id = ? AND role = 'assistant' AND status = 'active'
               ORDER BY turn_index DESC, created_at DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        existing_execution = connection.execute(
            """SELECT id, status FROM agent_choice_executions
               WHERE session_id = ? AND source_turn_id = ?""",
            (session_id, source_assistant_turn_id),
        ).fetchall()
    if row is None or str(row["role"] or "") != "assistant" or str(row["status"] or "") != "active":
        raise AssertionError("persisted clarification source turn is missing, stale, or not active")
    if latest_active_assistant is None or str(latest_active_assistant["id"] or "") != source_assistant_turn_id:
        raise AssertionError("persisted clarification source is not the latest active assistant turn")
    if existing_execution:
        raise AssertionError("persisted clarification source turn already has a consumed choice execution")
    try:
        response_payload = json.loads(str(row["agent_response_json"] or "{}"))
        request_payload = json.loads(str(row["agent_request_json"] or "{}"))
    except json.JSONDecodeError as error:
        raise AssertionError("persisted clarification source JSON is malformed") from error
    if not isinstance(response_payload, dict) or not isinstance(request_payload, dict):
        raise AssertionError("persisted clarification source payload must be an object")
    checkpoint = response_payload.get("clarificationCheckpoint")
    if not isinstance(checkpoint, dict):
        raise AssertionError("persisted clarification checkpoint is missing")
    source_contract = request_payload.get("requestIntentContract")
    if not isinstance(source_contract, dict) or not source_contract:
        raise AssertionError("persisted clarification source request contract is missing")
    checkpoint_fingerprint = str(checkpoint.get("fingerprint") or "")
    request_fingerprint = str(checkpoint.get("requestFingerprint") or "")
    if (
        checkpoint.get("schemaVersion") not in ClarificationCheckpointService.SUPPORTED_SCHEMA_VERSIONS
        or not checkpoint_fingerprint
        or checkpoint_fingerprint
        != ClarificationCheckpointService._fingerprint(
            {key: value for key, value in checkpoint.items() if key != "fingerprint"}
        )
        or not request_fingerprint
        or request_fingerprint != ClarificationCheckpointService._fingerprint(source_contract)
    ):
        raise AssertionError("persisted clarification checkpoint or request fingerprint is invalid")
    dimensions = [
        item
        for item in source_contract.get("clarificationDimensions") or []
        if isinstance(item, dict)
        and str(item.get("dimensionId") or "") == dimension_id
        and str(item.get("status") or "unresolved") == "unresolved"
    ]
    if len(dimensions) != 1 or dimensions[0].get("allowedSemanticFields") != [
        "detourTolerance",
        "adjacentLegConstraint",
    ]:
        raise AssertionError("persisted clarification dimension no longer matches the source contract")
    if (
        str(checkpoint.get("status") or "") != "awaiting_answer"
        or str(checkpoint.get("sourceAssistantTurnId") or "") != source_assistant_turn_id
        or str(checkpoint.get("nextQuestionDimensionId") or "") != dimension_id
    ):
        raise AssertionError("persisted clarification checkpoint is stale or belongs to another turn")
    consumed_ids = {str(item) for item in response_payload.get("consumedChoiceIds") or []}
    choices = [
        item
        for item in response_payload.get("choiceOptions") or []
        if isinstance(item, dict)
        and str(item.get("kind") or "") == "clarification_checkpoint"
        and str(item.get("action") or "") == "continue_clarification"
        and str(item.get("dimensionId") or "") == dimension_id
    ]
    question = checkpoint.get("question") if isinstance(checkpoint.get("question"), dict) else {}
    controller_options = [item for item in question.get("options") or [] if isinstance(item, dict)]
    if len(choices) < 2 or len(controller_options) < 2 or len(choices) != len(controller_options):
        raise AssertionError("persisted clarification question did not retain two Controller options")
    choice_ids = [str(item.get("id") or "") for item in choices]
    if not all(choice_ids) or len(choice_ids) != len(set(choice_ids)):
        raise AssertionError("persisted clarification choice identities are missing or ambiguous")
    controller_by_choice_id = {
        f"clarification:{checkpoint.get('checkpointId')}:{option.get('id')}": option
        for option in controller_options
        if str(option.get("id") or "")
    }
    if len(controller_by_choice_id) != len(controller_options):
        raise AssertionError("persisted Controller option identities are missing or ambiguous")
    for choice in choices:
        controller_option = controller_by_choice_id.get(str(choice.get("id") or ""))
        if (
            not isinstance(controller_option, dict)
            or str(choice.get("checkpointId") or "") != str(checkpoint.get("checkpointId") or "")
            or str(choice.get("planningSelectionRootTurnId") or "") != str(checkpoint.get("planningRootId") or "")
            or str(choice.get("id") or "") in consumed_ids
            or choice.get("semanticValue") != controller_option.get("semanticValue")
            or str(choice.get("label") or "") != str(controller_option.get("label") or "")
        ):
            raise AssertionError("persisted clarification option identity or lifecycle is invalid")
    source_user_turn_id = str(checkpoint.get("sourceUserTurnId") or "")
    with _open_db(db_path) as connection:
        source_user = connection.execute(
            """SELECT id FROM conversation_turns
               WHERE id = ? AND session_id = ? AND role = 'user' AND status = 'active'
                 AND turn_index < ?""",
            (source_user_turn_id, session_id, int(row["turn_index"] or 0)),
        ).fetchone()
    if (
        source_user is None
        or str(checkpoint.get("planningRootId") or "") != source_user_turn_id
        or (
            request_payload.get("planningSelectionRootTurnId") not in (None, "")
            and str(request_payload.get("planningSelectionRootTurnId") or "") != source_user_turn_id
        )
    ):
        raise AssertionError("persisted clarification source user turn or planning root is invalid")
    matches = [
        choice
        for choice in choices
        if _canonical_response_sha256(choice.get("semanticValue") or {}) == requested_fingerprint
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"persisted clarification semantic fingerprint matched {len(matches)} choices; expected exactly one"
        )
    selected = matches[0]
    return {
        "choiceId": str(selected["id"]),
        "dimensionId": dimension_id,
        "checkpointId": str(checkpoint.get("checkpointId") or ""),
        "checkpointFingerprint": checkpoint_fingerprint,
        "sourceUserTurnId": source_user_turn_id,
        "checkpointSourceAssistantTurnId": str(checkpoint.get("sourceAssistantTurnId") or ""),
        "persistedChoiceCount": len(choices),
        "semanticValue": deepcopy(selected.get("semanticValue")),
        "requestedSemanticValueFingerprint": requested_fingerprint,
        "selectedSemanticValueFingerprint": _canonical_response_sha256(selected.get("semanticValue") or {}),
        "selectedOptionFingerprint": _canonical_response_sha256(selected),
        "initialRequestIntentContract": deepcopy(request_payload.get("requestIntentContract") or {}),
    }


def _persisted_clarification_choice_result(
    db_path: Path,
    *,
    session_id: str,
    source_assistant_turn_id: str,
    response_assistant_turn_id: str,
    persisted: dict[str, Any],
    submitted_context: dict[str, Any],
    before_counts: dict[str, int],
) -> dict[str, Any]:
    if not response_assistant_turn_id:
        raise AssertionError("opaque clarification response did not persist an assistant turn")
    with _open_db(db_path) as connection:
        response_row = connection.execute(
            """SELECT turn_index, agent_request_json FROM conversation_turns
               WHERE id = ? AND session_id = ? AND role = 'assistant' AND status = 'active'""",
            (response_assistant_turn_id, session_id),
        ).fetchone()
        source_row = connection.execute(
            """SELECT agent_response_json FROM conversation_turns
               WHERE id = ? AND session_id = ? AND role = 'assistant'""",
            (source_assistant_turn_id, session_id),
        ).fetchone()
        execution = connection.execute(
            """SELECT source_user_turn_id, action, status, execution_turn_id,
                      request_turn_id, checkpoint_fingerprint
               FROM agent_choice_executions
               WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?""",
            (session_id, source_assistant_turn_id, persisted["choiceId"]),
        ).fetchone()
        request_user_turn = (
            connection.execute(
                """SELECT id FROM conversation_turns
                   WHERE session_id = ? AND role = 'user' AND status = 'active'
                     AND turn_index < ? ORDER BY turn_index DESC LIMIT 1""",
                (session_id, int(response_row["turn_index"] or 0)),
            ).fetchone()
            if response_row is not None
            else None
        )
        session = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if response_row is None or source_row is None or execution is None or request_user_turn is None or session is None:
        raise AssertionError("opaque clarification persistence evidence is incomplete")
    try:
        response_request = json.loads(str(response_row["agent_request_json"] or "{}"))
        updated_source = json.loads(str(source_row["agent_response_json"] or "{}"))
    except json.JSONDecodeError as error:
        raise AssertionError("opaque clarification persistence evidence is malformed") from error
    if persisted["choiceId"] not in {str(item) for item in updated_source.get("consumedChoiceIds") or []}:
        raise AssertionError("opaque clarification choice was not marked consumed on its source turn")
    selected_request = response_request.get("selectedAgentChoice")
    selected_request_option = selected_request.get("option") if isinstance(selected_request, dict) else None
    if (
        not isinstance(selected_request, dict)
        or str(selected_request.get("sourceAssistantTurnId") or "") != source_assistant_turn_id
        or str(selected_request.get("choiceId") or "") != persisted["choiceId"]
        or str(selected_request.get("requestChoiceId") or "") != persisted["choiceId"]
        or str(selected_request.get("persistedChoiceId") or "") != persisted["choiceId"]
        or selected_request.get("manualValue") not in (None, "")
        or not isinstance(selected_request_option, dict)
        or _canonical_response_sha256(selected_request_option) != persisted["selectedOptionFingerprint"]
        or str(execution["status"] or "") != "succeeded"
        or str(execution["action"] or "") != "continue_clarification"
        or str(execution["source_user_turn_id"] or "") != persisted["sourceUserTurnId"]
        or str(execution["execution_turn_id"] or "") != response_assistant_turn_id
        or str(execution["request_turn_id"] or "") != str(request_user_turn["id"] or "")
        or (
            execution["checkpoint_fingerprint"] not in (None, "")
            and str(execution["checkpoint_fingerprint"]) != persisted["checkpointFingerprint"]
        )
    ):
        raise AssertionError("opaque clarification execution identity or persisted request is invalid")
    final_contract = response_request.get("requestIntentContract")
    if not isinstance(final_contract, dict):
        raise AssertionError("opaque clarification response did not persist the request contract")
    resolved = [
        item
        for item in final_contract.get("clarificationAnswers") or []
        if isinstance(item, dict) and str(item.get("dimensionId") or "") == persisted["dimensionId"]
    ]
    if len(resolved) != 1:
        raise AssertionError("opaque clarification answer is missing or ambiguous")
    answer = resolved[0]
    if answer.get("semanticValue") != persisted["semanticValue"] or str(answer.get("source") or "") != (
        "structured_option"
    ):
        raise AssertionError("opaque clarification answer does not match the persisted selected option")
    route_contract = final_contract.get("routeDecisionContract")
    if not isinstance(route_contract, dict):
        raise AssertionError("opaque clarification response did not compile a route contract")
    route_contract_trace = {
        "status": str(route_contract.get("status") or ""),
        "missingFields": [str(item) for item in route_contract.get("missingFields") or []],
        "preferredMode": str((route_contract.get("provenance") or {}).get("transportMode") or ""),
        "detourToleranceSource": str(route_contract.get("detourToleranceSource") or ""),
        "fingerprint": str(route_contract.get("fingerprint") or ""),
    }
    route_contract_fingerprint = str(route_contract.get("fingerprint") or "")
    if re.fullmatch(r"[0-9a-f]{64}", route_contract_fingerprint) is None:
        raise AssertionError("opaque clarification route contract fingerprint is missing or malformed")
    initial_contract = persisted.get("initialRequestIntentContract") or {}
    initial_experience_specs = [
        item for item in initial_contract.get("experienceSpecs") or [] if isinstance(item, dict)
    ]
    experience_specs = [item for item in final_contract.get("experienceSpecs") or [] if isinstance(item, dict)]
    night_specs = [item for item in experience_specs if str(item.get("intentType") or "") == "night_view"]
    after_counts = _session_write_counts(db_path, session_id)
    return {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "checkpointSourceAssistantTurnId": persisted["checkpointSourceAssistantTurnId"],
        "checkpointFingerprint": persisted["checkpointFingerprint"],
        "choiceId": persisted["choiceId"],
        "dimensionId": persisted["dimensionId"],
        "persistedChoiceCount": persisted["persistedChoiceCount"],
        "requestedSemanticValueFingerprint": persisted["requestedSemanticValueFingerprint"],
        "selectedSemanticValueFingerprint": persisted["selectedSemanticValueFingerprint"],
        "submittedContext": deepcopy(submitted_context),
        "resolvedAnswer": {
            "dimensionId": str(answer.get("dimensionId") or ""),
            "source": str(answer.get("source") or ""),
            "semanticValueFingerprint": _canonical_response_sha256(answer.get("semanticValue") or {}),
        },
        "resolvedAnswerMatchesPersistedOption": answer.get("semanticValue") == persisted["semanticValue"],
        "routeDecisionContract": route_contract_trace,
        "nightViewOccurrenceCount": len(night_specs),
        "publicCityViewExperienceSpecCount": sum(
            1
            for item in night_specs
            if item.get("frequency") == "one" and item.get("experienceFamilies") == ["public_city_view"]
        ),
        "experienceSpecDetourToleranceCount": sum(
            1 for item in experience_specs if isinstance(item.get("detourTolerance"), dict)
        ),
        "initialExperienceSpecDetourToleranceCount": sum(
            1 for item in initial_experience_specs if isinstance(item.get("detourTolerance"), dict)
        ),
        "choiceExecutionStatus": str(execution["status"] or ""),
        "persistedRequestChoiceMatchesSubmittedIds": True,
        "activeVersionId": session["active_version_id"],
        "writeDelta": {key: int(after_counts[key]) - int(before_counts[key]) for key in ("version", "patch", "route")},
    }


def _json_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_profile_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFKC", str(raw)),
        ).strip()
        key = value.casefold()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return sorted(result, key=str.casefold)


def _compiler_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _adapter_plan_adoption_counts(
    runtime_trace: dict[str, Any],
) -> tuple[int, int]:
    web_calls = [item for item in runtime_trace.get("webCalls") or [] if isinstance(item, dict)]
    amap_search_calls = [item for item in runtime_trace.get("amapSearchCalls") or [] if isinstance(item, dict)]
    low_level_amap_calls = [
        item
        for item in runtime_trace.get("amapCalls") or []
        if isinstance(item, dict) and isinstance(item.get("params"), dict)
    ]
    adopted_keys: set[tuple[str, str, str]] = set()
    mismatch_keys: set[tuple[str, str, str]] = set()
    for call in runtime_trace.get("adapterCalls") or []:
        if not isinstance(call, dict):
            continue
        profile = call.get("profile") or {}
        source_plans = {
            str(item.get("planId") or ""): item for item in profile.get("queryPlans") or [] if isinstance(item, dict)
        }
        adapted_plans = [item for item in call.get("adaptedPlans") or [] if isinstance(item, dict)]
        for plan_id, source_plan in source_plans.items():
            if str(source_plan.get("mode") or "") != "web_seed_then_amap":
                continue
            keyword = str(source_plan.get("keyword") or "")
            adoption_key = (
                str(profile.get("profileId") or profile.get("id") or profile.get("fingerprint") or ""),
                plan_id,
                keyword,
            )
            adapted_categories = {
                str(item.get("category") or "")
                for item in adapted_plans
                if str(item.get("sourcePlanId") or "") == plan_id
                and str(item.get("keyword") or "") == keyword
                and str(item.get("providerCategoryKey") or "")
                and str(item.get("category") or "")
            }
            matching_web_calls = [item for item in web_calls if keyword and keyword in str(item.get("query") or "")]
            seed_names = {str(seed) for item in matching_web_calls for seed in item.get("seedNames") or [] if str(seed)}
            matching_searches = [
                item
                for item in amap_search_calls
                if str(item.get("keyword") or "") in seed_names
                and str(item.get("category") or "") in adapted_categories
            ]
            low_level_grounding = any(
                str((item.get("params") or {}).get("keywords") or "") in seed_names
                and bool(str((item.get("params") or {}).get("types") or ""))
                for item in low_level_amap_calls
            )
            if adapted_categories and matching_web_calls and seed_names and matching_searches and low_level_grounding:
                adopted_keys.add(adoption_key)
            elif matching_web_calls:
                mismatch_keys.add(adoption_key)
    mismatch_keys.difference_update(adopted_keys)
    return len(adopted_keys), len(mismatch_keys)


def _scheduled_goal_counts(planning_steps: list[dict[str, Any]], goal_id: str) -> dict[int, int]:
    for step in planning_steps:
        if step.get("type") != "agent_decision":
            continue
        directive = (step.get("metadata") or {}).get("actionDirective") or {}
        if directive.get("type") != "draft_itinerary":
            continue
        result: dict[int, int] = {}
        for strategy in directive.get("dayStrategies") or []:
            day_number = int(strategy.get("dayNumber") or 0)
            if day_number <= 0:
                continue
            counts = strategy.get("requiredGoalCounts") or {}
            required_ids = strategy.get("requiredGoalIds") or []
            count = int(counts[goal_id]) if goal_id in counts else (1 if goal_id in required_ids else 0)
            if count > 0:
                result[day_number] = count
        return result
    return {}


def _draft_decision_metadata(planning_steps: list[dict[str, Any]]) -> dict[str, Any]:
    for step in planning_steps:
        metadata = step.get("metadata") or {}
        if (
            step.get("type") == "agent_decision"
            and (metadata.get("actionDirective") or {}).get("type") == "draft_itinerary"
        ):
            return metadata
    return {}


def _persisted_intent_counts(body: dict[str, Any], intent_type: str) -> dict[int, int]:
    itinerary = body.get("itinerary") if isinstance(body.get("itinerary"), dict) else {}
    result: dict[int, int] = {}
    for day in itinerary.get("days") or []:
        day_number = int(day.get("dayNumber") or 0)
        if day_number <= 0:
            continue
        count = 0
        for segment in day.get("segments") or []:
            semantic = segment.get("semanticMetadata") or {}
            segment_intent = segment.get("intentType") or semantic.get("intentType")
            if segment_intent == intent_type:
                count += 1
        if count:
            result[day_number] = count
    return result


def _fake_or_non_amap_route_anchor_count(body: dict[str, Any]) -> int:
    itinerary = body.get("itinerary") if isinstance(body.get("itinerary"), dict) else {}
    count = 0
    for day in itinerary.get("days") or []:
        for segment in day.get("segments") or []:
            semantic = segment.get("semanticMetadata") or {}
            metadata = segment.get("metadata") or {}
            poi = segment.get("poi") or {}
            grounding = poi.get("grounding") or {}
            route_anchor = bool(
                segment.get("routeAnchor")
                or semantic.get("routeAnchor")
                or metadata.get("routeAnchor")
                or grounding.get("routeAnchor")
            )
            if not route_anchor:
                continue
            source = str(poi.get("source") or "").lower()
            if (
                not str(poi.get("amapId") or "").strip()
                or poi.get("latitude") is None
                or poi.get("longitude") is None
                or source != "amap-place-search"
            ):
                count += 1
    return count


def dual_adoption_golden_failures(
    metrics: dict[str, Any],
    contract: dict[str, Any],
) -> list[str]:
    """Validate replayed dual-proposal adoption evidence against a golden contract."""

    if not contract.get("required"):
        return []
    if not metrics:
        return ["required dual-adoption golden metrics are missing"]

    failures: list[str] = []
    expected_metrics_schema = contract.get("expectedMetricsSchemaVersion")
    if expected_metrics_schema is not None and metrics.get("metricsSchemaVersion") != expected_metrics_schema:
        failures.append("dual-adoption metrics schema differs from the golden contract")
    minimum_fields = (
        (
            "persistedProposalCount",
            "minimumPersistedProposalCount",
            "persisted proposal count",
        ),
        (
            "completeAdoptionReadyProposalCount",
            "minimumCompleteAdoptionReadyProposalCount",
            "complete adoption-ready proposal count",
        ),
        (
            "nightViewCoverage2of2ProposalCount",
            "minimumNightViewCoverage2of2ProposalCount",
            "night-view 2/2 proposal count",
        ),
        (
            "mealCoverageEveryDayProposalCount",
            "minimumMealCoverageEveryDayProposalCount",
            "daily meal-coverage proposal count",
        ),
        (
            "distinctThemeFamilySetCount",
            "minimumDistinctThemeFamilySetCount",
            "distinct theme-family set count",
        ),
        (
            "materialNoveltyAuditPassedCount",
            "minimumMaterialNoveltyAuditPassedCount",
            "material novelty audit count",
        ),
        (
            "titleGenerationSucceededCount",
            "minimumTitleGenerationSucceededCount",
            "Agent title-generation success count",
        ),
        (
            "validAgentTitleEvidenceCount",
            "minimumValidAgentTitleEvidenceCount",
            "valid Agent title-evidence count",
        ),
    )
    for metric_key, contract_key, label in minimum_fields:
        if int(metrics.get(metric_key) or 0) < int(contract.get(contract_key) or 0):
            failures.append(f"dual-adoption {label} is below required minimum")

    exact_fields = (
        ("visibleProposalCount", "expectedVisibleProposalCount", "visible proposal count"),
        (
            "dailyTargetActualMismatchCount",
            "expectedDailyTargetActualMismatchCount",
            "daily target/actual mismatch count",
        ),
        ("pendingSlotCount", "expectedPendingSlotCount", "pending-slot count"),
        (
            "routePairCoverageMismatchCount",
            "expectedRoutePairCoverageMismatchCount",
            "route-pair coverage mismatch count",
        ),
        (
            "nonPositiveProviderRouteLegCount",
            "expectedNonPositiveProviderRouteLegCount",
            "non-positive provider route-leg count",
        ),
        (
            "nonCanonicalProviderRouteLegCount",
            "expectedNonCanonicalProviderRouteLegCount",
            "non-canonical provider route-leg count",
        ),
        ("selectedProposalOrdinal", "expectedSelectedProposalOrdinal", "selected proposal ordinal"),
        (
            "uniqueProposalTitleCount",
            "expectedUniqueProposalTitleCount",
            "unique proposal-title count",
        ),
        (
            "unselectedCommittedProposalCount",
            "expectedUnselectedCommittedProposalCount",
            "unselected committed proposal count",
        ),
        (
            "canonicalSignatureMismatchCount",
            "expectedCanonicalSignatureMismatchCount",
            "canonical-signature mismatch count",
        ),
        (
            "recomputedCanonicalDuplicateCount",
            "expectedRecomputedCanonicalDuplicateCount",
            "recomputed canonical duplicate count",
        ),
        (
            "scoreVectorMismatchCount",
            "expectedScoreVectorMismatchCount",
            "score-vector mismatch count",
        ),
        (
            "scoreEvidenceMismatchCount",
            "expectedScoreEvidenceMismatchCount",
            "score-evidence mismatch count",
        ),
        (
            "scoreEvidenceMissingCount",
            "expectedScoreEvidenceMissingCount",
            "score-evidence missing count",
        ),
        (
            "lineageIntegrityMismatchCount",
            "expectedLineageIntegrityMismatchCount",
            "lineage-integrity mismatch count",
        ),
        (
            "artifactFailureCount",
            "expectedArtifactFailureCount",
            "artifact failure count",
        ),
        (
            "artifactRunCount",
            "expectedArtifactRunCount",
            "artifact run count",
        ),
        (
            "artifactReplaySuccessCount",
            "expectedArtifactReplaySuccessCount",
            "artifact replay-success count",
        ),
        (
            "artifactCanonicalSignatureMismatchCount",
            "expectedArtifactCanonicalSignatureMismatchCount",
            "artifact canonical-signature mismatch count",
        ),
        (
            "artifactScoreEvidenceMissingCount",
            "expectedArtifactScoreEvidenceMissingCount",
            "artifact score-evidence missing count",
        ),
        (
            "artifactLineageIntegrityMismatchCount",
            "expectedArtifactLineageIntegrityMismatchCount",
            "artifact lineage-integrity mismatch count",
        ),
        (
            "artifactPrivacyFailureCount",
            "expectedArtifactPrivacyFailureCount",
            "artifact privacy failure count",
        ),
        ("versionCount", "expectedVersionCount", "version count"),
        ("patchCount", "expectedPatchCount", "patch count"),
        (
            "succeededChoiceExecutionCount",
            "expectedSucceededChoiceExecutionCount",
            "succeeded choice-execution count",
        ),
    )
    for metric_key, contract_key, label in exact_fields:
        if contract_key not in contract:
            continue
        if int(metrics.get(metric_key) or 0) != int(contract[contract_key]):
            failures.append(f"dual-adoption {label} differs from the golden contract")

    if float(metrics.get("minPairwisePhysicalPoiJaccardDistance") or 0.0) < float(
        contract.get("minimumPairwisePhysicalPoiJaccardDistance") or 0.0
    ):
        failures.append("dual-adoption physical POI novelty is below required minimum")

    expected_day_count = int(contract.get("expectedDayCountPerProposal") or 0)
    expected_anchor_count = int(contract.get("expectedDayAnchorCountPerDay") or 0)
    targets_by_proposal = metrics.get("dayAnchorTargetsByProposal") or {}
    actuals_by_proposal = metrics.get("dayAnchorActualsByProposal") or {}
    if expected_day_count and expected_anchor_count:
        exact_density = bool(
            isinstance(targets_by_proposal, dict)
            and isinstance(actuals_by_proposal, dict)
            and targets_by_proposal.keys() == actuals_by_proposal.keys()
            and len(targets_by_proposal) == int(metrics.get("visibleProposalCount") or 0)
        )
        if exact_density:
            for proposal_id, targets in targets_by_proposal.items():
                actuals = actuals_by_proposal.get(proposal_id)
                if (
                    not isinstance(targets, dict)
                    or not isinstance(actuals, dict)
                    or targets != actuals
                    or len(targets) != expected_day_count
                    or any(int(value) != expected_anchor_count for value in targets.values())
                ):
                    exact_density = False
                    break
        if not exact_density:
            failures.append("dual-adoption daily density is not exact")

    expected_night_count = int(contract.get("expectedNightViewCountPerDay") or 0)
    nights_by_proposal = metrics.get("nightViewCountsByProposal") or {}
    if expected_day_count and expected_night_count:
        exact_night = bool(
            isinstance(nights_by_proposal, dict)
            and len(nights_by_proposal) == int(metrics.get("visibleProposalCount") or 0)
        )
        if exact_night:
            for day_counts in nights_by_proposal.values():
                if (
                    not isinstance(day_counts, dict)
                    or len(day_counts) != expected_day_count
                    or any(int(value) != expected_night_count for value in day_counts.values())
                ):
                    exact_night = False
                    break
        if not exact_night:
            failures.append("dual-adoption nightly coverage is not exact")

    expected_meal_count = int(contract.get("expectedMealCountPerDay") or 0)
    meals_by_proposal = metrics.get("mealCountsByProposal") or {}
    if expected_day_count and expected_meal_count:
        exact_meals = bool(
            isinstance(meals_by_proposal, dict)
            and len(meals_by_proposal) == int(metrics.get("visibleProposalCount") or 0)
        )
        if exact_meals:
            for day_counts in meals_by_proposal.values():
                if (
                    not isinstance(day_counts, dict)
                    or len(day_counts) != expected_day_count
                    or any(int(value) != expected_meal_count for value in day_counts.values())
                ):
                    exact_meals = False
                    break
        if not exact_meals:
            failures.append("dual-adoption daily meal coverage is not exact")

    expected_leg_count = contract.get("expectedRouteLegCountPerProposal")
    if expected_leg_count is not None:
        raw_route_leg_counts = metrics.get("routeLegCountsByProposal") or {}
        route_leg_counts = [
            int(value)
            for value in (
                raw_route_leg_counts.values() if isinstance(raw_route_leg_counts, dict) else raw_route_leg_counts
            )
        ]
        if len(route_leg_counts) != int(metrics.get("visibleProposalCount") or 0) or any(
            value != int(expected_leg_count) for value in route_leg_counts
        ):
            failures.append("dual-adoption provider route-leg counts are not exact")

    for metric_key, contract_key, label in (
        (
            "preAdoptionVersionPatchRouteDelta",
            "expectedPreAdoptionVersionPatchRouteDelta",
            "pre-adoption version/patch/route delta",
        ),
        (
            "duplicateVersionPatchRouteDelta",
            "expectedDuplicateVersionPatchRouteDelta",
            "duplicate version/patch/route delta",
        ),
    ):
        if list(metrics.get(metric_key) or []) != list(contract.get(contract_key) or []):
            failures.append(f"dual-adoption {label} differs from the golden contract")

    if int(metrics.get("routeRowCount") or 0) != int(metrics.get("expectedSelectedRouteRowCount") or 0):
        failures.append("dual-adoption selected route rows do not match expected adjacency")
    for metric_key, contract_key, label in (
        (
            "appendOnlyVisibleMembershipPassed",
            "requireAppendOnlyVisibleMembership",
            "append-only visible membership",
        ),
        (
            "refreshPersistencePassed",
            "requireRefreshPersistence",
            "refresh persistence",
        ),
        ("passed", "requireEvaluatorPass", "composite evaluator pass"),
        ("commitEventPassed", "requireCommitEvent", "commit event"),
        (
            "commitVerifierEventPassed",
            "requireCommitVerifierEvent",
            "commit verifier event",
        ),
        (
            "duplicateApplyPatchZeroEventPassed",
            "requireDuplicateApplyPatchZeroEvent",
            "duplicate zero-write apply-patch event",
        ),
        (
            "duplicateVerifierEventPassed",
            "requireDuplicateVerifierEvent",
            "duplicate verifier event",
        ),
        (
            "artifactIntegrityPassed",
            "requireArtifactIntegrity",
            "portable artifact integrity",
        ),
    ):
        if contract.get(contract_key) and metrics.get(metric_key) is not True:
            failures.append(f"dual-adoption {label} evidence is missing")
    if contract.get("requireSelectedProposalIdentity") and not str(metrics.get("selectedProposalId") or ""):
        failures.append("dual-adoption selected proposal identity is missing")
    if contract.get("requireActiveVersion") and not str(metrics.get("activeVersionId") or ""):
        failures.append("dual-adoption active version is missing after refresh")
    expected_status = contract.get("expectedPortfolioStatus")
    if expected_status is not None and metrics.get("portfolioStatus") != expected_status:
        failures.append("dual-adoption portfolio status differs from the golden contract")
    return failures


def _stability_failures(
    metrics: dict[str, Any],
    case: Optional[dict[str, Any]] = None,
) -> list[str]:
    failures = []
    profile_contract = (case or {}).get("searchProfileEval")
    dual_adoption_contract = (case or {}).get("dualAdoptionGoldenEval")
    if isinstance(dual_adoption_contract, dict):
        failures.extend(dual_adoption_golden_failures(metrics, dual_adoption_contract))
    if not metrics:
        if isinstance(profile_contract, dict) and profile_contract.get("required"):
            failures.append("required Search Profile metrics are missing")
        return failures
    if metrics.get("stabilityMetricEligible"):
        stability_contract = (case or {}).get("stabilityMetrics")
        expected_lifecycle = str(metrics.get("expectedLifecycle") or "committed_timeline")
        if expected_lifecycle == "proposal_only" and not metrics.get("proposalMaterialCreated"):
            failures.append("proposal-only stability material was not persisted")
        elif expected_lifecycle != "proposal_only" and not metrics["timelineCreated"]:
            failures.append("stability timeline was not created")
        if metrics["nightViewCardinalityMismatch"]:
            failures.append("night-view control-plane cardinality mismatch")
        if metrics["nightViewPersistedOverAllocation"]:
            failures.append("night-view persisted over-allocation")
        if metrics["nightViewPersistenceMismatch"]:
            failures.append("complete timeline night-view persistence mismatch")
        if metrics["cardinalityTerminalFailure"]:
            failures.append("cardinality error terminated before timeline creation")
        if metrics["groundingTerminalFailure"]:
            failures.append("raw partial grounding error reached the runtime result")
        if metrics["continuationScopeDrift"]:
            failures.append("partial pending-slot continuation scope is incomplete")
        if metrics["fakeOrNonAmapRouteAnchorCount"]:
            failures.append("fake or non-AMap route anchor persisted")
        if metrics["unexpectedWrite"]:
            failures.append("unexpected version/patch/route write count")
        if isinstance(stability_contract, dict) and expected_lifecycle == "proposal_only":
            expected_visible = stability_contract.get("expectedVisibleProposalCount")
            if expected_visible is not None and int(metrics.get("proposalVisibleCount") or 0) != int(expected_visible):
                failures.append("proposal-only visible proposal count mismatch")
            expected_ready = stability_contract.get("expectedAdoptionReadyProposalCount")
            if expected_ready is not None and int(metrics.get("proposalAdoptionReadyCount") or 0) != int(
                expected_ready
            ):
                failures.append("proposal-only adoption-ready proposal count mismatch")
            expected_attempts = stability_contract.get("expectedRouteProviderAttemptCount")
            if expected_attempts is not None and int(metrics.get("proposalRouteProviderAttemptCount") or 0) != int(
                expected_attempts
            ):
                failures.append("proposal-only route provider attempt count mismatch")
            if stability_contract.get("requireRouteCoverageComplete"):
                if metrics.get("proposalRouteCoverageComplete") is not True:
                    failures.append("proposal-only route coverage is incomplete")
                if metrics.get("proposalRouteEvidenceFingerprintBound") is not True:
                    failures.append("proposal-only route evidence fingerprint is not bound")
            if (
                stability_contract.get("requirePersistedSelectionCapability")
                and int(metrics.get("proposalPersistedSelectionCapabilityCount") or 0) <= 0
            ):
                failures.append("proposal-only persisted selection capability is missing")

    if not isinstance(profile_contract, dict) or not profile_contract.get("required"):
        return failures
    if not metrics.get("searchProfileMetricEligible"):
        failures.append("required Search Profile metrics are missing")
        return failures

    compiled_count = int(metrics.get("compiledSearchProfileCount") or 0)
    distinct_count = int(metrics.get("distinctSearchProfileFingerprintCount") or 0)
    family_count = int(metrics.get("familySearchProfileCoverageCount") or 0)
    if compiled_count <= 0:
        failures.append("required Search Profile compile metrics are all zero")
    if distinct_count <= 0:
        failures.append("Search Profile fingerprints are missing")
    if (
        int(metrics.get("uniqueSearchProfileIdCount") or 0) != compiled_count
        or int(metrics.get("uniqueSearchProfileScopeCount") or 0) != compiled_count
    ):
        failures.append("Search Profile profile IDs/scopes are missing or duplicated")
    contract_count = int(metrics.get("familyActivitySearchContractCount") or 0)
    if (
        contract_count <= 0
        or int(metrics.get("familyActivitySearchContractCoverageCount") or 0) != compiled_count
        or distinct_count < contract_count
        or int(metrics.get("profileFingerprintContractCollisionCount") or 0)
    ):
        failures.append("Search Profile fingerprint contract coverage is inconsistent")
    if family_count != compiled_count:
        failures.append("Search Profile family coverage does not match compile count")
    if int(metrics.get("familySearchSemanticMismatchCount") or 0):
        failures.append("Search Profile family semantic mismatch detected")
    if int(metrics.get("genericScenicCollapseCount") or 0):
        failures.append("Search Profile collapsed to a generic scenic family")
    if int(metrics.get("profileLineageMismatchCount") or 0):
        failures.append("Search Profile event/checkpoint/provider lineage mismatch")
    if int(metrics.get("invalidCoverageShortcutCount") or 0):
        failures.append("Search Profile coverage shortcut lineage is invalid")
    if int(metrics.get("nonCanonicalAmapProfileCandidateCount") or 0):
        failures.append("Search Profile accepted a non-canonical AMap candidate")
    if int(metrics.get("profileCheckpointCount") or 0) <= 0:
        failures.append("Search Profile persisted checkpoint is missing")
    if int(metrics.get("profileCheckpointReportCount") or 0) < compiled_count:
        failures.append("Search Profile checkpoint report coverage is incomplete")
    minimum_run_count = int(profile_contract.get("minimumPlanningRunCount") or 2)
    referenced_run_count = int(metrics.get("profilePlanningRunReferenceCount") or 0)
    loaded_run_count = int(metrics.get("profilePlanningRunLoadedCount") or 0)
    if (
        referenced_run_count < minimum_run_count
        or loaded_run_count < minimum_run_count
        or loaded_run_count != referenced_run_count
    ):
        failures.append("Search Profile planning-run lineage is incomplete")
    if int(metrics.get("profilePlanningRunMetricEventCount") or 0) < int(
        profile_contract.get("minimumPlanningRunMetricEventCount") or 4
    ):
        failures.append("Search Profile persisted compile/discovery events are missing")
    if int(metrics.get("profilePlanningRunMetricCycleCount") or 0) < int(
        profile_contract.get("minimumPlanningRunMetricCycleCount") or 2
    ):
        failures.append("Search Profile persisted replay cycle coverage is incomplete")
    if int(metrics.get("profilePlanningRunStagedVerifierCycleCount") or 0) < int(
        profile_contract.get("minimumPlanningRunStagedVerifierCycleCount") or 2
    ):
        failures.append("Search Profile per-run staging/verifier cycle coverage is incomplete")
    if int(metrics.get("profilePortfolioGenerateCallCount") or 0) <= 0:
        failures.append("production Creative Portfolio generator was not executed")
    if int(metrics.get("profilePortfolioStageCallCount") or 0) < int(
        profile_contract.get("minimumPortfolioStageCallCount") or 2
    ):
        failures.append("production Creative Portfolio staging was not executed")
    verifier_failure_count = int(metrics.get("profilePortfolioVerifierFailureCount") or 0)
    expected_verifier_failures = profile_contract.get("expectedPortfolioVerifierFailureCount")
    if (
        int(metrics.get("profilePortfolioVerifierCallCount") or 0)
        < int(profile_contract.get("minimumPortfolioVerifierCallCount") or 2)
        or int(metrics.get("profilePortfolioVerifierIncompleteEvidenceCount") or 0) > 0
        or (expected_verifier_failures is None and verifier_failure_count)
        or (expected_verifier_failures is not None and verifier_failure_count != int(expected_verifier_failures))
    ):
        failures.append("production Creative Portfolio verifier evidence is incomplete")
    if int(metrics.get("profileAdapterCallCount") or 0) <= 0:
        failures.append("production AMap Search Profile adapter was not executed")
    if int(metrics.get("profileAdapterPlanAdoptionCount") or 0) < int(
        profile_contract.get("minimumAdapterPlanAdoptionCount") or 1
    ):
        failures.append("adapted Search Profile plan/category/query was not adopted")
    if int(metrics.get("profileWebProviderCallCount") or 0) < int(
        profile_contract.get("minimumWebProviderCallCount") or 1
    ):
        failures.append("recorded Web provider boundary was not exercised")
    if int(metrics.get("profileAmapProviderCallCount") or 0) <= 0:
        failures.append("recorded AMap provider boundary was not exercised")

    minimum_fields = (
        (
            "compiledSearchProfileCount",
            "minimumCompiledProfileCount",
            "compiled Search Profile count",
        ),
        (
            "familySpecificAmapCandidateCount",
            "minimumFamilySpecificAmapCandidateCount",
            "family-specific AMap candidate count",
        ),
        (
            "semanticCandidateRejectedCount",
            "minimumSemanticCandidateRejectedCount",
            "semantic rejection count",
        ),
        (
            "duplicateExcludedBeforeRouteCount",
            "minimumDuplicateExcludedBeforeRouteCount",
            "pre-route duplicate exclusion count",
        ),
        (
            "profileCoverageShortcutHitCount",
            "minimumCoverageShortcutHitCount",
            "profile coverage shortcut count",
        ),
        (
            "consumerAdmissionCoverageRejectedCount",
            "minimumConsumerAdmissionCoverageRejectedCount",
            "consumer admission coverage rejection count",
        ),
        (
            "profileExcludedPhysicalPoiCount",
            "minimumExcludedPhysicalPoiCount",
            "persisted exclusion lineage count",
        ),
    )
    for metric_key, contract_key, label in minimum_fields:
        minimum = int(profile_contract.get(contract_key) or 0)
        if int(metrics.get(metric_key) or 0) < minimum:
            failures.append(f"Search Profile {label} is below required minimum")

    for metric_key, contract_key in (
        ("preAdoptionVersionWriteCount", "expectedPreAdoptionVersionWriteCount"),
        ("preAdoptionPatchWriteCount", "expectedPreAdoptionPatchWriteCount"),
        ("preAdoptionRouteWriteCount", "expectedPreAdoptionRouteWriteCount"),
    ):
        expected = int(profile_contract.get(contract_key) or 0)
        if int(metrics.get(metric_key) or 0) != expected:
            failures.append("unexpected pre-adoption version/patch/route DB delta")
            break
    return failures


def _load_cases(cases_dir: Path) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        raw_case = path.read_bytes()
        payload = json.loads(raw_case.decode("utf-8"))
        # Agent CLI quality scenarios are multi-turn runtime fixtures, not the
        # single-provider offline harness contract consumed below.
        if isinstance(payload, dict) and isinstance(payload.get("scenarios"), list):
            continue
        if not isinstance(payload, dict) or payload.get("schemaVersion") != "react-offline-harness-v2":
            continue
        payload["_offlineCaseSha256"] = hashlib.sha256(raw_case).hexdigest().upper()
        loaded.append(payload)
    return loaded


def _result(
    case_id: str,
    passed: bool,
    body: Optional[dict[str, Any]] = None,
    planning_steps: Optional[list[dict[str, Any]]] = None,
    invalid_patch_count: int = 0,
    failure_reason: str = "",
    expected_stages: Optional[list[str]] = None,
    allowed_failed_stages: Optional[list[str]] = None,
    latency_ms: int = 0,
    stability_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    steps = planning_steps or []
    replay = replay_trace(steps, expected_stages, allowed_failed_stages)
    response_replay = replay_agent_response(
        {"planningSteps": steps, "toolEvents": steps}, expected_stages, allowed_failed_stages
    )
    replay["allowedFailedStages"] = allowed_failed_stages or []
    return {
        "id": case_id,
        "passed": bool(passed) and replay["passed"],
        "failureReason": ""
        if (bool(passed) and replay["passed"])
        else failure_reason or _default_failure_reason(body, replay),
        "stepCount": len(steps),
        "latencyMs": latency_ms,
        "invalidPatchCount": invalid_patch_count,
        "verifierFailures": count_verifier_failures(steps),
        "toolCallCount": count_tool_calls(steps),
        "failedToolCallCount": count_failed_tool_calls(steps),
        "traceReplay": replay,
        "replaySummary": response_replay,
        "traceDurationMs": replay["durationMs"],
        **(stability_metrics or {}),
    }


def _default_failure_reason(body: Optional[dict[str, Any]], replay: Optional[dict[str, Any]] = None) -> str:
    if replay and not replay.get("passed"):
        return f"trace replay failed: missing={replay.get('missingStages')} outOfOrder={replay.get('outOfOrder')}"
    if not body:
        return "case assertions failed"
    if body.get("detail"):
        return str(body["detail"])
    if body.get("warnings"):
        return "; ".join(str(item) for item in body["warnings"])
    return "case assertions failed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline Agent Harness evals with mock providers.")
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--dry-route-discovery",
        action="store_true",
        help=(
            "Record the exact route requests attempted by the offline harness before fixture lookup. "
            "This never calls AMap and is not the release-gate replay result."
        ),
    )
    parser.add_argument(
        "--capture-semantic-manifest",
        action="store_true",
        help=(
            "Include server-compiled semantic/profile evidence for an offline recorded-capture "
            "manifest. This never calls AMap and does not make a route replay eligible."
        ),
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="Repeat each case N times. Defaults to 1; reserved for pass^k tracking."
    )
    args = parser.parse_args()
    result = run_offline_eval(
        args.cases_dir,
        repeat=args.repeat,
        dry_route_discovery=bool(args.dry_route_discovery),
        capture_semantic_manifest=bool(args.capture_semantic_manifest),
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    # A dry manifest is diagnostic evidence, never a release-gate pass.  Keep
    # the normal strict exit status so shell/CI cannot mistake it for green.
    return 0 if result["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
