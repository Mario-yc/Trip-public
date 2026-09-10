from fastapi.testclient import TestClient

from backend.tests.contract.test_agent_sessions_api import clear_database
from src.main import app


def test_export_agent_debug_bundle_uses_authoritative_session_schema():
    clear_database()
    with TestClient(app) as client:
        session = client.post(
            "/api/agent/sessions",
            json={"city": "北京", "title": "lossless debug export"},
        ).json()
        response = client.get(f"/api/agent/sessions/{session['sessionId']}/debug-bundle")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment;" in response.headers["content-disposition"]
    bundle = response.json()
    assert bundle["schemaVersion"] == "trip-debug-bundle-v4"
    completeness = bundle["sections"]["COMPLETENESS"]
    assert completeness["truncated"] is False
    assert completeness["captureSource"] == "server_authoritative"
    assert completeness["expected"] == completeness["exported"]
    assert completeness["sections"]["COMPLETENESS"]["status"] == "complete"


def test_export_agent_debug_bundle_returns_404_for_unknown_session():
    clear_database()
    with TestClient(app) as client:
        response = client.get("/api/agent/sessions/missing/debug-bundle")

    assert response.status_code == 404
    assert response.json()["detail"] == "agent_session_not_found"
