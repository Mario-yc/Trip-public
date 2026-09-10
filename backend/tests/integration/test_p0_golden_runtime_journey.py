from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.core.config import get_settings
from src.core.schema import initialize_database
from src.runtime.agent_runtime import TripAgentRuntime, clear_map_poi_runtime_state
from src.runtime.runtime_models import RuntimeRunOptions


TURN_1 = (
    "今年国庆参观985大学两日游，然后去体验下北京当地博物馆陶冶情操。"
    "10月1日到2日，2天，中等预算，1人，公交地铁优先。"
    "路途中能品尝北京当地特色美食。绕行最多30分钟，绕行比例最多35%"
)
TURN_2 = "重试一次，将第一天美术馆修改为清华美术馆"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _version_snapshot(connection: sqlite3.Connection, version_id: str) -> dict:
    row = connection.execute(
        "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["snapshot_json"])


def _segments(snapshot: dict) -> list[tuple[dict, dict]]:
    return [(day, segment) for day in snapshot.get("days") or [] for segment in day.get("segments") or []]


def _day_one_museum(snapshot: dict) -> tuple[dict, dict]:
    matches = [
        (day, segment)
        for day, segment in _segments(snapshot)
        if day.get("dayNumber") == 1
        and (
            "goalId=goal_museum" in str(segment.get("notes") or "")
            or str((segment.get("poi") or {}).get("category") or "") == "museum"
        )
    ]
    assert len(matches) == 1, [
        (day.get("dayNumber"), segment.get("id"), (segment.get("poi") or {}).get("name")) for day, segment in matches
    ]
    return matches[0]


def _duration_minutes(segment: dict) -> int:
    start_hour, start_minute = (int(item) for item in segment["startTime"].split(":"))
    end_hour, end_minute = (int(item) for item in segment["endTime"].split(":"))
    return (end_hour * 60 + end_minute) - (start_hour * 60 + start_minute)


def test_p0_exact_two_turn_runtime_journey_persists_one_verified_museum_patch(monkeypatch, tmp_path):
    database_path = tmp_path / "p0-golden.sqlite3"
    state_dir = tmp_path / "runs"
    database_url = f"sqlite:///{database_path.as_posix()}"
    mock_map_provider = {
        "enabled": True,
        "trustedPoiFixtures": True,
        "trustedFoodForDeterministicEdit": True,
        "mockRouteRefresh": True,
        "reactMultiturnMuseumEdit": True,
        "exactPoiFixtures": [
            {
                "city": "北京",
                "name": "海棠风味馆",
                "type": "餐饮服务;中餐厅;地方菜",
                "category": "food",
                "matchKeywords": ["当地特色美食", "地方风味餐厅"],
            }
        ],
    }

    assert not database_path.exists()
    assert not state_dir.exists()

    with monkeypatch.context() as scoped:
        scoped.setenv("DATABASE_URL", database_url)
        scoped.setenv("PROVIDER_MODE", "mock")
        scoped.setenv("MAP_PROVIDER_KEY", "recorded-runtime-fixture")
        get_settings.cache_clear()
        clear_map_poi_runtime_state()
        initialize_database()
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            runtime = TripAgentRuntime(connection)
            turn_1, turn_1_exit = runtime.run_once(
                RuntimeRunOptions(
                    input=TURN_1,
                    city="北京",
                    stateDir=str(state_dir / "turn_1"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider=mock_map_provider,
                ),
                argv=["trip-agent", "run", "--mock-providers", "--turn", "1"],
            )

            assert turn_1_exit == 0
            assert turn_1.active_version_changed is True
            assert turn_1.active_version_id
            assert turn_1.session_id
            session_id = turn_1.session_id
            plan_id = turn_1.active_plan_id
            before_version_id = turn_1.active_version_id
            before_snapshot = _version_snapshot(connection, before_version_id)
            assert [day["date"] for day in before_snapshot["days"]] == [
                "2026-10-01",
                "2026-10-02",
            ]
            assert [day["dayNumber"] for day in before_snapshot["days"]] == [1, 2]
            _before_day, before_museum = _day_one_museum(before_snapshot)
            before_museum_id = before_museum["id"]
            before_duration = _duration_minutes(before_museum)
            before_other_segments = {
                segment["id"]: segment
                for _day, segment in _segments(before_snapshot)
                if segment["id"] != before_museum_id
            }
            versions_before = connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            patches_before = connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

            turn_2, turn_2_exit = runtime.run_once(
                RuntimeRunOptions(
                    input=TURN_2,
                    city="北京",
                    sessionId=session_id,
                    stateDir=str(state_dir / "turn_2"),
                    json=True,
                    mockProviders=True,
                    mockMapProvider=mock_map_provider,
                ),
                argv=["trip-agent", "run", "--mock-providers", "--turn", "2"],
            )

            assert turn_2_exit == 0, turn_2
            assert turn_2.status == "success"
            assert turn_2.terminal_status == "success"
            assert turn_2.status != "needs_confirmation"
            assert turn_2.active_version_changed is True
            assert turn_2.active_version_id
            assert turn_2.active_version_id != before_version_id

            versions_after = connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            patches_after = connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            assert versions_after - versions_before == 1
            assert patches_after - patches_before == 1

            patch_row = connection.execute(
                "SELECT id, base_version_id, result_version_id, validation_status, operations_json "
                "FROM itinerary_patches WHERE session_id = ? AND result_version_id = ?",
                (session_id, turn_2.active_version_id),
            ).fetchone()
            assert patch_row is not None
            assert patch_row["base_version_id"] == before_version_id
            assert patch_row["result_version_id"] == turn_2.active_version_id
            assert patch_row["validation_status"] == "accepted"
            operations = json.loads(patch_row["operations_json"])
            assert len(operations) == 2
            assert operations[0]["op"] == "replace_segment_poi_from_candidate"
            assert operations[0]["segmentId"] == before_museum_id
            assert operations[1]["op"] == "replace_segment_duration"
            assert operations[1]["segmentId"] == before_museum_id
            assert operations[1]["durationMinutes"] == before_duration

            candidate_rows = connection.execute(
                "SELECT id, segment_id, status, selected_amap_id, candidates_json "
                "FROM amap_poi_candidates WHERE session_id = ? AND segment_id = ?",
                (session_id, before_museum_id),
            ).fetchall()
            selected_rows = [row for row in candidate_rows if row["selected_amap_id"] == "B0FIXTUREE5811EBA8D"]
            assert len(selected_rows) == 1
            assert any(
                item.get("name") == "清华大学艺术博物馆" for item in json.loads(selected_rows[0]["candidates_json"])
            )

            session_row = connection.execute(
                "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            assert session_row["active_version_id"] == turn_2.active_version_id
            after_snapshot = _version_snapshot(connection, turn_2.active_version_id)
            assert [day["date"] for day in after_snapshot["days"]] == [
                "2026-10-01",
                "2026-10-02",
            ]
            _after_day, after_museum = _day_one_museum(after_snapshot)
            assert after_museum["id"] == before_museum_id
            assert after_museum["poi"]["name"] == "清华大学艺术博物馆"
            assert after_museum["poi"]["amapId"] == "B0FIXTUREE5811EBA8D"
            assert _duration_minutes(after_museum) == before_duration
            after_other_segments = {
                segment["id"]: segment
                for _day, segment in _segments(after_snapshot)
                if segment["id"] != before_museum_id
            }
            assert after_other_segments == before_other_segments

            turn_2_artifact = Path(turn_2.artifact_path_absolute)
            decisions = _jsonl(turn_2_artifact / "agent_decisions.jsonl")
            observations = _jsonl(turn_2_artifact / "agent_observations.jsonl")
            planning_steps = _jsonl(turn_2_artifact / "planning_steps.jsonl")
            final_response = json.loads((turn_2_artifact / "final_response.json").read_text(encoding="utf-8"))
            artifact_snapshot = json.loads((turn_2_artifact / "itinerary_snapshot.json").read_text(encoding="utf-8"))

            assert [item["metadata"]["primaryAction"] for item in decisions] == [
                "resolve_poi",
                "patch_itinerary",
                "finish",
            ]
            assert all(item["providerName"] == "RuntimeStagedMapMockProvider" for item in decisions)
            assert [item["cycleIndex"] for item in observations] == [0, 1, 2]
            cycle_1 = observations[1]
            assert cycle_1["versionLineage"]["currentVersionId"] == before_version_id
            assert any(
                group.get("sourceSegmentId") == before_museum_id
                and group.get("selectedAmapId") == "B0FIXTUREE5811EBA8D"
                for group in cycle_1["candidateState"]["pendingGroups"]
            )
            cycle_2 = observations[2]
            assert cycle_2["versionLineage"]["currentVersionId"] == turn_2.active_version_id
            assert cycle_2["lastOutcome"]["status"] == "success"
            assert cycle_2["lastOutcome"]["patchIds"] == [patch_row["id"]]
            assert decisions[2]["metadata"]["postObservationDecision"] is True
            assert decisions[2]["metadata"]["actualExecutionRoute"] == "terminal_response"

            assert _jsonl(turn_2_artifact / "tool_events.jsonl") == []
            assert not any(
                item.get("type") == "generic_tool_loop"
                or str((item.get("metadata") or {}).get("toolName") or "")
                in {"web_search", "ticket_lookup", "amap_weather"}
                for item in planning_steps
            )
            action_outcomes = [item for item in planning_steps if item.get("type") == "agent_action_outcome"]
            assert action_outcomes
            for item in action_outcomes:
                external = (item.get("metadata") or {}).get("resultPreview", {}).get("externalCallCounts", {})
                assert int(external.get("webSearch") or 0) == 0
                assert int(external.get("ticketLookup") or 0) == 0
                assert int(external.get("amapWeather") or 0) == 0

            patch_outcomes = [
                item
                for item in action_outcomes
                if ((item.get("metadata") or {}).get("resultPreview") or {}).get("action") == "patch_itinerary"
            ]
            finish_outcomes = [
                item
                for item in action_outcomes
                if ((item.get("metadata") or {}).get("resultPreview") or {}).get("action") == "finish"
            ]
            assert len(patch_outcomes) == 1
            assert patch_outcomes[0]["metadata"]["resultPreview"]["patchIds"] == [patch_row["id"]]
            assert patch_outcomes[0]["metadata"]["resultPreview"]["resultVersionId"] == turn_2.active_version_id
            assert len(finish_outcomes) == 1
            finish_preview = finish_outcomes[0]["metadata"]["resultPreview"]
            assert finish_outcomes[0]["category"] == "internal"
            assert finish_preview["resultVersionId"] is None
            assert finish_preview["patchIds"] == []
            assert finish_preview["observedActiveVersionId"] == turn_2.active_version_id
            assert finish_preview["rollbackPerformed"] is False
            assert finish_preview["verifier"] == {
                "passed": None,
                "notApplicable": True,
                "reason": "no_itinerary_write",
            }

            assert final_response["status"] == "success"
            assert final_response["terminalStatus"] == "success"
            assert final_response["activeVersionChanged"] is True
            assert final_response["activeVersionId"] == turn_2.active_version_id
            assert artifact_snapshot == after_snapshot
            assert final_response["assistantReply"] == (
                "已将第一天美术馆替换为清华大学艺术博物馆，原停留时长保持不变。修改已写入持久化版本并通过校验。"
            )
            assert plan_id == turn_2.active_plan_id
        finally:
            connection.close()
            clear_map_poi_runtime_state()

    get_settings.cache_clear()
