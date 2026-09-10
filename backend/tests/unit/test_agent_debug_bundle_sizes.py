import json
import sqlite3

import pytest

from src.services.agent_debug_bundle_service import AgentDebugBundleService


@pytest.mark.parametrize("payload_bytes", [600_000, 1_000_000, 3_000_000])
def test_debug_bundle_never_truncates_large_conversation(payload_bytes: int):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE conversation_sessions (
          id TEXT PRIMARY KEY, active_plan_id TEXT, active_version_id TEXT
        );
        CREATE TABLE conversation_turns (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          turn_index INTEGER, status TEXT, agent_request_json TEXT,
          agent_response_json TEXT, error_json TEXT, created_at TEXT,
          updated_at TEXT
        );
        """
    )
    payload = "x" * payload_bytes
    db.execute("INSERT INTO conversation_sessions VALUES ('sess', NULL, NULL)")
    db.execute(
        "INSERT INTO conversation_turns VALUES "
        "('turn', 'sess', 'user', ?, 1, 'active', NULL, NULL, NULL, "
        "'2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')",
        (payload,),
    )

    bundle = AgentDebugBundleService(db).export(session_id="sess")
    encoded = json.dumps(bundle, ensure_ascii=False)

    assert bundle["sections"]["CONVERSATION_TURNS"][0]["content"] == payload
    assert bundle["sections"]["COMPLETENESS"]["truncated"] is False
    assert len(encoded.encode("utf-8")) >= payload_bytes
