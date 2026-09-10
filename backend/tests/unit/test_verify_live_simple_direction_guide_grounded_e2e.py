from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_PATH = REPO_ROOT / "scripts" / "verify_live_simple_direction_guide_grounded_e2e.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_live_simple_direction_guide_grounded_e2e", VERIFIER_PATH
)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def _run_summary(commit: str) -> dict:
    results = [
        {
            "path": path,
            "verified": True,
            "existsOnDisk": True,
            "tracked": True,
            "presentInExpectedCommit": True,
        }
        for path in sorted(verifier.REQUIRED_SOURCE_PATHS)
    ]
    return {
        "gitCommit": commit,
        "journeyMode": "simple_direction_guide",
        "playwrightSpec": "e2e/simple-direction-guide-grounded-user-journey.spec.ts",
        "providerMode": "default",
        "initialPlanningMode": "simple_open_v1",
        "databaseIsIsolated": True,
        "seededFromDatabase": None,
        "deepSeekEndpointHost": "api.deepseek.com",
        "sourceAttributionVerified": True,
        "requiredSourcePaths": sorted(verifier.REQUIRED_SOURCE_PATHS),
        "sourceAttributionChecks": [
            {
                "phase": "preflight",
                "verified": True,
                "headMatches": True,
                "trackedSourceClean": True,
                "requiredSourceResults": results,
            },
            {
                "phase": "post_journey",
                "verified": True,
                "headMatches": True,
                "trackedSourceClean": True,
                "requiredSourceResults": results,
            },
            {
                "phase": "final",
                "verified": True,
                "headMatches": True,
                "trackedSourceClean": True,
                "requiredSourceResults": results,
            },
        ],
    }


