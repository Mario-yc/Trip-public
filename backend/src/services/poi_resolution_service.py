import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.maps import (
    MapPoiPendingItemResponse,
    MapPoiResolveQueryRequest,
    MapPoiResolveRequest,
    MapPoiResolveResponse,
    MapPoiResolvedItemResponse,
    MapPoiResponse,
)
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.poi_candidate_dominance_service import PoiCandidateDominanceService


AUTO_ACCEPT_CONFIDENCE = 0.8


class PoiResolutionService:
    def __init__(self, db: sqlite3.Connection, map_service: Optional[MapPoiService] = None):
        self.db = db
        self.map_service = map_service or MapPoiService()
        self.semantic_policy = IntentCandidateSemanticPolicy()
        self.dominance_service = PoiCandidateDominanceService()
        self.last_dominance_evidence: dict[str, dict] = {}

    def resolve(self, payload: MapPoiResolveRequest, commit: bool = True) -> MapPoiResolveResponse:
        if not payload.queries:
            raise HTTPException(status_code=400, detail="At least one POI resolve query is required")

        self.last_dominance_evidence = {}
        resolved: list[MapPoiResolvedItemResponse] = []
        pending: list[MapPoiPendingItemResponse] = []
        for query in payload.queries:
            search_result = self._search(payload.city, query)
            search_result = self._semantic_candidates(query, search_result)
            strong_matches = [poi for poi in search_result if self._is_strong_match(query.name, poi)]
            accepted = self._accepted_candidate(query, search_result, strong_matches)
            if accepted is not None:
                self.last_dominance_evidence[query.name] = {
                    "action": "auto_select",
                    "selectedCandidateId": accepted.id,
                    "safeCandidateCount": 1,
                    "rejectedCandidateCount": max(0, len(search_result) - 1),
                    "dominant": True,
                    "margin": None,
                    "materialTradeoff": False,
                    "reason": "unique_strong_match",
                }
                resolved.append(
                    MapPoiResolvedItemResponse(
                        query=query.name,
                        status="accepted",
                        poi=accepted,
                    )
                )
                continue
            dominance = self._dominance_decision(payload.city, query, search_result)
            if dominance is not None:
                self.last_dominance_evidence[query.name] = {
                    "action": dominance.action,
                    "selectedCandidateId": dominance.selected_candidate_id,
                    "safeCandidateCount": len(dominance.safe_candidates),
                    "rejectedCandidateCount": len(dominance.rejected_candidates),
                    "safeCandidates": dominance.safe_candidates,
                    "rejectedCandidates": dominance.rejected_candidates,
                    **dominance.evidence,
                }
                safe_ids = {
                    str(item.get("candidateId") or "") for item in dominance.safe_candidates if item.get("candidateId")
                }
                search_result = [poi for poi in search_result if poi.id in safe_ids]
                if dominance.action == "auto_select" and dominance.selected_candidate_id:
                    accepted = next(
                        (poi for poi in search_result if poi.id == dominance.selected_candidate_id), None
                    )
                    if accepted is not None:
                        resolved.append(
                            MapPoiResolvedItemResponse(query=query.name, status="accepted", poi=accepted)
                        )
                        continue
                reason = "material_tradeoff" if dominance.action == "ask_user" else "no_safe_candidate"
                pending.append(
                    self._record_pending_candidate(
                        payload.session_id,
                        payload.turn_id,
                        payload.city,
                        query,
                        reason,
                        search_result,
                        record_status="pending" if dominance.action == "ask_user" else "rejected",
                    )
                )
                continue
            reason = self._pending_reason(search_result, strong_matches)
            pending.append(
                self._record_pending_candidate(
                    payload.session_id,
                    payload.turn_id,
                    payload.city,
                    query,
                    reason,
                    search_result,
                )
            )

        if commit:
            self.db.commit()
        return MapPoiResolveResponse(resolved=resolved, pending=pending)

    def persist_selected_candidate_identity(
        self,
        *,
        session_id: str,
        turn_id: Optional[str],
        city: str,
        query: MapPoiResolveQueryRequest,
        poi: MapPoiResponse,
        segment_id: str,
    ) -> str:
        """Stage a unique safe identity for the versioned patch executor.

        `pending` here means the itinerary write has not committed yet.  The
        selected AMap identity is explicit, and the normal candidate guard will
        atomically transition the row to `selected` with the patch.
        """
        existing = self.db.execute(
            """
            SELECT id FROM amap_poi_candidates
            WHERE session_id = ? AND segment_id = ? AND query = ?
              AND status = 'pending' AND selected_amap_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id, segment_id, query.name, poi.id),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        candidate_id = f"cand_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                candidate_id,
                session_id,
                turn_id,
                query.name,
                segment_id,
                city,
                query.category,
                json.dumps([poi.model_dump(by_alias=True)], ensure_ascii=False),
                poi.id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return candidate_id

    def _dominance_decision(
        self,
        city: str,
        query: MapPoiResolveQueryRequest,
        candidates: list[MapPoiResponse],
    ):
        intent_type = self._intent_type_from_category(query.category)
        if intent_type not in {"campus_visit", "museum", "meal"}:
            return None
        normalized_category = {
            "campus_visit": "campus",
            "museum": "museum",
            "meal": "food",
        }[intent_type]
        evidence_candidates = []
        for candidate in candidates:
            semantic = self.semantic_policy.evaluate(intent_type, candidate, raw_need=query.name)
            evidence_candidates.append(
                {
                    "candidateId": candidate.id,
                    "amapPoiId": candidate.id,
                    "name": candidate.name,
                    "provider": "amap" if candidate.source == AMAP_PLACE_SOURCE else candidate.source,
                    "city": candidate.city,
                    "category": normalized_category,
                    "longitude": candidate.longitude,
                    "latitude": candidate.latitude,
                    "distanceMeters": candidate.distance_meters,
                    "semanticScore": semantic.confidence,
                    "semanticReasonCode": semantic.reason_code,
                    "entityKey": self._normalize_name_without_brackets(candidate.name),
                }
            )
        return self.dominance_service.decide(
            intent={"intentType": intent_type, "city": city},
            candidates=evidence_candidates,
            route_context={},
        )

    def _semantic_candidates(
        self,
        query: MapPoiResolveQueryRequest,
        candidates: list[MapPoiResponse],
    ) -> list[MapPoiResponse]:
        intent_type = self._intent_type_from_category(query.category)
        if not intent_type:
            return candidates
        return [
            candidate
            for candidate in candidates
            if self.semantic_policy.evaluate(
                intent_type,
                candidate,
                raw_need=query.name,
            ).passed
        ]

    @staticmethod
    def _intent_type_from_category(category: str) -> str:
        normalized = str(category or "").strip().casefold()
        return {
            "museum": "museum",
            "university": "campus_visit",
            "education": "campus_visit",
            "school": "campus_visit",
            "park": "park",
            "food": "meal",
        }.get(normalized, "")

    def _search(self, city: str, query: MapPoiResolveQueryRequest) -> list[MapPoiResponse]:
        keyword = query.name.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="POI resolve query name is required")
        if query.near:
            response = self.map_service.search_nearby(
                city=city,
                longitude=query.near.longitude,
                latitude=query.near.latitude,
                keyword=keyword,
                category=query.category,
                radius=query.near.radius,
            )
        else:
            response = self.map_service.search(city=city, keyword=keyword, category=query.category)
        return response.pois

    def _is_strong_match(self, query_name: str, poi: MapPoiResponse) -> bool:
        if not self._is_valid_amap_candidate(poi):
            return False
        match_level = self._name_match_level(query_name, poi.name)
        if match_level == "none":
            return False
        if self._effective_confidence(poi, match_level) < AUTO_ACCEPT_CONFIDENCE:
            return False
        return True

    def _accepted_candidate(
        self,
        query: MapPoiResolveQueryRequest,
        candidates: list[MapPoiResponse],
        strong_matches: list[MapPoiResponse],
    ) -> Optional[MapPoiResponse]:
        if not candidates or not strong_matches:
            return None
        if query.near:
            near_match = self._accepted_near_candidate(query, strong_matches)
            if near_match is not None:
                return near_match

        exact_matches = [poi for poi in strong_matches if self._name_match_level(query.name, poi.name) == "exact"]
        contains_matches = [poi for poi in strong_matches if self._name_match_level(query.name, poi.name) == "contains"]
        if exact_matches:
            if self._is_education_category(query.category) and contains_matches:
                return None
            return self._best_exact_match(query, exact_matches)

        if len(contains_matches) == 1:
            return contains_matches[0]
        if len(contains_matches) > 1:
            return None
        return None

    def _accepted_near_candidate(
        self,
        query: MapPoiResolveQueryRequest,
        strong_matches: list[MapPoiResponse],
    ) -> Optional[MapPoiResponse]:
        if not query.near:
            return None
        origin = (query.near.longitude, query.near.latitude)
        ranked = sorted(strong_matches, key=lambda poi: self._candidate_distance(origin, poi))
        nearest = ranked[0]
        nearest_distance = self._candidate_distance(origin, nearest)
        max_distance = min(max(query.near.radius, 80), 500)
        if nearest_distance > max_distance:
            return None
        if len(ranked) == 1:
            return nearest
        second_distance = self._candidate_distance(origin, ranked[1])
        if second_distance - nearest_distance >= 120:
            return nearest
        return None

    def _best_exact_match(self, query: MapPoiResolveQueryRequest, exact_matches: list[MapPoiResponse]) -> Optional[MapPoiResponse]:
        if len(exact_matches) == 1:
            return exact_matches[0]
        ranked = sorted(exact_matches, key=lambda poi: self._category_match_score(query.category, poi), reverse=True)
        if self._category_match_score(query.category, ranked[0]) > self._category_match_score(query.category, ranked[1]):
            return ranked[0]
        return None

    def _is_valid_amap_candidate(self, poi: MapPoiResponse) -> bool:
        if poi.source != AMAP_PLACE_SOURCE:
            return False
        if not poi.id:
            return False
        if not poi.name.strip():
            return False
        if not math.isfinite(poi.longitude) or not math.isfinite(poi.latitude):
            return False
        if poi.longitude == 0 or poi.latitude == 0:
            return False
        return True

    def _candidate_distance(self, origin: tuple[float, float], poi: MapPoiResponse) -> float:
        if poi.distance_meters is not None:
            return poi.distance_meters
        return self._distance_meters(origin, (poi.longitude, poi.latitude))

    def _distance_meters(self, origin: tuple[float, float], point: tuple[float, float]) -> float:
        lon1, lat1 = origin
        lon2, lat2 = point
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        lat_delta = math.radians(lat2 - lat1)
        lon_delta = math.radians(lon2 - lon1)
        a = (
            math.sin(lat_delta / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(lon_delta / 2) ** 2
        )
        return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    def _is_education_category(self, category: str) -> bool:
        return category in {"university", "education", "school"}

    def _effective_confidence(self, poi: MapPoiResponse, match_level: str = "none") -> float:
        confidence = poi.confidence
        text = f"{poi.category} {poi.type} {poi.name}"
        category_markers = [
            "scenic",
            "museum",
            "park",
            "university",
            "education",
            "school",
            "风景名胜",
            "博物",
            "公园",
            "高等院校",
            "科教文化",
            "学校",
        ]
        entity_markers = ["大学", "学院", "博物院", "博物馆", "公园", "寺", "宫", "塔", "机场", "车站"]
        if any(marker in text for marker in category_markers) or any(marker in poi.name for marker in entity_markers):
            confidence = max(confidence, 0.82)
        if match_level == "exact":
            confidence = max(confidence, 0.95)
        elif match_level == "contains":
            confidence = max(confidence, 0.85)
        return confidence

    def _name_match_level(self, query_name: str, poi_name: str) -> str:
        normalized_query = self._normalize_name(query_name)
        normalized_name = self._normalize_name(poi_name)
        if not normalized_query or not normalized_name:
            return "none"
        if normalized_query == normalized_name:
            return "exact"
        if normalized_query == self._normalize_name_without_brackets(poi_name):
            return "exact"
        if normalized_query in normalized_name or normalized_name in normalized_query:
            return "contains"
        return "none"

    def _category_match_score(self, category: str, poi: MapPoiResponse) -> int:
        text = f"{poi.category} {poi.type} {poi.name}"
        groups = {
            "scenic": ["scenic", "风景名胜", "旅游景点", "景点"],
            "museum": ["museum", "博物馆", "博物院"],
            "park": ["park", "公园"],
            "university": ["university", "高等院校", "大学", "学院"],
            "education": ["education", "科教文化服务", "学校", "高等院校"],
            "school": ["school", "学校", "高等院校"],
            "food": ["food", "餐饮服务", "餐厅", "美食"],
        }
        markers = groups.get(category, [])
        return 1 if any(marker in text for marker in markers) else 0

    def _pending_reason(self, candidates: list[MapPoiResponse], strong_matches: list[MapPoiResponse]) -> str:
        if not candidates:
            return "no_results"
        if len(strong_matches) > 1:
            return "multiple_candidates"
        if not strong_matches:
            return "low_confidence"
        return "no_unique_strong_match"

    def _record_pending_candidate(
        self,
        session_id: str,
        turn_id: Optional[str],
        city: str,
        query: MapPoiResolveQueryRequest,
        reason: str,
        candidates: list[MapPoiResponse],
        record_status: str = "pending",
    ) -> MapPoiPendingItemResponse:
        candidate_id = f"cand_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session_id,
                turn_id,
                query.name,
                None,
                city,
                query.category,
                record_status,
                json.dumps([poi.model_dump(by_alias=True) for poi in candidates], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return MapPoiPendingItemResponse(
            candidateRecordId=candidate_id,
            query=query.name,
            reason=reason,
            candidates=candidates,
        )

    def _normalize_name(self, value: str) -> str:
        return re.sub(r"\s+", "", value).casefold().strip()

    def _normalize_name_without_brackets(self, value: str) -> str:
        without_brackets = re.sub(r"[\(（][^\)）]*[\)）]", "", value)
        return self._normalize_name(without_brackets)
