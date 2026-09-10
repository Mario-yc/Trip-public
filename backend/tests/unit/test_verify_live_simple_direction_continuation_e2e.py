from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_live_simple_direction_e2e.py"
SPEC = importlib.util.spec_from_file_location("verify_live_simple_direction_continuation_e2e", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _choice_id(source_assistant_turn_id: str) -> str:
    return (
        "simple_direction_continue_"
        + hashlib.sha256(f"portfolio_1:{source_assistant_turn_id}".encode("utf-8")).hexdigest()[:24]
    )


CURRENT_CHOICE_ID = _choice_id("assistant_offer")
NEXT_CHOICE_ID = _choice_id("assistant_result")


def _browser_evidence(
    *,
    post_frontier_status: str = "has_more",
    post_capability_available: bool = True,
    post_choice_id: str = NEXT_CHOICE_ID,
    post_stop_reason: str = "",
) -> list[dict]:
    return [
        {
            "phase": "direction_b",
            "choiceId": CURRENT_CHOICE_ID,
            "lifecycle": "offered",
            "sourceAssistantTurnId": "assistant_offer",
            "sourceUserTurnId": "root_user",
            "planningSelectionRootTurnId": "root_user",
            "rootPortfolioId": "portfolio_1",
            "requestContractFingerprint": "a" * 64,
            "workflowMode": "simple_direction_v1",
            "streamRequestDelta": 1,
            "activeVersionBefore": "version_a",
            "preFrontierStatus": "has_more",
            "postSourceAssistantTurnId": "assistant_result",
            "postFrontierStatus": post_frontier_status,
            "postCapabilityAvailable": post_capability_available,
            "postChoiceId": post_choice_id,
            "postRequestContractFingerprint": ("a" * 64 if post_capability_available else ""),
            "postStopReason": post_stop_reason,
            "activeVersionAfter": "version_a",
        }
    ]


def _execution(
    *,
    proposal_delta: int = 1,
    frontier_status: str = "has_more",
    attempt_kind: str = "compatibility",
    flattened_outcome: bool = False,
    legacy_flattened_outcome: bool = False,
) -> dict:
    next_choice_id = NEXT_CHOICE_ID if frontier_status == "has_more" else None
    completion = {
        "passed": True,
        "reason": "simple_direction_execution_evidence_verified",
        "sessionId": "session_1",
        "requestTurnId": "user_request",
        "assistantTurnId": "assistant_result",
        "sourceAssistantTurnId": "assistant_offer",
        "choiceId": CURRENT_CHOICE_ID,
        "planningSelectionRootTurnId": "root_user",
        "rootPortfolioId": "portfolio_1",
        "requestContractFingerprint": "a" * 64,
        "frontierExecutionId": "execution_1",
        "frontierAttemptFingerprint": "b" * 64,
        "attemptKind": attempt_kind,
        "frontierStatus": frontier_status,
        "proposalDelta": proposal_delta,
        "nextChoiceId": next_choice_id,
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
        "zeroWrite": True,
    }
    outcome = (
        completion
        if flattened_outcome
        else {
            "reason": (
                "new_simple_direction_proposal"
                if proposal_delta > 0
                else "simple_direction_frontier_advanced_without_proposal"
            ),
            "frontierStatus": frontier_status,
            "proposalDelta": proposal_delta,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "zeroWrite": True,
            "boundedAttemptConsumed": True,
            "completionEvidence": completion,
        }
    )
    if legacy_flattened_outcome:
        outcome = {
            **completion,
            "reason": (
                "new_simple_direction_proposal"
                if proposal_delta > 0
                else "simple_direction_frontier_advanced_without_proposal"
            ),
            "reconciledFrom": "failed_retryable/no_material_progress",
            "boundedAttemptConsumed": True,
        }
    return {
        "id": "execution_1",
        "session_id": "session_1",
        "source_turn_id": "assistant_offer",
        "source_user_turn_id": "root_user",
        "choice_id": CURRENT_CHOICE_ID,
        "action": "continue_plan_expansion",
        "status": "succeeded",
        "result_version_id": None,
        "execution_turn_id": "assistant_result",
        "request_turn_id": "user_request",
        "outcome_json": json.dumps(outcome),
    }


def _turns(*, frontier_status: str = "has_more", proposal_delta: int | None = None) -> dict[str, dict]:
    choices = []
    if frontier_status == "has_more":
        choices.append(
            {
                "id": NEXT_CHOICE_ID,
                "choiceId": NEXT_CHOICE_ID,
                "kind": "simple_direction_more_plans",
                "action": "continue_plan_expansion",
                "scopeKind": "comparison",
                "sourceAssistantTurnId": "assistant_result",
                "sourceUserTurnId": "root_user",
                "planningSelectionRootTurnId": "root_user",
                "rootPortfolioId": "portfolio_1",
                "requestContractFingerprint": "a" * 64,
                "expectedBaseVersionId": "version_a",
                "workflowMode": "simple_direction_v1",
            }
        )
    return {
        "assistant_offer": {
            "id": "assistant_offer",
            "role": "assistant",
            "response": {
                "choiceOptions": [
                    {
                        "id": CURRENT_CHOICE_ID,
                        "choiceId": CURRENT_CHOICE_ID,
                        "kind": "simple_direction_more_plans",
                        "action": "continue_plan_expansion",
                        "scopeKind": "comparison",
                        "sourceAssistantTurnId": "assistant_offer",
                        "sourceUserTurnId": "root_user",
                        "planningSelectionRootTurnId": "root_user",
                        "rootPortfolioId": "portfolio_1",
                        "requestContractFingerprint": "a" * 64,
                        "expectedBaseVersionId": "version_a",
                        "workflowMode": "simple_direction_v1",
                    }
                ]
            },
        },
        "user_request": {"id": "user_request", "role": "user", "response": {}},
        "assistant_result": {
            "id": "assistant_result",
            "role": "assistant",
            "response": {
                "mode": "simple_open_direction_proposal",
                "workflowMode": "simple_direction_v1",
                "planningSelectionRootTurnId": "root_user",
                "rootPortfolioId": "portfolio_1",
                "frontierExecutionId": "execution_1",
                "frontierStatus": frontier_status,
                "frontierAttemptConsumed": True,
                "proposalDelta": (
                    proposal_delta if proposal_delta is not None else (1 if frontier_status == "has_more" else 0)
                ),
                "versionDelta": 0,
                "patchDelta": 0,
                "routeWriteDelta": 0,
                "viewResolution": {
                    "schemaVersion": "agent-view-resolution-v1",
                    "resolutionSource": "server_validated_opaque_choice",
                    "resolvedAction": "generate_new_direction",
                    "planningSelectionRootTurnId": "root_user",
                    "rootPortfolioId": "portfolio_1",
                },
                "choiceOptions": choices,
            },
        },
    }


def _summary(*, status: str = "reconciled", made_progress: bool = True) -> dict:
    frontier_status = "has_more" if made_progress else "poi_exhausted"
    return {
        "workflowMode": "simple_direction_v1",
        "portfolioId": "portfolio_1",
        "planningSelectionRootTurnId": "root_user",
        "requestContractFingerprint": "a" * 64,
        "simpleDirectionCompatibilityAttempts": {
            "execution_1": {
                "schemaVersion": "simple-direction-compatibility-attempt-v3",
                "executionId": "execution_1",
                "choiceId": CURRENT_CHOICE_ID,
                "sourceAssistantTurnId": "assistant_offer",
                "requestTurnId": "user_request",
                "resultAssistantTurnId": "assistant_result",
                "planningSelectionRootTurnId": "root_user",
                "rootPortfolioId": "portfolio_1",
                "requestContractFingerprint": "a" * 64,
                "attemptFingerprint": "b" * 64,
                "status": status,
                "frontierStatus": frontier_status,
                "proposalDelta": 1 if made_progress else 0,
                "progress": {
                    "madeProgress": made_progress,
                    "queryProgress": made_progress,
                    "candidateProgress": made_progress,
                    "routeProgress": False,
                },
                "reasonCode": (None if made_progress else "no_progress_no_query_candidate_or_route_delta"),
            }
        },
    }


def _frontier_summary() -> dict:
    return {
        "workflowMode": "simple_direction_v1",
        "portfolioId": "portfolio_1",
        "planningSelectionRootTurnId": "root_user",
        "requestContractFingerprint": "a" * 64,
        "simpleDirectionFrontierAttempts": {
            "execution_1": {
                "schemaVersion": "simple-direction-frontier-claim-v1",
                "executionId": "execution_1",
                "requestContractFingerprint": "a" * 64,
                "status": "reconciled",
                "resultFrontierFingerprint": "c" * 64,
                "attempt": {
                    "frontierFingerprint": "d" * 64,
                    "attemptFingerprint": "b" * 64,
                },
            }
        },
    }


def test_artifact_commit_binding_requires_three_exact_matching_sha_values() -> None:
    commit = "c" * 40

    assert (
        verifier.artifact_commit_errors(
            journey={"gitCommit": commit},
            run_summary={"gitCommit": commit},
            expected_git_commit=commit,
        )
        == []
    )
    assert verifier.artifact_commit_errors(
        journey={"gitCommit": "d" * 40},
        run_summary={"gitCommit": commit},
        expected_git_commit=commit,
    ) == ["artifact_git_commit_mismatch", "journey_git_commit_mismatch"]


def test_cli_rejects_commit_mismatch_before_opening_database(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "must-not-be-opened.db"
    journey_path = tmp_path / "journey-result.json"
    run_summary_path = tmp_path / "run-summary.json"
    output_path = tmp_path / "verification.json"
    journey_path.write_text(json.dumps({"gitCommit": "d" * 40}), encoding="utf-8")
    run_summary_path.write_text(json.dumps({"gitCommit": "c" * 40}), encoding="utf-8")

    def fail_if_database_is_opened(*_args, **_kwargs):
        raise AssertionError("verify_database_must_not_run")

    monkeypatch.setattr(verifier, "verify_database", fail_if_database_is_opened)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(VERIFIER_PATH),
            "--database",
            str(database_path),
            "--journey-result",
            str(journey_path),
            "--run-summary",
            str(run_summary_path),
            "--expected-git-commit",
            "c" * 40,
            "--output",
            str(output_path),
        ],
    )

    assert verifier.main() == 1
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["failures"] == [
        "artifact_git_commit_mismatch",
        "journey_git_commit_mismatch",
    ]
    assert not database_path.exists()