def _fixture() -> tuple[dict, dict, dict]:
    commit = "c" * 40
    query_fingerprint = "a" * 64
    source_fingerprint = "b" * 64
    evidence_fingerprint = verifier.guide_evidence_fingerprint(
        query_fingerprint=query_fingerprint,
        source_fingerprints=[source_fingerprint],
    )
    requirement = {
        "schemaVersion": "guide-continuation-requirement-v1",
        "sourceAssistantTurnId": "turn-guide",
        "capabilitySourceAssistantTurnId": "turn-guide",
        "guideChoiceExecutionId": "execution-guide",
        "planningSelectionRootTurnId": "turn-root-user",
        "rootPortfolioId": "portfolio-guide",
        "requestContractFingerprint": "d" * 64,
        "expectedBaseVersionId": None,
        "queryFingerprint": query_fingerprint,
        "evidenceFingerprint": evidence_fingerprint,
        "minimumNovelGroundedPlaceCount": 1,
        "placeHints": [
            {
                "schemaVersion": "guide-place-hint-v1",
                "mentionText": "北海公园",
                "intentType": "park",
                "sourceRefIds": ["guide-ref-1"],
                "sourceFingerprints": [source_fingerprint],
                "guideEvidenceFingerprint": evidence_fingerprint,
                "verificationStatus": "unresolved_amap_grounding",
            }
        ],
    }
    requirement["requirementFingerprint"] = verifier.canonical_fingerprint(requirement)
    usage = {
        "schemaVersion": "guide-evidence-usage-v1",
        "status": "satisfied",
        "evidenceFingerprint": evidence_fingerprint,
        "requirementFingerprint": requirement["requirementFingerprint"],
        "requiredMinimum": 1,
        "usedPlaces": [
            {
                "mentionText": "北海公园",
                "intentType": "park",
                "sourceRefIds": ["guide-ref-1"],
                "amapPoiId": "B0GUIDEPARK",
                "physicalIdentityKey": "amap:b0guidepark",
                "dayNumber": 1,
                "planningSlotId": "slot-guide",
                "routeVerified": True,
            }
        ],
        "rejectionCounts": {},
    }
    snapshot = {
        "guideContinuationRequirement": requirement,
        "simpleOpenRouteAssignment": {
            "routeCoverageComplete": True,
            "verifiedPairs": [
                {
                    "fromAmapId": "B0GUIDEPARK",
                    "toAmapId": "B0OTHERPOI1",
                    "provider": "amap-webservice",
                    "durationSeconds": 900,
                    "distanceMeters": 3000,
                    "providerEvidenceFingerprint": "e" * 64,
                }
            ],
        },
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "id": "segment-guide",
                        "poi": {"amapId": "B0GUIDEPARK"},
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "planningSlotId": "slot-guide",
                            "scheduleConstraints": {
                                "guideEvidence": {
                                    "schemaVersion": "guide-place-evidence-v1",
                                    "mentionText": "北海公园",
                                    "intentType": "park",
                                    "sourceRefIds": ["guide-ref-1"],
                                    "sourceFingerprints": [source_fingerprint],
                                    "guideEvidenceFingerprint": evidence_fingerprint,
                                    "verificationStatus": "verified_amap_grounding",
                                    "amapPoiId": "B0GUIDEPARK",
                                    "planningSlotId": "slot-guide",
                                    "dayNumber": 1,
                                }
                            },
                        },
                    },
                    {
                        "id": "segment-other",
                        "poi": {"amapId": "B0OTHERPOI1"},
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "planningSlotId": "slot-other",
                        },
                    },
                ],
            }
        ],
    }
    ordinary_pair = {
        "sourceAssistantTurnId": "turn-offer",
        "choiceId": "choice-ordinary-continuation",
    }
    guide_pair = {
        "sourceAssistantTurnId": "turn-ordinary-proposal",
        "choiceId": "choice-guide-search",
    }
    continuation_pair = {
        "sourceAssistantTurnId": "turn-guide",
        "choiceId": "choice-guide-continuation",
    }
    selection_pair = {
        "sourceAssistantTurnId": "turn-proposal",
        "choiceId": "choice-select-guide-proposal",
    }
    guide_advice = {
        "status": "completed",
        "queryFingerprint": query_fingerprint,
        "evidenceFingerprint": evidence_fingerprint,
        "sourceRefs": [
            {
                "refId": "guide-ref-1",
                "sourceFingerprint": source_fingerprint,
                "title": "北海公园攻略",
                "url": "https://travel.example/guide",
            }
        ],
        "placeHints": requirement["placeHints"],
        "queryCount": 1,
        "successfulProviders": ["bing-html-search"],
    }
    proposal_payload = {
        "mode": "simple_open_direction_proposal",
        "workflowMode": "simple_direction_v1",
        "proposalDelta": 1,
        "versionDelta": 0,
        "patchDelta": 0,
        "routeWriteDelta": 0,
        "guideEvidenceUsage": usage,
        "controllerEvidence": {
            "source": "controller",
            "decisionPath": "full",
            "controllerFullCalled": True,
            "controllerLiteCalled": False,
            "controllerSucceeded": True,
            "accepted": True,
            "controllerPerformance": [
                {
                    "providerInvoked": True,
                    "captureState": "completed",
                    "responseHeadersReceived": True,
                    "httpStatus": 200,
                    "responseBytes": 1234,
                }
            ],
            "providerRawDecisions": [
                {"schemaVersion": "controller-v1", "primaryAction": "draft_itinerary"}
            ],
        },
        "planningSteps": [
            {
                "type": "simple_open_tool_call",
                "providerName": "amap-place-search",
                "status": "completed",
                "metadata": {
                    "providerOutcome": "success",
                    "cacheHit": False,
                    "resultCount": 3,
                    "selectedAmapId": "B0GUIDEPARK",
                },
            },
            {
                "type": "agent_stop",
                "status": "completed",
                "metadata": {
                    "controllerFullCallCount": 1,
                    "controllerLiteCallCount": 0,
                    "plannerCalled": True,
                },
            },
        ],
        "choiceOptions": [
            {
                "id": selection_pair["choiceId"],
                "choiceId": selection_pair["choiceId"],
                "sourceAssistantTurnId": selection_pair["sourceAssistantTurnId"],
                "action": "select_plan_proposal",
                "proposalId": "proposal-guide",
            }
        ],
    }
    journey = {
        "schemaVersion": "trip-simple-direction-guide-grounded-live-v1",
        "gitCommit": commit,
        "sessionId": "session-guide",
        "guideGroundedRequest": verifier.GUIDE_GROUNDED_REQUEST,
        "baselineProposalIds": ["proposal-baseline", "proposal-ordinary"],
        "proposalId": "proposal-guide",
        "activeVersionId": "version-guide",
        "providerReadiness": {
            "mode": "default",
            "agentProviderName": "DeepSeek",
            "agentConfigured": True,
            "amapConfigured": True,
            "browserMapEnabled": True,
        },
        "opaqueChoices": {
            "ordinaryContinuation": ordinary_pair,
            "guideSearch": guide_pair,
            "guideContinuation": continuation_pair,
            "proposalSelection": selection_pair,
        },
        "ordinaryContinuationEvidence": {
            "sourceAssistantTurnId": "turn-offer",
            "assistantTurnId": "turn-ordinary-proposal",
            "initialProposalIds": ["proposal-baseline"],
            "newProposalIds": ["proposal-ordinary"],
            "proposalDelta": 1,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
        },
        "guideEvidence": {
            "sourceAssistantTurnId": "turn-guide",
            "queryFingerprint": query_fingerprint,
            "evidenceFingerprint": evidence_fingerprint,
            "sourceCount": 1,
            "placeHintCount": 1,
        },
        "proposalEvidence": {
            "assistantTurnId": "turn-proposal",
            "proposalDelta": 1,
            "versionDelta": 0,
            "patchDelta": 0,
            "routeWriteDelta": 0,
            "controllerFullCallCount": 1,
            "controllerLiteCallCount": 0,
            "plannerCalled": True,
            "guideEvidenceUsage": usage,
        },
        "adoptionEvidence": {
            "assistantTurnId": "turn-adopt",
            "activeVersionId": "version-guide",
            "selectedProposalId": "proposal-guide",
            "versionDelta": 1,
            "patchDelta": 1,
            "routeWriteDelta": 1,
            "reloadPreservedActiveVersion": True,
        },
    }
    records = {
        "sessions": [
            {"id": "session-guide", "active_version_id": "version-guide"}
        ],
        "turns": [
            {
                "id": "turn-offer",
                "role": "assistant",
                "content": "initial proposal",
                "agent_response_json": json.dumps(
                    {
                        "choiceOptions": [
                            {
                                "id": ordinary_pair["choiceId"],
                                "choiceId": ordinary_pair["choiceId"],
                                "kind": "simple_direction_more_plans",
                                "action": "continue_plan_expansion",
                                "requestContractFingerprint": "1" * 64,
                                "sourceAssistantTurnId": ordinary_pair[
                                    "sourceAssistantTurnId"
                                ],
                            }
                        ]
                    }
                ),
            },
            {
                "id": "turn-ordinary-request",
                "role": "user",
                "content": "继续探索",
                "agent_request_json": json.dumps(
                    {"selectedAgentChoice": ordinary_pair}
                ),
            },
            {
                "id": "turn-ordinary-proposal",
                "role": "assistant",
                "content": "ordinary second proposal",
                "agent_response_json": json.dumps(
                    {
                        "mode": "simple_open_direction_proposal",
                        "workflowMode": "simple_direction_v1",
                        "proposalDelta": 1,
                        "versionDelta": 0,
                        "patchDelta": 0,
                        "routeWriteDelta": 0,
                        "choiceOptions": [
                            {
                                "id": guide_pair["choiceId"],
                                "choiceId": guide_pair["choiceId"],
                                "action": "search_travel_guide_advice",
                                "sourceAssistantTurnId": guide_pair[
                                    "sourceAssistantTurnId"
                                ],
                            }
                        ],
                    }
                ),
            },
            {
                "id": "turn-guide-request",
                "role": "user",
                "content": "搜索攻略",
                "agent_request_json": json.dumps(
                    {"selectedAgentChoice": guide_pair}
                ),
            },
            {
                "id": "turn-guide",
                "role": "assistant",
                "content": "guide result",
                "agent_response_json": json.dumps(
                    {
                        "mode": "travel_guide_advice",
                        "guideAdvice": guide_advice,
                        "choiceOptions": [
                            {
                                "id": continuation_pair["choiceId"],
                                "action": "continue_plan_expansion",
                                "sourceAssistantTurnId": continuation_pair[
                                    "sourceAssistantTurnId"
                                ],
                            }
                        ],
                    }
                ),
            },
            {
                "id": "turn-continuation-request",
                "role": "user",
                "content": verifier.GUIDE_GROUNDED_REQUEST,
                "agent_request_json": json.dumps(
                    {"selectedAgentChoice": continuation_pair}
                ),
            },
            {
                "id": "turn-proposal",
                "role": "assistant",
                "content": "guide grounded proposal",
                "agent_response_json": json.dumps(proposal_payload),
            },
            {
                "id": "turn-selection-request",
                "role": "user",
                "content": "select proposal",
                "agent_request_json": json.dumps(
                    {"selectedAgentChoice": selection_pair}
                ),
            },
            {
                "id": "turn-adopt",
                "role": "assistant",
                "content": "adopted",
                "agent_response_json": json.dumps(
                    {
                        "activeVersionId": "version-guide",
                        "selectedProposalId": "proposal-guide",
                        "versionDelta": 1,
                        "patchDelta": 1,
                        "routeWriteDelta": 1,
                    }
                ),
            },
        ],
        "portfolios": [
            {
                "id": "portfolio-guide",
                "selected_proposal_id": "proposal-guide",
            }
        ],
        "proposals": [
            {
                "id": "proposal-baseline",
                "portfolio_id": "portfolio-guide",
                "choice_id": "choice-baseline",
                "status": "comparison_only",
                "snapshot_json": "{}",
                "verifier_json": "{}",
                "evidence_json": "{}",
                "generation_lineage_json": "{}",
            },
            {
                "id": "proposal-guide",
                "portfolio_id": "portfolio-guide",
                "choice_id": selection_pair["choiceId"],
                "status": "committed",
                "snapshot_json": json.dumps(snapshot),
                "verifier_json": json.dumps(
                    {"guideEvidenceUsage": usage, "confirmationPassed": True}
                ),
                "evidence_json": json.dumps({"guideEvidenceUsage": usage}),
                "generation_lineage_json": json.dumps(
                    {
                        "workflowMode": "simple_direction_v1",
                        "frontierExecutionId": "execution-continuation",
                        "sourceAssistantTurnId": "turn-proposal",
                        "guideContinuationRequirementFingerprint": requirement[
                            "requirementFingerprint"
                        ],
                        "guideEvidenceSourceAssistantTurnId": "turn-guide",
                        "guideChoiceExecutionId": "execution-guide",
                        "guideEvidenceFingerprint": evidence_fingerprint,
                        "itineraryWriteCount": 0,
                    }
                ),
            },
            {
                "id": "proposal-ordinary",
                "portfolio_id": "portfolio-guide",
                "choice_id": "choice-ordinary-proposal",
                "status": "comparison_only",
                "snapshot_json": json.dumps({"days": []}),
                "verifier_json": "{}",
                "evidence_json": "{}",
                "generation_lineage_json": json.dumps(
                    {
                        "workflowMode": "simple_direction_v1",
                        "frontierExecutionId": "execution-ordinary",
                        "sourceAssistantTurnId": "turn-ordinary-proposal",
                        "requestContractFingerprint": "1" * 64,
                        "itineraryWriteCount": 0,
                    }
                ),
            },
        ],
        "executions": [
            {
                "id": "execution-ordinary",
                "source_turn_id": ordinary_pair["sourceAssistantTurnId"],
                "choice_id": ordinary_pair["choiceId"],
                "action": "continue_plan_expansion",
                "status": "succeeded",
                "execution_turn_id": "turn-ordinary-proposal",
                "request_turn_id": "turn-ordinary-request",
                "result_version_id": None,
                "outcome_json": json.dumps(
                    {
                        "reason": "new_simple_direction_proposal",
                        "proposalDelta": 1,
                        "versionDelta": 0,
                        "patchDelta": 0,
                        "routeWriteDelta": 0,
                    }
                ),
            },
            {
                "id": "execution-guide",
                "source_turn_id": guide_pair["sourceAssistantTurnId"],
                "choice_id": guide_pair["choiceId"],
                "action": "search_travel_guide_advice",
                "status": "succeeded",
                "execution_turn_id": "turn-guide",
                "request_turn_id": "turn-guide-request",
                "result_version_id": None,
                "outcome_json": json.dumps(
                    {
                        "mode": "travel_guide_advice",
                        "proposalDelta": 0,
                        "versionDelta": 0,
                        "patchDelta": 0,
                        "routeWriteDelta": 0,
                    }
                ),
            },
            {
                "id": "execution-continuation",
                "source_turn_id": continuation_pair["sourceAssistantTurnId"],
                "choice_id": continuation_pair["choiceId"],
                "action": "continue_plan_expansion",
                "status": "succeeded",
                "execution_turn_id": "turn-proposal",
                "request_turn_id": "turn-continuation-request",
                "result_version_id": None,
                "outcome_json": json.dumps(
                    {
                        "reason": "new_simple_direction_proposal",
                        "proposalDelta": 1,
                        "versionDelta": 0,
                        "patchDelta": 0,
                        "routeWriteDelta": 0,
                        "completionEvidence": {
                            "passed": True,
                            "reason": "simple_direction_execution_evidence_verified",
                        },
                    }
                ),
            },
            {
                "id": "execution-selection",
                "source_turn_id": selection_pair["sourceAssistantTurnId"],
                "source_user_turn_id": "turn-root-user",
                "choice_id": selection_pair["choiceId"],
                "action": "select_plan_proposal",
                "status": "succeeded",
                "execution_turn_id": "turn-adopt",
                "request_turn_id": "turn-selection-request",
                "result_version_id": "version-guide",
                "outcome_json": json.dumps(
                    {"versionDelta": 1, "patchDelta": 1, "routeWriteDelta": 1}
                ),
            },
        ],
        "versions": [
            {
                "id": "version-guide",
                "session_id": "session-guide",
                "plan_id": "plan-guide",
                "source_turn_id": "turn-adopt",
                "snapshot_json": json.dumps(snapshot),
            }
        ],
        "patches": [
            {
                "id": "patch-guide",
                "session_id": "session-guide",
                "plan_id": "plan-guide",
                "result_version_id": "version-guide",
                "source_turn_id": "turn-adopt",
                "validation_status": "accepted",
            }
        ],
        "routes": [
            {
                "id": "route-guide",
                "plan_id": "plan-guide",
                "from_segment_id": "segment-guide",
                "to_segment_id": "segment-other",
                "provider": "amap-webservice",
                "distance_meters": 3000,
                "duration_seconds": 900,
                "is_selected": 1,
            }
        ],
    }
    return journey, _run_summary(commit), records


