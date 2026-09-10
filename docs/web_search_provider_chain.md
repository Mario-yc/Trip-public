# Web Search Provider Chain

Trip risk search uses a configurable provider chain instead of relying only on free HTML search.

## Default Order

`WEB_SEARCH_PROVIDER_CHAIN=cheetah-ddg,ddgs,searxng,duckduckgo,baidu,multi-free,bocha`

The chain tries configured providers in order. Missing credentials are recorded as `skipped_missing_config`, not as hard failures. A provider error or timeout records diagnostics and the chain continues. Results are merged, deduplicated, ranked, and never replaced with mock search results.

Supported providers:

- `bocha`: Bocha web search, configured by `WEB_SEARCH_API_KEY` or `SEARCH_PROVIDER_KEY`.
- `tavily`: Tavily Search API, configured by `TAVILY_API_KEY`.
- `brave`: Brave Web Search API, configured by `BRAVE_SEARCH_API_KEY`.
- `searxng`: self-hosted SearXNG JSON API, configured by `SEARXNG_BASE_URL`.
- `google-cse`: optional Google Programmable Search, configured by `GOOGLE_CSE_API_KEY` and `GOOGLE_CSE_CX`.
- `cheetah-ddg`: free Cheetah-style DuckDuckGo HTML search using `https://html.duckduckgo.com/html/`.
- `multi-free`: free HTML fallback that aggregates Baidu HTML and DuckDuckGo HTML.
- `baidu`: Baidu HTML search only.
- `duckduckgo`: DuckDuckGo HTML search only.

HTML providers are fallback-only because page structure, anti-bot behavior, and network timeouts are outside the app's control.

## Environment Variables

- `WEB_SEARCH_PROVIDER_CHAIN`: comma-separated provider order.
- `WEB_SEARCH_MIN_ACCEPTED_RESULTS`: minimum credible result count before stopping on a stable provider.
- `WEB_SEARCH_PROVIDER_TIMEOUT_SECONDS`: per-provider timeout; falls back to `PROVIDER_TIMEOUT_SECONDS`.
- `WEB_SEARCH_MAX_PROVIDER_ATTEMPTS`: maximum actual provider calls; skipped missing-config providers do not consume attempts.
- `WEB_SEARCH_API_KEY` / `SEARCH_PROVIDER_KEY`: Bocha API key.
- `TAVILY_API_KEY`: Tavily API key.
- `BRAVE_SEARCH_API_KEY`: Brave Search API key.
- `SEARXNG_BASE_URL`: self-hosted SearXNG base URL, for example `http://localhost:8888`.
- `GOOGLE_CSE_API_KEY` and `GOOGLE_CSE_CX`: optional Google CSE credentials.

Bing legacy web search is intentionally not recommended because its older API path is not the current stable choice for this project.

## Diagnostics

`POIRiskAlert.sources` includes:

- `riskSearchDiagnostics`: query, query length, accepted source count, stale source count, official source count, and `riskStatusReason`.
- `webSearchProviderDiagnostics`: provider name, attempted/successful/failed/skipped providers, and per-provider diagnostics.

Common `riskStatusReason` values:

- `search_provider_unavailable`: providers were missing config, failed, or returned no usable response.
- `search_no_results`: search ran but no public result was available.
- `search_results_all_stale`: all results were older than the target travel year.
- `search_results_low_credibility`: results existed but did not meet official/credible source requirements.
- `search_success_degraded`: at least one accepted source exists, but some providers failed/skipped or source quality is incomplete.
- `search_success_available`: accepted source exists without degraded provider state.

Risk search keeps weather, route, budget, and traveler context in the synthesis payload, not in the search query. Search queries are capped at 180 characters and focus on city, POI, travel year/date range, official announcements, booking, opening hours, and limit-control terms.

## Smoke Test

Manual smoke test:

```powershell
.\trip\Scripts\python.exe -m src.cli.agent_cli web-search-smoke --query "北京大学 2026 国庆 预约 官方公告" --json
```

Without Tavily/Brave/SearXNG/Bocha credentials, the command should return `degraded` with skipped/failed provider diagnostics instead of crashing. CI tests use fake providers and do not require real network access.