def test_continuation_evidence_binds_browser_choice_to_one_zero_write_progress_execution() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution()],
        turns_by_id=_turns(),
        portfolio_summary=_summary(),
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["executionCount"] == 1
    assert evidence["terminalNoProgressCount"] == 0


def test_continuation_evidence_accepts_recovered_flat_completion_outcome() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution(flattened_outcome=True)],
        turns_by_id=_turns(),
        portfolio_summary=_summary(),
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []


def test_continuation_evidence_accepts_legacy_reconciled_flat_outcome() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution(legacy_flattened_outcome=True)],
        turns_by_id=_turns(),
        portfolio_summary=_summary(),
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []


def test_continuation_evidence_rejects_attempt_kind_bucket_mismatch() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution(attempt_kind="frontier")],
        turns_by_id=_turns(),
        portfolio_summary=_summary(),
    )

    assert "continuation_attempt_kind_bucket_mismatch:execution_1" in evidence["errors"]


def test_continuation_evidence_rejects_wrong_next_scope_and_missing_view_binding() -> None:
    turns = _turns()
    turns["assistant_result"]["response"]["choiceOptions"][0]["scopeKind"] = "clarification"
    turns["assistant_result"]["response"]["viewResolution"] = {}

    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution()],
        turns_by_id=turns,
        portfolio_summary=_summary(),
    )

    assert "continuation_next_choice_binding_invalid:execution_1" in evidence["errors"]
    assert "continuation_result_view_resolution_invalid:execution_1" in evidence["errors"]


