from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from src.api.schemas.maps import MapPoiResponse
from src.services.adjacent_insertion_candidate_service import AdjacentInsertionCandidateService
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.timeline_mutation_models import BoundTimelineMutation, TimelineMutationResolution


class TimelineMutationPoiResolver:
    def __init__(
        self,
        map_poi_service: Optional[MapPoiService] = None,
        semantic_policy: Optional[IntentCandidateSemanticPolicy] = None,
        adjacent_insertion_service: Optional[AdjacentInsertionCandidateService] = None,
    ):
        self.map_poi_service = map_poi_service or MapPoiService()
        self.semantic_policy = semantic_policy or IntentCandidateSemanticPolicy()
        self.adjacent_insertion_service = adjacent_insertion_service or AdjacentInsertionCandidateService(
            self.map_poi_service
        )

    def resolve(self, bound: BoundTimelineMutation, *, city: str) -> TimelineMutationResolution:
        if bound.intent.operation not in {"add_segment", "replace_poi"}:
            return TimelineMutationResolution(status="not_required")
        query = str(bound.intent.replacement.poi_query or "").strip()
        if bound.intent.operation == "add_segment":
            candidates, evidence, failure = self.adjacent_insertion_service.resolve(bound, city=city)
            if failure:
                status = (
                    "provider_failure"
                    if failure.startswith(("provider_failure:", "route_provider_"))
                    else "no_safe_candidate"
                )
                return TimelineMutationResolution(status=status, semanticDecisions=evidence, failureReason=failure)
            if len(candidates) == 1:
                return TimelineMutationResolution(
                    status="unique_safe_candidate",
                    selectedPoi=candidates[0],
                    candidates=candidates,
                    semanticDecisions=evidence,
                )
            return TimelineMutationResolution(
                status="material_choice", candidates=candidates, semanticDecisions=evidence
            )
        try:
            response = self.map_poi_service.search(
                city=city,
                keyword=query,
                category=(
                    self._category(bound.intent.selector.intent_type)
                    if bound.intent.operation == "add_segment"
                    else "all"
                ),
                limit=8,
            )
        except (HTTPException, RuntimeError, OSError) as error:
            return TimelineMutationResolution(status="provider_failure", failureReason=self._error_text(error))

        safe = []
        decisions = []
        route_failure: str | None = None
        expected_intent = self._expected_intent(bound)
        for candidate in response.pois:
            decision = self.semantic_policy.evaluate(expected_intent, candidate, raw_need=query)
            decision_payload = {"amapPoiId": candidate.id, **decision.to_camel_dict()}
            decisions.append(decision_payload)
            trusted = bool(
                candidate.id
                and candidate.source == AMAP_PLACE_SOURCE
                and candidate.longitude is not None
                and candidate.latitude is not None
                and self._city_matches(city, candidate.city)
            )
            if trusted and decision.passed:
                accepted, route_decisions, failure = self.adjacent_insertion_service.validate_selected(
                    bound,
                    candidate,
                    city=city,
                )
                if route_decisions:
                    decision_payload["routeVerification"] = route_decisions[0].get("routeVerification")
                    decision_payload["insertion"] = route_decisions[0].get("insertion")
                if accepted:
                    safe.append(candidate)
                elif str(failure or "").startswith("route_provider_"):
                    route_failure = str(failure)
        if not safe:
            if route_failure:
                return TimelineMutationResolution(
                    status="provider_failure",
                    candidates=response.pois,
                    semanticDecisions=decisions,
                    failureReason=route_failure,
                )
            return TimelineMutationResolution(
                status="no_safe_candidate", candidates=response.pois, semanticDecisions=decisions
            )
        safe.sort(key=lambda item: float(item.confidence or 0), reverse=True)
        if len(safe) == 1 or self._dominant(safe[0], safe[1], query):
            return TimelineMutationResolution(
                status="unique_safe_candidate",
                selectedPoi=safe[0],
                candidates=safe,
                semanticDecisions=decisions,
            )
        return TimelineMutationResolution(status="material_choice", candidates=safe, semanticDecisions=decisions)

    def resolve_selected(
        self,
        bound: BoundTimelineMutation,
        candidate: MapPoiResponse,
        *,
        city: str,
    ) -> TimelineMutationResolution:
        if bound.intent.operation in {"add_segment", "replace_poi"}:
            accepted, decisions, failure = self.adjacent_insertion_service.validate_selected(
                bound, candidate, city=city
            )
            if not accepted:
                status = (
                    "provider_failure"
                    if str(failure or "").startswith(("provider_failure:", "route_provider_"))
                    else "no_safe_candidate"
                )
                return TimelineMutationResolution(
                    status=status,
                    candidates=[candidate],
                    semanticDecisions=decisions,
                    failureReason=failure,
                )
            return TimelineMutationResolution(
                status="unique_safe_candidate",
                selectedPoi=candidate,
                candidates=[candidate],
                semanticDecisions=decisions,
            )
        expected_intent = self._expected_intent(bound)
        query = str(bound.intent.replacement.poi_query or "").strip()
        decision = self.semantic_policy.evaluate(expected_intent, candidate, raw_need=query)
        trusted = bool(
            candidate.id
            and candidate.source == AMAP_PLACE_SOURCE
            and candidate.longitude is not None
            and candidate.latitude is not None
            and self._city_matches(city, candidate.city)
        )
        if not trusted or not decision.passed:
            return TimelineMutationResolution(
                status="no_safe_candidate",
                candidates=[candidate],
                semanticDecisions=[{"amapPoiId": candidate.id, **decision.to_camel_dict()}],
                failureReason="structured candidate no longer satisfies identity, city, coordinates, or semantic policy",
            )
        return TimelineMutationResolution(
            status="unique_safe_candidate",
            selectedPoi=candidate,
            candidates=[candidate],
            semanticDecisions=[{"amapPoiId": candidate.id, **decision.to_camel_dict()}],
        )

    @staticmethod
    def _dominant(first, second, query: str) -> bool:
        query_text = "".join(str(query or "").split()).casefold()
        first_name = "".join(str(first.name or "").split()).casefold()
        second_name = "".join(str(second.name or "").split()).casefold()
        first_exact = query_text and (query_text in first_name or first_name in query_text)
        second_exact = query_text and (query_text in second_name or second_name in query_text)
        return bool(first_exact and not second_exact and float(first.confidence or 0) >= float(second.confidence or 0))

    @staticmethod
    def _city_matches(expected: str, actual: str) -> bool:
        left = str(expected or "").removesuffix("市")
        right = str(actual or "").removesuffix("市")
        return bool(left and right and left == right)

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, HTTPException):
            return str(error.detail)
        return str(error)

    @staticmethod
    def _expected_intent(bound: BoundTimelineMutation) -> str:
        descriptor_intent = bound.target_descriptors[0].intent_type if bound.target_descriptors else None
        return str(descriptor_intent or bound.intent.selector.intent_type or "")

    @staticmethod
    def _category(intent_type: Optional[str]) -> str:
        return {
            "campus_visit": "campus",
            "museum": "museum",
            "meal": "food",
            "park": "scenic",
            "night_view": "scenic",
        }.get(str(intent_type or ""), "all")
