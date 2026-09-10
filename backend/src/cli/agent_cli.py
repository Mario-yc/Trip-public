import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

from src.api.schemas.agent import AgentSessionCreateRequest
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.core.schema import initialize_database
from src.providers.travel_tools import (
    ChainedWebSearchProvider,
    ResilientWebSearchProvider,
    WebSearchItem,
    WebSearchResponse,
    clear_web_search_runtime_state,
    web_search_provider_config_diagnostics,
)
from src.runtime.agent_runtime import TripAgentRuntime
from src.runtime.agent_quality_eval import AgentQualityEvaluator
from src.runtime.run_artifacts import redact
from src.runtime.runtime_models import RuntimeRunOptions
from src.services.conversation_service import ConversationService
from src.services.controller_availability_check_service import ControllerAvailabilityCheckService
from src.services.deepseek_agent_provider import DeepSeekAgentProvider
from src.services.travel_tool_registry import TravelToolRegistry, parse_tool_arguments
from src.services.tool_schema_compiler import (
    TOOL_SCHEMA_VERSION,
    ToolSchemaValidator,
    compile_deepseek_tools,
    tool_validation_schema,
)


def _open_db() -> sqlite3.Connection:
    initialize_database()
    settings = get_settings()
    db_path = sqlite_path_from_url(settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _emit_json(payload: dict, output: Optional[str] = None) -> None:
    text = json.dumps(redact(payload), ensure_ascii=False, indent=2, default=str)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trip-agent", description="Trip AI Planner backend runtime CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health")
    health.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")
    health.add_argument("--state-dir", default=".ai-runs")
    health.add_argument("--baseline-ref", default="preview/agent-mvp-foundation-20260629-agentcli")

    init_session = subparsers.add_parser("init-session")
    init_session.add_argument("--city", default="北京")
    init_session.add_argument("--title", default=None)
    init_session.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    run = subparsers.add_parser("run")
    run.add_argument("--input", default="")
    run.add_argument("--input-file", default=None)
    run.add_argument("--city", default="北京")
    run.add_argument("--session-id", default=None)
    run.add_argument("--state-dir", default=".ai-runs")
    run.add_argument("--output", default=None)
    run.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")
    run.add_argument("--mock-providers", action="store_true")
    run.add_argument("--baseline-ref", default="preview/agent-mvp-foundation-20260629-agentcli")
    run.add_argument("--debug", action="store_true")

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--session-id", required=True)
    inspect.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    export_state = subparsers.add_parser("export-state")
    export_state.add_argument("--session-id", required=True)
    export_state.add_argument("--output", default=None)
    export_state.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    replay = subparsers.add_parser("replay")
    replay.add_argument("--artifact", required=True)
    replay.add_argument("--output", default=None)
    replay.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    judge_run = subparsers.add_parser("judge-run")
    judge_run.add_argument("--artifact", required=True)
    judge_run.add_argument("--evaluator-model", required=True)
    judge_run.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--scenario-file", required=True)
    eval_parser.add_argument("--state-dir", default=".ai-runs/evals")
    eval_parser.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")
    eval_parser.add_argument("--mock-providers", action="store_true")
    eval_parser.add_argument("--mock-map-rate-limit-stage", default=None)
    eval_parser.add_argument("--mock-map-rate-limit-after", type=int, default=None)
    eval_parser.add_argument("--mock-map-recover", action="store_true")
    eval_parser.add_argument("--baseline-ref", default="preview/agent-mvp-foundation-20260629-agentcli")
    eval_parser.add_argument("--debug", action="store_true")
    eval_parser.add_argument("--max-scenarios", type=int, default=None)
    eval_parser.add_argument("--artifact-root", default=None, help="Rescore existing scenario artifact directories instead of running providers.")

    cost_audit = subparsers.add_parser("cost-audit")
    cost_audit.add_argument("--scenario-file", required=True)
    cost_audit.add_argument("--state-dir", default=".ai-runs/evals")
    cost_audit.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")
    cost_audit.add_argument("--mock-providers", action="store_true")
    cost_audit.add_argument("--mock-map-rate-limit-stage", default=None)
    cost_audit.add_argument("--mock-map-rate-limit-after", type=int, default=None)
    cost_audit.add_argument("--mock-map-recover", action="store_true")
    cost_audit.add_argument("--baseline-ref", default="preview/agent-mvp-foundation-20260629-agentcli")
    cost_audit.add_argument("--debug", action="store_true")
    cost_audit.add_argument("--max-scenarios", type=int, default=None)
    cost_audit.add_argument("--artifact-root", default=None, help="Rescore existing scenario artifact directories instead of running providers.")
    cost_audit.add_argument("--max-amap-poi-calls", type=int, default=18)
    cost_audit.add_argument("--max-amap-route-calls", type=int, default=24)
    cost_audit.add_argument("--max-web-search-calls", type=int, default=0)
    cost_audit.add_argument("--max-tool-rounds", type=int, default=5)
    cost_audit.add_argument("--max-repeated-amap-poi-queries", type=int, default=0)

    smoke = subparsers.add_parser("web-search-smoke")
    smoke.add_argument("--query", required=True)
    smoke.add_argument("--count", type=int, default=5)
    smoke.add_argument("--freshness", default="oneYear")
    smoke.add_argument("--provider", default=None, help="Run a single web search provider such as bocha or brave.")
    smoke.add_argument("--provider-chain", default=None, help="Comma-separated provider chain to test.")
    smoke.add_argument("--offline-fake", action="store_true", help="Use deterministic fake provider adapters; no network calls.")
    smoke.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    reset_search = subparsers.add_parser("web-search-reset")
    reset_search.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    schema_lint = subparsers.add_parser("tool-schema-lint")
    schema_lint.add_argument("--provider", default="deepseek")
    schema_lint.add_argument("--strict", action="store_true")
    schema_lint.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    deepseek_smoke = subparsers.add_parser("deepseek-tool-smoke")
    deepseek_smoke.add_argument(
        "--tool",
        default="patch_itinerary",
        choices=["patch_itinerary", "read_itinerary", "read_preference_memory"],
    )
    deepseek_smoke.add_argument("--strict", action="store_true")
    deepseek_smoke.add_argument("--json", action="store_true", dest="json_output", help="Emit stable JSON; currently the only output mode.")

    controller_availability = subparsers.add_parser("controller-availability-check")
    controller_availability.add_argument("--check-id", required=True)
    controller_availability.add_argument("--state-dir", default=".ai-runs/controller-availability")
    controller_availability.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit the fixed sanitized telemetry schema; currently the only output mode.",
    )
    return parser