def test_continuation_evidence_accepts_frontier_cursor_progress_without_proposal() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[_execution(proposal_delta=0, attempt_kind="frontier")],
        turns_by_id=_turns(proposal_delta=0),
        portfolio_summary=_frontier_summary(),
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []


def test_continuation_evidence_accepts_one_terminal_no_progress_without_reissued_choice() -> None:
    evidence = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(
            post_frontier_status="poi_exhausted",
            post_capability_available=False,
            post_choice_id="",
            post_stop_reason="frontier_terminal:poi_exhausted",
        ),
        execution_rows=[_execution(proposal_delta=0, frontier_status="poi_exhausted")],
        turns_by_id=_turns(frontier_status="poi_exhausted"),
        portfolio_summary=_summary(status="no_progress", made_progress=False),
    )

    assert evidence["verified"] is True
    assert evidence["errors"] == []
    assert evidence["terminalNoProgressCount"] == 1


def test_continuation_evidence_rejects_missing_execution_and_false_terminal_reissue() -> None:
    missing = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(),
        execution_rows=[],
        turns_by_id=_turns(),
        portfolio_summary=_summary(),
    )
    assert "continuation_execution_count_mismatch" in missing["errors"]

    false_terminal = verifier.continuation_execution_evidence(
        browser_evidence=_browser_evidence(
            post_frontier_status="poi_exhausted",
            post_capability_available=True,
            post_choice_id=NEXT_CHOICE_ID,
        ),
        execution_rows=[_execution(proposal_delta=0, frontier_status="poi_exhausted")],
        turns_by_id=_turns(frontier_status="poi_exhausted"),
        portfolio_summary=_summary(status="no_progress", made_progress=False),
    )
    assert "continuation_no_progress_choice_reissued:execution_1" in false_terminal["errors"]
