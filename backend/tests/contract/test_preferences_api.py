import sqlite3

from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.main import app


def clear_database() -> None:
    initialize_database()
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DELETE FROM preference_summary_cards;
            DELETE FROM preference_profiles;
            DELETE FROM session_preference_memories;
            DELETE FROM travel_preference_memories;
            """
        )


def test_preference_extract_and_update_contract():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/preferences/extract",
            json={"conversationText": "两个大人一个老人，预算 3000 左右，不想太赶"},
        )
        card_id = response.json()["summaryCard"]["id"]
        update_response = client.patch(
            f"/api/preferences/{card_id}",
            json={"partySize": 2, "budgetRange": "2500 左右", "pacePreference": "紧凑高效"},
        )

    assert response.status_code == 200
    card = response.json()["summaryCard"]
    assert card["partySize"] == 3
    assert "elder" in card["travelerTypes"]
    assert card["budgetRange"] == "3000 左右"
    assert "轻松不赶路" in card["summaryText"]
    assert update_response.status_code == 200
    updated = update_response.json()["summaryCard"]
    assert updated["partySize"] == 2
    assert updated["budgetRange"] == "2500 左右"
    assert updated["pacePreference"] == "紧凑高效"


def test_preference_update_accepts_summary_text():
    clear_database()
    with TestClient(app) as client:
        response = client.post(
            "/api/preferences/extract",
            json={"conversationText": "预算 3000 左右，不想太赶"},
        )
        card_id = response.json()["summaryCard"]["id"]
        update_response = client.patch(
            f"/api/preferences/{card_id}",
            json={"summaryText": "用户偏好轻松不赶路，预算约 3500 元，公共交通优先。"},
        )

    assert update_response.status_code == 200
    assert update_response.json()["summaryCard"]["summaryText"] == "用户偏好轻松不赶路，预算约 3500 元，公共交通优先。"


def test_preference_memory_markdown_crud_and_restore_default():
    clear_database()
    with TestClient(app) as client:
        initial = client.get("/api/preferences/memory")
        updated = client.patch(
            "/api/preferences/memory",
            json={
                "memoryText": "# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n",
                "autoUpdateEnabled": False,
            },
        )
        restored = client.post("/api/preferences/memory/restore-default")

    assert initial.status_code == 200
    assert initial.json()["memoryText"].startswith("# 我的旅行偏好")
    assert initial.json()["autoUpdateEnabled"] is True
    assert updated.status_code == 200
    assert updated.json()["memoryText"].endswith("喜欢轻松不赶路。\n")
    assert updated.json()["autoUpdateEnabled"] is False
    assert restored.status_code == 200
    assert "## 需要确认" in restored.json()["memoryText"]
    assert restored.json()["autoUpdateEnabled"] is True


def test_preference_memory_api_returns_diagnostics_for_session_memory():
    clear_database()
    with TestClient(app) as client:
        updated = client.patch(
            "/api/preferences/memory?sessionId=session_contract_memory",
            json={
                "memoryText": "# 我的旅行偏好\n\n## 交通偏好\n- 公共交通优先。\n",
                "autoUpdateEnabled": True,
            },
        )
        reloaded = client.get("/api/preferences/memory?sessionId=session_contract_memory")

    assert updated.status_code == 200
    assert reloaded.status_code == 200
    body = reloaded.json()
    diagnostics = body["memoryDiagnostics"]
    assert body["memoryText"].endswith("公共交通优先。\n")
    assert diagnostics["source"] == "session"
    assert diagnostics["sessionId"] == "session_contract_memory"
    assert diagnostics["frontendLoadedFrom"] == "api"
    assert diagnostics["dbPath"].endswith(".db")
    assert diagnostics["sessionMemoryUpdatedAt"]
