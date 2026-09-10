from fastapi import APIRouter

from src.core.config import get_settings
from src.providers.base.results import ProviderKind
from src.providers.default.registry import build_default_registry
from src.providers.mock.registry import build_mock_registry
from src.services.agent_model_registry import agent_model_options, resolve_agent_model
from src.services.web_search_health_service import WebSearchHealthService

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("/status")
def get_provider_status() -> dict:
    settings = get_settings()
    web_search_provider = settings.web_search_provider.lower()
    web_search_health = WebSearchHealthService().provider_health()
    web_search_requires_key = web_search_provider in {"bocha", "bocha-web-search", "bochaai"}
    web_search_configured = (
        bool(web_search_health.get("configuredProviderCount"))
        if settings.web_search_provider_mode == "chain"
        else (not web_search_requires_key or bool(settings.search_provider_key))
    )
    if web_search_provider in {"multi", "multi-free", "multi-free-search", "free", "free-search", "html-multi"}:
        web_search_caveat = "当前使用多源免费 HTML 搜索，会聚合 Baidu 与 DuckDuckGo 结果并按可信度、相关性和时效排序；它是非官方 HTML 解析方案，可能受页面结构、网络或反爬限制影响。"
    elif web_search_provider in {"anysearch", "any-search"}:
        web_search_caveat = "当前使用 AnySearch 官方聚合搜索 API；未配置 ANYSEARCH_API_KEY 时使用匿名免费额度，可能受到更低的配额和并发限制。"
    elif web_search_provider in {"bing", "bing-html", "bing-html-search"}:
        web_search_caveat = "当前使用免费 Bing HTML 搜索，无需 API key；它是非官方 HTML 解析方案，可能受页面结构、网络或反爬限制影响。"
    elif web_search_provider in {"baidu", "baidu-html", "baidu-html-search"}:
        web_search_caveat = "当前使用免费 Baidu HTML 搜索，无需 API key；它是非官方 HTML 解析方案，可能受页面结构、网络或反爬限制影响。"
    elif web_search_provider in {
        "cheetah-ddg",
        "cheetah-duckduckgo",
        "cheetah-duckduckgo-html",
        "cheetah-duckduckgo-html-search",
    }:
        web_search_caveat = "当前使用免费 Cheetah DuckDuckGo HTML 搜索，无需 API key；它是非官方 HTML 解析方案，可能受页面结构、网络或反爬限制影响。"
    elif web_search_provider in {"duckduckgo", "duckduckgo-html", "duckduckgo-html-search", "ddg"}:
        web_search_caveat = "当前使用免费 DuckDuckGo HTML 搜索，无需 API key；它是非官方 HTML 解析方案，可能受页面结构、网络或反爬限制影响。"
    elif web_search_configured:
        web_search_caveat = ""
    else:
        web_search_caveat = "WEB_SEARCH_API_KEY is not configured; Bocha web search will return failure metadata without mock results."
    api_keys = {
        ProviderKind.llm: settings.deepseek_api_key,
        ProviderKind.vision: settings.deepseek_api_key,
        ProviderKind.map: settings.map_provider_key,
        ProviderKind.ticket: settings.ticket_provider_key,
        ProviderKind.weather: settings.weather_provider_key,
        ProviderKind.traffic: settings.map_provider_key,
        ProviderKind.search: settings.search_provider_key,
        ProviderKind.email: "",
    }

    default_registry = build_default_registry(api_keys)
    mock_registry = build_mock_registry()
    current_model = resolve_agent_model(settings.deepseek_model)

    return {
        "mode": settings.provider_mode,
        "agent": {
            "providerName": "DeepSeek",
            "configured": bool(settings.deepseek_api_key),
            "model": current_model.label,
            "selectedModel": current_model.id,
            "availableModels": agent_model_options(),
            "rawModel": settings.deepseek_model,
            "timeoutSeconds": settings.deepseek_timeout_seconds,
            "status": "available" if settings.deepseek_api_key else "unavailable",
            "userVisibleCaveat": ""
            if settings.deepseek_api_key
            else "DEEPSEEK_API_KEY is not configured; Agent 主循环会直接返回错误，不使用旧 workflow 兜底。",
        },
        "tools": {
            "webSearch": {
                "providerName": settings.web_search_provider,
                "providerMode": settings.web_search_provider_mode,
                "providerChain": web_search_health.get("providerChain", []),
                "configured": web_search_configured,
                "timeoutSeconds": settings.provider_timeout_seconds,
                "status": "available" if web_search_configured else "degraded",
                "userVisibleCaveat": web_search_caveat,
                "health": web_search_health,
            },
            "amapWeather": {
                "providerName": "AMap Weather",
                "configured": bool(settings.weather_provider_key),
                "timeoutSeconds": settings.provider_timeout_seconds,
                "status": "available" if settings.weather_provider_key else "degraded",
                "userVisibleCaveat": ""
                if settings.weather_provider_key
                else "AMAP_WEB_SERVICE_KEY is not configured; weather calls will return explicit unavailable/degraded results instead of mock success data.",
            },
        },
        "default": [provider.health() for provider in default_registry.values()],
        "mock": [provider.health() for provider in mock_registry.values()],
    }
