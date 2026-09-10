import sqlite3

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.preference_service import PreferenceService


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


def open_db() -> sqlite3.Connection:
    db_path = sqlite_path_from_url(get_settings().database_url)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def test_preference_service_extracts_party_budget_and_pace():
    clear_database()
    with open_db() as connection:
        card = PreferenceService(connection).extract_from_text("两个大人一个老人，预算 3000 左右，不想太赶，少换乘")

    assert card.party_size == 3
    assert "adult" in card.traveler_types
    assert "elder" in card.traveler_types
    assert card.budget_range == "3000 左右"
    assert card.pace_preference == "轻松不赶路"
    assert "预算约 3000 左右" in card.summary_text
    assert "轻松不赶路" in card.summary_text
    assert card.profile_id.startswith("pref_")


def test_preference_service_updates_free_text_summary():
    clear_database()
    with open_db() as connection:
        card = PreferenceService(connection).extract_from_text("预算 3000，不想太赶")
        updated = PreferenceService(connection).update_card(card.id, summary_text="用户希望轻松不赶路，预算约 3500 元。")

    assert updated.summary_text == "用户希望轻松不赶路，预算约 3500 元。"
    assert updated.status == "revised"


def test_preference_service_does_not_invent_summary_without_explicit_preferences():
    clear_database()
    raw_text = "北京一天只去故宫"
    with open_db() as connection:
        card = PreferenceService(connection).extract_from_text(raw_text)

    assert card.summary_text != raw_text
    assert card.summary_text == ""
    assert card.party_size == 0
    assert card.traveler_types == []


def test_preference_memory_defaults_update_restore_and_auto_update():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        initial = service.get_memory()
        updated = service.update_memory(
            memory_text="# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n",
            auto_update_enabled=False,
        )
        unchanged = service.auto_update_memory_from_turn("我想多拍照，少换乘")
        restored = service.restore_default_memory()

    assert initial.memory_text.startswith("# 我的旅行偏好")
    assert initial.auto_update_enabled is True
    assert updated.auto_update_enabled is False
    assert "喜欢轻松不赶路" in updated.memory_text
    assert unchanged.memory_text == updated.memory_text
    assert restored.auto_update_enabled is True
    assert "## 需要确认" in restored.memory_text


def test_preference_memory_diagnostics_and_persistence_after_reopen():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        updated = service.update_memory(
            memory_text="# 我的旅行偏好\n\n## 交通偏好\n- 公共交通优先。\n",
            session_id="session_memory_restart",
        )

    with open_db() as connection:
        reloaded = PreferenceService(connection).get_memory(session_id="session_memory_restart")

    diagnostics = reloaded.memory_diagnostics
    assert reloaded.memory_text == updated.memory_text
    assert diagnostics["source"] == "session"
    assert diagnostics["sessionId"] == "session_memory_restart"
    assert diagnostics["frontendLoadedFrom"] == "api"
    assert diagnostics["dbPath"].endswith(".db")
    assert diagnostics["sessionMemoryUpdatedAt"]


def test_preference_memory_auto_update_adds_supported_explicit_preferences():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        service.get_memory()
        updated = service.auto_update_memory_from_turn("我们想轻松一点，公共交通少换乘，预算 3000 左右，喜欢拍照。")

    assert "轻松不赶路" in updated.memory_text
    assert "拍照" in updated.memory_text
    assert "少换乘" in updated.memory_text
    assert "3000" in updated.memory_text
    facts = updated.structured_memory["facts"]
    values = "\n".join(fact["value"] for fact in facts)
    assert "轻松不赶路" in values
    assert "公共交通" in values
    assert "拍照" in values
    assert updated.compiled_rules["pace"]["maxVisitSegmentsPerDay"] == 3
    assert updated.compiled_rules["routePlanning"]["preferredMode"] == "transit"
    assert updated.compiled_rules["mealHandling"]["pureMealLabelsAreNotPois"] is True
    assert updated.compiled_rules["riskChecks"]["checkCrowdingWeather"] is True


