"""Bounded, deterministic critic; it returns defects and never mutates a plan."""
from __future__ import annotations

from dataclasses import dataclass

from src.services.creative_planning_models import ConstraintLedger, PlanCandidate


@dataclass(frozen=True)
class PlanDefect:
    defect_type: str
    severity: str
    evidence: str
    repair_scope: str


class PlanCriticService:
    def review(self, candidate: PlanCandidate, ledger: ConstraintLedger) -> list[PlanDefect]:
        defects: list[PlanDefect] = []
        if not candidate.score.hard_constraint_passed:
            defects.append(PlanDefect("hard_constraint", "high", "proposal_verifier_failed", "reject"))
        if candidate.score.uncertainty_penalty > 50:
            defects.append(PlanDefect("uncertainty_too_high", "medium", "score_uncertainty_penalty_gt_50", "remove_optional"))
        if candidate.score.pacing_quality < 40:
            defects.append(PlanDefect("daily_overload", "medium", "score_pacing_quality_lt_40", "move_or_remove_optional"))
        for day in candidate.itinerary_snapshot.get("days") or []:
            for segment in day.get("segments") or []:
                semantic = segment.get("semanticMetadata") if isinstance(segment, dict) else {}
                if isinstance(semantic, dict) and semantic.get("portfolioOptional") and semantic.get("repairSuggested"):
                    defects.append(PlanDefect("optional_schedule_tune", "low", "optional_repair_suggested", "move_or_remove_optional"))
        required = {item.goal_id for item in ledger.hard_goals}
        if set(candidate.brief.required_goal_ids) != required:
            defects.append(PlanDefect("required_goal_omission", "high", "brief_required_goal_ids_mismatch", "reject"))
        return defects
