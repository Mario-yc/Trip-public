import json
import sqlite3

from src.services.agent_debug_bundle_service import AgentDebugBundleService


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE conversation_sessions (id TEXT PRIMARY KEY, active_plan_id TEXT, active_version_id TEXT);
        CREATE TABLE conversation_turns (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, turn_index INTEGER,
          status TEXT, agent_request_json TEXT, agent_response_json TEXT, error_json TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE planning_runs (id TEXT PRIMARY KEY, session_id TEXT, trace_json TEXT, created_at TEXT);
        """
    )
    return db


def test_lossless_bundle_keeps_all_turn_states_and_deduplicates_events():
    db = _db()
    db.execute("INSERT INTO conversation_sessions VALUES ('sess', NULL, NULL)")
    large = "完整正文" * 200_000
    event = {"id": "event_1", "type": "tool", "metadata": {"Authorization": "Bearer leaked"}}
    for index, status in enumerate(("active", "failed", "superseded"), start=1):
        db.execute(
            "INSERT INTO conversation_turns VALUES (?, 'sess', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"turn_{index}",
                "user" if index == 1 else "assistant",
                large if index == 1 else f"content_{index}",
                index,
                status,
                json.dumps({"apiKey": "secret", "path": r"C:\\Users\\Alice\\trace.json"}),
                json.dumps({"planningSteps": [event], "toolEvents": [event]}),
                json.dumps({"message": "failure"}) if status == "failed" else None,
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
            ),
        )
    db.commit()

    bundle = AgentDebugBundleService(db).export(session_id="sess")
    encoded = json.dumps(bundle, ensure_ascii=False)
    completeness = bundle["sections"]["COMPLETENESS"]

    assert bundle["schemaVersion"] == "trip-debug-bundle-v4"
    assert completeness["truncated"] is False
    assert completeness["expected"]["conversationTurnCount"] == 3
    assert completeness["exported"] == completeness["expected"]
    assert [turn["status"] for turn in bundle["sections"]["CONVERSATION_TURNS"]] == ["active", "failed", "superseded"]
    assert large in encoded
    assert list(bundle["sections"]["EVENT_STORE"]) == ["event_1"]
    response_value = bundle["sections"]["AGENT_RESPONSES"][0]["value"]
    assert response_value["planningSteps"] == {
        "eventIds": ["event_1"],
        "eventStoreRef": "EVENT_STORE",
    }
    assert "metadata" not in response_value["planningSteps"]
    assert "secret" not in encoded
    assert "Alice" not in encoded
    assert "leaked" not in encoded
    assert len(encoded.encode("utf-8")) > 1_000_000


def test_redaction_covers_url_credentials_and_all_local_absolute_paths():
    service = AgentDebugBundleService(_db())
    value = service._redact(
        {
            "url": "https://x.test/?api_key=TOPSECRET&access_token=LEAK&sig=SIGNATURE#token=FRAGMENT",
            "canonical_signature": "STRUCTURAL_HASH",
            "transportLabel": "公交/地铁",
            "routeNarrative": "公交/地铁/换乘/步行",
            "paths": [
                r"E:\items\trip\trace.json",
                r"C:\tmp\trace.json",
                "E:/items/trip/trace.json",
                "C:/tmp/trace.json",
                "/tmp/agent/trace.json",
                "/var/log/trip.log",
                "/workspace/project/trace.json",
                "/data/trip/trace.json",
                "/root/.cache/trace.json",
                r"\\server\share\trace.json",
                "//server/share/trace.json",
            ],
        }
    )
    encoded = json.dumps(value, ensure_ascii=False)

    for secret in (
        "TOPSECRET",
        "LEAK",
        "SIGNATURE",
        "FRAGMENT",
        "E:\\items",
        "C:\\tmp",
        "E:/items",
        "C:/tmp",
        "/tmp/",
        "/data/",
        "/var/",
        "/workspace/",
        "/root/",
        "server",
    ):
        assert secret not in encoded
    assert encoded.count("[LOCAL_PATH]") == 11
    assert value["routeNarrative"] == "公交/地铁/换乘/步行"
    assert value["canonical_signature"] == "STRUCTURAL_HASH"
    assert value["transportLabel"] == "公交/地铁"