def test_preference_memory_auto_update_adds_trip_p0_preferences():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        updated = service.auto_update_memory_from_turn(
            "这次北京国庆高校两日游，想看城市夜景，中等预算，1人，公交地铁优先，路途中体验北京当地特色美食。",
            session_id="session_p0_preferences",
        )

    text = updated.memory_text
    assert "高校/校园参观" in text
    assert "夜景/城市观景" in text
    assert "中等预算" in text
    assert "1 人" in text
    assert "当地特色美食" in text
    assert "公共交通" in text
    values = "\n".join(fact["value"] for fact in updated.structured_memory["facts"])
    assert "高校/校园" in values
    assert "夜景/城市观景" in values
    assert "中等预算" in values
    assert "1 人" in values
    assert "当地特色美食" in values
    assert updated.compiled_rules["routePlanning"]["preferredMode"] == "transit"
    assert "campus" in updated.compiled_rules["poiSelection"]["preferredThemes"]
    assert "night_view" in updated.compiled_rules["poiSelection"]["preferredThemes"]


def test_preference_memory_auto_update_does_not_persist_assistant_option_examples():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        service.get_memory()
        updated = service.auto_update_memory_from_turn("需要确认：是否有老人、亲子或儿童同行？")

    assert "老人" not in updated.memory_text
    assert "亲子" not in updated.memory_text
    assert "儿童" not in updated.memory_text


def test_preference_memory_classifies_scope_inferred_and_needs_confirmation():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        global_memory = service.auto_update_memory_from_turn("我以后都喜欢校园参观，公共交通少换乘。")
        session_memory = service.get_memory(session_id="session_seeded")
        trip_memory = service.auto_update_memory_from_turn(
            "这次可能有老人同行，也想找美食餐厅。",
            session_id="session_seeded",
        )

    assert any(fact["scope"] == "global" and fact["key"] == "interest.campus" for fact in global_memory.structured_memory["facts"])
    assert any(fact["key"] == "interest.campus" for fact in session_memory.structured_memory["facts"])
    assert any(fact["scope"] == "global" for fact in session_memory.structured_memory["facts"])
    classifications = trip_memory.structured_memory["autoUpdateClassifications"]
    assert any(item["classification"] == "needs_confirmation" for item in classifications)
    assert any(item["classification"] == "inferred" for item in classifications)
    assert trip_memory.pending_confirmations
    assert trip_memory.compiled_rules["poiSelection"]["preferredThemes"] == ["campus"]
    assert trip_memory.compiled_rules["riskChecks"]["travelerSensitivity"] == "normal"


def test_trip_scoped_likes_do_not_become_global_memory():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        session_memory = service.auto_update_memory_from_turn("这次我喜欢校园参观，公共交通少换乘。", session_id="session_trip")
        global_memory = service.get_memory()

    assert any(fact["scope"] == "trip" and fact["key"] == "interest.campus" for fact in session_memory.structured_memory["facts"])
    assert not any(fact["key"] == "interest.campus" for fact in global_memory.structured_memory["facts"])


def test_session_auto_update_promotes_global_facts_for_future_session_seed():
    clear_database()
    with open_db() as connection:
        service = PreferenceService(connection)
        updated = service.auto_update_memory_from_turn("我以后都喜欢校园参观，公共交通少换乘。", session_id="session_a")
        global_memory = service.get_memory()
        seeded = service.get_memory(session_id="session_b")

    assert any(fact["scope"] == "global" and fact["key"] == "interest.campus" for fact in updated.structured_memory["facts"])
    assert any(fact["scope"] == "global" and fact["key"] == "interest.campus" for fact in global_memory.structured_memory["facts"])
    assert any(fact["scope"] == "global" and fact["key"] == "interest.campus" for fact in seeded.structured_memory["facts"])
    assert seeded.compiled_rules["poiSelection"]["preferredThemes"] == ["campus"]


def test_effective_memory_text_filters_default_template_placeholders():
    clear_database()
    with open_db() as connection:
        memory = PreferenceService(connection).get_memory()

    assert PreferenceService.effective_memory_text(memory.memory_text) == ""


def test_effective_memory_text_returns_only_actual_user_preferences():
    memory_text = """# 我的旅行偏好

## 旅行节奏
- 暂无明确记录。
- 喜欢轻松不赶路。

## 交通偏好
- 公共交通优先。

## 需要确认
- 暂无明确记录。
"""

    assert PreferenceService.effective_memory_text(memory_text) == "旅行节奏：喜欢轻松不赶路。\n交通偏好：公共交通优先。"
