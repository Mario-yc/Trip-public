import json
from pathlib import Path
from types import SimpleNamespace

from src.services.agent_service import AgentService
from src.services.experience_intent_service import ExperienceIntentService


GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "evals" / "cases" / "creative_experience_intent_soft_draft_golden.json"
)


def test_golden_manifest_preserves_full_multiturn_contract_and_opaque_adoption():
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    scenario = payload["scenarios"][0]
    contract = payload["goldenContract"]

    assert payload["schemaVersion"] == "trip-agent-quality-scenarios-v1"
    assert len(scenario["turns"]) == 3
    assert scenario["turns"][1]["selectedAgentChoiceExactIdFromTurn"] == 1
    assert scenario["turns"][1]["selectedAgentChoiceOptionIndex"] == 1
    assert scenario["turns"][2]["selectedAgentChoiceExactIdFromTurn"] == 2
    assert scenario["turns"][2]["selectedAgentChoiceOptionIndex"] == 1
    assert contract["writer"] == "PlanProposalCommitService"
    assert contract["adoptionMode"] == "editable_partial"
    assert contract["proposalDirectionCount"] == {"minimum": 2, "maximum": 3}
    assert scenario["turnExpectations"][0]["versionWriteCount"] == 0
    assert scenario["turnExpectations"][1]["proposalPreviewWriteCount"] == 0
    assert scenario["turnExpectations"][1]["patchWriteCount"] == 0
    assert scenario["turnExpectations"][2] == {
        "proposalCommitAttemptCount": 1,
        "versionWriteCount": 1,
        "patchWriteCount": 1,
        "pendingSlotPreservedCount": 4,
    }


def test_golden_turn_one_exposes_only_machine_ambiguity_for_controller_authorship():
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    user_input = payload["scenarios"][0]["turns"][0]["input"]

    inferred = ExperienceIntentService().infer(user_input)

    assert inferred["highImpactAmbiguityDetected"] is True
    assert inferred["clarificationDimensionId"] == "experience_intent.primary_axis"
    assert inferred["clarificationQuestion"] == ""
    assert inferred["clarificationOptions"] == []
    assert set(inferred["contract"]["allowedExperienceShapes"]) == {
        "single_poi",
        "area",
        "micro_route",
        "open_walk",
    }
    assert "B0" not in json.dumps(inferred, ensure_ascii=False)

    request_context = {
        "requestIntentContract": {"clarificationRequired": False},
        "understoodRequirements": {
            "highImpactAmbiguityDetected": True,
            "experienceIntentClarificationOptions": inferred["clarificationOptions"],
        },
    }
    assert AgentService._request_contract_requires_clarification(request_context) is True
    service = AgentService.__new__(AgentService)
    assert not hasattr(service, "_clarification_choice_options")

    intent_contract = service._request_intent_contract(
        user_input,
        {"dates": ["2026-10-01", "2026-10-02", "2026-10-03"]},
        SimpleNamespace(meal_slots=[]),
    )
    museum = next(item for item in intent_contract["requiredIntents"] if item["intentType"] == "museum")
    local_life = next(item for item in intent_contract["requiredIntents"] if item["intentType"] == "local_culture")
    assert museum["exactEntity"] == "故宫博物院"
    assert museum["requirementLevel"] == "required"
    assert intent_contract["lockedEntities"] == ["故宫博物院"]
    assert local_life["requirementLevel"] == "soft_experience"
    assert local_life["requiredMin"] == 0