def _verify(journey: dict, run_summary: dict, records: dict) -> dict:
    return verifier.verify_records(
        journey=journey,
        run_summary=run_summary,
        records=records,
        expected_git_commit="c" * 40,
        deepseek_endpoint_host="api.deepseek.com",
    )


def test_guide_grounded_verifier_accepts_exact_opaque_zero_write_then_single_commit() -> None:
    journey, run_summary, records = _fixture()

    report = _verify(journey, run_summary, records)

    assert report["passed"] is True, report["failures"]
    assert report["routeVerifiedGuidePlaceCount"] == 1
    assert report["formalDeltas"] == {"version": 1, "patch": 1, "route": 1}


def test_guide_grounded_verifier_rejects_requirement_and_choice_tampering() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(records)
    proposal = tampered["proposals"][1]
    snapshot = json.loads(proposal["snapshot_json"])
    snapshot["guideContinuationRequirement"]["evidenceFingerprint"] = "f" * 64
    proposal["snapshot_json"] = json.dumps(snapshot)
    selection_request = next(
        turn for turn in tampered["turns"] if turn["id"] == "turn-selection-request"
    )
    selection_request["agent_request_json"] = json.dumps(
        {
            "selectedAgentChoice": {
                "sourceAssistantTurnId": "turn-proposal",
                "choiceId": "choice-tampered",
            }
        }
    )

    report = _verify(journey, run_summary, tampered)

    assert report["passed"] is False
    assert "guide_requirement_fingerprint_mismatch" in report["failures"]
    assert "guide_requirement_usage_binding_mismatch" in report["failures"]
    assert "persisted_selection_choice_identity_mismatch" in report["failures"]