def _deepseek_tool_smoke_payload(
    registry: TravelToolRegistry,
    *,
    tool_name: str,
    strict: bool,
    provider: Optional[DeepSeekAgentProvider] = None,
) -> dict:
    tools, hashes = compile_deepseek_tools(registry.tool_definitions(), strict=strict)
    selected = next((item for item in tools if (item.get("function") or {}).get("name") == tool_name), None)
    if selected is None:
        return {
            "schemaVersion": "trip-deepseek-tool-smoke-v1",
            "status": "failed",
            "tool": tool_name,
            "reason": "tool_not_registered",
        }
    provider = provider or DeepSeekAgentProvider()
    if not provider.api_key:
        return {
            "schemaVersion": "trip-deepseek-tool-smoke-v1",
            "status": "failed",
            "tool": tool_name,
            "strict": strict,
            "reason": "DEEPSEEK_API_KEY is not configured",
        }
    provider.tool_strict_mode = strict
    provider.base_url = get_settings().deepseek_base_url.rstrip("/")
    if strict and not provider.base_url.endswith("/beta"):
        provider.base_url = f"{provider.base_url}/beta"
    smoke_arguments = {
        "patch_itinerary": '{"baseVersionId":"ver_smoke","operations":[{"op":"remove_segment","segmentId":"seg_smoke"}]}',
        "read_itinerary": "{}",
        "read_preference_memory": "{}",
    }[tool_name]
    payload = {
        "model": provider.model,
        "messages": [
            {
                "role": "system",
                "content": "You validate one tool schema. Call the required tool exactly once and do not return a normal answer.",
            },
            {
                "role": "user",
                "content": f"Call {tool_name} with exactly this valid smoke payload: {smoke_arguments}.",
            },
        ],
        "tools": [selected],
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
        "thinking": {"type": "disabled"},
        "temperature": 0,
    }
    started = time.monotonic()
    try:
        body = provider._post_json(payload)
        message = body["choices"][0]["message"]
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        tool_call = tool_calls[0] if isinstance(tool_calls, list) and tool_calls else {}
        function = tool_call.get("function") if isinstance(tool_call, dict) else {}
        returned_tool = str((function or {}).get("name") or "")
        arguments, parse_error = parse_tool_arguments((function or {}).get("arguments"))
        schema = tool_validation_schema(selected.get("function") or {})
        issues = [] if parse_error else ToolSchemaValidator().validate(arguments, schema)
        tool_call_count = len(tool_calls) if isinstance(tool_calls, list) else 0
        passed = tool_call_count == 1 and returned_tool == tool_name and not parse_error and not issues
        return {
            "schemaVersion": "trip-deepseek-tool-smoke-v1",
            "status": "passed" if passed else "failed",
            "provider": "deepseek",
            "model": provider.model,
            "tool": tool_name,
            "strict": strict,
            "endpointMode": "beta" if strict else "standard",
            "thinkingMode": "disabled",
            "toolSchemaVersion": TOOL_SCHEMA_VERSION,
            "toolSchemaHash": hashes.get(tool_name),
            "providerRequestId": body.get("id"),
            "toolCallReceived": bool(tool_call),
            "toolCallCount": tool_call_count,
            "returnedTool": returned_tool,
            "argumentKeys": sorted(arguments),
            "argumentsValid": not parse_error and not issues,
            "parseError": parse_error,
            "invalidPaths": [item.get("path") for item in issues[:8]],
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }
    except Exception as error:
        return {
            "schemaVersion": "trip-deepseek-tool-smoke-v1",
            "status": "failed",
            "provider": "deepseek",
            "model": provider.model,
            "tool": tool_name,
            "strict": strict,
            "endpointMode": "beta" if strict else "standard",
            "toolSchemaVersion": TOOL_SCHEMA_VERSION,
            "toolSchemaHash": hashes.get(tool_name),
            "reason": str(error),
            "elapsedMs": int((time.monotonic() - started) * 1000),
        }


