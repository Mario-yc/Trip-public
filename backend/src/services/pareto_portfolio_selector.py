"""Deterministic filtering and diversification for proposal-only candidates."""

from __future__ import annotations

from itertools import combinations

from src.services.creative_planning_models import PlanCandidate
from src.services.plan_comparison_preview_service import PlanComparisonPreviewService
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


class ParetoPortfolioSelector:
    EPSILON = 5.0

    def select(self, candidates: list[PlanCandidate]) -> list[PlanCandidate]:
        unique: dict[str, PlanCandidate] = {}
        for candidate in candidates:
            if not candidate.score.hard_constraint_passed:
                continue
            existing = unique.get(candidate.canonical_signature)
            if existing is None or self._sort_key(candidate) < self._sort_key(existing):
                unique[candidate.canonical_signature] = candidate
        frontier = [
            candidate
            for candidate in unique.values()
            if not any(self._dominates(other, candidate) for other in unique.values() if other is not candidate)
        ]
        result: list[PlanCandidate] = []
        for candidate in sorted(frontier, key=self._sort_key):
            if all(
                self.distance(candidate, selected) >= 0.25 and self._physical_novelty_passed(candidate, selected)
                for selected in result
            ):
                result.append(candidate)
        # Selection is a quality/diversity boundary, not the product's
        # portfolio-size budget.  The caller/frontier applies the configured
        # visible/generated/round limits after this bounded candidate batch.
        return result

    def distance(self, left: PlanCandidate, right: PlanCandidate) -> float:
        """Compatibility entry point for structural proposal distance.

        A creative axis or a label is a declaration, not evidence that two plans
        differ.  Portfolio diversity must therefore be calculated only from the
        grounded itinerary structure that a user would actually experience.
        """
        return self.structural_distance_without_axis(left, right)

    def structural_distance_without_axis(self, left: PlanCandidate, right: PlanCandidate) -> float:
        left_pois = self._poi_names(left)
        right_pois = self._poi_names(right)
        union = left_pois | right_pois
        poi_distance = 1.0 if not union else 1.0 - len(left_pois & right_pois) / len(union)
        left_families = self._experience_families(left)
        right_families = self._experience_families(right)
        family_union = left_families | right_families
        family_distance = 1.0 if not family_union else 1.0 - len(left_families & right_families) / len(family_union)
        left_sequence = self._day_role_sequence(left)
        right_sequence = self._day_role_sequence(right)
        sequence_union = left_sequence | right_sequence
        sequence_distance = (
            1.0 if not sequence_union else 1.0 - len(left_sequence & right_sequence) / len(sequence_union)
        )
        return round(0.5 * poi_distance + 0.3 * family_distance + 0.2 * sequence_distance, 4)

    def min_pairwise_distance(self, candidates: list[PlanCandidate]) -> float:
        return min((self.distance(a, b) for a, b in combinations(candidates, 2)), default=1.0)

    @staticmethod
    def _physical_novelty_passed(candidate: PlanCandidate, selected: PlanCandidate) -> bool:
        candidate_projection = {
            "proposalId": candidate.proposal_id,
            "days": candidate.itinerary_snapshot.get("days") or [],
            "portfolioPendingSlots": candidate.itinerary_snapshot.get("portfolioPendingSlots") or [],
        }
        selected_projection = {
            "proposalId": selected.proposal_id,
            "days": selected.itinerary_snapshot.get("days") or [],
            "portfolioPendingSlots": selected.itinerary_snapshot.get("portfolioPendingSlots") or [],
        }
        if (candidate.verifier.get("draftPassed") and not candidate.verifier.get("passed")) or (
            selected.verifier.get("draftPassed") and not selected.verifier.get("passed")
        ):
            return bool(
                PlanComparisonPreviewService.concept_novelty_audit(
                    candidate_projection,
                    [selected_projection],
                ).get("passed")
            )
        candidate_ids = PlanComparisonPreviewService.physical_poi_ids(candidate_projection)
        selected_ids = PlanComparisonPreviewService.physical_poi_ids(selected_projection)
        if not candidate_ids or not selected_ids:
            # Legacy/non-map fixtures still use the established structural gate.
            # Verified production proposals contain canonical AMap identities.
            return True
        return bool(
            PlanComparisonPreviewService.material_novelty_audit(
                candidate_projection,
                [selected_projection],
            ).get("passed")
        )

    def _dominates(self, left: PlanCandidate, right: PlanCandidate) -> bool:
        l, r = left.score, right.score
        high = (
            "preference_fit",
            "thematic_coherence",
            "experience_diversity",
            "route_efficiency",
            "pacing_quality",
            "novelty",
            "robustness",
        )
        no_worse = all(getattr(l, key) + self.EPSILON >= getattr(r, key) for key in high)
        no_worse = no_worse and l.uncertainty_penalty <= r.uncertainty_penalty + self.EPSILON
        strictly_better = (
            any(getattr(l, key) > getattr(r, key) + self.EPSILON for key in high)
            or l.uncertainty_penalty + self.EPSILON < r.uncertainty_penalty
        )
        return no_worse and strictly_better

    @staticmethod
    def _poi_names(candidate: PlanCandidate) -> set[str]:
        return {
            PoiPhysicalIdentityService.canonical_amap_id(item) or str(item.get("name") or "")
            for item in candidate.grounded_evidence
            if PoiPhysicalIdentityService.canonical_amap_id(item) or item.get("name")
        }

    @staticmethod
    def _experience_families(candidate: PlanCandidate) -> set[str]:
        families = {str(item.get("family")) for item in candidate.grounded_evidence if item.get("family")}
        for day in candidate.itinerary_snapshot.get("days", []):
            for segment in day.get("segments", []):
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                family = (
                    semantic.get("optionalExperienceFamily")
                    or semantic.get("experienceFamily")
                    or segment.get("optionalExperienceFamily")
                    or segment.get("experienceFamily")
                    or segment.get("family")
                )
                if family:
                    families.add(str(family))
        return families

    @staticmethod
    def _day_role_sequence(candidate: PlanCandidate) -> set[tuple[int, str, str]]:
        sequence: set[tuple[int, str, str]] = set()
        for day in candidate.itinerary_snapshot.get("days", []):
            day_number = int(day.get("dayNumber") or day.get("day") or 0)
            for segment in day.get("segments", []):
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                family = (
                    semantic.get("optionalExperienceFamily")
                    or semantic.get("experienceFamily")
                    or segment.get("optionalExperienceFamily")
                    or segment.get("experienceFamily")
                    or segment.get("family")
                    or ""
                )
                kind = semantic.get("intentType") or segment.get("kind") or segment.get("type") or ""
                sequence.add((day_number, str(kind), str(family)))
        for day_number, target in (candidate.itinerary_snapshot.get("portfolioDayAnchorTargets") or {}).items():
            sequence.add((int(day_number), "__density__", str(target)))
        return sequence

    @staticmethod
    def _sort_key(candidate: PlanCandidate) -> tuple[float, float, float, str]:
        score = candidate.score
        return (
            -score.route_efficiency,
            score.uncertainty_penalty,
            -score.preference_fit,
            candidate.canonical_signature,
        )
