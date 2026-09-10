"""HTTP/stream identity regression; deterministic support, not live acceptance."""
import json
import sqlite3

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.main import app


def counts():
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("conversation_turns", "planning_runs", "agent_choice_executions",
                              "agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options")}


def test_message_then_stream_replay_is_zero_execution_and_changed_payload_conflicts(monkeypatch):
    monkeypatch.setenv("AGENT_INTENT_ROUTING_MODE", "active-all")
    get_settings.cache_clear()
    with TestClient(app) as client:
        session = client.post("/api/agent/sessions", json={"city": "北京市"}).json()["sessionId"]
        url = f"/api/agent/sessions/{session}/messages"
        payload = {"content": "为什么还没有生成行程？", "requestId": "readonly-replay-one", "context": {}}
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text[:500]
        before = counts()
        replay = client.post(url, json=payload)
        assert replay.status_code == 200, replay.text[:500]
        assert replay.json()["requestReplay"] == {"replayed": True, "isHistorical": False}
        assert replay.json()["assistantTurn"]["id"] == first.json()["assistantTurn"]["id"]
        stream = client.post(url + "/stream", json=payload)
        assert stream.status_code == 200
        events = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
        assert any(item.get("event") == "message_response" for item in events)
        assert counts() == before
        conflict = client.post(url, json={**payload, "content": "生成另一个行程"})
        assert conflict.status_code == 409
        assert "request_id_payload_conflict" in conflict.text
        assert counts() == before