def _mock_map_provider_config(args) -> dict:
    config = {}
    if getattr(args, "mock_map_rate_limit_stage", None):
        config["rateLimitAtStage"] = args.mock_map_rate_limit_stage
    if getattr(args, "mock_map_rate_limit_after", None) is not None:
        config["rateLimitAfter"] = args.mock_map_rate_limit_after
    if getattr(args, "mock_map_recover", False):
        config["recover"] = True
    return config


class _OfflineFakeSearchProvider:
    def __init__(self, provider_name: str, configured: bool):
        self.provider_name = provider_name
        self.configured = configured

    def missing_config_reason(self) -> str:
        return "" if self.configured else f"{self.provider_name} is not configured."

    def search(self, query: str, count: int = 5, freshness: str = "oneYear") -> WebSearchResponse:
        return WebSearchResponse(
            query=query,
            results=[
                WebSearchItem(
                    title=f"{query} 官方公告",
                    url="https://www.pku.edu.cn/notice/2026-national-day",
                    snippet="离线 fake：官方公告结果，用于验证 provider chain、配置诊断和输出结构。",
                    source_name="北京大学",
                    confidence=0.82,
                    credibility_rank="official",
                    provider_name=self.provider_name,
                )
            ][: max(1, min(count, 10))],
            confidence=0.82,
            provider_name=self.provider_name,
            provider_diagnostics=[
                {
                    "providerName": self.provider_name,
                    "status": "success",
                    "reason": "offline_fake",
                    "resultCount": 1,
                }
            ],
            attempted_providers=[self.provider_name],
            successful_providers=[self.provider_name],
        )


def _configured_from_diagnostics(config_diagnostics: dict, provider_name: str) -> bool:
    for item in config_diagnostics.get("providers") or []:
        if item.get("providerName") == provider_name:
            return bool(item.get("configured"))
    return provider_name in {
        "bing-html-search",
        "multi-free-search",
        "baidu-html-search",
        "duckduckgo-html-search",
        "cheetah-duckduckgo-html-search",
    }


