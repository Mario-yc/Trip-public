"""Deterministic portfolio scoring with machine-readable calculation evidence."""
from __future__ import annotations

from typing import Any

from src.services.creative_planning_models import ConstraintLedger, CreativeBrief, PlanScoreVector
from src.services.proposal_route_evidence_normalizer import ProposalRouteEvidenceNormalizer


class PlanScoreService:
    _EVIDENCE_FIELDS = (
        "preferenceFit", "thematicCoherence", "experienceDiversity", "routeEfficiency",
        "pacingQuality", "novelty", "robustness", "uncertaintyPenalty", "estimatedCostCny",
    )

    def score(self, *, snapshot: dict[str, Any], ledger: ConstraintLedger, brief: CreativeBrief, verifier: dict[str, Any]) -> PlanScoreVector:
        segments = [segment for day in snapshot.get("days") or [] if isinstance(day, dict) for segment in day.get("segments") or [] if isinstance(segment, dict)]
        families = [str(
            (segment.get("semanticMetadata") or {}).get("optionalExperienceFamily")
            or (segment.get("semanticMetadata") or {}).get("experienceFamily")
            or (segment.get("semanticMetadata") or {}).get("intentType")
            or ""
        ) for segment in segments]
        nonempty_families = [family for family in families if family]
        unique_families = set(nonempty_families)
        admission_components = [
            dict(report.get("scoreComponents") or {})
            for segment in segments
            for report in [
                (segment.get("semanticMetadata") or {}).get("consumerAdmissionReport")
            ]
            if isinstance(report, dict) and isinstance(report.get("scoreComponents"), dict)
        ]
        evidence_strength = self._component_average(admission_components, "evidenceStrength")
        source_freshness = self._component_average(admission_components, "sourceFreshness")
        local_distinctiveness = self._component_average(admission_components, "localDistinctiveness")
        user_intent_fit = self._component_average(admission_components, "userIntentFit")
        admission_uncertainty = self._component_average(admission_components, "uncertaintyPenalty")
        has_admission_evidence = bool(admission_components)
        routes = [
            route
            for route in ProposalRouteEvidenceNormalizer.normalize_snapshot(snapshot)
            if route.get("isSelected")
            and ProposalRouteEvidenceNormalizer.is_verified(route)
        ]
        route_minutes = sum(max(0, int(route.get("durationMinutes") or 0)) for route in routes)
        unresolved = sum(1 for segment in segments if (segment.get("semanticMetadata") or {}).get("groundingStatus") not in {"selected", "confirmed", "grounded", "agent_selected_candidate"})
        anchors = sum(1 for segment in segments if bool((segment.get("semanticMetadata") or {}).get("routeAnchor", segment.get("routeAnchor", False))))
        cost = sum(max(0.0, float(segment.get("estimatedCost") or 0)) for segment in segments)
        budget = self._numeric_budget(ledger)
        axis_matches = sum(1 for family in unique_families if family in {item.family for item in brief.optional_experiences})
        targets = {
            int(day): int(target)
            for day, target in (verifier.get("dayAnchorTargets") or {}).items()
        }
        actuals = {
            int(day): int(actual)
            for day, actual in (verifier.get("dayAnchorActuals") or {}).items()
        }
        target_total = sum(targets.values())
        actual_total = sum(actuals.values())
        target_coverage = 1.0 if not targets else min(1.0, actual_total / max(1, target_total))
        duplicate_count = max(0, len(nonempty_families) - len(unique_families))
        route_efficiency = max(0.0, min(100.0, 100.0 - route_minutes / 3.0 - (30.0 if anchors > 1 and not routes else 0.0)))
        density_gap = sum(abs(actuals.get(day, 0) - target) for day, target in targets.items())
        pacing = max(0.0, min(100.0, 100.0 - density_gap * 35.0 - route_minutes / max(8.0, 12.0 * max(1, len(targets)))))
        thematic = max(0.0, min(100.0, 30.0 + 35.0 * axis_matches + 10.0 * len(brief.day_roles) - 30.0 * sum(1 for family in unique_families if family in set(brief.avoid_experience_types)) + (10.0 * (local_distinctiveness - 0.5) if has_admission_evidence else 0.0)))
        diversity = max(0.0, min(100.0, 35.0 + 25.0 * len(unique_families) - 25.0 * duplicate_count))
        preference = max(0.0, min(100.0, 70.0 + (10.0 if "transit" in ledger.transport_preferences or "公交" in " ".join(ledger.transport_preferences) else 0.0) - (20.0 if budget is not None and cost > budget else 0.0) + (15.0 * (user_intent_fit - 0.5) if has_admission_evidence else 0.0)))
        uncertainty = min(100.0, unresolved * 35.0 + (25.0 if anchors > 1 and not routes else 0.0) + (15.0 if not verifier.get("passed") else 0.0) + (25.0 * admission_uncertainty if has_admission_evidence else 0.0))
        robustness = max(0.0, min(100.0, 100.0 * target_coverage - uncertainty - (15.0 if not verifier.get("passed") else 0.0) + (10.0 * (evidence_strength + source_freshness - 1.0) if has_admission_evidence else 0.0)))
        novelty = max(0.0, min(100.0, 35.0 + 15.0 * len(unique_families)))
        evidence = {
            "densityDecisionSource": [str(snapshot.get("portfolioDensityDecisionSource") or "unspecified")],
            "preferenceFit": [f"transport={ledger.transport_preferences}", f"cost={cost}", f"budget={budget}", f"consumerEvidenceStrength={evidence_strength}", f"userIntentFit={user_intent_fit}"],
            "thematicCoherence": [f"axis={brief.primary_axis}", f"matchedOptionalFamilies={axis_matches}", f"dayRoles={len(brief.day_roles)}", f"localDistinctiveness={local_distinctiveness}"],
            "experienceDiversity": [f"families={sorted(unique_families)}", f"duplicateFamilyCount={duplicate_count}"],
            "routeEfficiency": [f"selectedRouteCount={len(routes)}", f"travelMinutes={route_minutes}"],
            "pacingQuality": [f"dayAnchorTargets={targets}", f"dayAnchorActuals={actuals}", f"pace={ledger.pace}", f"travelMinutes={route_minutes}"],
            "novelty": [f"families={sorted(unique_families)}", f"familyCount={len(unique_families)}"],
            "robustness": [f"verifierPassed={bool(verifier.get('passed'))}", f"targetCoverage={target_coverage}", f"unresolvedCount={unresolved}", f"sourceFreshness={source_freshness}"],
            "uncertaintyPenalty": [f"unresolvedCount={unresolved}", f"routeEvidenceCount={len(routes)}", f"consumerAdmissionUncertainty={admission_uncertainty}"],
            "estimatedCostCny": [f"snapshotSegmentCostTotal={cost}"],
        }
        return PlanScoreVector(
            hardConstraintPassed=bool(verifier.get("passed") or verifier.get("draftPassed")), preferenceFit=preference,
            thematicCoherence=thematic, experienceDiversity=diversity, routeEfficiency=route_efficiency,
            pacingQuality=pacing, novelty=novelty, robustness=robustness,
            uncertaintyPenalty=uncertainty, estimatedCostCny=cost, evidence=evidence,
        )

    @staticmethod
    def _numeric_budget(ledger: ConstraintLedger) -> float | None:
        for key in ("budgetCny", "budget", "totalBudgetCny"):
            value = ledger.preference_snapshot.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
        return None

    @staticmethod
    def _component_average(rows: list[dict[str, Any]], key: str) -> float:
        if not rows:
            return 0.0
        return round(
            sum(max(0.0, min(1.0, float(row.get(key) or 0))) for row in rows)
            / len(rows),
            4,
        )
