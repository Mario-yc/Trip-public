import os
from pathlib import Path

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.services.deepseek_agent_provider import _DEEPSEEK_CIRCUIT_BREAKER
from src.services.map_poi_service import clear_map_poi_runtime_state
from src.services import image_cleanup_service, source_material_service


REAL_DB_PATH = Path(__file__).resolve().parents[2] / "trip_demo.db"


@pytest.fixture(autouse=True)
def isolated_test_database(tmp_path, monkeypatch):
    test_db_path = tmp_path / "trip_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db_path}")
    monkeypatch.setenv("PROVIDER_MODE", "mock")
    # Feature rollouts must be opt-in in generic tests; Portfolio suites enable it explicitly.
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "false")
    # Local manual-acceptance profiles must not leak from backend/.env into the
    # generic suite.  Simple Direction tests opt in explicitly; the product
    # default and the broad regression baseline remain strict Portfolio.
    monkeypatch.setenv("AGENT_INITIAL_PLANNING_MODE", "strict_portfolio")
    # Generic regression tests keep the deployed legacy authority unless the
    # state-aware router test opts into a rollout mode explicitly.
    monkeypatch.setenv("AGENT_INTENT_ROUTING_MODE", "legacy-only")
    monkeypatch.setenv("DEFAULT_USER_ID", "test-user")
    # Keep broad service tests deterministic: product default is free DuckDuckGo,
    # but tests that do not inject a search fake should exercise the no-mock
    # failure path without making live network calls.
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "bocha")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_MODE", "single")
    # Upload contract tests must never write to the shared project cache.  A
    # full gate can overlap a running local app or antivirus scanner on
    # Windows, so bind both writer and cleanup reader to this test's tmp root.
    monkeypatch.setattr(source_material_service, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(image_cleanup_service, "PROJECT_ROOT", tmp_path)

    for key in (
        "DEEPSEEK_API_KEY",
        "MAP_PROVIDER_KEY",
        "AMAP_WEB_SERVICE_KEY",
        "AMAP_JS_API_KEY",
        "MAP_JS_API_KEY",
        "MAP_PROVIDER_SECURITY_JS_CODE",
        "AMAP_SECURITY_JS_CODE",
        "WEB_SEARCH_API_KEY",
        "SEARCH_PROVIDER_KEY",
        "TICKET_PROVIDER_KEY",
        "WEATHER_PROVIDER_KEY",
    ):
        monkeypatch.setenv(key, "")

    get_settings.cache_clear()
    clear_map_poi_runtime_state()
    _DEEPSEEK_CIRCUIT_BREAKER._state.clear()
    resolved_db_path = sqlite_path_from_url(get_settings().database_url)
    if resolved_db_path == REAL_DB_PATH:
        raise RuntimeError("Tests must not use the real trip_demo.db database")

    initialize_database()
    try:
        yield
    finally:
        clear_map_poi_runtime_state()
        _DEEPSEEK_CIRCUIT_BREAKER._state.clear()
        get_settings.cache_clear()
        if os.environ.get("DATABASE_URL", "").endswith("trip_demo.db"):
            raise RuntimeError("Test DATABASE_URL unexpectedly points at trip_demo.db")