def _offline_fake_providers(chain: list[str], config_diagnostics: dict) -> list[_OfflineFakeSearchProvider]:
    from src.providers.travel_tools import _provider_result_name

    return [
        _OfflineFakeSearchProvider(
            _provider_result_name(name),
            _configured_from_diagnostics(config_diagnostics, _provider_result_name(name)),
        )
        for name in chain
    ]


def _web_search_smoke_payload(
    query: str,
    count: int,
    freshness: str,
    provider_name: Optional[str] = None,
    provider_chain: Optional[str] = None,
    offline_fake: bool = False,
) -> dict:
    settings = get_settings()
    chain_text = provider_name or provider_chain or settings.web_search_provider_chain
    provider_chain_items = [item.strip() for item in chain_text.split(",") if item.strip()]
    config_diagnostics = web_search_provider_config_diagnostics(chain_text)
    if offline_fake:
        provider = ChainedWebSearchProvider(
            providers=_offline_fake_providers(provider_chain_items, config_diagnostics),
            provider_chain=chain_text,
        )
    elif provider_name or provider_chain:
        provider = ChainedWebSearchProvider(provider_chain=chain_text)
    else:
        provider = ResilientWebSearchProvider()
    result = provider.search(query, count=count, freshness=freshness)
    accepted_count = sum(1 for item in result.results if item.credibility_rank in {"official", "ota_aggregator"} or item.confidence >= 0.58)
    config_inconsistencies = _web_search_config_inconsistencies(config_diagnostics)
    if accepted_count and not config_inconsistencies:
        status = "passed"
    elif result.attempted_providers or result.skipped_providers or result.failed_providers:
        status = "degraded"
    else:
        status = "failed"
    return {
        "schemaVersion": "trip-web-search-smoke-v1",
        "status": status,
        "query": query,
        "providerChain": provider_chain_items,
        "configDiagnostics": config_diagnostics,
        "configInconsistencies": config_inconsistencies,
        "attemptedProviders": result.attempted_providers,
        "successfulProviders": result.successful_providers,
        "failedProviders": result.failed_providers,
        "skippedProviders": result.skipped_providers,
        "acceptedSourceCount": accepted_count,
        "diagnostics": result.provider_diagnostics,
        "providerDiagnostics": result.provider_diagnostics,
        "topResults": [
            {
                "title": item.title,
                "url": item.url,
                "sourceName": item.source_name,
                "providerName": item.provider_name,
                "credibilityRank": item.credibility_rank,
                "confidence": item.confidence,
            }
            for item in result.results[: max(1, min(count, 10))]
        ],
        "results": [
            {
                "title": item.title,
                "url": item.url,
                "sourceName": item.source_name,
                "providerName": item.provider_name,
                "credibilityRank": item.credibility_rank,
                "confidence": item.confidence,
            }
            for item in result.results[: max(1, min(count, 10))]
        ],
    }


def _judge_run_payload(artifact: str, evaluator_model: str) -> dict:
    artifact_path = Path(artifact)
    checks = {
        "artifactExists": artifact_path.exists(),
        "finalResponsePresent": False,
        "planningStepsPresent": False,
        "toolEventsPresent": False,
        "verifierReportPresent": False,
        "providerDiagnosticsVisible": False,
    }
    failures: list[str] = []
    loaded: dict[str, object] = {}
    if not artifact_path.exists():
        failures.append("artifact_missing")
    elif artifact_path.is_dir():
        loaded = _load_judge_artifact_dir(artifact_path)
    else:
        try:
            loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append("artifact_unreadable")
            loaded = {}
    checks["finalResponsePresent"] = bool(loaded.get("final_response") or loaded.get("finalResponse"))
    checks["planningStepsPresent"] = bool(loaded.get("planning_steps") or loaded.get("planningSteps"))
    checks["toolEventsPresent"] = bool(loaded.get("tool_events") or loaded.get("toolEvents"))
    checks["verifierReportPresent"] = bool(loaded.get("verifier_report") or loaded.get("verifierReport"))
    checks["providerDiagnosticsVisible"] = _contains_provider_diagnostics(loaded)
    for check_name, passed in checks.items():
        if check_name == "providerDiagnosticsVisible":
            continue
        if not passed:
            failures.append(_camel_to_snake_missing(check_name))
    return {
        "schemaVersion": "trip-agent-judge-run-v1",
        "status": "passed" if not failures else "failed",
        "artifactPath": str(artifact_path),
        "evaluatorModel": evaluator_model,
        "judgeMode": "deterministic_artifact_static_checks",
        "checks": checks,
        "failures": failures,
        "verdict": {
            "approved": not failures,
            "summary": "artifact replay structure is present" if not failures else "artifact replay structure is incomplete",
            "providerDiagnosticsVisible": checks["providerDiagnosticsVisible"],
        },
    }


