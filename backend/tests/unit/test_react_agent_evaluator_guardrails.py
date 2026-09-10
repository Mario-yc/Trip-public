import json
from pathlib import Path

from src.runtime.agent_quality_eval import AgentQualityEvaluator


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_turn_cycle_sequence_uses_persisted_decision_and_outcome_events():
    turn = {
        "context": {},
        "planningSteps": [
            {
                "type": "agent_decision",
                "metadata": {"cycleIndex": 0, "primaryAction": "patch_itinerary"},
            },
            {
                "type": "agent_action_outcome",
                "metadata": {
                    "cycleIndex": 0,
                    "resultPreview": {"executionRoute": "timeline_mutation_executor"},
                },
            },
            {
                "type": "agent_decision",
                "metadata": {"cycleIndex": 1, "primaryAction": "finish"},
            },
            {
                "type": "agent_action_outcome",
                "metadata": {
                    "cycleIndex": 1,
                    "resultPreview": {"executionRoute": "terminal_response"},
                },
            },
        ],
    }

    assert AgentQualityEvaluator._turn_cycle_sequence(turn) == [
        ("patch_itinerary", "timeline_mutation_executor"),
        ("finish", "terminal_response"),
    ]


def test_multiturn_eval_requires_real_turn1_and_excludes_seeded_executor_case_from_control_kpi():
    react_case = json.loads(
        (REPO_ROOT / "backend/evals/cases/react_multiturn_museum_time_edit.json").read_text(encoding="utf-8")
    )["scenarios"][0]
    executor_case = json.loads(
        (REPO_ROOT / "backend/evals/cases/timeline_mutation_set_time.json").read_text(encoding="utf-8")
    )["scenarios"][0]

    assert "seedItinerary" not in react_case
    assert [turn["input"] for turn in react_case["turns"]] == [
        "10月1日至2日北京两日游，1人，中等预算，公交地铁优先，必须参观清华大学和美术馆。",
        "把第一天美术馆改到 15:30",
    ]
    assert react_case["requiresTurn1CreatedItinerary"] is True
    assert executor_case["executorOnly"] is True
    assert executor_case["controlOwnershipExcluded"] is True


def test_evaluator_rejects_self_reported_react_success_without_causal_evidence():
    evaluator = AgentQualityEvaluator(runtime=None)
    artifact = {
        "context": {
            # These are deliberately forged. The evaluator must derive metrics
            # from event order, persisted rows and snapshots instead.
            "preControllerDomainRouterCount": 0,
            "localMutationBypassCount": 0,
            "falseSuccessClaimCount": 0,
            "transactionEvidenceMissingCount": 0,
            "durationPreservationFailureCount": 0,
        },
        "planningSteps": [
            {
                "type": "timeline_edit",
                "status": "completed",
                "metadata": {
                    "resultPreview": {
                        "changed": True,
                        "toolLoopEntered": False,
                        "before": {"startTime": "16:15", "endTime": "17:45", "durationMinutes": 90},
                        "after": {"startTime": "15:30", "endTime": "18:00", "durationMinutes": 150},
                    }
                },
            }
        ],
        "toolEvents": [],
        "agentObservations": [],
        "agentDecisions": [],
        "agentActionOutcomes": [],
        "sessionSnapshot": {
            "conversation_turns": [
                {
                    "role": "assistant",
                    "content": "已把第一天美术馆改到 15:30。",
                    "agent_response_json": {
                        "reply": "已把第一天美术馆改到 15:30。",
                        "timelineMutationOutcome": {"status": "success"},
                        "timelineMutationTransaction": None,
                        "planningRun": None,
                    },
                }
            ],
            "itinerary_versions": [],
            "itinerary_patches": [],
            "timeline_mutation_transactions": [],
        },
        "itinerarySnapshot": {},
        "verifierReport": {},
    }
    metrics = evaluator._metrics(
        artifact,
        {
            "status": "completed",
            "reply": "已把第一天美术馆改到 15:30。",
            "activeVersionId": "ver_claimed_only",
        },
    )

    assert metrics["preControllerDomainRouterCount"] == 1
    assert metrics["localMutationBypassCount"] == 1
    assert metrics["falseSuccessClaimCount"] == 1
    assert metrics["transactionEvidenceMissingCount"] == 1
    assert metrics["durationPreservationFailureCount"] == 1


def test_evaluator_accepts_verified_candidate_patch_transaction_evidence():
    evaluator = AgentQualityEvaluator(runtime=None)
    preview = {
        "action": "patch_itinerary",
        "executionRoute": "timeline_mutation_executor",
        "status": "success",
        "resultVersionId": "ver_result",
        "patchIds": ["patch_result"],
        "changedSegmentIds": ["seg_museum"],
        "candidateSummary": {"candidateRecordId": "cand_1", "amapPoiId": "B0MUSEUM"},
        "verifier": {"passed": True},
        "executionEvents": [
            {"name": "candidate_patch_started"},
            {"name": "candidate_patch_applied"},
            {"name": "candidate_patch_verifier"},
            {"name": "candidate_patch_committed"},
        ],
    }
    artifact = {
        "planningSteps": [
            {
                "type": "agent_decision",
                "metadata": {"source": "controller", "accepted": True, "primaryAction": "patch_itinerary"},
            },
            {"type": "agent_action_outcome", "metadata": {"resultPreview": preview}},
        ],
        "toolEvents": [],
        "agentActionOutcomes": [{"metadata": {"resultPreview": preview}}],
        "sessionSnapshot": {
            "conversation_turns": [],
            "itinerary_versions": [{"id": "ver_result"}],
            "itinerary_patches": [{"id": "patch_result", "result_version_id": "ver_result"}],
            "timeline_mutation_transactions": [],
        },
        "itinerarySnapshot": {},
        "verifierReport": {"passed": True},
    }

    metrics = evaluator._metrics(
        artifact,
        {
            "status": "success",
            "assistantReply": "已将第一天美术馆替换并保存。",
            "activeVersionId": "ver_result",
        },
    )

    assert metrics["falseSuccessClaimCount"] == 0
    assert metrics["transactionEvidenceMissingCount"] == 0


def test_executor_only_result_is_excluded_from_control_ownership_aggregate():
    evaluator = AgentQualityEvaluator(runtime=None)
    aggregate, failures = evaluator._aggregate_metrics(
        [
            {
                "metrics": {"preControllerDomainRouterCount": 9, "localMutationBypassCount": 7},
                "executorOnly": True,
                "controlOwnershipExcluded": True,
            },
            {
                "metrics": {"preControllerDomainRouterCount": 0, "localMutationBypassCount": 0},
                "executorOnly": False,
                "controlOwnershipExcluded": False,
            },
        ],
        {"preControllerDomainRouterCount": 0, "localMutationBypassCount": 0},
    )

    assert failures == []
    assert aggregate["controlOwnershipEligibleScenarioCount"] == 1
    assert aggregate["controlOwnershipExcludedScenarioCount"] == 1
    assert aggregate["preControllerDomainRouterCount"] == 0
    assert aggregate["localMutationBypassCount"] == 0
