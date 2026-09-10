from datetime import datetime, timedelta, timezone

from src.services.portfolio_continuation_checkpoint_service import (
    PortfolioContinuationCheckpointService,
)


def _summary() -> dict:
    return {
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "requestContractFingerprint": "request_fp",
        "visibleProposalIds": ["proposal_1", "proposal_2", "proposal_3"],
        "creativeExplorationFrontier": {
            "frontierState": "has_more",
            "currentFocusBriefId": "brief_3",
            "nextBriefId": "",
            "continuationRound": 2,
            "remainingBudget": 7,
            "attemptedDirectionSignatures": ["sig_a", "sig_b"],
            "acceptedDirectionSignatures": ["sig_a"],
            "rejectedDirectionSignatures": ["sig_b"],
        },
    }


def test_attached_checkpoint_binds_full_resume_identity_and_frontier() -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    options = PortfolioContinuationCheckpointService.attach(
        [
            {
                "id": "continue_4",
                "action": "retry_model_planning",
                "kind": "portfolio_more_plans",
                "planningSelectionRootTurnId": "turn_root",
                "rootPortfolioId": "portfolio_root",
                "focusBriefId": "brief_3",
                "expectedBaseVersionId": None,
                "lifecycle": "offered",
            }
        ],
        session_id="session_1",
        source_assistant_turn_id="assistant_1",
        portfolio_id="portfolio_root",
        portfolio_summary=_summary(),
        default_planning_root_id="turn_root",
        default_request_fingerprint="request_fp",
        default_expected_base_version_id=None,
        default_proposal_id="partial:portfolio_root",
        expires_at=expires_at,
    )

    checkpoint = options[0]["continuationCheckpoint"]
    assert checkpoint["sessionId"] == "session_1"
    assert checkpoint["planningRootId"] == "turn_root"
    assert checkpoint["portfolioId"] == "portfolio_root"
    assert checkpoint["proposalId"] == "partial:portfolio_root"
    assert checkpoint["briefId"] == "brief_3"
    assert checkpoint["sourceAssistantTurnId"] == "assistant_1"
    assert checkpoint["choiceId"] == "continue_4"
    assert checkpoint["requestFingerprint"] == "request_fp"
    assert checkpoint["visibleProposalIds"] == ["proposal_1", "proposal_2", "proposal_3"]
    assert checkpoint["frontierCursor"]["nextBriefId"] == ""
    assert checkpoint["attemptedDirectionSignatures"] == ["sig_a", "sig_b"]
    assert options[0]["checkpointFingerprint"] == checkpoint["checkpointFingerprint"]
    assert "proposalId" not in options[0]


def test_checkpoint_validation_is_fail_closed_and_accepts_exact_identity() -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    option = {
        "id": "continue_4",
        "action": "retry_model_planning",
        "kind": "portfolio_more_plans",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "focusBriefId": "brief_3",
        "expectedBaseVersionId": "version_1",
        "lifecycle": "offered",
    }
    attached = PortfolioContinuationCheckpointService.attach(
        [option],
        session_id="session_1",
        source_assistant_turn_id="assistant_1",
        portfolio_id="portfolio_root",
        portfolio_summary=_summary(),
        default_planning_root_id="turn_root",
        default_request_fingerprint="request_fp",
        default_expected_base_version_id="version_1",
        expires_at=expires_at,
    )[0]
    checkpoint = attached["continuationCheckpoint"]

    assert (
        PortfolioContinuationCheckpointService.validate(
            checkpoint,
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="continue_4",
            option=attached,
            active_version_id="version_1",
        )
        is None
    )
    assert (
        PortfolioContinuationCheckpointService.validate(
            checkpoint,
            session_id="session_2",
            source_assistant_turn_id="assistant_1",
            choice_id="continue_4",
            option=attached,
            active_version_id="version_1",
        )
        == "portfolio_checkpoint_session_mismatch"
    )
    assert (
        PortfolioContinuationCheckpointService.validate(
            checkpoint,
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="continue_4",
            option=attached,
            active_version_id="version_2",
        )
        == "portfolio_checkpoint_base_version_mismatch"
    )