def test_guide_grounded_verifier_rejects_preselection_formal_write_claim() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(records)
    continuation = next(
        item for item in tampered["executions"] if item["id"] == "execution-continuation"
    )
    outcome = json.loads(continuation["outcome_json"])
    outcome["versionDelta"] = 1
    continuation["outcome_json"] = json.dumps(outcome)

    report = _verify(journey, run_summary, tampered)

    assert report["passed"] is False
    assert "guide_continuation_outcome_nonzero_write" in report["failures"]


def test_guide_grounded_verifier_rejects_duplicate_continuation_and_extra_proposal() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(records)
    duplicate = copy.deepcopy(
        next(
            item
            for item in tampered["executions"]
            if item["id"] == "execution-continuation"
        )
    )
    duplicate["id"] = "execution-continuation-duplicate"
    duplicate["choice_id"] = "choice-continuation-duplicate"
    tampered["executions"].append(duplicate)
    extra_proposal = copy.deepcopy(tampered["proposals"][1])
    extra_proposal["id"] = "proposal-guide-extra"
    tampered["proposals"].append(extra_proposal)

    report = _verify(journey, run_summary, tampered)

    assert report["passed"] is False
    assert "continuation_execution_set_not_exactly_two" in report["failures"]
    assert "guide_continuation_proposal_count_not_one" in report["failures"]


