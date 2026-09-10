"""Bounded preflight for adding a generic POI into an existing day.

The service is deliberately read-only: it performs semantic filtering and
geometry insertion screening before the existing mutation transaction can
create a patch/version.  Exact entities retain their identity; category
requests search only around the adjacent route anchors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from src.api.schemas.maps import MapPoiResponse
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.provider_route_insertion_service import (
    ProviderRouteInsertionService,
)
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import RouteService
from src.services.timeline_mutation_models import BoundTimelineMutation


class AdjacentInsertionCandidateService:
    MAX_NEARBY_SEARCHES = 3
    MAX_CANDIDATES = 3

    def __init__(
        self,
        map_poi_service: MapPoiService | None = None,
        route_service: RouteService | None = None,
        route_insertion_service: ProviderRouteInsertionService | None = None,
    ) -> None:
        self.map_poi_service = map_poi_service or MapPoiService()
        self.route_service = route_service or RouteService()
        self.route_insertion_service = route_insertion_service or ProviderRouteInsertionService(
            route_service=self.route_service
        )
        self.semantic_policy = IntentCandidateSemanticPolicy()
        self.insertion_scorer = RouteInsertionScorer()

    def resolve(
        self, bound: BoundTimelineMutation, *, city: str
    ) -> tuple[list[MapPoiResponse], list[dict[str, Any]], str | None]:
        query = str(bound.intent.replacement.poi_query or "").strip()
        # Existing persisted semantic intents predate poiQueryMode. Their operation
        # and structured selector supply a safe compatibility default; no user
        # text/Chinese phrase is inspected here.
        mode = bound.intent.replacement.poi_query_mode or "category_intent"
        anchors = list(bound.adjacent_descriptors)
        legacy_text_adapter = False
        try:
            if mode == "exact_entity":
                responses = [self.map_poi_service.search(city=city, keyword=query, category="all", limit=8)]
            else:
                centers = self._centers(anchors)
                if not centers:
                    return [], [], "adjacent_route_anchor_missing"
                nearby = getattr(self.map_poi_service, "search_nearby", None)
                if callable(nearby):
                    responses = [
                        nearby(
                            city=city,
                            longitude=longitude,
                            latitude=latitude,
                            keyword=query,
                            category=self._category(bound.intent.selector.intent_type),
                            radius=2500,
                            limit=8,
                        )
                        for longitude, latitude in centers[: self.MAX_NEARBY_SEARCHES]
                    ]
                else:
                    return [], [], "provider_failure:nearby_search_unavailable"
        except (HTTPException, RuntimeError, OSError) as error:
            return [], [], f"provider_failure:{error}"

        expected = str(bound.intent.selector.intent_type or "")
        previous = self._anchor(anchors[0]) if anchors else None
        following = self._anchor(anchors[-1]) if len(anchors) > 1 else None
        admitted: list[tuple[MapPoiResponse, Any, dict[str, Any]]] = []
        evidence: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for response in responses:
            for candidate in response.pois:
                if candidate.id in seen_ids or not self._trusted(candidate, city):
                    continue
                seen_ids.add(candidate.id)
                semantic = self.semantic_policy.evaluate(expected, candidate, raw_need=query)
                score = self.insertion_scorer.score(previous, candidate, following, transport_mode="transit")
                decision = {
                    "amapPoiId": candidate.id,
                    "semantic": semantic.to_camel_dict(),
                    "insertion": self._score_dict(score),
                    "queryMode": mode,
                    "legacyTextAdapter": legacy_text_adapter,
                }
                evidence.append(decision)
                if mode == "exact_entity" and not self._same_entity(query, candidate.name):
                    decision["identity"] = {"passed": False, "reason": "exact_entity_identity_mismatch"}
                    continue
                if not semantic.passed:
                    continue
                # Geometry only ranks the bounded provider batch.  A hard
                # acceptance/rejection must come from _verify_route_impact,
                # whose legs are provider-supplied rather than Haversine.
                if score is None:
                    continue
                admitted.append((candidate, score, decision))

        ranked = sorted(
            admitted,
            key=lambda item: (
                self._added_distance(previous, item[0], following),
                -float(item[0].confidence or 0),
                item[0].id,
            ),
        )[: self.MAX_CANDIDATES]
        verified: list[MapPoiResponse] = []
        route_provider_failure: str | None = None
        for candidate, _score, decision in ranked:
            route_evidence, route_failure = self._verify_route_impact(bound, anchors, candidate)
            decision["routeVerification"] = route_evidence
            if route_failure:
                # Preserve the decisive fail-closed reason.  Collapsing a
                # missing contract or impossible schedule into "no candidate"
                # would invite a later writer to retry without the required
                # route authority.
                route_provider_failure = route_failure
                continue
            verified.append(candidate)
        if not verified:
            return [], evidence, route_provider_failure or "no_adjacent_route_feasible_candidate"
        return verified, evidence, None

    def _verify_route_impact(
        self,
        bound: BoundTimelineMutation,
        anchors: list[Any],
        candidate: MapPoiResponse,
    ) -> tuple[dict[str, Any], str | None]:
        previous = self._matrix_anchor(anchors[0]) if anchors else None
        following = self._matrix_anchor(anchors[-1]) if len(anchors) > 1 else None
        transport_mode, transport_error = self._verified_transport_mode(bound, anchors)
        if transport_error:
            return (
                {
                    "status": "failed",
                    "failureReason": transport_error,
                    "networkVerified": False,
                    "legs": {},
                    "routeDecisionContract": bound.route_decision_contract,
                },
                transport_error,
            )
        candidate_duration = self._candidate_duration_minutes(bound)
        if candidate_duration is None:
            return (
                {
                    "status": "failed",
                    "failureReason": "candidate_duration_missing",
                    "networkVerified": False,
                    "legs": {},
                    "routeDecisionContract": bound.route_decision_contract,
                },
                "candidate_duration_missing",
            )
        candidate_schedule = self._candidate_schedule(bound, candidate_duration)
        result = self.route_insertion_service.evaluate(
            plan_id=f"mutation_preflight_{bound.mutation_id}",
            previous=previous,
            candidate={
                "segmentId": f"preflight_candidate_{candidate.id}",
                "dayId": str(bound.target_day_id or "route_matrix_day"),
                "amapId": str(candidate.id),
                "name": str(candidate.name),
                "city": str(candidate.city),
                "type": str(candidate.type or ""),
                "source": str(candidate.source or ""),
                "longitude": candidate.longitude,
                "latitude": candidate.latitude,
                **candidate_schedule,
            },
            following=following,
            baseline_candidate=(
                self._matrix_anchor(bound.target_descriptors[0])
                if bound.intent.operation == "replace_poi" and bound.target_descriptors
                else None
            ),
            transport_mode=transport_mode,
            candidate_duration_minutes=candidate_duration,
            # Do not claim that a candidate fits its time window before the
            # route legs are known.  ProviderRouteInsertionService projects
            # the actual adjacent schedule from the persisted descriptors;
            # missing schedule facts are a pending/fail-closed result.
            detour_tolerance=(
                bound.route_decision_contract.get("detourTolerance")
                if isinstance(bound.route_decision_contract, dict)
                else None
            ),
            mobility_profile=(
                bound.route_decision_contract.get("mobilityProfile")
                if isinstance(bound.route_decision_contract, dict)
                else None
            ),
            route_decision_contract=bound.route_decision_contract,
        )
        payload = result.to_dict()
        if result.passed:
            return payload, None
        failure = str(result.failure_reason or "provider_route_matrix_incomplete")
        if result.status == "pending":
            return payload, "route_provider_unavailable"
        if failure.startswith("provider_route_leg_missing"):
            return payload, "route_provider_matrix_missing"
        return payload, failure

    @staticmethod
    def _verified_transport_mode(bound: BoundTimelineMutation, anchors: list[Any]) -> tuple[str | None, str | None]:
        """Resolve only server-signed or persisted transport evidence.

        The mutation request's ``replacement.transportMode`` is a user/model
        suggestion, so it must never select a Provider matrix mode.  A signed
        contract may name one mode; otherwise all adjacent persisted segments
        must agree on an explicit mode.
        """
        contract = bound.route_decision_contract
        provenance = contract.get("provenance") if isinstance(contract, dict) else {}
        signed_mode = str(provenance.get("transportMode") or "").strip() if isinstance(provenance, dict) else ""
        persisted_modes = {
            str(anchor.transport_mode or "").strip() for anchor in anchors if str(anchor.transport_mode or "").strip()
        }
        if len(persisted_modes) > 1:
            return None, "route_transport_mode_conflict"
        persisted_mode = next(iter(persisted_modes), "")
        if signed_mode and persisted_mode and signed_mode != persisted_mode:
            return None, "route_transport_mode_conflict"
        mode = signed_mode or persisted_mode
        if not mode:
            return None, "route_transport_mode_missing"
        return mode, None

    @staticmethod
    def _candidate_duration_minutes(bound: BoundTimelineMutation) -> int | None:
        explicit = bound.intent.replacement.duration_minutes
        if isinstance(explicit, int) and explicit > 0:
            return explicit
        if bound.intent.operation != "replace_poi" or not bound.target_descriptors:
            return None
        target = bound.target_descriptors[0]
        start = AdjacentInsertionCandidateService._minutes(target.start_time)
        end = AdjacentInsertionCandidateService._minutes(target.end_time)
        if start is None or end is None or end <= start:
            return None
        return end - start

    @classmethod
    def _candidate_schedule(cls, bound: BoundTimelineMutation, duration_minutes: int) -> dict[str, str]:
        start_time = str(bound.intent.replacement.start_time or "").strip()
        if not start_time and bound.intent.operation == "replace_poi" and bound.target_descriptors:
            start_time = str(bound.target_descriptors[0].start_time or "").strip()
        start = cls._minutes(start_time)
        if start is None:
            return {}
        end = start + duration_minutes
        if end >= 24 * 60:
            return {"startTime": start_time}
        return {
            "startTime": start_time,
            "endTime": f"{end // 60:02d}:{end % 60:02d}",
        }

    @staticmethod
    def _minutes(value: Any) -> int | None:
        try:
            hour, minute = (int(item) for item in str(value).split(":", 1))
        except (TypeError, ValueError):
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    @staticmethod
    def _same_entity(query: str, name: str) -> bool:
        left = "".join(str(query or "").split()).casefold()
        right = "".join(str(name or "").split()).casefold()
        return bool(left and right and left == right)

    def validate_selected(
        self, bound: BoundTimelineMutation, candidate: MapPoiResponse, *, city: str
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        """Recheck a persisted material-choice candidate before the writer runs."""
        query = str(bound.intent.replacement.poi_query or "").strip()
        mode = bound.intent.replacement.poi_query_mode or "category_intent"
        expected = str(bound.intent.selector.intent_type or "")
        semantic = self.semantic_policy.evaluate(expected, candidate, raw_need=query)
        anchors = list(bound.adjacent_descriptors)
        previous = self._anchor(anchors[0]) if anchors else None
        following = self._anchor(anchors[-1]) if len(anchors) > 1 else None
        score = self.insertion_scorer.score(previous, candidate, following, transport_mode="transit")
        decision = {
            "amapPoiId": candidate.id,
            "semantic": semantic.to_camel_dict(),
            "insertion": self._score_dict(score),
            "queryMode": mode,
        }
        if not self._trusted(candidate, city):
            return False, [decision], "candidate_identity_or_city_invalid"
        if mode == "exact_entity" and not self._same_entity(query, candidate.name):
            decision["identity"] = {"passed": False, "reason": "exact_entity_identity_mismatch"}
            return False, [decision], "exact_entity_identity_mismatch"
        if not semantic.passed:
            return False, [decision], "no_adjacent_route_feasible_candidate"
        if not anchors:
            decision["routeVerification"] = {"status": "not_required", "legs": []}
            return True, [decision], None
        if score is None:
            return False, [decision], "no_adjacent_route_feasible_candidate"
        route_evidence, failure = self._verify_route_impact(bound, anchors, candidate)
        decision["routeVerification"] = route_evidence
        return failure is None, [decision], failure

    @staticmethod
    def _anchor(value: Any) -> Any:
        return SimpleNamespace(longitude=value.poi_longitude, latitude=value.poi_latitude)

    @staticmethod
    def _matrix_anchor(value: Any) -> dict[str, Any]:
        return {
            "segmentId": str(value.segment_id),
            "dayId": str(value.day_id),
            "amapId": str(value.poi_amap_id or ""),
            "name": str(value.poi_name or value.poi_amap_id or ""),
            "city": "",
            "type": "route_anchor",
            "source": "amap-place-search",
            "longitude": value.poi_longitude,
            "latitude": value.poi_latitude,
            "startTime": str(value.start_time or ""),
            "endTime": str(value.end_time or ""),
        }

    @staticmethod
    def _centers(anchors: list[Any]) -> list[tuple[float, float]]:
        coords = [
            (float(item.poi_longitude), float(item.poi_latitude))
            for item in anchors
            if item.poi_longitude is not None and item.poi_latitude is not None
        ]
        if len(coords) >= 2:
            first, last = coords[0], coords[-1]
            return [((first[0] + last[0]) / 2, (first[1] + last[1]) / 2), first, last]
        return coords

    @staticmethod
    def _trusted(candidate: MapPoiResponse, city: str) -> bool:
        return bool(
            candidate.id
            and candidate.source == AMAP_PLACE_SOURCE
            and candidate.longitude is not None
            and candidate.latitude is not None
            and candidate.city.removesuffix("市") == city.removesuffix("市")
        )

    @staticmethod
    def _category(intent_type: str | None) -> str:
        return {"museum": "museum", "meal": "food", "campus_visit": "campus", "park": "scenic"}.get(
            str(intent_type or ""), "all"
        )

    def _added_distance(self, previous: Any, candidate: Any, following: Any) -> float:
        score = self.insertion_scorer.score(previous, candidate, following, transport_mode="transit")
        return score.added_distance_km if score is not None else float("inf")

    @staticmethod
    def _score_dict(score: Any) -> dict[str, Any] | None:
        if score is None:
            return None
        return {
            "addedDistanceKm": score.added_distance_km,
            "addedDurationMinutes": score.added_duration_minutes,
            "detourLevel": score.detour_level,
            "reason": score.reason,
        }
