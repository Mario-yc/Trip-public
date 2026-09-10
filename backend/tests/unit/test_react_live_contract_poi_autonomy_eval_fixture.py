from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = REPO_ROOT / "backend/evals/cases/react_live_contract_poi_autonomy.json"


EXPECTED_SCENARIO_IDS = {
    "react_live_contract_initial_request",
    "react_live_contract_retry_choice",
    "react_live_contract_manual_museum_replacement",
    "react_poi_dominant_museum_auto_select",
    "react_poi_material_tradeoff_asks",
    "react_restaurant_dominant_auto_select",
    "react_no_safe_action_truthful_trace",
    "react_goal_cardinality_unquantified_985",
}


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_eval_manifest_contains_the_full_p0_provider_shaped_matrix() -> None:
    payload = _fixture()

    assert payload["schemaVersion"] == "trip-agent-quality-scenarios-v1"
    assert {scenario["id"] for scenario in payload["scenarios"]} == EXPECTED_SCENARIO_IDS
    assert payload["recordingProvenance"]["kind"] == "sanitized_provider_shaped"
    assert payload["recordingProvenance"]["liveProviderClaim"] is False


def test_recorded_alias_fixtures_preserve_canonical_continuation_context() -> None:
    payload = _fixture()
    fixtures = {item["id"]: item for item in payload["recordedProviderShapeFixtures"]}
    segment_alias = fixtures["resolve_poi_segment_id_search_text"]
    day_alias = fixtures["resolve_poi_target_segment_id_day_number"]

    first_directive = segment_alias["rawDecision"]["actionDirective"]
    assert first_directive["segmentId"] == "seg_day1_museum"
    assert first_directive["searchText"] == "清华美术馆"
    assert segment_alias["expectedNormalization"]["normalizedDirective"]["targetSegmentIds"] == [
        "seg_day1_museum"
    ]
    assert segment_alias["expectedNormalization"]["normalizedDirective"]["searchIntent"] == "清华美术馆"

    second_directive = day_alias["rawDecision"]["actionDirective"]
    assert second_directive["targetSegmentId"] == "seg_day1_museum"
    assert second_directive["dayNumber"] == 1
    assert day_alias["canonicalContextRef"] == "resolve_poi_segment_id_search_text.canonicalContext"

    context = segment_alias["canonicalContext"]
    assert context["latestUserMessage"] == "重试一次，将第一天美术馆修改为清华美术馆"
    assert "985大学两日游" in context["effectiveUserMessage"]
    assert context["activeVersionId"] == "ver_recorded_v3"
    assert context["resolvedTripDates"]["dates"] == ["2026-10-01", "2026-10-02"]
    assert context["requestIntentContract"]["sourceUserTurnId"] == "turn_recorded_source"

    observation = context["observation"]
    assert observation["versionLineage"]["currentVersionId"] == context["activeVersionId"]
    assert "seg_day1_museum" in observation["targetInventory"]["segmentIds"]
    museum_ref = next(item for item in observation["segmentRefs"] if item["goalId"] == "goal_museum")
    assert (museum_ref["dayNumber"], museum_ref["segmentId"]) == (1, "seg_day1_museum")
    assert observation["unresolvedSlots"] == [
        {
            "slotId": "slot_day1_museum",
            "segmentId": "seg_day1_museum",
            "goalId": "goal_museum",
            "dayNumber": 1,
            "intentType": "museum",
            "rawNeed": "美术馆参观",
            "required": True,
        }
    ]


def test_unquantified_985_goal_is_one_hard_goal_not_day_count() -> None:
    payload = _fixture()
    scenario = next(
        item for item in payload["scenarios"] if item["id"] == "react_goal_cardinality_unquantified_985"
    )

    assert "985大学两日游" in scenario["turns"][0]["input"]
    assert scenario["expectedGoalLedger"] == {
        "goalId": "goal_campus_visit",
        "intentType": "campus_visit",
        "requirementLevel": "hard",
        "requiredMin": 1,
        "preferred": 1,
        "max": None,
        "mustNotInferCountFromDayCount": True,
    }