def _load_judge_artifact_dir(path: Path) -> dict[str, object]:
    file_map = {
        "manifest": "manifest.json",
        "final_response": "final_response.json",
        "planning_steps": "planning_steps.jsonl",
        "tool_events": "tool_events.jsonl",
        "verifier_report": "verifier_report.json",
        "itinerary_snapshot": "itinerary_snapshot.json",
    }
    loaded: dict[str, object] = {}
    for key, filename in file_map.items():
        item_path = path / filename
        if not item_path.exists():
            continue
        try:
            if filename.endswith(".jsonl"):
                loaded[key] = [
                    json.loads(line)
                    for line in item_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            else:
                loaded[key] = json.loads(item_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded[key] = {"unreadable": True}
    return loaded


def _contains_provider_diagnostics(value: object) -> bool:
    if isinstance(value, dict):
        if any(key in value for key in ("providerDiagnostics", "providerDebug", "webSearchProviderDiagnostics")):
            return True
        return any(_contains_provider_diagnostics(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_provider_diagnostics(item) for item in value)
    return False


def _camel_to_snake_missing(name: str) -> str:
    text = re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()
    return f"{text}_missing"


def _web_search_config_inconsistencies(config_diagnostics: dict) -> list[str]:
    issues: list[str] = []
    for item in config_diagnostics.get("providers") or []:
        if item.get("providerName") != "bocha-web-search":
            continue
        aliases = item.get("envAliases") if isinstance(item.get("envAliases"), dict) else {}
        if aliases.get("SEARCH_PROVIDER_KEY") == "present" and not item.get("configured"):
            issues.append("bocha_search_provider_key_present_but_not_configured")
    return issues


def _external_call_audit_from_eval(eval_payload: dict, thresholds: dict[str, int]) -> dict:
    metric_keys = [
        "webSearchCallCount",
        "ticketLookupCallCount",
        "amapWeatherCallCount",
        "amapPoiExternalCallCount",
        "amapPoiTextExternalCallCount",
        "amapPoiAroundExternalCallCount",
        "amapRouteExternalCallCount",
        "amapCacheHitCount",
        "amapSkippedBecauseBudget",
        "toolRoundsUsed",
        "amapRepeatedQueryCount",
    ]
    totals = {key: 0 for key in metric_keys}
    scenario_audits: list[dict] = []
    for result in eval_payload.get("results") or []:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        scenario_totals = {key: _safe_int(metrics.get(key)) for key in metric_keys}
        repeated_query_keys = _repeated_query_keys(metrics)
        if repeated_query_keys:
            scenario_totals["amapRepeatedQueryCount"] = len(repeated_query_keys)
        for key, value in scenario_totals.items():
            totals[key] += value
        exceeded = _budget_exceeded_keys(scenario_totals, thresholds)
        scenario_audits.append(
            {
                "scenarioId": result.get("scenarioId"),
                "status": result.get("status"),
                "runtimeStatus": result.get("runtimeStatus"),
                "artifactPath": result.get("artifactPath"),
                "calls": scenario_totals,
                "budgetExceeded": exceeded,
                "repeatedQueryKeys": repeated_query_keys,
                "abnormalCallPatterns": _abnormal_call_patterns(scenario_totals, repeated_query_keys),
            }
        )
        for turn in result.get("turnResults") or []:
            turn_metrics = turn.get("metrics") if isinstance(turn.get("metrics"), dict) else {}
            turn_calls = {key: _safe_int(turn_metrics.get(key)) for key in metric_keys}
            turn_repeated_query_keys = _repeated_query_keys(turn_metrics)
            if turn_repeated_query_keys:
                turn_calls["amapRepeatedQueryCount"] = len(turn_repeated_query_keys)
            turn_exceeded = _budget_exceeded_keys(turn_calls, thresholds)
            scenario_audits.append(
                {
                    "scenarioId": result.get("scenarioId"),
                    "turnIndex": turn.get("turnIndex"),
                    "status": turn.get("runtimeStatus"),
                    "artifactPath": turn.get("artifactPath"),
                    "calls": turn_calls,
                    "budgetExceeded": turn_exceeded,
                    "repeatedQueryKeys": turn_repeated_query_keys,
                    "abnormalCallPatterns": _abnormal_call_patterns(turn_calls, turn_repeated_query_keys),
                }
            )
    total_exceeded = _budget_exceeded_keys(totals, thresholds)
    return {
        "totals": totals,
        "thresholds": thresholds,
        "budgetExceeded": total_exceeded,
        "scenarioAudits": scenario_audits,
        "abnormalCallPatterns": _abnormal_call_patterns(totals, []),
    }


def _budget_exceeded_keys(calls: dict[str, int], thresholds: dict[str, int]) -> list[str]:
    exceeded: list[str] = []
    mapping = {
        "amapPoiExternalCallCount": "maxAmapPoiCalls",
        "amapRouteExternalCallCount": "maxAmapRouteCalls",
        "webSearchCallCount": "maxWebSearchCalls",
        "toolRoundsUsed": "maxToolRounds",
        "amapRepeatedQueryCount": "maxRepeatedAmapPoiQueries",
    }
    for metric_key, threshold_key in mapping.items():
        threshold = int(thresholds.get(threshold_key) or 0)
        if threshold >= 0 and int(calls.get(metric_key) or 0) > threshold:
            exceeded.append(metric_key)
    return exceeded


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _repeated_query_keys(metrics: dict) -> list[str]:
    raw_keys = metrics.get("amapPoiQueryKeys") or metrics.get("amapQueryKeys") or metrics.get("queryKeys") or []
    if not isinstance(raw_keys, list):
        return []
    counts: dict[str, int] = {}
    for raw_key in raw_keys:
        key = str(raw_key or "").strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return sorted(key for key, count in counts.items() if count > 1)


def _abnormal_call_patterns(calls: dict[str, int], repeated_query_keys: list[str]) -> list[str]:
    patterns: list[str] = []
    if repeated_query_keys or int(calls.get("amapRepeatedQueryCount") or 0) > 0:
        patterns.append("repeated_amap_poi_query_within_run")
    if int(calls.get("webSearchCallCount") or 0) > 0:
        patterns.append("web_search_during_timeline_generation")
    return patterns


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        payload = TripAgentRuntime.replay_artifact(args.artifact)
        _emit_json(payload, output=args.output)
        return 0 if payload["status"] == "success" else 1
    if args.command == "judge-run":
        payload = _judge_run_payload(args.artifact, args.evaluator_model)
        _emit_json(payload)
        return 0 if payload["status"] == "passed" else 1
    if args.command == "web-search-smoke":
        payload = _web_search_smoke_payload(
            args.query,
            args.count,
            args.freshness,
            provider_name=args.provider,
            provider_chain=args.provider_chain,
            offline_fake=args.offline_fake,
        )
        _emit_json(payload)
        return 0 if payload["status"] in {"passed", "degraded"} else 1
    if args.command == "web-search-reset":
        payload = {
            "schemaVersion": "trip-web-search-reset-v1",
            "status": "cleared",
            **clear_web_search_runtime_state(),
        }
        _emit_json(payload)
        return 0
    if args.command == "controller-availability-check":
        payload = ControllerAvailabilityCheckService().run(
            check_id=args.check_id,
            state_dir=Path(args.state_dir),
        )
        _emit_json(payload)
        if payload["status"] == "AVAILABLE":
            return 0
        return 2 if payload["status"] == "DUPLICATE_REJECTED" else 1
    with _open_db() as db:
        runtime = TripAgentRuntime(db)
        if args.command == "health":
            payload = runtime.health(state_dir=args.state_dir, baseline_ref=args.baseline_ref)
            _emit_json(payload)
            return 0 if payload["status"] in {"ok", "degraded"} else 1
        if args.command == "init-session":
            session = runtime.create_session(
                AgentSessionCreateRequest(city=args.city, title=args.title),
            )
            _emit_json(session.model_dump(by_alias=True))
            return 0
        if args.command == "tool-schema-lint":
            if args.provider != "deepseek":
                _emit_json({"status": "failed", "reason": f"Unsupported provider: {args.provider}"})
                return 2
            with _open_db() as connection:
                session = ConversationService(connection).create_session("北京", "tool schema lint")
                session_row = connection.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
                registry = TravelToolRegistry(connection, session_row, {})
                tools, hashes = compile_deepseek_tools(registry.tool_definitions(), strict=bool(args.strict))
            _emit_json({
                "schemaVersion": TOOL_SCHEMA_VERSION,
                "status": "passed",
                "provider": "deepseek",
                "strict": bool(args.strict),
                "toolCount": len(tools),
                "toolSchemaHash": hashes,
            })
            return 0
        if args.command == "deepseek-tool-smoke":
            session = ConversationService(db).create_session("北京", "deepseek tool smoke")
            session_row = db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session.session_id,)).fetchone()
            registry = TravelToolRegistry(db, session_row, {})
            payload = _deepseek_tool_smoke_payload(
                registry,
                tool_name=args.tool,
                strict=bool(args.strict),
            )
            _emit_json(payload)
            return 0 if payload["status"] == "passed" else 1
        if args.command == "inspect":
            _emit_json(runtime.inspect_session(args.session_id))
            return 0
        if args.command == "export-state":
            _emit_json(runtime.export_state(args.session_id), output=args.output)
            return 0
        if args.command == "run":
            input_text = args.input
            if args.input_file:
                file_text = Path(args.input_file).read_text(encoding="utf-8").strip()
                input_text = f"{file_text}\n{input_text}".strip() if input_text.strip() else file_text
            options = RuntimeRunOptions(
                input=input_text,
                city=args.city,
                sessionId=args.session_id,
                stateDir=str(Path(args.state_dir)),
                json=args.json_output,
                mockProviders=args.mock_providers,
                baselineRef=args.baseline_ref,
                debug=args.debug,
            )
            final, exit_code = runtime.run_once(options, argv=sys.argv if argv is None else argv)
            _emit_json(final.model_dump(by_alias=True), output=args.output)
            return exit_code
        if args.command in {"eval", "cost-audit"}:
            payload = AgentQualityEvaluator(runtime).run(
                scenario_file=args.scenario_file,
                state_dir=args.state_dir,
                mock_providers=args.mock_providers,
                baseline_ref=args.baseline_ref,
                debug=args.debug,
                max_scenarios=args.max_scenarios,
                artifact_root=args.artifact_root,
                mock_map_provider=_mock_map_provider_config(args),
            )
            if args.command == "cost-audit":
                thresholds = {
                    "maxAmapPoiCalls": int(args.max_amap_poi_calls),
                    "maxAmapRouteCalls": int(args.max_amap_route_calls),
                    "maxWebSearchCalls": int(args.max_web_search_calls),
                    "maxToolRounds": int(args.max_tool_rounds),
                    "maxRepeatedAmapPoiQueries": int(args.max_repeated_amap_poi_queries),
                }
                audit = _external_call_audit_from_eval(payload, thresholds)
                cost_payload = {
                    "schemaVersion": "trip-agent-cost-audit-v1",
                    "status": "passed" if payload.get("status") == "passed" and not audit["budgetExceeded"] else "failed",
                    "scenarioFile": payload.get("scenarioFile"),
                    "mode": payload.get("mode"),
                    "summary": payload.get("summary"),
                    "externalCallAudit": audit,
                    "qualityEvalStatus": payload.get("status"),
                }
                _emit_json(cost_payload)
                return 0 if cost_payload["status"] == "passed" else 1
            _emit_json(payload)
            return 0 if payload["status"] == "passed" else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
