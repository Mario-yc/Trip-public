from datetime import datetime, timezone
from typing import Optional

from src.providers.travel_tools import web_search_provider_config_diagnostics


class WebSearchHealthService:
    def provider_health(self, provider_chain: Optional[str] = None) -> dict:
        diagnostics = web_search_provider_config_diagnostics(provider_chain)
        providers = []
        for provider in diagnostics.get("providers", []):
            configured = bool(provider.get("configured"))
            public_provider = {key: value for key, value in provider.items() if key != "proxyHost"}
            providers.append(
                {
                    **public_provider,
                    "reachable": None,
                    "status": "configured" if configured else "missing_config",
                    "lastFailure": None,
                    "lastSuccessAt": None,
                    "circuitOpenUntil": None,
                    "userVisibleHint": self._hint(provider),
                }
            )
        configured_count = sum(1 for provider in providers if provider.get("configured"))
        return {
            **diagnostics,
            "configuredProviderCount": configured_count,
            "providers": providers,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }

    def _hint(self, provider: dict) -> str:
        name = str(provider.get("providerName") or "")
        if provider.get("configured"):
            if name == "ddgs":
                if provider.get("cliAvailable") and not provider.get("endpointHost"):
                    return "DDGS CLI 已可用；后端会直接调用 ddgs text 作为免费搜索 fallback。"
                if provider.get("cliAvailable"):
                    return "DDGS 已配置；本地 API 不可用时会 fallback 到 ddgs text CLI。"
                return "DDGS 已配置；确认本地 API server 使用 /search/text endpoint。"
            if name == "searxng":
                return "SearXNG 已配置；若 JSON format 被禁用，请启用本地实例的 json output。"
            if name == "anysearch":
                return "AnySearch 已启用；未设置 ANYSEARCH_API_KEY 时使用匿名免费额度，设置后可获得更高配额与并发。"
            return ""
        if name == "ddgs":
            return "启动 DDGS 本地 API；如需代理，请使用 -pr 参数并从本地安全配置读取代理地址。"
        if name == "searxng":
            return "配置 SEARXNG_BASE_URL=http://127.0.0.1:8080，并启用 json output。"
        if name == "cheetah-duckduckgo-html-search":
            return "Cheetah DuckDuckGo HTML 搜索无需 key；网络受限时配置 DUCKDUCKGO_PROXY_URL 或 WEB_SEARCH_PROXY_URL。"
        if name == "duckduckgo-html-search":
            return "DuckDuckGo HTML 搜索无需 key；网络受限时配置 DUCKDUCKGO_PROXY_URL。"
        if name == "baidu-html-search":
            return "Baidu HTML 搜索无需 key；网络受限时配置 BAIDU_PROXY_URL。"
        if name == "bing-html-search":
            return "Bing HTML 搜索无需 key；网络受限时配置 BING_PROXY_URL。"
        return ""
