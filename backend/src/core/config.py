from functools import lru_cache
from os import getenv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from dotenv import load_dotenv


def _load_env_files() -> None:
    project_root = Path(__file__).resolve().parents[3]
    candidates = [
        Path.cwd() / ".env",
        project_root / ".env",
        project_root / "backend" / ".env",
    ]
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)


def first_non_empty_env(*names: str, default: str = "") -> str:
    for name in names:
        value = getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


_load_env_files()


ProviderMode = Literal["default", "mock"]
AgentInitialPlanningMode = Literal["strict_portfolio", "simple_open_v1"]
AgentIntentRoutingMode = Literal["legacy-only", "shadow", "active-read", "active-all", "kill-switch"]
NamedBoundaryProviderMode = Literal["disabled", "osm_overpass"]
DailyRouteOverlapPolicy = Literal["observe", "rank"]


class Settings(BaseModel):
    app_name: str = Field(default="去哪玩AI 行程规划师")
    app_env: str = Field(default="development")
    database_url: str = Field(default="sqlite:///./trip_demo.db")
    default_user_id: str = Field(default="local-demo-user")
    provider_mode: ProviderMode = Field(default="mock")
    frontend_origin: str = Field(default="http://localhost:5173")

    deepseek_api_key: str = Field(default="")
    deepseek_model: str = Field(default="deepseek-v4-flash")
    deepseek_timeout_seconds: float = Field(default=30.0)
    agent_controller_decision_timeout_seconds: float = Field(default=10.0, ge=1.0, le=20.0)
    agent_controller_lite_timeout_seconds: float = Field(default=2.5, ge=0.5, le=5.0)
    agent_controller_total_budget_seconds: float = Field(default=12.5, ge=1.5, le=25.0)
    agent_creative_portfolio_enabled: bool = Field(default=False)
    agent_creative_portfolio_target_count: int = Field(default=4, ge=1, le=4)
    # Root-wide limits are separate from the bounded size of one provider call.
    agent_creative_portfolio_initial_batch_size: int = Field(default=3, ge=1, le=6)
    agent_creative_portfolio_max_visible_proposals: int = Field(default=6, ge=1, le=12)
    agent_creative_portfolio_max_generated_proposals: int = Field(default=12, ge=1, le=24)
    agent_creative_portfolio_max_continuation_rounds: int = Field(default=6, ge=1, le=12)
    agent_experience_grounding_v2_mode: Literal["off", "shadow", "enforce"] = Field(default="enforce")
    agent_soft_slot_draft_adoption_enabled: bool = Field(default=True)
    agent_creative_output_quality_v2_mode: Literal["off", "shadow", "enforce"] = Field(default="off")
    # The execution profile is server-owned.  Client context and model output
    # are deliberately unable to select the initial itinerary pipeline.
    agent_initial_planning_mode: AgentInitialPlanningMode = Field(default="strict_portfolio")
    # Normal text uses server-scoped semantic actions by default. Shadow and
    # legacy-only remain explicit rollback modes, never an implicit authority.
    agent_intent_routing_mode: AgentIntentRoutingMode = Field(default="active-all")
    # Request-contract defaults for an explicitly confirmed compact-route
    # choice. They are versioned product policy, not city-specific geography,
    # and are frozen into each request contract.
    simple_open_compact_radius_meters: float = Field(default=5000.0, ge=100.0, le=50000.0)
    simple_open_max_transit_leg_minutes: float = Field(default=45.0, ge=1.0, le=480.0)
    # A user-selected balanced/flexible detour envelope still needs a product
    # safety floor.  It is intentionally looser than the explicit compact
    # choice and never rewrites the selected detour tolerance.
    simple_open_quality_floor_radius_meters: float = Field(default=8000.0, ge=100.0, le=50000.0)
    simple_open_quality_floor_max_transit_leg_minutes: float = Field(default=60.0, ge=1.0, le=480.0)
    simple_open_max_backtrack_ratio: float = Field(default=0.15, ge=0.0, le=1.0)
    simple_open_compact_detour_ratio_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    # Two three-anchor days require four mandatory adjacent Provider pairs. A
    # hard cap of eight reserves exactly one additional two-leg topology per
    # incomplete day while keeping exploration bounded and coverage-first.
    simple_open_route_provider_budget: int = Field(default=8, ge=1, le=8)
    simple_open_max_pages_per_query: int = Field(default=3, ge=1, le=10)
    daily_route_overlap_policy: DailyRouteOverlapPolicy = Field(default="observe")
    named_boundary_provider: NamedBoundaryProviderMode = Field(default="osm_overpass")
    named_boundary_overpass_url: str = Field(default="https://overpass-api.de/api/interpreter")
    named_boundary_timeout_seconds: float = Field(default=8.0, gt=0.0, le=30.0)
    named_boundary_simplification_max_deviation_meters: float = Field(default=50.0, gt=0.0, le=500.0)
    deepseek_base_url: str = Field(default="https://api.deepseek.com")
    deepseek_tool_strict_mode: bool = Field(default=False)
    deepseek_thinking_mode: Literal["enabled", "disabled", "auto"] = Field(default="auto")
    deepseek_reasoning_effort: str = Field(default="high")
    map_provider_key: str = Field(default="")
    map_js_api_key: str = Field(default="")
    map_provider_security_js_code: str = Field(default="")
    ticket_provider_key: str = Field(default="")
    visit_facts_max_age_hours: int = Field(default=24, ge=1, le=168)
    social_source_max_age_hours: int = Field(default=24, ge=1, le=168)
    weather_provider_key: str = Field(default="")
    search_provider_key: str = Field(default="")
    bocha_api_key: str = Field(default="")
    web_search_provider: str = Field(default="multi-free")
    web_search_provider_mode: str = Field(default="chain")
    web_search_provider_chain: str = Field(default="anysearch,ddgs,bing,duckduckgo,baidu,multi-free")
    tavily_api_key: str = Field(default="")
    anysearch_api_key: str = Field(default="")
    anysearch_domain: str = Field(default="travel")
    anysearch_tag: str = Field(default="")
    anysearch_zone: str = Field(default="cn")
    anysearch_language: str = Field(default="zh-CN")
    anysearch_proxy_mode: Literal["auto", "system", "direct"] = Field(default="auto")
    anysearch_timeout_seconds: float = Field(default=6.0, gt=0.0, le=60.0)
    brave_search_api_key: str = Field(default="")
    searxng_base_url: str = Field(default="")
    ddgs_api_base_url: str = Field(default="")
    google_cse_api_key: str = Field(default="")
    google_cse_cx: str = Field(default="")
    duckduckgo_search_base_url: str = Field(default="")
    duckduckgo_html_search_url: str = Field(default="")
    duckduckgo_lite_search_url: str = Field(default="")
    web_search_proxy_url: str = Field(default="")
    duckduckgo_proxy_url: str = Field(default="")
    baidu_proxy_url: str = Field(default="")
    bing_proxy_url: str = Field(default="")
    web_search_min_accepted_results: int = Field(default=1)
    web_search_provider_timeout_seconds: float = Field(default=6.0)
    web_search_max_provider_attempts: int = Field(default=6)
    web_search_chain_deadline_seconds: float = Field(default=20.0, gt=0.0, le=60.0)
    provider_timeout_seconds: float = Field(default=5.0)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=getenv("APP_NAME", "去哪玩AI 行程规划师"),
        app_env=getenv("APP_ENV", "development"),
        database_url=getenv("DATABASE_URL", "sqlite:///./trip_demo.db"),
        default_user_id=getenv("DEFAULT_USER_ID", "local-demo-user"),
        provider_mode=getenv("PROVIDER_MODE", "mock"),  # type: ignore[arg-type]
        frontend_origin=getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        deepseek_api_key=getenv("DEEPSEEK_API_KEY", ""),
        deepseek_model=getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        deepseek_timeout_seconds=float(getenv("DEEPSEEK_TIMEOUT_SECONDS", "30")),
        agent_controller_decision_timeout_seconds=float(getenv("AGENT_CONTROLLER_DECISION_TIMEOUT_SECONDS", "10")),
        agent_controller_lite_timeout_seconds=float(getenv("AGENT_CONTROLLER_LITE_TIMEOUT_SECONDS", "2.5")),
        agent_controller_total_budget_seconds=float(getenv("AGENT_CONTROLLER_TOTAL_BUDGET_SECONDS", "12.5")),
        agent_creative_portfolio_enabled=getenv("AGENT_CREATIVE_PORTFOLIO_ENABLED", "false").lower()
        in {"1", "true", "yes", "on"},
        agent_creative_portfolio_target_count=max(1, min(4, int(getenv("AGENT_CREATIVE_PORTFOLIO_TARGET_COUNT", "4")))),
        agent_creative_portfolio_initial_batch_size=max(
            1, min(6, int(getenv("AGENT_CREATIVE_PORTFOLIO_INITIAL_BATCH_SIZE", "3")))
        ),
        agent_creative_portfolio_max_visible_proposals=max(
            1, min(12, int(getenv("AGENT_CREATIVE_PORTFOLIO_MAX_VISIBLE_PROPOSALS", "6")))
        ),
        agent_creative_portfolio_max_generated_proposals=max(
            1, min(24, int(getenv("AGENT_CREATIVE_PORTFOLIO_MAX_GENERATED_PROPOSALS", "12")))
        ),
        agent_creative_portfolio_max_continuation_rounds=max(
            1, min(12, int(getenv("AGENT_CREATIVE_PORTFOLIO_MAX_CONTINUATION_ROUNDS", "6")))
        ),
        agent_experience_grounding_v2_mode=getenv("AGENT_EXPERIENCE_GROUNDING_V2_MODE", "enforce"),  # type: ignore[arg-type]
        agent_soft_slot_draft_adoption_enabled=getenv("AGENT_SOFT_SLOT_DRAFT_ADOPTION_ENABLED", "true").lower()
        in {"1", "true", "yes", "on"},
        agent_creative_output_quality_v2_mode=getenv("AGENT_CREATIVE_OUTPUT_QUALITY_V2_MODE", "off"),  # type: ignore[arg-type]
        agent_initial_planning_mode=getenv("AGENT_INITIAL_PLANNING_MODE", "strict_portfolio"),  # type: ignore[arg-type]
        agent_intent_routing_mode=getenv("AGENT_INTENT_ROUTING_MODE", "active-all"),  # type: ignore[arg-type]
        simple_open_compact_radius_meters=float(getenv("SIMPLE_OPEN_COMPACT_RADIUS_METERS", "5000")),
        simple_open_max_transit_leg_minutes=float(getenv("SIMPLE_OPEN_MAX_TRANSIT_LEG_MINUTES", "45")),
        simple_open_quality_floor_radius_meters=float(
            getenv("SIMPLE_OPEN_QUALITY_FLOOR_RADIUS_METERS", "8000")
        ),
        simple_open_quality_floor_max_transit_leg_minutes=float(
            getenv("SIMPLE_OPEN_QUALITY_FLOOR_MAX_TRANSIT_LEG_MINUTES", "60")
        ),
        simple_open_max_backtrack_ratio=float(getenv("SIMPLE_OPEN_MAX_BACKTRACK_RATIO", "0.15")),
        simple_open_compact_detour_ratio_threshold=float(getenv("SIMPLE_OPEN_COMPACT_DETOUR_RATIO_THRESHOLD", "0.15")),
        simple_open_route_provider_budget=int(getenv("SIMPLE_OPEN_ROUTE_PROVIDER_BUDGET", "8")),
        simple_open_max_pages_per_query=int(getenv("SIMPLE_OPEN_MAX_PAGES_PER_QUERY", "3")),
        daily_route_overlap_policy=getenv("DAILY_ROUTE_OVERLAP_POLICY", "observe"),  # type: ignore[arg-type]
        named_boundary_provider=getenv("NAMED_BOUNDARY_PROVIDER", "osm_overpass"),  # type: ignore[arg-type]
        named_boundary_overpass_url=getenv("NAMED_BOUNDARY_OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
        named_boundary_timeout_seconds=float(getenv("NAMED_BOUNDARY_TIMEOUT_SECONDS", "8")),
        named_boundary_simplification_max_deviation_meters=float(
            getenv("NAMED_BOUNDARY_SIMPLIFICATION_MAX_DEVIATION_METERS", "50")
        ),
        deepseek_base_url=getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_tool_strict_mode=getenv("DEEPSEEK_TOOL_STRICT_MODE", "false").lower() in {"1", "true", "yes", "on"},
        deepseek_thinking_mode=getenv("DEEPSEEK_THINKING_MODE", "auto"),  # type: ignore[arg-type]
        deepseek_reasoning_effort=getenv("DEEPSEEK_REASONING_EFFORT", "high"),
        map_provider_key=getenv("MAP_PROVIDER_KEY", ""),
        map_js_api_key=getenv("MAP_JS_API_KEY", getenv("AMAP_JS_API_KEY", getenv("MAP_PROVIDER_KEY", ""))),
        map_provider_security_js_code=getenv(
            "MAP_PROVIDER_SECURITY_JS_CODE",
            getenv("AMAP_SECURITY_JS_CODE", ""),
        ),
        ticket_provider_key=getenv("TICKET_PROVIDER_KEY", ""),
        visit_facts_max_age_hours=int(getenv("VISIT_FACTS_MAX_AGE_HOURS", "24")),
        social_source_max_age_hours=int(getenv("SOCIAL_SOURCE_MAX_AGE_HOURS", "24")),
        weather_provider_key=getenv(
            "WEATHER_PROVIDER_KEY", getenv("AMAP_WEB_SERVICE_KEY", getenv("MAP_PROVIDER_KEY", ""))
        ),
        search_provider_key=first_non_empty_env("WEB_SEARCH_API_KEY", "SEARCH_PROVIDER_KEY", "BOCHA_API_KEY"),
        bocha_api_key=first_non_empty_env("BOCHA_API_KEY", "WEB_SEARCH_API_KEY", "SEARCH_PROVIDER_KEY"),
        web_search_provider=getenv("WEB_SEARCH_PROVIDER", "multi-free"),
        web_search_provider_mode=getenv("WEB_SEARCH_PROVIDER_MODE", "chain"),
        web_search_provider_chain=getenv(
            "WEB_SEARCH_PROVIDER_CHAIN", "anysearch,ddgs,bing,duckduckgo,baidu,multi-free"
        ),
        tavily_api_key=getenv("TAVILY_API_KEY", ""),
        anysearch_api_key=getenv("ANYSEARCH_API_KEY", ""),
        anysearch_domain=getenv("ANYSEARCH_DOMAIN", "travel"),
        anysearch_tag=getenv("ANYSEARCH_TAG", ""),
        anysearch_zone=getenv("ANYSEARCH_ZONE", "cn"),
        anysearch_language=getenv("ANYSEARCH_LANGUAGE", "zh-CN"),
        anysearch_proxy_mode=getenv("ANYSEARCH_PROXY_MODE", "auto").strip().lower(),  # type: ignore[arg-type]
        anysearch_timeout_seconds=float(
            first_non_empty_env(
                "ANYSEARCH_TIMEOUT_SECONDS",
                default=getenv("WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS", "6"),
            )
        ),
        brave_search_api_key=getenv("BRAVE_SEARCH_API_KEY", ""),
        searxng_base_url=getenv("SEARXNG_BASE_URL", ""),
        ddgs_api_base_url=getenv("DDGS_API_BASE_URL", ""),
        google_cse_api_key=getenv("GOOGLE_CSE_API_KEY", ""),
        google_cse_cx=getenv("GOOGLE_CSE_CX", ""),
        duckduckgo_search_base_url=getenv("DUCKDUCKGO_SEARCH_BASE_URL", ""),
        duckduckgo_html_search_url=getenv("DUCKDUCKGO_HTML_SEARCH_URL", ""),
        duckduckgo_lite_search_url=getenv("DUCKDUCKGO_LITE_SEARCH_URL", ""),
        web_search_proxy_url=first_non_empty_env(
            "WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY", "WEB_SEARCH_HTTPS_PROXY"
        ),
        duckduckgo_proxy_url=first_non_empty_env(
            "DUCKDUCKGO_PROXY_URL",
            "DUCKDUCKGO_HTTP_PROXY",
            default=first_non_empty_env("WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY", "WEB_SEARCH_HTTPS_PROXY"),
        ),
        baidu_proxy_url=first_non_empty_env(
            "BAIDU_PROXY_URL",
            "BAIDU_HTTP_PROXY",
            default=first_non_empty_env("WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY", "WEB_SEARCH_HTTPS_PROXY"),
        ),
        bing_proxy_url=first_non_empty_env(
            "BING_PROXY_URL",
            "BING_HTTP_PROXY",
            default=first_non_empty_env("WEB_SEARCH_PROXY_URL", "WEB_SEARCH_HTTP_PROXY", "WEB_SEARCH_HTTPS_PROXY"),
        ),
        web_search_min_accepted_results=int(getenv("WEB_SEARCH_MIN_ACCEPTED_RESULTS", "1")),
        web_search_provider_timeout_seconds=float(getenv("WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS", "6")),
        web_search_max_provider_attempts=int(getenv("WEB_SEARCH_MAX_PROVIDER_ATTEMPTS", "6")),
        web_search_chain_deadline_seconds=float(getenv("WEB_SEARCH_CHAIN_DEADLINE_SECONDS", "20")),
        provider_timeout_seconds=float(getenv("PROVIDER_TIMEOUT_SECONDS", "5")),
    )