def test_guide_grounded_verifier_rejects_missing_ordinary_second_proposal() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(records)
    tampered["executions"] = [
        item
        for item in tampered["executions"]
        if item["id"] != "execution-ordinary"
    ]
    tampered["proposals"] = [
        item for item in tampered["proposals"] if item["id"] != "proposal-ordinary"
    ]

    report = _verify(journey, run_summary, tampered)

    assert report["passed"] is False
    assert "continuation_execution_set_not_exactly_two" in report["failures"]
    assert "ordinary_second_proposal_lineage_invalid" in report["failures"]
    assert "guide_continuation_proposal_count_not_one" in report["failures"]


def test_guide_grounded_verifier_rejects_tampered_ordinary_outcome_and_lineage() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(records)
    execution = next(
        item for item in tampered["executions"] if item["id"] == "execution-ordinary"
    )
    outcome = json.loads(execution["outcome_json"])
    outcome["proposalDelta"] = 2
    execution["outcome_json"] = json.dumps(outcome)
    proposal = next(
        item for item in tampered["proposals"] if item["id"] == "proposal-ordinary"
    )
    lineage = json.loads(proposal["generation_lineage_json"])
    lineage["sourceAssistantTurnId"] = "turn-tampered"
    lineage["requestContractFingerprint"] = "f" * 64
    proposal["generation_lineage_json"] = json.dumps(lineage)

    report = _verify(journey, run_summary, tampered)

    assert report["passed"] is False
    assert "ordinary_continuation_delta_or_mode_invalid" in report["failures"]
    assert "ordinary_second_proposal_lineage_invalid" in report["failures"]


