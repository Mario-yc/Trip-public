import inspect

import pytest

from src.services.agent_service import AgentService
from src.services.plan_proposal_commit_service import PlanProposalCommitService


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [
        (
            {
                "visibilityMode": "skeleton_preview_only",
                "adoptionReady": False,
                "hardPendingSlotCount": 2,
            },
            False,
        ),
        (
            {
                "visibilityMode": "neutral_skeleton",
                "adoptionReady": False,
                "hardPendingSlotCount": 0,
            },
            False,
        ),
        (
            {
                "visibilityMode": "neutral_skeleton",
                "adoptionReady": True,
                "hardPendingSlotCount": 1,
            },
            False,
        ),
        (
            {
                "visibilityMode": "neutral_skeleton",
                "adoptionReady": True,
                "draftAdoptionReady": True,
                "hardPendingSlotCount": 0,
            },
            False,
        ),
    ],
)
def test_unadopted_partial_timeline_writer_is_always_disabled(readiness, expected):
    assert AgentService._partial_timeline_write_eligible(readiness) is expected


def test_partial_adoption_has_only_the_plan_proposal_commit_single_writer_entrypoint():
    source = inspect.getsource(AgentService._execute_selected_plan_proposal)

    assert f"{PlanProposalCommitService.__name__}(store).commit" in source
    assert "_persist_portfolio_partial_timeline" not in source


def test_editable_partial_counts_as_successful_comparison_delta():
    assert (
        AgentService._comparison_delta_eligible(
            status="offered",
            score={"hardConstraintPassed": True},
            verifier={
                "passed": False,
                "draftPassed": True,
                "pendingHardSlotCount": 0,
            },
            readiness={
                "adoptionReady": True,
                "adoptionMode": "editable_partial",
                "hardPendingSlotCount": 0,
            },
        )
        is True
    )


def test_hard_pending_partial_does_not_count_as_successful_comparison_delta():
    assert (
        AgentService._comparison_delta_eligible(
            status="offered",
            score={"hardConstraintPassed": True},
            verifier={"passed": False, "draftPassed": True},
            readiness={
                "adoptionReady": False,
                "adoptionMode": "blocked",
                "hardPendingSlotCount": 1,
            },
        )
        is False
    )
