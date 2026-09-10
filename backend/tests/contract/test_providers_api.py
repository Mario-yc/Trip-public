from fastapi.testclient import TestClient

from src.core.config import get_settings
from src.main import app


def test_provider_status_reports_deepseek_configuration_without_leaking_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-secret")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-test-model")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "9")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    assert response.status_code == 200
    assert body["agent"]["providerName"] == "DeepSeek"
    assert body["agent"]["configured"] is True
    assert body["agent"]["model"] == "deepseek-v4-flash"
    assert body["agent"]["selectedModel"] == "deepseek-v4-flash"
    assert body["agent"]["rawModel"] == "deepseek-test-model"
    assert {item["id"] for item in body["agent"]["availableModels"]} == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert body["agent"]["timeoutSeconds"] == 9
    assert "sk-test-secret" not in response.text
    get_settings.cache_clear()


def test_provider_status_marks_deepseek_unavailable_when_key_missing(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    assert response.status_code == 200
    assert body["agent"]["configured"] is False
    assert body["agent"]["model"] == "deepseek-v4-flash"
    assert body["agent"]["selectedModel"] == "deepseek-v4-flash"
    assert body["agent"]["rawModel"] == "deepseek-chat"
    assert body["agent"]["status"] == "unavailable"
    assert "DEEPSEEK_API_KEY is not configured" in body["agent"]["userVisibleCaveat"]
    get_settings.cache_clear()


def test_provider_status_marks_free_duckduckgo_search_available_without_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER_KEY", raising=False)
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    assert response.status_code == 200
    assert body["tools"]["webSearch"]["providerName"] == "duckduckgo"
    assert body["tools"]["webSearch"]["configured"] is True
    assert body["tools"]["webSearch"]["status"] == "available"
    assert "免费 DuckDuckGo HTML 搜索" in body["tools"]["webSearch"]["userVisibleCaveat"]
    assert "WEB_SEARCH_API_KEY" not in body["tools"]["webSearch"]["userVisibleCaveat"]
    get_settings.cache_clear()


def test_provider_status_marks_multi_free_search_available_without_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "multi-free")
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER_KEY", raising=False)
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    assert response.status_code == 200
    assert body["tools"]["webSearch"]["providerName"] == "multi-free"
    assert body["tools"]["webSearch"]["configured"] is True
    assert body["tools"]["webSearch"]["status"] == "available"
    assert "多源免费 HTML 搜索" in body["tools"]["webSearch"]["userVisibleCaveat"]
    assert "WEB_SEARCH_API_KEY" not in body["tools"]["webSearch"]["userVisibleCaveat"]
    get_settings.cache_clear()


def test_provider_status_marks_anysearch_available_without_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "anysearch")
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    assert response.status_code == 200
    assert body["tools"]["webSearch"]["providerName"] == "anysearch"
    assert body["tools"]["webSearch"]["configured"] is True
    assert body["tools"]["webSearch"]["status"] == "available"
    assert "匿名免费额度" in body["tools"]["webSearch"]["userVisibleCaveat"]
    assert "ANYSEARCH_API_KEY" in body["tools"]["webSearch"]["userVisibleCaveat"]
    get_settings.cache_clear()


def test_provider_status_exposes_web_search_health_without_proxy_url(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_MODE", "chain")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_CHAIN", "searxng,ddgs,duckduckgo,baidu")
    monkeypatch.setenv("SEARXNG_BASE_URL", "")
    monkeypatch.setenv("DDGS_API_BASE_URL", "http://127.0.0.1:4479")
    monkeypatch.setenv("DUCKDUCKGO_PROXY_URL", "http://127.0.0.1:10793")
    monkeypatch.setenv("BAIDU_PROXY_URL", "http://127.0.0.1:10793")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    body = response.json()
    health = body["tools"]["webSearch"]["health"]
    assert body["tools"]["webSearch"]["providerMode"] == "chain"
    assert body["tools"]["webSearch"]["providerChain"] == ["searxng", "ddgs", "duckduckgo", "baidu"]
    assert health["configuredProviderCount"] >= 3
    providers = {provider["providerName"]: provider for provider in health["providers"]}
    assert providers["ddgs"]["configured"] is True
    assert providers["ddgs"]["endpointHost"] == "127.0.0.1:4479"
    assert providers["duckduckgo-html-search"]["proxyUsed"] is True
    assert "proxyHost" not in providers["duckduckgo-html-search"]
    assert "127.0.0.1:10793" not in response.text
    assert "http://127.0.0.1:10793" not in response.text
    get_settings.cache_clear()


def test_provider_status_missing_ddgs_hint_does_not_publish_proxy_address(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_MODE", "chain")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER_CHAIN", "ddgs")
    monkeypatch.setenv("DDGS_API_BASE_URL", "")
    monkeypatch.setattr("src.providers.travel_tools._ddgs_package_available", lambda: False)
    monkeypatch.setattr("src.providers.travel_tools._ddgs_cli_path", lambda: "")
    get_settings.cache_clear()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    assert "127.0.0.1:10793" not in response.text
    assert "http://127.0.0.1:10793" not in response.text
    get_settings.cache_clear()