def test_guide_grounded_verifier_rejects_browser_artifact_lineage_tampering() -> None:
    journey, run_summary, records = _fixture()
    tampered = copy.deepcopy(journey)
    tampered["guideEvidence"]["queryFingerprint"] = "f" * 64
    tampered["guideEvidence"]["evidenceFingerprint"] = "e" * 64
    tampered["proposalEvidence"]["assistantTurnId"] = "turn-proposal-tampered"
    tampered["adoptionEvidence"]["assistantTurnId"] = "turn-adoption-tampered"

    report = _verify(tampered, run_summary, records)

    assert report["passed"] is False
    assert "browser_guide_turn_identity_mismatch" in report["failures"]
    assert "browser_database_proposal_turn_mismatch" in report["failures"]
    assert "browser_database_adoption_delta_mismatch" in report["failures"]


def test_guide_grounded_verifier_requires_its_own_frozen_source_attribution() -> None:
    journey, run_summary, records = _fixture()
    verifier_path = "scripts/verify_live_simple_direction_guide_grounded_e2e.py"
    tampered = copy.deepcopy(run_summary)
    tampered["requiredSourcePaths"].remove(verifier_path)
    for check_result in tampered["sourceAttributionChecks"]:
        check_result["requiredSourceResults"] = [
            item
            for item in check_result["requiredSourceResults"]
            if item["path"] != verifier_path
        ]

    report = _verify(journey, tampered, records)

    assert report["passed"] is False
    assert "required_source_allowlist_incomplete" in report["failures"]
    assert "required_source_results_incomplete:preflight" in report["failures"]


