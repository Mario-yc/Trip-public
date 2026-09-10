import pytest
from pydantic import ValidationError

from src.api.schemas.itineraries import BudgetBreakdownResponse
from src.services.budget_invariant_policy import BudgetInvariantPolicy


def test_budget_policy_floors_provisional_values_at_known_total():
    normalized = BudgetInvariantPolicy.normalize(
        known_total=411,
        provisional_min=261,
        provisional_preferred=341,
        provisional_max=421,
    )

    assert normalized.known_total == 411
    assert normalized.provisional_min == 411
    assert normalized.provisional_preferred == 411
    assert normalized.provisional_max == 421
    assert normalized.invariant_valid is True


def test_budget_schema_rejects_inverted_range():
    with pytest.raises(ValidationError, match="budget invariant"):
        BudgetBreakdownResponse(
            knownTotal=411,
            provisionalMin=261,
            provisionalPreferred=341,
            provisionalMax=421,
        )
