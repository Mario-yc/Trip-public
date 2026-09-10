import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from copy import deepcopy
from threading import Lock, Semaphore
from time import monotonic
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from src.core.config import get_settings
from src.models.itinerary_segment import ItinerarySegment
from src.models.poi import POI
from src.models.route_option import RouteOption, normalize_route_mode, route_mode_label
from src.services.amap_call_budget import (
    current_amap_call_budget,
    current_amap_route_repair_scope,
)
from src.services.amap_rate_limiter import AMAP_BASIC_WEB_SERVICE_QPS, AMAP_WEB_SERVICE_RATE_LIMITER
from src.services.map_poi_service import AMAP_PLACE_SOURCE
from src.services.night_view_candidate_policy import NightViewCandidatePolicy
from src.services.poi_trust_policy import PoiTrustPolicy


AMAP_BASE_URL = "https://restapi.amap.com"
AMAP_ROUTE_SOURCE = "amap-webservice"
AGENT_TEXT_TIMELINE_SOURCE = "agent-text-timeline"
ROUTE_MODES = ("walking", "bicycling", "transit", "driving", "taxi")
ROUTE_CACHE_TTL_SECONDS = 20 * 60
ROUTE_PARALLELISM = 3
ROUTE_MAP_REQUEST_PARALLELISM = AMAP_BASIC_WEB_SERVICE_QPS
ROUTE_MAP_REQUEST_SEMAPHORE = Semaphore(ROUTE_MAP_REQUEST_PARALLELISM)
MAX_PROVIDER_ROUTE_ALTERNATIVES = 3


class RouteProviderError(RuntimeError):
    pass