def test_checkpoint_validation_rejects_tampering_and_expiry() -> None:
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    option = {
        "id": "slot_refresh",
        "action": "refresh_portfolio_density_slot",
        "kind": "portfolio_density_refresh",
        "planningSelectionRootTurnId": "turn_root",
        "rootPortfolioId": "portfolio_root",
        "briefId": "brief_1",
        "poolId": "pool_night",
        "planningSlotId": "slot_night_day_1",
        "dayNumber": 1,
        "expectedBaseVersionId": None,
        "lifecycle": "offered",
    }
    attached = PortfolioContinuationCheckpointService.attach(
        [option],
        session_id="session_1",
        source_assistant_turn_id="assistant_1",
        portfolio_id="portfolio_root",
        portfolio_summary=_summary(),
        default_planning_root_id="turn_root",
        default_request_fingerprint="request_fp",
        default_expected_base_version_id=None,
        expires_at=expired_at,
    )[0]
    checkpoint = attached["continuationCheckpoint"]
    assert (
        PortfolioContinuationCheckpointService.validate(
            checkpoint,
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="slot_refresh",
            option=attached,
            active_version_id=None,
        )
        == "portfolio_checkpoint_expired"
    )

    tampered = dict(checkpoint)
    tampered["planningSlotId"] = "another_slot"
    assert (
        PortfolioContinuationCheckpointService.validate(
            tampered,
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="slot_refresh",
            option=attached,
            active_version_id=None,
        )
        == "portfolio_checkpoint_fingerprint_mismatch"
    )


def test_legacy_continuation_payload_keeps_existing_guards_without_v1_schema() -> None:
    assert (
        PortfolioContinuationCheckpointService.validate(
            {"briefId": "brief_legacy", "planningSlotId": "slot_legacy"},
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="legacy_choice",
            option={"id": "legacy_choice", "expectedBaseVersionId": "version_1"},
            active_version_id="version_1",
        )
        is None
    )


def test_explicit_unknown_checkpoint_schema_is_rejected() -> None:
    assert (
        PortfolioContinuationCheckpointService.validate(
            {"schemaVersion": "legacy-typo"},
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="legacy_choice",
            option={"id": "legacy_choice", "expectedBaseVersionId": "version_1"},
            active_version_id="version_1",
        )
        == "portfolio_checkpoint_schema_mismatch"
    )


def test_known_density_checkpoint_schema_is_delegated_to_density_validator() -> None:
    assert (
        PortfolioContinuationCheckpointService.validate(
            {"schemaVersion": "portfolio-density-continuation-v1"},
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="density_choice",
            option={"id": "density_choice", "expectedBaseVersionId": "version_1"},
            active_version_id="version_1",
        )
        is None
    )


def test_v1_checkpoint_rejects_naive_expiry() -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    attached = PortfolioContinuationCheckpointService.attach(
        [
            {
                "id": "continue_4",
                "action": "retry_model_planning",
                "kind": "portfolio_more_plans",
                "expectedBaseVersionId": "version_1",
                "lifecycle": "offered",
            }
        ],
        session_id="session_1",
        source_assistant_turn_id="assistant_1",
        portfolio_id="portfolio_root",
        portfolio_summary=_summary(),
        default_planning_root_id="turn_root",
        default_request_fingerprint="request_fp",
        default_expected_base_version_id="version_1",
        expires_at=expires_at,
    )[0]
    checkpoint = dict(attached["continuationCheckpoint"])
    checkpoint["expiresAt"] = (datetime.now() + timedelta(minutes=30)).isoformat()
    checkpoint["checkpointFingerprint"] = PortfolioContinuationCheckpointService.fingerprint(checkpoint)

    assert (
        PortfolioContinuationCheckpointService.validate(
            checkpoint,
            session_id="session_1",
            source_assistant_turn_id="assistant_1",
            choice_id="continue_4",
            option=attached,
            active_version_id="version_1",
        )
        == "portfolio_checkpoint_expiry_invalid"
    )


def test_scoped_manual_input_is_signed_without_becoming_an_adoption_choice() -> None:
    option = PortfolioContinuationCheckpointService.attach(
        [
            {
                "id": "manual_night_1",
                "action": "manual_continuation",
                "kind": "custom_input",
                "briefId": "brief_1",
                "poolId": "pool_night",
                "planningSlotId": "slot_night_1",
                "dayNumber": 1,
                "expectedBaseVersionId": "version_1",
                "lifecycle": "offered",
            }
        ],
        session_id="session_1",
        source_assistant_turn_id="assistant_1",
        portfolio_id="portfolio_root",
        portfolio_summary=_summary(),
        default_planning_root_id="turn_root",
        default_request_fingerprint="request_fp",
        default_expected_base_version_id="version_1",
        default_proposal_id="partial:portfolio_root",
    )[0]

    checkpoint = option["continuationCheckpoint"]
    assert checkpoint["proposalId"] == "partial:portfolio_root"
    assert checkpoint["briefId"] == "brief_1"
    assert checkpoint["poolId"] == "pool_night"
    assert checkpoint["planningSlotId"] == "slot_night_1"
    assert "proposalId" not in option
