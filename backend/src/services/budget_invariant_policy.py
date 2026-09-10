from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedBudgetRange:
    known_total: float
    provisional_min: float
    provisional_preferred: float
    provisional_max: float

    @property
    def invariant_valid(self) -> bool:
        return (
            0 <= self.known_total
            <= self.provisional_min
            <= self.provisional_preferred
            <= self.provisional_max
        )


class BudgetInvariantPolicy:
    """Keeps every user-visible estimate above already-counted costs."""

    @staticmethod
    def normalize(
        *,
        known_total: float,
        provisional_min: float,
        provisional_preferred: float,
        provisional_max: float,
    ) -> NormalizedBudgetRange:
        known = max(0.0, float(known_total or 0))
        minimum = max(known, float(provisional_min or 0))
        preferred = max(minimum, float(provisional_preferred or 0))
        maximum = max(preferred, float(provisional_max or 0))
        return NormalizedBudgetRange(
            known_total=round(known, 2),
            provisional_min=round(minimum, 2),
            provisional_preferred=round(preferred, 2),
            provisional_max=round(maximum, 2),
        )

    @staticmethod
    def is_valid(
        *,
        known_total: float,
        provisional_min: float,
        provisional_preferred: float,
        provisional_max: float,
    ) -> bool:
        values = [
            float(known_total or 0),
            float(provisional_min or 0),
            float(provisional_preferred or 0),
            float(provisional_max or 0),
        ]
        return 0 <= values[0] <= values[1] <= values[2] <= values[3]