class RouteService:
    COMPACT_WALK_MAX_DISTANCE_METERS = 2_000
    _cache: dict[tuple, tuple[float, bool, object]] = {}
    _cache_lock = Lock()

    def __init__(self, map_provider_key: Optional[str] = None, timeout_seconds: float = 5.0):
        settings = get_settings()
        self.map_provider_key = map_provider_key if map_provider_key is not None else settings.map_provider_key
        self.timeout_seconds = timeout_seconds
        self.warnings: list[str] = []
        self.poi_trust_policy = PoiTrustPolicy()
        self.night_view_candidate_policy = NightViewCandidatePolicy()

    def build_routes(
        self,
        plan_id: str,
        pois: list[POI],
        transport_mode: str = "public_transit",
        segments: Optional[list[ItinerarySegment]] = None,
        route_pairs: Optional[set[tuple[str, str]]] = None,
        preferred_mode_only: bool = False,
        include_compact_fallbacks: bool = False,
        allow_semantic_route_anchors: bool = False,
        include_provider_alternatives: bool = False,
    ) -> list[RouteOption]:
        self.warnings = []
        if len(pois) < 2:
            return []
        if not self.map_provider_key:
            self.warnings.append("MAP_PROVIDER_KEY is not configured; route candidates were not generated.")
            return []

        groups = self._route_groups(
            pois,
            segments,
            allow_semantic_route_anchors=allow_semantic_route_anchors,
        )
        routes: list[RouteOption] = []
        preferred_mode = normalize_route_mode(transport_mode)
        if preferred_mode_only:
            modes = [preferred_mode]
        elif include_compact_fallbacks:
            # The compact feasibility seam only needs the requested mode and a
            # Provider-verified short walk.  Adding taxi here consumes one
            # third of the bounded route budget without helping the declared
            # public-transit preference or the insertion baseline proof.
            fallback_modes = ["walking"]
            modes = list(dict.fromkeys([preferred_mode, *fallback_modes]))
        else:
            modes = self._candidate_modes(preferred_mode)
        route_tasks = []
        skipped_groups: set[int] = set()
        active_group_indices: set[int] = set()
        for group_index, group in enumerate(groups, start=1):
            from_segment, to_segment, from_poi, to_poi = group
            if route_pairs is not None:
                if not from_segment or not to_segment or (from_segment.id, to_segment.id) not in route_pairs:
                    continue
            active_group_indices.add(group_index)
            if not self._is_routeable_poi(from_poi) or not self._is_routeable_poi(to_poi):
                self.warnings.append(
                    f"route_skipped_waiting_for_poi_grounding: {from_poi.name} -> {to_poi.name} endpoints are not confirmed AMap places."
                )
                skipped_groups.add(group_index)
                continue
            for poi in (from_poi, to_poi):
                if self._is_agent_text_timeline_anchor(poi):
                    self.warnings.append(
                        f"Route {from_poi.name} -> {to_poi.name} uses pending Agent POI anchor {poi.name}; verify AMap grounding before relying on it."
                    )
            for sort_order, mode in enumerate(modes, start=1):
                route_tasks.append((group_index, sort_order, from_segment, to_segment, from_poi, to_poi, mode))

        routes_by_group: dict[int, list[RouteOption]] = {
            index: [] for index in active_group_indices if index not in skipped_groups
        }
        if route_tasks:
            worker_count = min(ROUTE_PARALLELISM, len(route_tasks))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                tasks_by_future = defaultdict(list)
                futures_by_fetch_key = {}
                for group_index, sort_order, from_segment, to_segment, from_poi, to_poi, mode in route_tasks:
                    fetch_key = self._route_fetch_key(from_poi, to_poi, mode)
                    if fetch_key not in futures_by_fetch_key:
                        context = copy_context()
                        futures_by_fetch_key[fetch_key] = executor.submit(
                            context.run, self._fetch_amap_route, from_poi, to_poi, mode
                        )
                    tasks_by_future[futures_by_fetch_key[fetch_key]].append(
                        (group_index, sort_order, from_segment, to_segment, from_poi, to_poi, mode)
                    )
                for future in as_completed(tasks_by_future):
                    try:
                        payload = future.result()
                    except RouteProviderError as error:
                        for (
                            group_index,
                            _sort_order,
                            _from_segment,
                            _to_segment,
                            from_poi,
                            to_poi,
                            mode,
                        ) in tasks_by_future[future]:
                            self.warnings.append(
                                f"AMap {mode} route failed for {from_poi.name} -> {to_poi.name}: {error}"
                            )
                        continue
                    except Exception as error:
                        for (
                            group_index,
                            _sort_order,
                            _from_segment,
                            _to_segment,
                            from_poi,
                            to_poi,
                            mode,
                        ) in tasks_by_future[future]:
                            self.warnings.append(
                                f"AMap {mode} route failed for {from_poi.name} -> {to_poi.name}: {error}"
                            )
                        continue
                    for group_index, sort_order, from_segment, to_segment, from_poi, to_poi, mode in tasks_by_future[
                        future
                    ]:
                        try:
                            provider_payloads = (
                                self._bounded_provider_alternative_payloads(payload, mode)
                                if include_provider_alternatives
                                else [payload]
                            )
                            routes_by_group[group_index].extend(
                                self._route_from_payload(
                                    plan_id,
                                    group_index,
                                    sort_order,
                                    from_poi,
                                    to_poi,
                                    mode,
                                    provider_payload,
                                    from_segment_id=from_segment.id if from_segment else None,
                                    to_segment_id=to_segment.id if to_segment else None,
                                )
                                for provider_payload in provider_payloads
                            )
                        except RouteProviderError as error:
                            self.warnings.append(
                                f"AMap {mode} route failed for {from_poi.name} -> {to_poi.name}: {error}"
                            )
                        except Exception as error:
                            self.warnings.append(
                                f"AMap {mode} route failed for {from_poi.name} -> {to_poi.name}: {error}"
                            )

        for group_index, group in enumerate(groups, start=1):
            if group_index not in active_group_indices:
                continue
            if group_index in skipped_groups:
                continue
            from_segment, to_segment, from_poi, to_poi = group
            group_routes = routes_by_group.get(group_index, [])
            if group_routes:
                group_routes = sorted(
                    group_routes,
                    key=lambda route: self._route_selection_sort_key(
                        route,
                        preferred_mode,
                        include_compact_fallbacks=include_compact_fallbacks,
                    ),
                )
                if include_provider_alternatives and preferred_mode_only:
                    # The explicit bounded-options seam preserves Provider
                    # response order.  Observe mode can therefore keep option
                    # one, while a downstream deterministic ranker may inspect
                    # the remaining options without changing legacy callers.
                    group_routes = sorted(
                        group_routes,
                        key=lambda route: int((route.provider_payload or {}).get("providerAlternativeIndex") or 1),
                    )
                group_routes = self._annotate_meal_route_matrix_boundary(
                    group_routes,
                    preferred_mode,
                    from_segment,
                    to_segment,
                )
                self._annotate_preferred_mode_fallback(
                    group_routes,
                    preferred_mode,
                    include_compact_fallbacks=include_compact_fallbacks,
                )
                for index, route in enumerate(group_routes, start=1):
                    route.sort_order = index
                    route.is_selected = index == 1
                routes.extend(group_routes)
            else:
                self.warnings.append(f"No available AMap routes for {from_poi.name} -> {to_poi.name}.")
        return routes

    def _route_groups(
        self,
        pois: list[POI],
        segments: Optional[list[ItinerarySegment]],
        *,
        allow_semantic_route_anchors: bool = False,
    ) -> list[tuple[Optional[ItinerarySegment], Optional[ItinerarySegment], POI, POI]]:
        if not segments:
            return [(None, None, from_poi, to_poi) for from_poi, to_poi in zip(pois, pois[1:])]

        pois_by_id = {poi.id: poi for poi in pois}
        groups = []
        day_ids = []
        for segment in segments:
            if segment.day_id not in day_ids:
                day_ids.append(segment.day_id)
        for day_id in day_ids:
            day_segments = sorted(
                [
                    segment
                    for segment in segments
                    if segment.day_id == day_id
                    and self._is_route_anchor_segment(
                        segment,
                        pois_by_id.get(segment.poi_id),
                        allow_semantic_route_anchor=allow_semantic_route_anchors,
                    )
                ],
                key=lambda segment: segment.segment_order,
            )
            for from_segment, to_segment in zip(day_segments, day_segments[1:]):
                from_poi = pois_by_id.get(from_segment.poi_id)
                to_poi = pois_by_id.get(to_segment.poi_id)
                if from_poi and to_poi:
                    groups.append((from_segment, to_segment, from_poi, to_poi))
        return groups

    def _is_route_anchor_segment(
        self,
        segment: ItinerarySegment,
        poi: Optional[POI] = None,
        *,
        allow_semantic_route_anchor: bool = False,
    ) -> bool:
        if self._is_pending_route_anchor_segment(segment):
            return False
        semantic_metadata = segment.semantic_metadata if isinstance(segment.semantic_metadata, dict) else {}
        semantic_grounding_status = str(semantic_metadata.get("groundingStatus") or "")
        if (
            allow_semantic_route_anchor
            and semantic_metadata.get("routeAnchor") is True
            and semantic_grounding_status
            in {"verified_amap", "provisional", "selected", "user_confirmed", "agent_selected_candidate"}
            and poi is not None
            and self._is_routeable_poi(poi)
        ):
            return True
        if segment.kind in {"visit", "activity"}:
            if poi is not None and self._night_view_requires_concrete_poi(poi):
                return False
            return True
        if segment.kind != "meal" or poi is None:
            return False
        return self._is_routeable_poi(poi) and not self._is_non_route_meal_poi(poi)

    def _is_pending_route_anchor_segment(self, segment: ItinerarySegment) -> bool:
        notes = str(segment.notes or "")
        return any(
            token in notes
            for token in (
                "groundingStatus：waiting_for_poi_grounding",
                "groundingStatus: waiting_for_poi_grounding",
                "groundingStatus：provider_rate_limited",
                "groundingStatus: provider_rate_limited",
            )
        )

    def _is_non_route_meal_poi(self, poi: POI) -> bool:
        source_note = str(poi.source_note or "")
        return any(
            token in source_note
            for token in [
                "groundingStatus：not_required",
                "groundingStatus: not_required",
                "groundingStatus：optional_waiting",
                "groundingStatus: optional_waiting",
                "groundingStatus：draft_only",
                "groundingStatus: draft_only",
                "groundingStatus：waiting_for_poi_grounding",
                "groundingStatus: waiting_for_poi_grounding",
                "groundingStatus：area_unresolved",
                "groundingStatus: area_unresolved",
                "groundingStatus：provider_rate_limited",
                "groundingStatus: provider_rate_limited",
                "routeAnchor=false",
            ]
        )

    def _candidate_modes(self, preferred_mode: str) -> list[str]:
        practical_alternatives = {
            "bicycling": ["bicycling", "walking"],
            "bike": ["bicycling", "walking"],
            "walk": ["walking", "bicycling"],
            "walking": ["walking", "bicycling"],
            "transit": ["transit", "walking", "driving", "taxi"],
            "driving": ["driving", "taxi"],
            "taxi": ["taxi", "driving"],
        }
        if preferred_mode in practical_alternatives:
            return practical_alternatives[preferred_mode]
        return ["transit", "walking"]

    def _route_selection_sort_key(
        self,
        route: RouteOption,
        preferred_mode: str,
        *,
        include_compact_fallbacks: bool = False,
    ) -> tuple[int, int, float, int]:
        mode_rank = self._route_mode_rank(route.mode, preferred_mode)
        if (
            include_compact_fallbacks
            and normalize_route_mode(preferred_mode) == "transit"
            and normalize_route_mode(route.mode) == "walking"
            and 0 < int(route.distance_meters or 0) <= self.COMPACT_WALK_MAX_DISTANCE_METERS
        ):
            # Transit remains the trip preference, but a Provider-verified
            # compact walking connection competes on actual duration.  This
            # prevents bus waiting/transfer time from making a few blocks look
            # like an hour-long detour while keeping longer urban legs on the
            # requested transit mode.
            mode_rank = 0
        return (
            mode_rank,
            route.duration_seconds,
            route.cost_amount,
            route.distance_meters,
        )

    def _route_mode_rank(self, mode: str, preferred_mode: str) -> int:
        normalized_mode = normalize_route_mode(mode)
        preferred = normalize_route_mode(preferred_mode)
        ranks = {
            "transit": {"transit": 0, "walking": 1, "bicycling": 2, "taxi": 3, "driving": 4},
            "bicycling": {"bicycling": 0, "walking": 1, "transit": 2, "taxi": 3, "driving": 4},
            "walking": {"walking": 0, "bicycling": 1, "transit": 2, "taxi": 3, "driving": 4},
            "taxi": {"taxi": 0, "driving": 1, "transit": 2, "bicycling": 3, "walking": 4},
            "driving": {"driving": 0, "taxi": 1, "transit": 2, "bicycling": 3, "walking": 4},
        }
        return ranks.get(preferred, {}).get(normalized_mode, 9)

    def _annotate_preferred_mode_fallback(
        self,
        group_routes: list[RouteOption],
        preferred_mode: str,
        *,
        include_compact_fallbacks: bool = False,
    ) -> None:
        preferred = normalize_route_mode(preferred_mode)
        if preferred != "transit" or not group_routes:
            return
        selected = group_routes[0]
        if normalize_route_mode(selected.mode) == preferred:
            return
        if (selected.provider_payload or {}).get("fallbackFromPreferredMode"):
            return
        compact_walk_selected = bool(
            include_compact_fallbacks
            and normalize_route_mode(selected.mode) == "walking"
            and 0 < int(selected.distance_meters or 0) <= self.COMPACT_WALK_MAX_DISTANCE_METERS
        )
        if any(normalize_route_mode(route.mode) == preferred for route in group_routes) and not compact_walk_selected:
            return
        selected.provider_payload = {
            **dict(selected.provider_payload or {}),
            "fallbackFromPreferredMode": preferred,
            "fallbackReason": "compact_walk_faster_than_transit" if compact_walk_selected else "route_unavailable",
            "preferredMode": preferred,
            "selectedMode": normalize_route_mode(selected.mode),
            "preferredModeUnavailableReason": (
                "compact_walk_faster_than_transit"
                if compact_walk_selected
                else "amap_transit_route_unavailable"
            ),
            "userVisibleCaveat": (
                "该段距离较短，高德实测步行比公交/地铁更省时，已按步行衔接。"
                if compact_walk_selected
                else f"用户偏好公交/地铁，但该段没有可用公交/地铁路线，暂用{route_mode_label(selected.mode)}候选。"
            ),
            "routeStatus": "fallback_from_preferred_mode",
        }

    def _annotate_meal_route_matrix_boundary(
        self,
        group_routes: list[RouteOption],
        preferred_mode: str,
        from_segment: Optional[ItinerarySegment],
        to_segment: Optional[ItinerarySegment],
    ) -> list[RouteOption]:
        if not group_routes or not self._meal_adjacent_route(from_segment, to_segment):
            return group_routes
        selected = group_routes[0]
        selected.provider_payload = {
            **dict(selected.provider_payload or {}),
            "mealRouteSelectionBoundary": {
                "preferredMode": normalize_route_mode(preferred_mode),
                "selectedMode": normalize_route_mode(selected.mode),
                "decisionRole": "route_mode_candidate_ordering_only",
                "providerRouteMatrixRequiredForDetourDecision": True,
                "fixedAbsoluteDetourThresholdApplied": False,
            },
        }
        return group_routes

    def _meal_adjacent_route(
        self,
        from_segment: Optional[ItinerarySegment],
        to_segment: Optional[ItinerarySegment],
    ) -> bool:
        return bool((from_segment and from_segment.kind == "meal") or (to_segment and to_segment.kind == "meal"))

    def _is_real_amap_poi(self, poi: POI) -> bool:
        trust_policy = getattr(self, "poi_trust_policy", None)
        if trust_policy is None:
            trust_policy = PoiTrustPolicy()
            self.poi_trust_policy = trust_policy
        if trust_policy.is_mock_or_synthetic_poi_values(
            source=poi.source,
            amap_id=poi.amap_id,
            source_note=poi.source_note,
            name=poi.name,
            intent_type="meal" if poi.category == "food" else "",
        ):
            return False
        return bool(poi.amap_id and poi.source == AMAP_PLACE_SOURCE and self._has_valid_coordinates(poi))

    def _is_routeable_poi(self, poi: POI) -> bool:
        if self._night_view_requires_concrete_poi(poi):
            return False
        return self._is_real_amap_poi(poi) or self._is_agent_text_timeline_anchor(poi)

    def _is_agent_text_timeline_anchor(self, poi: POI) -> bool:
        return bool(poi.source == AGENT_TEXT_TIMELINE_SOURCE and poi.amap_id and self._has_valid_coordinates(poi))

    def _night_view_requires_concrete_poi(self, poi: POI) -> bool:
        text = f"{poi.name or ''} {poi.source_note or ''}"
        is_night_view = (
            "intentType：night_view" in text
            or "intentType=night_view" in text
            or "intentType: night_view" in text
            or bool(re.search(r"(夜景|夜游|观景|灯光|晚上|夜晚)", text))
        )
        if not is_night_view:
            return False
        # Provider provenance may truthfully say that a ``周边范围`` bounded
        # discovery, but that phrase does not turn an exact AMap entity into a
        # range placeholder.  Placeholder identity is owned by the POI name;
        # source/provenance is checked independently below.
        if self.night_view_candidate_policy.placeholder_reject_reason(poi.name):
            return True
        return poi.source != AMAP_PLACE_SOURCE

    def _has_valid_coordinates(self, poi: POI) -> bool:
        try:
            latitude = float(poi.latitude)
            longitude = float(poi.longitude)
        except (TypeError, ValueError):
            return False
        return -90 <= latitude <= 90 and -180 <= longitude <= 180 and (latitude != 0 or longitude != 0)

    def _build_amap_route(
        self,
        plan_id: str,
        group_index: int,
        sort_order: int,
        from_poi: POI,
        to_poi: POI,
        mode: str,
        from_segment_id: Optional[str] = None,
        to_segment_id: Optional[str] = None,
    ) -> RouteOption:
        payload = self._fetch_amap_route(from_poi, to_poi, mode)
        return self._route_from_payload(
            plan_id,
            group_index,
            sort_order,
            from_poi,
            to_poi,
            mode,
            payload,
            from_segment_id=from_segment_id,
            to_segment_id=to_segment_id,
        )

    def _route_from_payload(
        self,
        plan_id: str,
        group_index: int,
        sort_order: int,
        from_poi: POI,
        to_poi: POI,
        mode: str,
        payload: dict,
        from_segment_id: Optional[str] = None,
        to_segment_id: Optional[str] = None,
    ) -> RouteOption:
        replay_metadata = payload.get("_tripRecordedReplay") if isinstance(payload, dict) else None
        alternative_metadata = (
            payload.get("_tripProviderAlternative") if isinstance(payload, dict) else None
        )
        provider_payload = dict(payload)
        provider_payload.pop("_tripRecordedReplay", None)
        provider_payload.pop("_tripProviderAlternative", None)
        if isinstance(replay_metadata, dict):
            provider_payload["recordedProviderEvidence"] = dict(replay_metadata)
        if isinstance(alternative_metadata, dict):
            provider_payload.update(
                {
                    "providerAlternativeIndex": int(alternative_metadata["index"]),
                    "providerAlternativeCount": int(alternative_metadata["count"]),
                    "providerAlternativesTruncated": bool(alternative_metadata["truncated"]),
                    "providerAlternativeFingerprint": str(alternative_metadata["fingerprint"]),
                }
            )
        distance_meters, duration_seconds, cost_amount, polyline, steps = self._parse_amap_route(provider_payload, mode)
        route_id = (
            f"route_{plan_id}_{from_segment_id}_{to_segment_id}_{mode}"
            if from_segment_id and to_segment_id
            else f"route_{plan_id}_{group_index}_{mode}"
        )
        if isinstance(alternative_metadata, dict):
            route_id = f"{route_id}_alternative_{str(alternative_metadata['fingerprint'])[:16]}"
        return RouteOption(
            id=route_id,
            plan_id=plan_id,
            from_segment_id=from_segment_id,
            to_segment_id=to_segment_id,
            from_poi_id=from_poi.id,
            to_poi_id=to_poi.id,
            provider=AMAP_ROUTE_SOURCE,
            mode=mode,
            label=route_mode_label(mode),
            is_selected=False,
            sort_order=sort_order,
            distance_meters=distance_meters,
            duration_seconds=duration_seconds,
            cost_amount=cost_amount,
            cost_currency="CNY",
            polyline=polyline,
            steps=steps,
            provider_payload=provider_payload,
        )

    def _bounded_provider_alternative_payloads(self, payload: dict, transport_mode: str) -> list[dict]:
        """Split one AMap response into at most three same-mode payloads.

        The cap is server-owned and cannot be increased by a caller or model.
        No Provider work happens here; every returned payload is derived from
        the one already-fetched HTTP response.
        """

        mode = normalize_route_mode(transport_mode)
        root_key = "route"
        alternatives_key = "transits" if mode == "transit" else "paths"
        if mode == "bicycling":
            root_key = "data"
        container = payload.get(root_key) if isinstance(payload, dict) else None
        alternatives = container.get(alternatives_key) if isinstance(container, dict) else None
        if not isinstance(alternatives, list) or not alternatives:
            return [payload]
        count = len(alternatives)
        result: list[dict] = []
        for index, option in enumerate(alternatives[:MAX_PROVIDER_ROUTE_ALTERNATIVES], start=1):
            option_fingerprint = hashlib.sha256(
                json.dumps(
                    option,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            candidate = deepcopy(payload)
            candidate_container = candidate.get(root_key)
            if not isinstance(candidate_container, dict):
                continue
            candidate_container[alternatives_key] = [deepcopy(option)]
            candidate["_tripProviderAlternative"] = {
                "index": index,
                "count": count,
                "truncated": count > MAX_PROVIDER_ROUTE_ALTERNATIVES,
                "fingerprint": option_fingerprint,
            }
            result.append(candidate)
        return result or [payload]

    def _route_fetch_key(
        self, from_poi: POI, to_poi: POI, transport_mode: str
    ) -> tuple[str, str, str, str, tuple[tuple[str, str], ...], str]:
        endpoint, params = self._amap_request_params(from_poi, to_poi, transport_mode)
        repair_scope_certificate = current_amap_route_repair_scope()
        return (
            str(from_poi.amap_id or "").strip().upper(),
            str(to_poi.amap_id or "").strip().upper(),
            normalize_route_mode(transport_mode),
            endpoint,
            tuple(sorted(params.items())),
            str((repair_scope_certificate or {}).get("scopeFingerprint") or ""),
        )

    def _fetch_amap_route(self, from_poi: POI, to_poi: POI, transport_mode: str) -> dict:
        normalized_mode = normalize_route_mode(transport_mode)
        budget_endpoint = f"route/{normalized_mode}"
        budget_keyword = f"{from_poi.name}->{to_poi.name}"
        from_amap_id = str(from_poi.amap_id or "").strip().upper()
        to_amap_id = str(to_poi.amap_id or "").strip().upper()
        budget = current_amap_call_budget()
        repair_scope_certificate = current_amap_route_repair_scope()
        repair_scope_fingerprint = str(
            (repair_scope_certificate or {}).get("scopeFingerprint") or ""
        )
        if budget is not None and not budget.validate_route_work(
            from_amap_id=from_amap_id,
            to_amap_id=to_amap_id,
            mode=normalized_mode,
            endpoint=budget_endpoint,
            source="route_service",
            repair_scope_certificate=repair_scope_certificate,
        ):
            raise RouteProviderError("amap route work is not authorized")
        endpoint, params = self._amap_request_params(from_poi, to_poi, transport_mode)
        params["key"] = self.map_provider_key or ""
        cache_key = (
            id(urlopen),
            from_amap_id,
            to_amap_id,
            normalized_mode,
            endpoint,
            tuple(sorted(params.items())),
            repair_scope_fingerprint,
        )
        cached = self._cached_route_payload(cache_key)
        if cached is not None:
            if budget is not None:
                cache_lease_valid = budget.record_cache_hit(
                    endpoint=budget_endpoint,
                    keyword=budget_keyword,
                    category=normalized_mode,
                    source="route_service",
                    from_amap_id=from_amap_id,
                    to_amap_id=to_amap_id,
                    mode=normalized_mode,
                    repair_scope_certificate=repair_scope_certificate,
                )
                if not cache_lease_valid:
                    raise RouteProviderError("amap route cache work is not authorized")
            ok, value = cached
            if ok:
                return deepcopy(value)
            raise RouteProviderError(str(value))

        if budget is not None and not budget.try_acquire(
            endpoint=budget_endpoint,
            keyword=budget_keyword,
            category=normalized_mode,
            source="route_service",
            from_amap_id=from_amap_id,
            to_amap_id=to_amap_id,
            mode=normalized_mode,
            repair_scope_certificate=repair_scope_certificate,
        ):
            if budget.last_denial_reason == "budget_exceeded":
                raise RouteProviderError("amap route budget exceeded")
            raise RouteProviderError("amap route work is not authorized")

        url = f"{AMAP_BASE_URL}{endpoint}?{urlencode(params)}"
        with ROUTE_MAP_REQUEST_SEMAPHORE:
            AMAP_WEB_SERVICE_RATE_LIMITER.acquire()
            try:
                with urlopen(url, timeout=self.timeout_seconds) as response:
                    body = response.read().decode("utf-8")
            except HTTPError as error:
                message = f"HTTP {error.code} from {endpoint}"
                self._store_route_cache(cache_key, False, message)
                raise RouteProviderError(message) from error
            except URLError as error:
                message = str(error.reason)
                self._store_route_cache(cache_key, False, message)
                raise RouteProviderError(message) from error
            except TimeoutError as error:
                self._store_route_cache(cache_key, False, "request timed out")
                raise RouteProviderError("request timed out") from error

            try:
                payload = json.loads(body)
            except json.JSONDecodeError as error:
                self._store_route_cache(cache_key, False, "invalid JSON response")
                raise RouteProviderError("invalid JSON response") from error

        if not self._is_success_payload(payload):
            message = (
                payload.get("info")
                or payload.get("errmsg")
                or payload.get("infocode")
                or payload.get("errcode")
                or "unknown AMap error"
            )
            self._store_route_cache(cache_key, False, str(message))
            raise RouteProviderError(str(message))
        self._store_route_cache(cache_key, True, payload)
        return payload

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()

    @classmethod
    def _cached_route_payload(cls, cache_key: tuple) -> Optional[tuple[bool, object]]:
        now = monotonic()
        with cls._cache_lock:
            cached = cls._cache.get(cache_key)
            if cached is None:
                return None
            expires_at, ok, value = cached
            if expires_at <= now:
                cls._cache.pop(cache_key, None)
                return None
            return ok, deepcopy(value) if ok else value

    @classmethod
    def _store_route_cache(cls, cache_key: tuple, ok: bool, value: object) -> None:
        with cls._cache_lock:
            cls._cache[cache_key] = (monotonic() + ROUTE_CACHE_TTL_SECONDS, ok, deepcopy(value) if ok else str(value))

    def _amap_request_params(self, from_poi: POI, to_poi: POI, transport_mode: str) -> tuple[str, dict[str, str]]:
        origin = f"{from_poi.longitude},{from_poi.latitude}"
        destination = f"{to_poi.longitude},{to_poi.latitude}"
        mode = normalize_route_mode(transport_mode)
        if mode == "walking":
            return "/v3/direction/walking", {"origin": origin, "destination": destination}
        if mode == "bicycling":
            return "/v4/direction/bicycling", {"origin": origin, "destination": destination}
        if mode == "transit":
            return (
                "/v3/direction/transit/integrated",
                {
                    "origin": origin,
                    "destination": destination,
                    "city": from_poi.city,
                    "cityd": to_poi.city,
                    "strategy": "0",
                },
            )
        return "/v3/direction/driving", {
            "origin": origin,
            "destination": destination,
            "strategy": "0",
            "extensions": "all",
        }

    def _parse_amap_route(
        self, payload: dict, transport_mode: str
    ) -> tuple[int, int, float, list[list[float]], list[dict]]:
        mode = normalize_route_mode(transport_mode)
        route = payload.get("route") or {}
        if mode == "transit":
            transits = route.get("transits") or []
            if not transits:
                raise RouteProviderError("no transit route returned")
            first = transits[0]
            distance_meters = self._positive_int(first.get("distance"), "distance")
            duration_seconds = self._positive_int(first.get("duration"), "duration")
            cost_amount = self._float_or_default(first.get("cost"), 0.0)
            steps, polyline = self._transit_steps_and_polyline(first)
            if not polyline:
                raise RouteProviderError("no transit polyline returned")
            return distance_meters, duration_seconds, cost_amount, polyline, steps

        route_container = payload.get("data") if mode == "bicycling" else route
        if not isinstance(route_container, dict):
            route_container = {}
        paths = route_container.get("paths") or []
        if not paths:
            raise RouteProviderError("no route path returned")
        first = paths[0]
        distance_meters = self._positive_int(first.get("distance"), "distance")
        duration_seconds = self._positive_int(first.get("duration"), "duration")
        steps = self._path_steps(first.get("steps") or [], mode)
        polyline = self._polyline_from_steps(first.get("steps") or [])
        if not polyline:
            raise RouteProviderError("no route polyline returned")
        cost_amount = self._path_cost(first, route, distance_meters, mode)
        return distance_meters, duration_seconds, cost_amount, polyline, steps

    def _path_steps(self, raw_steps: list[dict], mode: str) -> list[dict]:
        steps = []
        for step in raw_steps:
            steps.append(
                {
                    "mode": mode,
                    "instruction": str(step.get("instruction") or ""),
                    "road": str(step.get("road") or ""),
                    "distance": self._int_or_default(step.get("distance"), 0),
                    "duration": self._int_or_default(step.get("duration"), 0),
                    "polyline": str(step.get("polyline") or ""),
                }
            )
        return steps

    def _transit_steps_and_polyline(self, transit: dict) -> tuple[list[dict], list[list[float]]]:
        steps = []
        polyline: list[list[float]] = []
        for segment in transit.get("segments") or []:
            walking = segment.get("walking") or {}
            for step in walking.get("steps") or []:
                line = str(step.get("polyline") or "")
                steps.append(
                    {
                        "mode": "walking",
                        "instruction": str(step.get("instruction") or ""),
                        "road": str(step.get("road") or ""),
                        "distance": self._int_or_default(step.get("distance"), 0),
                        "duration": self._int_or_default(step.get("duration"), 0),
                        "polyline": line,
                    }
                )
                polyline.extend(self._parse_polyline(line))
            bus = segment.get("bus") or {}
            for busline in bus.get("buslines") or []:
                line = str(busline.get("polyline") or "")
                steps.append(
                    {
                        "mode": "transit",
                        "instruction": str(busline.get("name") or ""),
                        "road": str(busline.get("name") or ""),
                        "distance": self._int_or_default(busline.get("distance"), 0),
                        "duration": self._int_or_default(busline.get("duration"), 0),
                        "polyline": line,
                    }
                )
                polyline.extend(self._parse_polyline(line))
        return steps, self._dedupe_polyline(polyline)

    def _polyline_from_steps(self, raw_steps: list[dict]) -> list[list[float]]:
        points: list[list[float]] = []
        for step in raw_steps:
            points.extend(self._parse_polyline(str(step.get("polyline") or "")))
        return self._dedupe_polyline(points)

    def _parse_polyline(self, value: str) -> list[list[float]]:
        points = []
        for item in value.split(";"):
            if not item.strip():
                continue
            try:
                longitude, latitude = item.split(",", 1)
                points.append([float(longitude), float(latitude)])
            except ValueError as error:
                raise RouteProviderError("invalid polyline in AMap response") from error
        return points

    def _dedupe_polyline(self, points: list[list[float]]) -> list[list[float]]:
        deduped: list[list[float]] = []
        for point in points:
            if not deduped or deduped[-1] != point:
                deduped.append(point)
        return deduped

    def _path_cost(self, path: dict, route: dict, distance_meters: int, mode: str) -> float:
        if mode == "taxi":
            return self._float_or_default(route.get("taxi_cost"), self.estimate_cost(distance_meters, mode))
        if mode == "driving":
            tolls = self._float_or_default(path.get("tolls"), 0.0)
            return round(tolls + self.estimate_cost(distance_meters, mode), 1)
        return 0.0

    def estimate_cost(self, distance_meters: int, transport_mode: str) -> float:
        mode = normalize_route_mode(transport_mode)
        if mode == "taxi":
            return round(13 + distance_meters / 1000 * 2.4, 1)
        if mode == "driving":
            return round(12 + distance_meters / 1000 * 1.2, 1)
        return 0.0

    def _is_success_payload(self, payload: dict) -> bool:
        if "status" in payload:
            return str(payload.get("status")) == "1"
        if "errcode" in payload:
            return str(payload.get("errcode")) == "0"
        return bool(payload.get("route") or payload.get("data"))

    def _positive_int(self, value: object, field_name: str) -> int:
        try:
            parsed = int(float(str(value)))
        except (TypeError, ValueError) as error:
            raise RouteProviderError(f"invalid {field_name} in AMap response") from error
        if parsed <= 0:
            raise RouteProviderError(f"invalid {field_name} in AMap response")
        return parsed

    def _float_or_default(self, value: object, default: float) -> float:
        try:
            return round(float(str(value)), 1)
        except (TypeError, ValueError):
            return default

    def _int_or_default(self, value: object, default: int) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return default
