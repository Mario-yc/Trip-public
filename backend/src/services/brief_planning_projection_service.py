"""Pure projection of a strict CreativeBrief into bounded planning constraints."""
from __future__ import annotations

from dataclasses import dataclass

from src.services.creative_planning_models import ConstraintLedger
from src.services.creative_portfolio_provider_service import PortfolioSkeleton


@dataclass(frozen=True)
class BriefPlanningProjection:
    brief_id: str
    primary_axis: str
    day_role_signature: tuple[str, ...]
    required_goal_ids: tuple[str, ...]
    optional_families: tuple[str, ...]
    optional_pool_ids: tuple[str, ...]
    optional_slot_ids: tuple[str, ...]
    explicit_soft_goal_slot_ids: tuple[str, ...]
    brief_optional_slot_ids: tuple[str, ...]
    preferred_day_numbers: tuple[int, ...]
    day_anchor_targets: tuple[tuple[int, int], ...]
    density_evidence: tuple[tuple[int, tuple[str, ...]], ...]
    transport_preference: str
    density_decision_source: str
    day_evidence: tuple[tuple[int, int, int, int, str], ...]
    pace: str
    max_route_anchors_per_day: tuple[tuple[int, int], ...]
    max_optional_segments: int
    avoid_experience_types: tuple[str, ...]

    def as_lineage(self) -> dict[str, object]:
        return {
            "briefId": self.brief_id,
            "primaryAxis": self.primary_axis,
            "dayRoleSignature": list(self.day_role_signature),
            "requiredGoalIds": list(self.required_goal_ids),
            "optionalFamilies": list(self.optional_families),
            "optionalPoolIds": list(self.optional_pool_ids),
            "optionalSlotIds": list(self.optional_slot_ids),
            "explicitSoftGoalSlotIds": list(self.explicit_soft_goal_slot_ids),
            "briefOptionalSlotIds": list(self.brief_optional_slot_ids),
            "dayAnchorTargets": {str(day): target for day, target in self.day_anchor_targets},
            "densityEvidence": {str(day): list(evidence) for day, evidence in self.density_evidence},
            "transportPreference": self.transport_preference,
            "densityDecisionSource": self.density_decision_source,
            "dayEvidence": {
                str(day): {
                    "requiredGoalCount": required_count,
                    "explicitSoftGoalCount": soft_count,
                    "briefOptionalCount": optional_count,
                    "availableWindow": available_window,
                }
                for day, required_count, soft_count, optional_count, available_window in self.day_evidence
            },
            "pace": self.pace,
            "maxRouteAnchorsPerDay": {
                str(day): maximum for day, maximum in self.max_route_anchors_per_day
            },
            "maxOptionalSegments": self.max_optional_segments,
        }


class BriefPlanningProjectionService:
    MAX_OPTIONAL_SEGMENTS = 2

    def project(
        self,
        skeleton: PortfolioSkeleton,
        ledger: ConstraintLedger,
        *,
        max_route_anchors_by_day: dict[int, int] | None = None,
        density_decision_source: str = "creative_portfolio_provider",
    ) -> BriefPlanningProjection:
        optional_families = tuple(
            item.family for item in skeleton.brief.optional_experiences
            if item.family not in set(ledger.forbidden_experience_types)
        )[: self.MAX_OPTIONAL_SEGMENTS]
        optional_slot_ids = {
            slot.slot_id for slot in skeleton.day_slots
            if slot.optional_experience_family in optional_families
        }
        optional_pools = [
            pool for pool in skeleton.intent_pools
            if pool.requirement_level == "optional"
            and pool.soft_goal_id is None
            and pool.optional_experience_family in optional_families
            and bool(pool.assign_to_slots)
            and set(pool.assign_to_slots) <= optional_slot_ids
        ]
        preferred_days = tuple(sorted({role.day_number for role in skeleton.brief.day_roles}))
        if preferred_days != tuple(range(1, ledger.day_count + 1)):
            raise ValueError("brief_projection_day_role_coverage_invalid")
        day_anchor_targets: list[tuple[int, int]] = []
        density_evidence: list[tuple[int, tuple[str, ...]]] = []
        day_evidence: list[tuple[int, int, int, int, str]] = []
        maximums: list[tuple[int, int]] = []
        for role in sorted(skeleton.brief.day_roles, key=lambda item: item.day_number):
            if role.target_route_anchors is None or not role.density_evidence:
                raise ValueError("brief_projection_day_anchor_target_missing")
            maximum = int((max_route_anchors_by_day or {}).get(role.day_number, 6))
            if role.target_route_anchors > maximum:
                raise ValueError("brief_projection_day_anchor_target_exceeds_limit")
            actual_slots = sum(
                1 for slot in skeleton.day_slots
                if slot.day_number == role.day_number and slot.route_anchor
            )
            if actual_slots != role.target_route_anchors:
                raise ValueError("brief_projection_day_anchor_target_slot_mismatch")
            day_anchor_targets.append((role.day_number, role.target_route_anchors))
            density_evidence.append((role.day_number, tuple(role.density_evidence)))
            required_count = sum(
                1
                for slot in skeleton.day_slots
                if slot.day_number == role.day_number and slot.required_goal_id is not None
            )
            soft_count = sum(
                1
                for slot in skeleton.day_slots
                if slot.day_number == role.day_number and slot.soft_goal_id is not None
            )
            optional_count = sum(
                1
                for slot in skeleton.day_slots
                if slot.day_number == role.day_number
                and slot.slot_id in optional_slot_ids
                and slot.optional_experience_family in optional_families
            )
            day_evidence.append((
                role.day_number,
                required_count,
                soft_count,
                optional_count,
                self._evidence_value(role.density_evidence, "availableWindow") or "unspecified",
            ))
            maximums.append((role.day_number, maximum))
        explicit_soft_slots = tuple(sorted(
            slot.slot_id for slot in skeleton.day_slots if slot.soft_goal_id is not None
        ))
        return BriefPlanningProjection(
            brief_id=skeleton.brief.brief_id,
            primary_axis=skeleton.brief.primary_axis,
            day_role_signature=tuple(f"{role.day_number}:{role.role}" for role in skeleton.brief.day_roles),
            required_goal_ids=tuple(sorted(goal.goal_id for goal in ledger.hard_goals)),
            optional_families=optional_families,
            optional_pool_ids=tuple(pool.pool_id for pool in optional_pools),
            optional_slot_ids=tuple(sorted(optional_slot_ids)),
            explicit_soft_goal_slot_ids=explicit_soft_slots,
            brief_optional_slot_ids=tuple(sorted(optional_slot_ids)),
            preferred_day_numbers=preferred_days,
            day_anchor_targets=tuple(day_anchor_targets),
            density_evidence=tuple(density_evidence),
            transport_preference=(ledger.transport_preferences or ["unspecified"])[0],
            density_decision_source=density_decision_source,
            day_evidence=tuple(day_evidence),
            pace=ledger.pace,
            max_route_anchors_per_day=tuple(maximums),
            max_optional_segments=min(self.MAX_OPTIONAL_SEGMENTS, len(optional_slot_ids)),
            avoid_experience_types=tuple(sorted(set(skeleton.brief.avoid_experience_types) | set(ledger.forbidden_experience_types))),
        )

    @staticmethod
    def _evidence_value(values: list[str], key: str) -> str:
        prefix = f"{key}="
        return next(
            (str(value)[len(prefix):] for value in values if str(value).startswith(prefix)),
            "",
        )
