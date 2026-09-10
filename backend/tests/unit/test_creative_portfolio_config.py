from src.core.config import get_settings


def test_creative_portfolio_feature_is_opt_in(monkeypatch):
    monkeypatch.delenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().agent_creative_portfolio_enabled is False
    monkeypatch.setenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().agent_creative_portfolio_enabled is True


def test_semantic_v2_feature_branch_runtime_config_is_enforced_and_bounded(monkeypatch):
    monkeypatch.delenv("AGENT_EXPERIENCE_GROUNDING_V2_MODE", raising=False)
    monkeypatch.delenv("AGENT_SOFT_SLOT_DRAFT_ADOPTION_ENABLED", raising=False)
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_CHAIN", raising=False)
    monkeypatch.delenv("WEB_SEARCH_MAX_PROVIDER_ATTEMPTS", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.agent_experience_grounding_v2_mode == "enforce"
    assert settings.agent_soft_slot_draft_adoption_enabled is True
    assert settings.web_search_provider_chain == "anysearch,ddgs,bing,duckduckgo,baidu,multi-free"
    assert settings.web_search_max_provider_attempts == 6

    monkeypatch.setenv("AGENT_EXPERIENCE_GROUNDING_V2_MODE", "off")
    monkeypatch.setenv("AGENT_SOFT_SLOT_DRAFT_ADOPTION_ENABLED", "false")
    get_settings.cache_clear()
    rollback = get_settings()
    assert rollback.agent_experience_grounding_v2_mode == "off"
    assert rollback.agent_soft_slot_draft_adoption_enabled is False


def test_simple_open_compact_route_policy_is_configured_and_capped(monkeypatch):
    for key in (
        "SIMPLE_OPEN_COMPACT_RADIUS_METERS",
        "SIMPLE_OPEN_COMPACT_DETOUR_RATIO_THRESHOLD",
        "SIMPLE_OPEN_MAX_TRANSIT_LEG_MINUTES",
        "SIMPLE_OPEN_MAX_BACKTRACK_RATIO",
        "SIMPLE_OPEN_ROUTE_PROVIDER_BUDGET",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    defaults = get_settings()
    assert defaults.simple_open_compact_radius_meters == 5000
    assert defaults.simple_open_max_transit_leg_minutes == 45
    assert defaults.simple_open_max_backtrack_ratio == 0.15
    assert defaults.simple_open_compact_detour_ratio_threshold == 0.15
    assert defaults.simple_open_route_provider_budget == 8

    monkeypatch.setenv("SIMPLE_OPEN_ROUTE_PROVIDER_BUDGET", "3")
    get_settings.cache_clear()
    assert get_settings().simple_open_route_provider_budget == 3