def test_guide_grounded_verifier_rejects_lite_controller_on_guide_turn() -> None:
    journey, run_summary, records = _fixture()
    proposal_turn = next(
        item for item in records["turns"] if item["id"] == "turn-proposal"
    )
    payload = json.loads(proposal_turn["agent_response_json"])
    payload["controllerEvidence"].update(
        {
            "decisionPath": "lite",
            "controllerFullCalled": False,
            "controllerLiteCalled": True,
        }
    )
    stop = next(
        item for item in payload["planningSteps"] if item["type"] == "agent_stop"
    )
    stop["metadata"].update(
        {
            "controllerFullCallCount": 0,
            "controllerLiteCallCount": 1,
            "plannerCalled": False,
        }
    )
    proposal_turn["agent_response_json"] = json.dumps(payload, ensure_ascii=False)

    report = _verify(journey, run_summary, records)

    assert report["passed"] is False
    assert "real_deepseek_controller_evidence_missing" in report["failures"]
    assert "guide_continuation_full_controller_metrics_invalid" in report["failures"]


def test_runner_registers_guide_mode_default_spec_verifier_and_required_sources() -> None:
    source = (REPO_ROOT / "scripts" / "run-live-portfolio-e2e.ps1").read_text(
        encoding="utf-8"
    )

    assert '"simple_direction_guide"' in source
    assert '"e2e/simple-direction-guide-grounded-user-journey.spec.ts"' in source
    assert '"scripts\\verify_live_simple_direction_guide_grounded_e2e.py"' in source
    assert '"backend/src/services/guide_continuation_requirement_service.py"' in source
    for required_path in (
        "backend/src/services/conversation_intent_router.py",
        "backend/src/services/agent_service.py",
        "backend/src/services/conversation_service.py",
        "backend/src/services/simple_open_itinerary_executor.py",
        "frontend/src/services/apiClient.ts",
        "frontend/src/state/planComparisonPreview.ts",
        "frontend/src/components/comparison/PlanComparison.tsx",
    ):
        assert f'"{required_path}"' in source
    assert (
        '$simpleDirectionJourneyModes = @("simple_direction", "simple_direction_guide", '
        '"simple_direction_frontier", "two_day_meal_regression")'
        in source
    )


def test_guide_playwright_contract_uses_exact_phrase_and_exact_selected_choice() -> None:
    source = (
        REPO_ROOT / "e2e" / "simple-direction-guide-grounded-user-journey.spec.ts"
    ).read_text(encoding="utf-8")

    assert 'const GUIDE_GROUNDED_REQUEST = "参考攻略建议的地点，生成新的方案";' in source
    assert "extractExactSelectedAgentChoice(continuationRequests[0].body)" in source
    assert "extractExactSelectedAgentChoice(ordinaryRequests[0].body)" in source
    assert 'expect(ordinaryChoice.kind).toBe("simple_direction_more_plans")' in source
    assert 'expect(proposalTurn.guideEvidenceUsage' not in source
    assert "assertSatisfiedGuideUsage(guideUsage)" in source
    assert 'expect(Number(proposalTurn[key] || 0)).toBe(0)' in source
    assert "proposalControllerMetrics.controllerFullCallCount" in source
    assert "proposalControllerMetrics.controllerLiteCallCount" in source
    assert "expect(proposalControllerMetrics.plannerCalled).toBe(true)" in source
    assert "await selectionButton.click()" in source
