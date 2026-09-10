import hashlib
import json
import os
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from threading import Semaphore
from time import monotonic
from typing import Optional, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import HTTPException

from src.api.schemas.maps import MapPoiPhotoResponse, MapPoiResponse, MapPoiSearchResponse
from src.core.config import get_settings
from src.services.amap_call_budget import current_amap_call_budget
from src.services.amap_rate_limiter import AMAP_BASIC_WEB_SERVICE_QPS, AMAP_WEB_SERVICE_RATE_LIMITER
from src.services.experience_independence_service import ExperienceIndependenceService
from src.services.spatial_geometry_service import SpatialGeometryService


AMAP_PLACE_TEXT_URL = "https://restapi.amap.com/v3/place/text"
AMAP_PLACE_AROUND_URL = "https://restapi.amap.com/v3/place/around"
AMAP_PLACE_DETAIL_URL = "https://restapi.amap.com/v3/place/detail"
AMAP_DISTRICT_URL = "https://restapi.amap.com/v3/config/district"
AMAP_COORDINATE_CONVERT_URL = "https://restapi.amap.com/v3/assistant/coordinate/convert"
AMAP_PLACE_SOURCE = "amap-place-search"
AMAP_PLACE_AROUND_MAX_RADIUS_METERS = 5000
MAP_BASIC_SEARCH_PARALLELISM = AMAP_BASIC_WEB_SERVICE_QPS
_MAP_POI_REQUEST_SEMAPHORE = Semaphore(MAP_BASIC_SEARCH_PARALLELISM)
AMAP_POI_CACHE_TTL_SECONDS = 1800
AMAP_POI_RATE_LIMIT_COOLDOWN_SECONDS = 90
_AMAP_POI_CACHE: dict[tuple, tuple[float, dict]] = {}
_AMAP_RATE_LIMIT_UNTIL = 0.0
_AMAP_RATE_LIMIT_REASON = ""
_SERVER_QUERY_SCOPE_FINGERPRINT_RE = re.compile(r"[0-9A-Fa-f]{64}")
_ADMINISTRATIVE_NAME_SUFFIXES = (
    "特别行政区",
    "维吾尔自治区",
    "壮族自治区",
    "回族自治区",
    "自治区",
    "自治州",
    "自治县",
    "地区",
    "city",
    "province",
    "prefecture",
    "municipality",
    "省",
    "市",
    "盟",
    "区",
    "县",
)

CITY_ADCODES = {
    "北京": "110000",
    "北京市": "110000",
    "上海": "310000",
    "上海市": "310000",
    "广州": "440100",
    "广州市": "440100",
    "深圳": "440300",
    "深圳市": "440300",
}

_STANDALONE_PARK_TYPE_CODES = ExperienceIndependenceService.accepted_provider_typecodes("standalone_park")

CATEGORY_QUERY = {
    "all": ("", ""),
    "scenic": ("景点", "110000"),
    "food": ("餐厅", "050000"),
    "experience": ("体验", "080000|110000"),
    "shopping": ("购物", "060000"),
    "market": ("市场", "050000|060000"),
    "culture": ("文化场馆", "140000"),
    "park": ("公园", "|".join(_STANDALONE_PARK_TYPE_CODES)),
    "local_service": ("生活服务", "070000"),
    "campus": ("大学", "141200"),
    "museum": ("博物馆", "140100"),
    "lodging": ("酒店", "100000"),
    "transport": ("交通", "150000"),
}


class MapPoiProviderError(RuntimeError):
    def __init__(self, message: str, debug: Optional[dict] = None):
        super().__init__(message)
        self.debug = debug or {}


def clear_map_poi_runtime_state() -> None:
    global _AMAP_RATE_LIMIT_UNTIL, _AMAP_RATE_LIMIT_REASON
    _AMAP_POI_CACHE.clear()
    _AMAP_RATE_LIMIT_UNTIL = 0.0
    _AMAP_RATE_LIMIT_REASON = ""


def _compact_provider_name(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _canonical_administrative_name(value: object) -> str:
    compact = _compact_provider_name(value)
    for suffix in _ADMINISTRATIVE_NAME_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            return compact[: -len(suffix)]
    return compact


def _provider_name_rank(name: object, keyword: str) -> tuple[int, int, str]:
    compact_name = _compact_provider_name(name)
    compact_keyword = _compact_provider_name(keyword)
    canonical_name = _canonical_administrative_name(name)
    canonical_keyword = _canonical_administrative_name(keyword)
    if compact_name == compact_keyword:
        match_rank = 0
    elif canonical_name and canonical_name == canonical_keyword:
        match_rank = 1
    else:
        match_rank = 2
    return match_rank, len(compact_name), compact_name


class MapPoiService:
    def __init__(self, map_provider_key: Optional[str] = None, timeout_seconds: float = 5.0):
        settings = get_settings()
        self.map_provider_key = map_provider_key if map_provider_key is not None else settings.map_provider_key
        self.timeout_seconds = timeout_seconds
        self.detail_fetch_count = 0
        self.detail_cache_hit_count = 0

    def search(
        self,
        city: str,
        keyword: str = "",
        category: str = "all",
        limit: int = 12,
        bypass_cache: bool = False,
        *,
        query_scope_fingerprint: Optional[str] = None,
        page: int = 1,
        offset: Optional[int] = None,
        provider_types: Optional[str] = None,
    ) -> MapPoiSearchResponse:
        if not self.map_provider_key:
            raise HTTPException(status_code=400, detail="MAP_PROVIDER_KEY is not configured; cannot search AMap POIs")

        normalized_category = category if category in CATEGORY_QUERY else "all"
        search_keyword = keyword.strip()
        if not search_keyword:
            raise HTTPException(status_code=400, detail="POI search keyword is required")

        _default_keyword, default_type_codes = CATEGORY_QUERY[normalized_category]
        type_codes = self._provider_types_param(
            normalized_category,
            default_type_codes,
            provider_types,
        )
        if normalized_category == "park" and not type_codes:
            raise HTTPException(
                status_code=503,
                detail="Standalone park category evidence is unavailable; AMap POI search was not started",
            )
        city_param = self._city_param(city)
        params = {
            "key": self.map_provider_key,
            "keywords": search_keyword,
            "city": city_param,
            "citylimit": "true",
            "offset": str(self._bounded_page_size(offset if offset is not None else limit)),
            "page": str(self._bounded_page_number(page)),
            "extensions": "all",
            "output": "json",
        }
        if type_codes:
            params["types"] = type_codes

        scope_discriminator = self._query_scope_cache_discriminator(query_scope_fingerprint)
        cache_key = self._cache_key(
            "place/text",
            params,
            query_scope_fingerprint=scope_discriminator,
        )
        cached = self._cached_response(cache_key)
        if cached is not None and not bypass_cache:
            self._record_budget_cache_hit("place/text", params, normalized_category)
            return cached
        self._raise_if_rate_limited(source="place/text", params=params)
        self._consume_budget_or_raise(
            "place/text",
            params,
            normalized_category,
            query_scope_fingerprint=scope_discriminator,
            query_variant_fingerprint=self._provider_query_ledger_fingerprint(
                scope_discriminator,
                params,
            ),
        )
        try:
            payload = self._fetch_amap_place(params)
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(str(error), source="place/text", params=params, debug=error.debug),
            ) from error

        queried_at = datetime.now(timezone.utc)
        query_receipt = self._provider_query_receipt_fingerprint(
            "place/text",
            params,
            query_scope_fingerprint=scope_discriminator,
        )
        parsed_pois = [
            self._attach_query_receipt(
                self._parse_poi(item, normalized_category),
                queried_at=queried_at,
                query_receipt_fingerprint=query_receipt,
            )
            for item in payload.get("pois", [])
        ]
        result = MapPoiSearchResponse(
            city=city,
            keyword=search_keyword,
            category=normalized_category,
            provider_name=AMAP_PLACE_SOURCE,
            queried_at=queried_at,
            pois=self._rank_pois(parsed_pois, search_keyword),
            provider_query_receipt_fingerprint=query_receipt,
        )
        self._store_cache(cache_key, result)
        return result

    def search_nearby(
        self,
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
        page: int = 1,
        offset: Optional[int] = None,
        provider_types: Optional[str] = None,
    ) -> MapPoiSearchResponse:
        if not self.map_provider_key:
            raise HTTPException(
                status_code=400, detail="MAP_PROVIDER_KEY is not configured; cannot search nearby AMap POIs"
            )

        search_keyword = keyword.strip()
        if not search_keyword:
            raise HTTPException(status_code=400, detail="Nearby POI search keyword is required")

        normalized_category = category if category in CATEGORY_QUERY else "all"
        _default_keyword, default_type_codes = CATEGORY_QUERY[normalized_category]
        type_codes = self._provider_types_param(
            normalized_category,
            default_type_codes,
            provider_types,
        )
        if normalized_category == "park" and not type_codes:
            raise HTTPException(
                status_code=503,
                detail="Standalone park category evidence is unavailable; nearby AMap POI search was not started",
            )
        city_param = self._city_param(city)
        params = {
            "key": self.map_provider_key,
            "keywords": search_keyword,
            "location": f"{longitude},{latitude}",
            "city": city_param,
            "citylimit": "true",
            "radius": str(max(50, min(radius, AMAP_PLACE_AROUND_MAX_RADIUS_METERS))),
            "sortrule": "distance",
            "offset": str(self._bounded_page_size(offset if offset is not None else limit)),
            "page": str(self._bounded_page_number(page)),
            "extensions": "all",
            "output": "json",
        }
        if type_codes:
            params["types"] = type_codes

        scope_discriminator = self._query_scope_cache_discriminator(query_scope_fingerprint)
        cache_key = self._cache_key(
            "place/around",
            params,
            query_scope_fingerprint=scope_discriminator,
        )
        cached = self._cached_response(cache_key)
        if cached is not None and not bypass_cache:
            self._record_budget_cache_hit("place/around", params, normalized_category)
            return cached
        self._raise_if_rate_limited(source="place/around", params=params)
        self._consume_budget_or_raise(
            "place/around",
            params,
            normalized_category,
            query_scope_fingerprint=scope_discriminator,
            query_variant_fingerprint=self._provider_query_ledger_fingerprint(
                scope_discriminator,
                params,
            ),
        )
        try:
            payload = self._fetch_amap_around(params)
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(str(error), source="place/around", params=params, debug=error.debug),
            ) from error

        queried_at = datetime.now(timezone.utc)
        query_receipt = self._provider_query_receipt_fingerprint(
            "place/around",
            params,
            query_scope_fingerprint=scope_discriminator,
        )
        parsed_pois = [
            self._attach_query_receipt(
                self._parse_poi(item, normalized_category, origin=(longitude, latitude)),
                queried_at=queried_at,
                query_receipt_fingerprint=query_receipt,
            )
            for item in payload.get("pois", [])
        ]
        result = MapPoiSearchResponse(
            city=city,
            keyword=search_keyword,
            category=normalized_category,
            provider_name=AMAP_PLACE_SOURCE,
            queried_at=queried_at,
            pois=self._rank_pois(parsed_pois, search_keyword, origin=(longitude, latitude)),
            provider_query_receipt_fingerprint=query_receipt,
        )
        self._store_cache(cache_key, result)
        return result

    def _fetch_amap_place(self, params: dict[str, str]) -> dict:
        url = f"{AMAP_PLACE_TEXT_URL}?{urlencode(params)}"
        return self._fetch_with_limit(url, source="place/text", params=params)

    def _fetch_amap_around(self, params: dict[str, str]) -> dict:
        url = f"{AMAP_PLACE_AROUND_URL}?{urlencode(params)}"
        return self._fetch_with_limit(url, source="place/around", params=params)

    def detail(self, amap_id: str, *, bypass_cache: bool = False) -> MapPoiResponse:
        """Fetch one shortlisted, identity-screened POI by canonical AMap ID."""
        normalized_id = str(amap_id or "").strip().upper()
        if not self.map_provider_key:
            raise HTTPException(
                status_code=400, detail="MAP_PROVIDER_KEY is not configured; cannot fetch AMap POI detail"
            )
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", normalized_id):
            raise HTTPException(status_code=400, detail="A valid AMap POI ID is required")
        params = {
            "key": self.map_provider_key,
            "id": normalized_id,
            "extensions": "all",
            "output": "json",
        }
        cache_key = self._cache_key("place/detail", params)
        cached = self._cached_response(cache_key)
        if cached is not None and not bypass_cache:
            self.detail_cache_hit_count += 1
            self._record_budget_cache_hit("place/detail", params, "detail")
            return cached
        self._raise_if_rate_limited(source="place/detail", params=params)
        self._consume_budget_or_raise("place/detail", params, "detail")
        try:
            payload = self._fetch_amap_detail(params)
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(str(error), source="place/detail", params=params, debug=error.debug),
            ) from error
        items = [item for item in payload.get("pois") or [] if isinstance(item, dict)]
        if not items:
            raise HTTPException(status_code=404, detail="AMap POI detail was not found")
        self.detail_fetch_count += 1
        queried_at = datetime.now(timezone.utc)
        result = self._attach_query_receipt(
            self._parse_poi(items[0], "all"),
            queried_at=queried_at,
            query_receipt_fingerprint=self._provider_query_receipt_fingerprint(
                "place/detail",
                params,
                query_scope_fingerprint=None,
            ),
        )
        self._store_cache(cache_key, result)
        return result

    def enrich_shortlist(self, pois: list[MapPoiResponse], *, limit: int = 3) -> list[MapPoiResponse]:
        """Bound detail work to the first N already-grounded candidates."""
        enriched: list[MapPoiResponse] = []
        for index, poi in enumerate(pois):
            if index >= max(0, min(int(limit), 5)):
                enriched.append(poi)
                continue
            try:
                enriched.append(self.detail(poi.id))
            except HTTPException:
                enriched.append(poi)
        return enriched

    def resolve_administrative_area(self, area_text: str) -> list[dict[str, str]]:
        """Resolve administrative identity through AMap's district API.

        The caller must handle zero or multiple results as unresolved.  This
        method never consults a built-in city/district table and does not pick
        a plausible first result on the caller's behalf.
        """

        keyword = str(area_text or "").strip()
        if not self.map_provider_key:
            raise HTTPException(
                status_code=400,
                detail="MAP_PROVIDER_KEY is not configured; cannot resolve an administrative area",
            )
        if not keyword:
            raise HTTPException(status_code=400, detail="Administrative area text is required")
        params = {
            "key": self.map_provider_key,
            "keywords": keyword,
            "subdistrict": "0",
            "extensions": "base",
            "output": "json",
        }
        self._raise_if_rate_limited(source="config/district", params=params)
        self._consume_budget_or_raise("config/district", params, "administrative_area")
        try:
            payload = self._fetch_with_limit(
                f"{AMAP_DISTRICT_URL}?{urlencode(params)}",
                source="config/district",
                params=params,
            )
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(
                    str(error),
                    source="config/district",
                    params=params,
                    debug=error.debug,
                ),
            ) from error
        matches = []
        for item in payload.get("districts") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            adcode = str(item.get("adcode") or "").strip()
            if name and adcode:
                matches.append(
                    {
                        "name": name,
                        "adcode": adcode,
                        "level": str(item.get("level") or "").strip(),
                        "center": str(item.get("center") or "").strip(),
                    }
                )
        return matches

    def resolve_city_scope(self, city_text: str) -> list[dict[str, object]]:
        """Return canonical AMap city identity and its Provider geometry bounds.

        The result is used only to scope a named-boundary lookup. It does not
        reinterpret a user boundary expression or make that expression valid.
        """

        keyword = str(city_text or "").strip()
        if not self.map_provider_key or not keyword:
            return []
        params = {
            "key": self.map_provider_key,
            "keywords": keyword,
            "subdistrict": "0",
            "extensions": "all",
            "output": "json",
        }
        self._raise_if_rate_limited(source="config/district", params=params)
        self._consume_budget_or_raise("config/district", params, "city_scope")
        try:
            payload = self._fetch_with_limit(
                f"{AMAP_DISTRICT_URL}?{urlencode(params)}",
                source="config/district",
                params=params,
            )
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(
                    str(error), source="config/district", params=params, debug=error.debug
                ),
            ) from error
        matches_by_adcode: dict[str, dict[str, object]] = {}
        representation_ranks: dict[str, tuple[int, int, str]] = {}
        for item in payload.get("districts") or []:
            if not isinstance(item, dict):
                continue
            points: list[tuple[float, float]] = []
            for ring_group in str(item.get("polyline") or "").split("|"):
                for token in ring_group.split(";"):
                    try:
                        longitude, latitude = (float(value) for value in token.split(",", 1))
                    except (TypeError, ValueError):
                        continue
                    points.append((longitude, latitude))
            name, adcode = str(item.get("name") or "").strip(), str(item.get("adcode") or "").strip()
            if not name or not adcode or not points:
                continue
            # AMap district geometry is GCJ-02. The padded query bbox is only a
            # Provider search scope; it is never persisted as boundary evidence.
            longitudes, latitudes = [point[0] for point in points], [point[1] for point in points]
            query_bbox = [
                min(latitudes) - 0.02,
                min(longitudes) - 0.02,
                max(latitudes) + 0.02,
                max(longitudes) + 0.02,
            ]
            candidate_rank = _provider_name_rank(name, keyword)
            existing = matches_by_adcode.get(adcode)
            if existing is None:
                matches_by_adcode[adcode] = {
                    "name": name,
                    "adcode": adcode,
                    "level": str(item.get("level") or "").strip(),
                    "queryBbox": query_bbox,
                    "queryBboxSource": "amap_gcj02_district_bounds_padded_for_provider_lookup",
                }
                representation_ranks[adcode] = candidate_rank
                continue

            existing_bbox = existing["queryBbox"]
            if isinstance(existing_bbox, list) and len(existing_bbox) == 4:
                existing["queryBbox"] = [
                    min(float(existing_bbox[0]), query_bbox[0]),
                    min(float(existing_bbox[1]), query_bbox[1]),
                    max(float(existing_bbox[2]), query_bbox[2]),
                    max(float(existing_bbox[3]), query_bbox[3]),
                ]
            if candidate_rank < representation_ranks[adcode]:
                existing["name"] = name
                existing["level"] = str(item.get("level") or "").strip()
                representation_ranks[adcode] = candidate_rank

        matches = list(matches_by_adcode.values())
        canonical_keyword = _canonical_administrative_name(keyword)
        exact_matches = [
            match
            for match in matches
            if canonical_keyword and _canonical_administrative_name(match.get("name")) == canonical_keyword
        ]
        return exact_matches or matches

    def convert_wgs84_coordinates(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Convert one closed WGS84 polygon to GCJ-02 in one bounded AMap call."""

        if not self.map_provider_key:
            raise HTTPException(status_code=400, detail="MAP_PROVIDER_KEY is not configured; cannot convert geometry")
        if not 1 <= len(points) <= 40:
            raise HTTPException(status_code=422, detail="Coordinate conversion requires between 1 and 40 points")
        params = {
            "key": self.map_provider_key,
            "locations": "|".join(f"{longitude:.7f},{latitude:.7f}" for longitude, latitude in points),
            "coordsys": "gps",
            "output": "json",
        }
        coordinate_fingerprint = hashlib.sha256(params["locations"].encode("utf-8")).hexdigest()
        self._consume_budget_or_raise(
            "assistant/coordinate/convert",
            {**params, "id": coordinate_fingerprint},
            "coordinate_conversion",
            query_scope_fingerprint=coordinate_fingerprint,
        )
        try:
            payload = self._fetch_with_limit(
                f"{AMAP_COORDINATE_CONVERT_URL}?{urlencode(params)}",
                source="assistant/coordinate/convert",
                params=params,
            )
        except MapPoiProviderError as error:
            raise HTTPException(
                status_code=502,
                detail=self._provider_error_detail(
                    str(error), source="assistant/coordinate/convert", params=params, debug=error.debug
                ),
            ) from error
        converted: list[tuple[float, float]] = []
        for token in str(payload.get("locations") or "").split(";"):
            try:
                longitude, latitude = (float(value) for value in token.split(",", 1))
            except (TypeError, ValueError):
                raise HTTPException(status_code=502, detail="AMap coordinate conversion returned invalid geometry")
            converted.append((longitude, latitude))
        if len(converted) != len(points) or any(
            not (-180 <= longitude <= 180 and -90 <= latitude <= 90) for longitude, latitude in converted
        ):
            raise HTTPException(status_code=502, detail="AMap coordinate conversion result did not match input")
        if points[0] == points[-1] and converted[0] != converted[-1]:
            raise HTTPException(status_code=502, detail="AMap coordinate conversion did not preserve polygon closure")
        if points[0] == points[-1] and not SpatialGeometryService.valid_closed_polygon(converted):
            raise HTTPException(status_code=502, detail="AMap coordinate conversion returned an invalid polygon")
        return converted

    def _fetch_amap_detail(self, params: dict[str, str]) -> dict:
        url = f"{AMAP_PLACE_DETAIL_URL}?{urlencode(params)}"
        return self._fetch_with_limit(url, source="place/detail", params=params)

    def _fetch_with_limit(self, url: str, source: str, params: Optional[dict[str, str]] = None) -> dict:
        params = params or {}
        self._raise_if_rate_limited(source=source, params=params)
        with _MAP_POI_REQUEST_SEMAPHORE:
            if source in {
                "place/text",
                "place/around",
                "place/detail",
                "config/district",
                "assistant/coordinate/convert",
            }:
                AMAP_WEB_SERVICE_RATE_LIMITER.acquire()
            try:
                with urlopen(url, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except HTTPError as error:
                debug = self._http_error_debug(error, source=source, params=params)
                if error.code == 429:
                    self._mark_rate_limited(f"HTTP 429 rate limit from {source}")
                    raise MapPoiProviderError(f"HTTP 429 rate limit from {source}", debug=debug) from error
                raise MapPoiProviderError(f"HTTP {error.code} from {source}", debug=debug) from error
            except URLError as error:
                message = self._safe_debug_text(str(error.reason))
                raise MapPoiProviderError(
                    message,
                    debug=self._amap_debug(
                        source=source,
                        params=params,
                        raw_info=message,
                        classified_reason="provider_down",
                    ),
                ) from error
            except TimeoutError as error:
                raise MapPoiProviderError(
                    "request timed out",
                    debug=self._amap_debug(
                        source=source,
                        params=params,
                        raw_info="request timed out",
                        classified_reason="provider_down",
                    ),
                ) from error

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise MapPoiProviderError(
                "invalid JSON response",
                debug=self._amap_debug(
                    source=source,
                    params=params,
                    raw_info="invalid JSON response",
                    classified_reason="provider_down",
                ),
            ) from error

        if payload.get("status") != "1":
            message = payload.get("info") or payload.get("infocode") or "unknown AMap error"
            debug = self._amap_debug_from_payload(source=source, params=params, payload=payload)
            if self._is_rate_limit_message(message):
                self._mark_rate_limited(str(message))
            raise MapPoiProviderError(str(message), debug=debug)
        return payload

    def _http_error_debug(self, error: HTTPError, source: str, params: dict[str, str]) -> dict:
        payload = self._http_error_json_payload(error)
        classified_reason = "http_429" if error.code == 429 else "provider_down"
        if payload:
            return self._amap_debug(
                source=source,
                params=params,
                http_status_code=error.code,
                raw_status=payload.get("status"),
                raw_info=payload.get("info") or f"HTTP {error.code}",
                raw_infocode=payload.get("infocode"),
                raw_errmsg=payload.get("errmsg"),
                raw_errcode=payload.get("errcode"),
                classified_reason=classified_reason,
            )
        return self._amap_debug(
            source=source,
            params=params,
            http_status_code=error.code,
            raw_info=f"HTTP {error.code}",
            classified_reason=classified_reason,
        )

    def _http_error_json_payload(self, error: HTTPError) -> dict:
        try:
            raw_body = error.read()
        except Exception:
            return {}
        if not raw_body:
            return {}
        try:
            body = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else str(raw_body)
            payload = json.loads(body)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _query_scope_cache_discriminator(value: Optional[str]) -> Optional[str]:
        """Validate an opaque server query identity used only to partition cache.

        The discriminator never enters AMap request parameters and never grants
        semantic, route, or admission authority.  A caller can at most force a
        cache miss; it cannot broaden or approve provider results.
        """

        if value is None:
            return None
        normalized = str(value).strip()
        if _SERVER_QUERY_SCOPE_FINGERPRINT_RE.fullmatch(normalized) is None:
            raise HTTPException(
                status_code=400,
                detail="A valid server query scope fingerprint is required",
            )
        return normalized

    @staticmethod
    def _bounded_page_number(value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 1
        return max(1, min(parsed, 100))

    @staticmethod
    def _bounded_page_size(value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 1
        return max(1, min(parsed, 25))

    def _provider_query_receipt_fingerprint(
        self,
        source: str,
        params: dict[str, str],
        *,
        query_scope_fingerprint: Optional[str],
    ) -> str:
        material = {
            "provider": AMAP_PLACE_SOURCE,
            "endpoint": str(source or ""),
            "requestParams": self._safe_request_params(params),
            "serverQueryScopeFingerprint": str(query_scope_fingerprint or "") or None,
        }
        canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _provider_query_ledger_fingerprint(
        self,
        query_scope_fingerprint: Optional[str],
        params: dict[str, str],
    ) -> str:
        """Keep Provider-budget identity distinct across server-owned pages."""

        material = {
            "serverQueryScopeFingerprint": str(query_scope_fingerprint or "") or None,
            "page": str(params.get("page") or "1"),
            "offset": str(params.get("offset") or ""),
            "providerTypes": str(params.get("types") or "") or None,
        }
        canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _attach_query_receipt(
        poi: MapPoiResponse,
        *,
        queried_at: datetime,
        query_receipt_fingerprint: str,
    ) -> MapPoiResponse:
        return poi.model_copy(
            update={
                "provider_queried_at": queried_at,
                "provider_query_receipt_fingerprint": query_receipt_fingerprint,
            }
        )

    def _cache_key(
        self,
        source: str,
        params: dict[str, str],
        *,
        query_scope_fingerprint: Optional[str] = None,
    ) -> tuple:
        normalized_items = []
        for key, value in sorted(params.items()):
            if key == "key":
                continue
            normalized_value = str(value)
            if key == "location":
                try:
                    lon, lat = normalized_value.split(",", 1)
                    normalized_value = f"{float(lon):.4f},{float(lat):.4f}"
                except Exception:
                    pass
            normalized_items.append((key, normalized_value))
        if query_scope_fingerprint is not None:
            normalized_items.append(("serverQueryScopeFingerprint", query_scope_fingerprint))
        return (source, tuple(normalized_items))

    def _cached_response(self, cache_key: tuple) -> Optional[Union[MapPoiSearchResponse, MapPoiResponse]]:
        cached = _AMAP_POI_CACHE.get(cache_key)
        if cached is None:
            return None
        expires_at, payload = cached
        if expires_at <= monotonic():
            _AMAP_POI_CACHE.pop(cache_key, None)
            return None
        if cache_key and cache_key[0] == "place/detail":
            return MapPoiResponse.model_validate(deepcopy(payload))
        response = MapPoiSearchResponse.model_validate(deepcopy(payload))
        response.cache_hit = True
        return response

    def _store_cache(self, cache_key: tuple, response: Union[MapPoiSearchResponse, MapPoiResponse]) -> None:
        payload = response.model_dump(by_alias=True)
        if isinstance(response, MapPoiSearchResponse):
            payload["cacheHit"] = False
        _AMAP_POI_CACHE[cache_key] = (monotonic() + AMAP_POI_CACHE_TTL_SECONDS, payload)

    def _raise_if_rate_limited(self, source: str = "unknown", params: Optional[dict[str, str]] = None) -> None:
        remaining = int(max(0, _AMAP_RATE_LIMIT_UNTIL - monotonic()))
        if remaining <= 0:
            return
        budget = current_amap_call_budget()
        if budget is not None:
            budget.rate_limited = True
            budget.cooldown_remaining_seconds = remaining
        reason = _AMAP_RATE_LIMIT_REASON or "高德 POI 查询频率超限，请稍后重试"
        debug = self._amap_debug(
            source=source,
            params=params or {},
            raw_info=reason,
            classified_reason="local_cooldown",
            retry_after_seconds=remaining,
            local_cooldown=True,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "code": "provider_rate_limited",
                "message": "高德 POI 查询暂在本地冷却中，请稍后重试。",
                "providerName": "amap",
                "retryAfterSeconds": remaining,
                "debug": debug,
            },
        )

    def _record_budget_cache_hit(self, source: str, params: dict[str, str], category: str) -> None:
        budget = current_amap_call_budget()
        if budget is None:
            return
        budget.record_cache_hit(
            endpoint=source,
            keyword=str(params.get("keywords") or params.get("id") or ""),
            category=category,
            center=str(params.get("location") or params.get("city") or ""),
            radius=str(params.get("radius") or ""),
            source="map_poi_service",
        )

    def _consume_budget_or_raise(
        self,
        source: str,
        params: dict[str, str],
        category: str,
        *,
        query_scope_fingerprint: Optional[str] = None,
        query_variant_fingerprint: Optional[str] = None,
    ) -> None:
        budget = current_amap_call_budget()
        if budget is None:
            return
        allowed = budget.try_acquire(
            endpoint=source,
            keyword=str(params.get("keywords") or params.get("id") or ""),
            category=category,
            center=str(params.get("location") or params.get("city") or ""),
            radius=str(params.get("radius") or ""),
            source="map_poi_service",
            query_scope_fingerprint=str(query_scope_fingerprint or ""),
            query_variant_fingerprint=str(query_variant_fingerprint or ""),
        )
        if allowed:
            return
        reason = budget.last_denial_reason or "budget_exceeded"
        debug = self._amap_debug(
            source=source,
            params=params,
            raw_info="duplicate AMap query suppressed"
            if reason == "duplicate_query_suppressed"
            else "amap call budget exceeded",
            classified_reason=reason,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "duplicate_query_suppressed"
                if reason == "duplicate_query_suppressed"
                else "amap_budget_exceeded",
                "message": "本轮相同高德 POI 查询已执行，已复用 ledger 并停止重复外呼。"
                if reason == "duplicate_query_suppressed"
                else "本轮高德 POI 外部调用预算已用尽，已停止继续请求。",
                "providerName": "amap",
                "debug": debug,
            },
        )

    def _mark_rate_limited(self, reason: str) -> None:
        global _AMAP_RATE_LIMIT_UNTIL, _AMAP_RATE_LIMIT_REASON
        _AMAP_RATE_LIMIT_UNTIL = monotonic() + AMAP_POI_RATE_LIMIT_COOLDOWN_SECONDS
        _AMAP_RATE_LIMIT_REASON = reason

    def _parse_poi(self, item: dict, category: str, origin: Optional[tuple[float, float]] = None) -> MapPoiResponse:
        longitude, latitude = self._parse_location(item.get("location"))
        distance = self._parse_optional_float(item.get("distance"))
        if distance is None and origin is not None:
            distance = self._distance_meters(origin, (longitude, latitude))
        photos = []
        for photo in item.get("photos") or []:
            url = str(photo.get("url") or "")
            if url:
                photos.append(MapPoiPhotoResponse(title=str(photo.get("title") or item.get("name") or ""), url=url))
        biz_ext = item.get("biz_ext") if isinstance(item.get("biz_ext"), dict) else {}
        tags_raw = item.get("tag") or item.get("tags") or []
        tags = (
            [str(value).strip() for value in re.split(r"[;|,，]", tags_raw) if str(value).strip()]
            if isinstance(tags_raw, str)
            else [str(value).strip() for value in tags_raw if str(value).strip()]
            if isinstance(tags_raw, list)
            else []
        )
        children = [dict(value) for value in item.get("children") or [] if isinstance(value, dict)]
        indoor_data = item.get("indoor_data")
        if isinstance(indoor_data, list):
            indoor_data = next((value for value in indoor_data if isinstance(value, dict)), {})
        if not isinstance(indoor_data, dict):
            indoor_data = {}
        return MapPoiResponse(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            type=str(item.get("type") or ""),
            city=str(item.get("cityname") or ""),
            district=str(item.get("adname") or ""),
            adcode=str(item.get("adcode") or "") or None,
            address=self._address_text(item.get("address")),
            longitude=longitude,
            latitude=latitude,
            category=category,
            source=AMAP_PLACE_SOURCE,
            source_note="来源：高德地图",
            distance_meters=distance,
            confidence=0.86,
            provider_type_code=str(item.get("typecode") or "") or None,
            provider_aliases=self._provider_aliases(item.get("alias")),
            tags=tags,
            business_area=str(item.get("business_area") or "") or None,
            rating=self._parse_optional_float(biz_ext.get("rating")),
            cost=self._parse_optional_float(biz_ext.get("cost")),
            open_time_today=str(biz_ext.get("opentime_today") or "") or None,
            open_time_week=str(biz_ext.get("opentime_week") or "") or None,
            parent_poi_id=str(item.get("parent") or "") or None,
            indoor_parent_poi_id=str(indoor_data.get("cpid") or "") or None,
            business_status=str(item.get("business_status") or biz_ext.get("business_status") or "") or None,
            children=children,
            photos=photos,
        )

    @staticmethod
    def _provider_aliases(value: object) -> list[str]:
        """Preserve bounded official aliases from the existing full POI result.

        AMap v3 documents ``alias`` with extensions=all. That field is already
        included in our search/detail budget; no additional lookup is needed.
        Do not derive aliases from tags, excerpts, names, or model source claims.
        """
        if isinstance(value, str):
            # Never turn a truncated string into an invented shorter alias.
            if len(value) > 8192:
                return []
            raw_values = re.split(r"[;；|]", value)
        elif isinstance(value, list):
            raw_values = value[:64]
        else:
            return []
        aliases: list[str] = []
        for raw in raw_values:
            if not isinstance(raw, str):
                continue
            alias = raw.strip()
            if not 1 <= len(alias) <= 256 or any(ord(char) < 32 for char in alias):
                continue
            if alias not in aliases:
                aliases.append(alias)
            if len(aliases) == 16:
                break
        return aliases

    def _parse_location(self, value: object) -> tuple[float, float]:
        try:
            longitude, latitude = str(value).split(",", 1)
            return float(longitude), float(latitude)
        except (AttributeError, TypeError, ValueError) as error:
            raise MapPoiProviderError("invalid POI location in AMap response") from error

    def _address_text(self, value: object) -> str:
        if isinstance(value, list):
            return " ".join(str(item) for item in value if item)
        return str(value or "")

    def _city_param(self, city: str) -> str:
        return CITY_ADCODES.get(city.strip(), city.strip())

    @staticmethod
    def _provider_types_param(category: str, default_type_codes: str, provider_types: Optional[str]) -> str:
        if provider_types is None:
            return default_type_codes
        normalized = str(provider_types).strip()
        if category != "food" or not re.fullmatch(r"[A-Za-z\u4e00-\u9fff·]{2,30}菜", normalized):
            raise HTTPException(
                status_code=400,
                detail="Provider type override is restricted to a server-owned destination cuisine subtype",
            )
        return normalized

    def _rank_pois(
        self,
        pois: list[MapPoiResponse],
        keyword: str,
        origin: Optional[tuple[float, float]] = None,
    ) -> list[MapPoiResponse]:
        normalized_keyword = self._normalize_name(keyword)

        def score(poi: MapPoiResponse) -> tuple[int, float, str]:
            normalized_name = self._normalize_name(poi.name)
            if normalized_keyword and normalized_name == normalized_keyword:
                name_score = 0
            elif normalized_keyword and (
                normalized_keyword in normalized_name or normalized_name in normalized_keyword
            ):
                name_score = 1
            else:
                name_score = 2
            distance = poi.distance_meters
            if distance is None and origin is not None:
                distance = self._distance_meters(origin, (poi.longitude, poi.latitude))
            return name_score, distance if distance is not None else float("inf"), poi.name

        return sorted(pois, key=score)

    def _normalize_name(self, value: str) -> str:
        return "".join(str(value or "").casefold().split())

    def _parse_optional_float(self, value: object) -> Optional[float]:
        if value in (None, "", []):
            return None
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    def _distance_meters(self, origin: tuple[float, float], point: tuple[float, float]) -> float:
        import math

        earth_radius_meters = 6371000
        lon1, lat1 = origin
        lon2, lat2 = point
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        lat_delta = math.radians(lat2 - lat1)
        lon_delta = math.radians(lon2 - lon1)
        a = math.sin(lat_delta / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(lon_delta / 2) ** 2
        return earth_radius_meters * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _provider_error_detail(
        self,
        message: str,
        source: str = "unknown",
        params: Optional[dict[str, str]] = None,
        debug: Optional[dict] = None,
    ) -> dict:
        debug_payload = (
            deepcopy(debug)
            if debug
            else self._amap_debug(
                source=source,
                params=params or {},
                raw_info=message,
                classified_reason=self._classify_amap_failure(message),
            )
        )
        classified_reason = str(debug_payload.get("classifiedReason") or self._classify_amap_failure(message))
        debug_payload["classifiedReason"] = classified_reason
        if self._rate_limit_classification(classified_reason):
            self._mark_rate_limited(message)
            retry_after = AMAP_POI_RATE_LIMIT_COOLDOWN_SECONDS
            debug_payload["retryAfterSeconds"] = retry_after
            return {
                "code": "provider_rate_limited",
                "message": self._rate_limit_user_message(classified_reason),
                "providerName": "amap",
                "retryAfterSeconds": retry_after,
                "debug": debug_payload,
            }
        return {
            "code": "provider_down",
            "message": f"AMap POI search failed: {self._safe_debug_text(message)}",
            "providerName": "amap",
            "debug": debug_payload,
        }

    def _amap_debug_from_payload(self, source: str, params: dict[str, str], payload: dict) -> dict:
        raw_text = " ".join(
            str(payload.get(key) or "")
            for key in ("status", "info", "infocode", "errmsg", "errcode")
            if payload.get(key) is not None
        )
        return self._amap_debug(
            source=source,
            params=params,
            raw_status=payload.get("status"),
            raw_info=payload.get("info"),
            raw_infocode=payload.get("infocode"),
            raw_errmsg=payload.get("errmsg"),
            raw_errcode=payload.get("errcode"),
            classified_reason=self._classify_amap_failure(raw_text),
        )

    def _amap_debug(
        self,
        *,
        source: str,
        params: dict[str, str],
        classified_reason: str,
        http_status_code: Optional[int] = None,
        raw_status: object = None,
        raw_info: object = None,
        raw_infocode: object = None,
        raw_errmsg: object = None,
        raw_errcode: object = None,
        retry_after_seconds: Optional[int] = None,
        cache_hit: bool = False,
        local_cooldown: bool = False,
    ) -> dict:
        debug = {
            "endpoint": source,
            "source": source,
            "httpStatusCode": http_status_code,
            "rawStatus": self._safe_debug_text(raw_status),
            "rawInfo": self._safe_debug_text(raw_info),
            "rawInfocode": self._safe_debug_text(raw_infocode),
            "rawErrmsg": self._safe_debug_text(raw_errmsg),
            "rawErrcode": self._safe_debug_text(raw_errcode),
            "classifiedReason": classified_reason,
            "retryAfterSeconds": retry_after_seconds,
            "requestParams": self._safe_request_params(params),
            "processId": os.getpid(),
            "cacheHit": cache_hit,
            "localCooldown": local_cooldown,
        }
        return {key: value for key, value in debug.items() if value not in (None, "", {})}

    def _safe_request_params(self, params: dict[str, str]) -> dict:
        allowed = {"keywords", "city", "types", "radius", "offset", "page", "location"}
        safe: dict[str, str] = {}
        for key in allowed:
            if key not in params:
                continue
            value = str(params.get(key) or "")
            if key == "location":
                value = self._rounded_location(value)
            safe[key] = self._safe_debug_text(value)
        return safe

    def _rounded_location(self, value: str) -> str:
        try:
            lon, lat = str(value).split(",", 1)
            return f"{float(lon):.4f},{float(lat):.4f}"
        except (TypeError, ValueError):
            return self._safe_debug_text(value)

    def _safe_debug_text(self, value: object) -> str:
        text = str(value or "")
        if self.map_provider_key:
            text = text.replace(self.map_provider_key, "<redacted>")
        text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<query-redacted>", text)
        return re.sub(
            r"(?i)\b(?:key|token|access_token|refresh_token|secret|api_key)=[^&\s]+",
            "<redacted-param>",
            text,
        )

    def _classify_amap_failure(self, message: object) -> str:
        text = str(message or "")
        upper = text.upper()
        lowered = text.lower()
        if "HTTP 429" in upper or "STATUS 429" in upper:
            return "http_429"
        if "USER_DAILY_QUERY_OVER_LIMIT" in upper or "USER_OVER_QUOTA" in upper:
            return "account_quota_limited"
        if "DAILY_QUERY_OVER_LIMIT" in upper:
            return "daily_quota_limited"
        if (
            "CUQPS_HAS_EXCEEDED" in upper
            or "CKQPS_HAS_EXCEEDED" in upper
            or "ACCESS_TOO_FREQUENT" in upper
            or "qps" in lowered
            or "频率超限" in text
            or "请求过于频繁" in text
            or "查询频率" in text
        ):
            return "qps_limited"
        if (
            "rate limit" in lowered
            or "too many requests" in lowered
            or "quota" in lowered
            or "limit" in lowered
            or "访问已超出" in text
            or "限流" in text
            or "配额" in text
            or "超限" in text
            or ("超出" in text and "限制" in text)
        ):
            return "unknown_rate_limited"
        return "provider_down"

    def _rate_limit_classification(self, classified_reason: str) -> bool:
        return classified_reason in {
            "qps_limited",
            "daily_quota_limited",
            "account_quota_limited",
            "http_429",
            "local_cooldown",
            "unknown_rate_limited",
        }

    def _rate_limit_user_message(self, classified_reason: str) -> str:
        if classified_reason in {"daily_quota_limited", "account_quota_limited"}:
            return "高德 POI 查询配额已达上限，行程未写入，避免插入未确认地点。"
        if classified_reason == "local_cooldown":
            return "高德 POI 查询暂在本地冷却中，请稍后重试。"
        return "高德 POI 查询暂时受限，请稍后重试。行程未写入，避免插入未确认地点。"

    def _is_rate_limit_message(self, message: object) -> bool:
        return self._rate_limit_classification(self._classify_amap_failure(message))
