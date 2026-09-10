import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from backend.tests.intent_contract_support import IntentContractProviderMixin
from src.cli.agent_cli import build_parser
from src.cli.agent_cli import _external_call_audit_from_eval
from src.cli.agent_cli import main
from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.runtime.agent_quality_eval import AgentQualityEvaluator
from src.runtime.agent_runtime import RuntimeMockAgentProvider, RuntimeStagedMapMockProvider, TripAgentRuntime
from src.runtime.run_artifacts import RunArtifactWriter
from src.runtime.runtime_models import RuntimeRunOptions
from src.services.agent_service import AgentService
from src.services.deepseek_agent_provider import AgentToolLoopError, AgentToolLoopResult
from src.providers.travel_tools import WebSearchItem, WebSearchResponse


def _json_stdout(capsys):
    return json.loads(capsys.readouterr().out)


def _route_ready_input(value: str) -> str:
    return f"{value}，绕行最多30分钟，绕行比例最多35%"


def _required_goals_from_initial_plan_context(context: dict) -> list[dict]:
    goal_requirements = context.get("goalRequirements")
    if isinstance(goal_requirements, list) and goal_requirements:
        return [item for item in goal_requirements if isinstance(item, dict) and str(item.get("goalId") or "").strip()]
    request_contract = context.get("requestIntentContract")
    if not isinstance(request_contract, dict):
        return []
    return [
        item
        for item in request_contract.get("requiredIntents") or []
        if isinstance(item, dict)
        and str(item.get("goalId") or "").strip()
        and str(item.get("requirementLevel") or "required") not in {"soft_experience", "optional"}
    ]


def _align_runtime_initial_plan_to_required_goals(raw_payload: str, context: dict) -> str:
    payload = json.loads(raw_payload)
    required_goals = _required_goals_from_initial_plan_context(context)
    if payload.get("mode") != "day_slots" or not required_goals:
        return raw_payload

    slots = [item for item in payload.get("daySlots") or [] if isinstance(item, dict)]
    slots_by_id = {str(item.get("slotId") or ""): item for item in slots if str(item.get("slotId") or "").strip()}
    pools = [item for item in payload.get("intentPools") or [] if isinstance(item, dict)]
    available_pools = [item for item in pools if item.get("assignToSlots")]
    used_pool_ids: set[str] = set()
    test_entities = {
        "museum": ("故宫博物院", "museum", ["博物馆", "博物院", "美术馆"]),
        "park": ("景山公园", "park", ["公园", "风景名胜"]),
    }

    for goal in required_goals:
        goal_id = str(goal.get("goalId") or "").strip()
        intent_type = str(goal.get("intentType") or "").strip()
        allowed_days = {
            int(item)
            for item in goal.get("allowedDayNumbers") or []
            if isinstance(item, int) and not isinstance(item, bool) and int(item) > 0
        }
        matching_pool = next(
            (
                pool
                for pool in available_pools
                if str(pool.get("poolId") or "") not in used_pool_ids
                and str(pool.get("intentType") or "") == intent_type
                and any(
                    not allowed_days or int(slots_by_id.get(str(slot_id), {}).get("dayNumber") or 0) in allowed_days
                    for slot_id in pool.get("assignToSlots") or []
                )
            ),
            None,
        )
        if matching_pool is None:
            matching_pool = next(
                (
                    pool
                    for pool in available_pools
                    if str(pool.get("poolId") or "") not in used_pool_ids
                    and any(
                        not allowed_days or int(slots_by_id.get(str(slot_id), {}).get("dayNumber") or 0) in allowed_days
                        for slot_id in pool.get("assignToSlots") or []
                    )
                ),
                None,
            )
        if matching_pool is None:
            continue

        pool_id = str(matching_pool.get("poolId") or "")
        used_pool_ids.add(pool_id)
        entity_name, slot_kind, preferred_types = test_entities.get(
            intent_type,
            (str(goal.get("exactEntity") or intent_type), "visit", list(matching_pool.get("preferredTypes") or [])),
        )
        matching_pool.update(
            {
                "rawNeed": entity_name,
                "intentType": intent_type,
                "targetCount": 1,
                "requirementLevel": "required",
                "goalId": goal_id,
                "softGoalId": None,
                "preferredTypes": preferred_types,
                "candidateHints": [entity_name],
                "hintPolicy": "llm_common_knowledge_hint",
                "entityBindingMode": "category",
                "exactEntity": None,
            }
        )
        for slot_id in matching_pool.get("assignToSlots") or []:
            slot = slots_by_id.get(str(slot_id))
            if slot is None:
                continue
            slot.update(
                {
                    "kind": slot_kind,
                    "rawNeed": entity_name,
                    "routeAnchor": True,
                    "priority": 95,
                }
            )

    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def goal_aligned_runtime_initial_plan(monkeypatch):
    original_initial_plan = RuntimeStagedMapMockProvider.generate_initial_plan
    original_map_candidates = TripAgentRuntime._mock_map_candidates

    def generate_initial_plan(provider, context):
        return _align_runtime_initial_plan_to_required_goals(original_initial_plan(provider, context), context)

    def mock_map_candidates(runtime, city, keyword, category, *, exact_poi_fixtures=None):
        required_poi_fixtures = [
            {
                "city": "北京",
                "name": "故宫博物院",
                "matchKeywords": ["故宫博物院", "故宫"],
                "type": "科教文化服务;博物馆;博物院",
                "category": "museum",
            },
            {
                "city": "北京",
                "name": "景山公园",
                "matchKeywords": ["景山公园", "景山"],
                "type": "风景名胜;公园广场;公园",
                "category": "scenic",
            },
        ]
        return original_map_candidates(
            runtime,
            city,
            keyword,
            category,
            exact_poi_fixtures=[*required_poi_fixtures, *(exact_poi_fixtures or [])],
        )

    monkeypatch.setattr(RuntimeStagedMapMockProvider, "generate_initial_plan", generate_initial_plan)
    monkeypatch.setattr(TripAgentRuntime, "_mock_map_candidates", mock_map_candidates)


def test_manifest_provider_mode_reflects_effective_agent_execution_path(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "recorded-provider-key")
    monkeypatch.setenv("PROVIDER_MODE", "mock")
    get_settings.cache_clear()

    with _open_test_db() as connection:
        runtime = TripAgentRuntime(connection)
        live_manifest = runtime._manifest(
            RunArtifactWriter(tmp_path / "live-runs"),
            RuntimeRunOptions(input="北京两日游", mockProviders=False),
            ["test-runtime", "run"],
            started_at="2026-07-15T00:00:00+00:00",
            finished_at=None,
            status="running",
        )
        mock_manifest = runtime._manifest(
            RunArtifactWriter(tmp_path / "mock-runs"),
            RuntimeRunOptions(input="北京两日游", mockProviders=True),
            ["test-runtime", "run", "--mock-providers"],
            started_at="2026-07-15T00:00:00+00:00",
            finished_at=None,
            status="running",
        )

    assert [
        live_manifest["providerStatus"]["mode"],
        mock_manifest["providerStatus"]["mode"],
    ] == ["live", "mock"]


def test_manifest_redacts_absolute_path_command_operands(tmp_path):
    with _open_test_db() as connection:
        manifest = TripAgentRuntime(connection)._manifest(
            RunArtifactWriter(tmp_path / "runs"),
            RuntimeRunOptions(input="北京两日游"),
            ["trip-agent", "run", "--state-dir", r"C:\secret\runs", r"\\server\share\runs"],
            started_at="2026-07-15T00:00:00+00:00",
            finished_at=None,
            status="running",
        )
    assert manifest["command"] == ["trip-agent", "run", "--state-dir", "[redacted-path]", "[redacted-path]"]


def test_recorded_controller_uses_latest_turn_for_museum_replacement_and_preserves_source_context():
    provider = RuntimeStagedMapMockProvider()
    decision = json.loads(
        provider.decide_autonomy(
            {
                "latestUserMessage": "重试一次，将第一天美术馆修改为清华美术馆",
                "effectiveUserMessage": "今年国庆参观985大学两日游，10月1日到2日",
                "observation": {
                    "cycleIndex": 0,
                    "itinerary": {"lifecycleState": "draft_pending_grounding"},
                    "versionLineage": {"currentVersionId": "ver_current"},
                    "targetInventory": {"segmentIds": ["seg_museum"]},
                    "segmentRefs": [
                        {
                            "segmentId": "seg_museum",
                            "dayNumber": 1,
                            "intentType": "museum",
                            "goalId": "goal_museum",
                        }
                    ],
                    "candidateState": {"pendingGroups": []},
                    "unresolvedSlots": [{"segmentId": "seg_museum"}],
                },
            },
            timeout_seconds=1.0,
        )
    )

    assert decision["primaryAction"] == "resolve_poi"
    assert decision["actionDirective"]["targetGoalId"] == "goal_museum"
    assert decision["actionDirective"]["targetSegmentIds"] == ["seg_museum"]
    assert decision["actionDirective"]["searchIntent"] == "清华美术馆"


def test_staged_map_mock_provider_uses_explicit_exact_poi_fixture():
    runtime = TripAgentRuntime.__new__(TripAgentRuntime)
    response = runtime._mock_map_search_response(
        "测试城市",
        "用户明确场馆",
        "museum",
        limit=4,
        trusted_all=True,
        exact_poi_fixtures=[
            {
                "city": "测试城市",
                "name": "用户明确场馆",
                "type": "科教文化服务;博物馆;博物院",
                "category": "museum",
            }
        ],
    )

    assert [poi.name for poi in response.pois] == ["用户明确场馆"]
    assert response.pois[0].source == "amap-place-search"


def test_recorded_controller_finishes_after_draft_when_only_soft_slots_remain():
    decision = json.loads(
        RuntimeStagedMapMockProvider().decide_autonomy(
            {
                "latestUserMessage": "北京两日游",
                "observation": {
                    "cycleIndex": 1,
                    "itinerary": {"lifecycleState": "draft_pending_grounding"},
                    "unresolvedSlots": [{"segmentId": "seg_optional_meal", "required": False}],
                    "candidateState": {"pendingGroups": []},
                    "targetInventory": {"segmentIds": ["seg_optional_meal"]},
                    "segmentRefs": [],
                },
            },
            timeout_seconds=1.0,
        )
    )

    assert decision["primaryAction"] == "finish"


def test_recorded_controller_patches_persisted_dominant_candidate_before_finish():
    decision = json.loads(
        RuntimeStagedMapMockProvider().decide_autonomy(
            {
                "latestUserMessage": "重试一次，将第一天美术馆修改为清华美术馆",
                "observation": {
                    "cycleIndex": 1,
                    "itinerary": {"lifecycleState": "draft_pending_grounding"},
                    "versionLineage": {"currentVersionId": "ver_current"},
                    "targetInventory": {"segmentIds": ["seg_museum"]},
                    "segmentRefs": [
                        {
                            "segmentId": "seg_museum",
                            "dayNumber": 1,
                            "intentType": "museum",
                            "goalId": "goal_museum",
                        }
                    ],
                    "candidateState": {
                        "pendingGroups": [
                            {
                                "id": "candidate_group",
                                "sourceSegmentId": "seg_museum",
                                "safeCandidates": [{"id": "B0TSINGHUAART"}],
                            }
                        ]
                    },
                    "unresolvedSlots": [],
                },
            },
            timeout_seconds=1.0,
        )
    )

    assert decision["primaryAction"] == "patch_itinerary"
    assert decision["actionDirective"]["candidateId"] == "candidate_group"
    assert decision["actionDirective"]["amapPoiId"] == "B0TSINGHUAART"


def test_recorded_initial_plan_places_requested_museum_on_day_one_for_day_scoped_followup():
    plan = json.loads(
        RuntimeStagedMapMockProvider().generate_initial_plan(
            {
                "selectedCity": "北京",
                "effectiveUserMessage": "10月1日到2日参观985大学和博物馆，品尝当地特色美食",
                "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
            }
        )
    )

    museum_slots = [slot for slot in plan["daySlots"] if slot["kind"] == "museum"]
    assert len(museum_slots) == 1
    assert museum_slots[0]["dayNumber"] == 1
    day_one = [slot for slot in plan["daySlots"] if slot["dayNumber"] == 1]
    assert [(slot["startTime"], slot["durationMinutes"]) for slot in day_one] == [
        ("09:00", 120),
        ("12:00", 60),
        ("13:30", 90),
        ("15:30", 90),
        ("18:30", 60),
    ]
    meal_pool = next(pool for pool in plan["intentPools"] if pool["intentType"] == "meal")
    assert meal_pool["targetCount"] == 1
    assert len(meal_pool["assignToSlots"]) == 4


@pytest.mark.parametrize(
    ("message", "expected_days"),
    [
        ("今年国庆参观北京高校两日游，晚上看北京夜景。", [1, 2]),
        ("今年国庆参观北京高校两日游，每晚都看北京夜景。", [1, 2]),
    ],
)
def test_recorded_initial_plan_respects_night_view_cardinality(message, expected_days):
    plan = json.loads(
        RuntimeStagedMapMockProvider().generate_initial_plan(
            {
                "selectedCity": "北京",
                "effectiveUserMessage": message,
                "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
            }
        )
    )

    night_slots = [slot for slot in plan["daySlots"] if slot["kind"] == "night_view"]
    night_pool = next(pool for pool in plan["intentPools"] if pool["intentType"] == "night_view")
    assert [slot["dayNumber"] for slot in night_slots] == expected_days
    assert night_pool["targetCount"] == len(expected_days)
    assert night_pool["assignToSlots"] == [slot["slotId"] for slot in night_slots]


def _run_dirs(state_dir: Path) -> list[Path]:
    return sorted(path for path in state_dir.iterdir() if path.is_dir() and path.name.startswith("run_"))


def _open_test_db() -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_path_from_url(get_settings().database_url), check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _count_turns(session_id: str) -> int:
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )


def _state_counts(session_id: str) -> dict[str, int]:
    db_path = sqlite_path_from_url(get_settings().database_url)
    with sqlite3.connect(db_path) as connection:
        session = connection.execute(
            "SELECT active_plan_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        plan_id = session[0] if session else ""
        return {
            "turns": int(
                connection.execute(
                    "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            ),
            "versions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            ),
            "planning_runs": int(
                connection.execute(
                    "SELECT COUNT(*) FROM planning_runs WHERE itinerary_plan_id = ? OR user_input IN (SELECT content FROM conversation_turns WHERE session_id = ? AND role = 'user')",
                    (plan_id, session_id),
                ).fetchone()[0]
            ),
        }


def _active_version_id(session_id: str) -> Optional[str]:
    with _open_test_db() as connection:
        row = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row["active_version_id"] if row else None


def _expected_artifact_files() -> set[str]:
    return {
        "manifest.json",
        "input.json",
        "context.json",
        "agent_plan.json",
        "agent_decision.json",
        "agent_observations.jsonl",
        "agent_decisions.jsonl",
        "agent_action_outcomes.jsonl",
        "agent_stop.json",
        "planning_steps.jsonl",
        "tool_events.jsonl",
        "patches.jsonl",
        "final_response.json",
        "session_snapshot.json",
        "verifier_report.json",
        "itinerary_snapshot.json",
        "errors.jsonl",
        "README.md",
    }


def test_cost_audit_summarizes_external_call_metrics():
    payload = {
        "results": [
            {
                "scenarioId": "a",
                "status": "passed",
                "runtimeStatus": "success",
                "artifactPath": "runs/a",
                "metrics": {
                    "webSearchCallCount": 0,
                    "amapPoiExternalCallCount": 8,
                    "amapPoiTextExternalCallCount": 5,
                    "amapPoiAroundExternalCallCount": 3,
                    "amapRouteExternalCallCount": 4,
                    "toolRoundsUsed": 2,
                },
            },
            {
                "scenarioId": "b",
                "status": "passed",
                "runtimeStatus": "success",
                "artifactPath": "runs/b",
                "metrics": {
                    "webSearchCallCount": 1,
                    "amapPoiExternalCallCount": 12,
                    "amapRouteExternalCallCount": 1,
                    "toolRoundsUsed": 1,
                    "amapPoiQueryKeys": ["text|北京|大学", "around|北京|餐厅|116.400,39.900", "text|北京|大学"],
                },
            },
        ]
    }

    audit = _external_call_audit_from_eval(
        payload,
        {
            "maxAmapPoiCalls": 18,
            "maxAmapRouteCalls": 10,
            "maxWebSearchCalls": 0,
            "maxToolRounds": 5,
            "maxRepeatedAmapPoiQueries": 0,
        },
    )

    assert audit["totals"]["amapPoiExternalCallCount"] == 20
    assert audit["totals"]["amapRouteExternalCallCount"] == 5
    assert audit["totals"]["amapRepeatedQueryCount"] == 1
    assert set(audit["budgetExceeded"]) == {"amapPoiExternalCallCount", "webSearchCallCount", "amapRepeatedQueryCount"}
    assert set(audit["scenarioAudits"][1]["budgetExceeded"]) == {"webSearchCallCount", "amapRepeatedQueryCount"}
    assert audit["scenarioAudits"][1]["repeatedQueryKeys"] == ["text|北京|大学"]
    assert "repeated_amap_poi_query_within_run" in audit["scenarioAudits"][1]["abnormalCallPatterns"]


def test_cost_audit_command_runs_eval_and_enforces_thresholds(tmp_path, capsys):
    scenario_file = tmp_path / "quality_scenarios_cost_audit.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "cost_audit_inline_budget",
                        "mockArtifact": {
                            "itinerarySnapshot": {"days": []},
                            "planningSteps": [
                                {
                                    "type": "collect_candidates",
                                    "metadata": {
                                        "resultPreview": {
                                            "amapCallBudget": {
                                                "usedTotalExternal": 3,
                                                "usedPlaceText": 2,
                                                "usedPlaceAround": 1,
                                                "usedRoute": 0,
                                                "cacheHitCount": 0,
                                                "skippedBecauseBudget": 0,
                                            }
                                        }
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "cost-audit",
            "--scenario-file",
            str(scenario_file),
            "--state-dir",
            str(tmp_path / "eval-runs"),
            "--json",
            "--max-amap-poi-calls",
            "1",
        ]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["qualityEvalStatus"] == "passed"
    audit = payload["externalCallAudit"]
    assert audit["totals"]["amapPoiExternalCallCount"] == 3
    assert audit["budgetExceeded"] == ["amapPoiExternalCallCount"]


class ProviderRuntime(TripAgentRuntime):
    def __init__(self, db, provider):
        super().__init__(db)
        self.provider = provider

    def send_agent_message(
        self,
        session_id,
        payload,
        event_sink=None,
        user_turn_sink=None,
        *,
        mock_providers=False,
        mock_map_provider=None,
    ):
        provider = self.provider
        if not callable(getattr(provider, "decide_autonomy", None)):
            provider = ControllerToolLoopAdapter(provider)
        return AgentService(self.db, provider=provider).send_message(
            session_id,
            payload,
            event_sink=event_sink,
            user_turn_sink=user_turn_sink,
        )


class ControllerToolLoopAdapter:
    """Test adapter: model selects a complex patch before the delegated tool loop runs."""

    def __init__(self, delegate):
        self.delegate = delegate

    def decide_autonomy_lite(self, context, *, timeout_seconds):
        return self.delegate.decide_autonomy_lite(
            context,
            timeout_seconds=timeout_seconds,
        )

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
        if int(observation.get("cycleIndex") or 0) > 0:
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {"type": "finish", "assistantReply": "本轮执行完成。"},
            }
        version_id = str((observation.get("versionLineage") or {}).get("currentVersionId") or "")
        segment_ids = [
            str(item) for item in (observation.get("targetInventory") or {}).get("segmentIds") or [] if str(item)
        ]
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "patch_itinerary",
            "actionDirective": {
                "type": "patch_itinerary",
                "operationIntent": "complex_patch",
                "baseVersionId": version_id,
                "targetSegmentIds": segment_ids[:1],
                "requestedOutcome": "执行复杂 patch 测试",
            },
        }

    def run_tool_loop(self, *args, **kwargs):
        return self.delegate.run_tool_loop(*args, **kwargs)


def _run_with_provider(tmp_path, provider, text: str, session_id: Optional[str] = None, debug: bool = False):
    with _open_test_db() as connection:
        runtime = ProviderRuntime(connection, provider)
        return runtime.run_once(
            RuntimeRunOptions(
                input=text,
                sessionId=session_id,
                stateDir=str(tmp_path / "runs"),
                mockProviders=True,
                debug=debug,
            ),
            argv=["test-runtime", "run"],
        )


def _valid_full_itinerary(city: str = "北京") -> dict:
    return RuntimeMockAgentProvider()._mock_full_itinerary(city)


class InvalidPatchProvider(IntentContractProviderMixin):
    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        tool_registry.execute("invalid_read", "read_itinerary", {})
        tool_registry.execute(
            "invalid_patch",
            "patch_itinerary",
            {
                "baseVersionId": context.get("activeVersionId"),
                "operations": [
                    {
                        "op": "replace_itinerary",
                        "fullItinerary": {
                            "title": "Invalid draft",
                            "city": "北京",
                            "days": [
                                {
                                    "dayNumber": 1,
                                    "title": "Invalid",
                                    "segments": [
                                        {
                                            "startTime": "09:00",
                                            "endTime": "10:00",
                                            "kind": "visit",
                                            "transportMode": "walk",
                                            "estimatedCost": 0,
                                            "notes": "missing poi object",
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            },
        )
        return AgentToolLoopResult(reply="invalid patch attempted", tool_events=tool_registry.events)


class StalePatchProvider(IntentContractProviderMixin):
    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        tool_registry.execute("stale_read", "read_itinerary", {})
        tool_registry.execute(
            "stale_patch",
            "patch_itinerary",
            {
                "baseVersionId": "ver_stale_for_test",
                "operations": [{"op": "replace_itinerary", "fullItinerary": _valid_full_itinerary()}],
            },
        )
        return AgentToolLoopResult(reply="stale patch attempted", tool_events=tool_registry.events)


class OverrunAfterWriteProvider(IntentContractProviderMixin):
    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        tool_registry.execute("partial_read", "read_itinerary", {})
        tool_registry.execute(
            "partial_patch",
            "patch_itinerary",
            {
                "baseVersionId": context.get("activeVersionId"),
                "operations": [{"op": "replace_itinerary", "fullItinerary": _valid_full_itinerary()}],
            },
        )
        raise AgentToolLoopError("Agent tool loop exceeded maxToolRounds", tool_events=tool_registry.events)


class SuccessWithRejectedDiagnosticsProvider(IntentContractProviderMixin):
    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        tool_registry.execute("diagnostic_read", "read_itinerary", {})
        tool_registry.execute(
            "diagnostic_patch",
            "patch_itinerary",
            {
                "baseVersionId": context.get("activeVersionId"),
                "operations": [{"op": "replace_itinerary", "fullItinerary": _valid_full_itinerary()}],
            },
        )
        tool_registry.events.append(
            {
                "id": "diagnostic_candidates",
                "toolName": "candidate_scoring",
                "type": "tool",
                "label": "candidate_scoring",
                "status": "succeeded",
                "providerName": "agent-service",
                "fallbackUsed": False,
                "failureReason": None,
                "detail": "candidate diagnostics",
                "metadata": {
                    "rejectedCount": 2,
                    "rejectedReasonCounts": {"below_threshold": 2},
                    "topCandidates": [
                        {"decision": "rejected", "rejectedReasons": ["below_threshold"]},
                    ],
                },
            }
        )
        return AgentToolLoopResult(
            reply="success with rejected candidate diagnostics", tool_events=tool_registry.events
        )


class OverrunBeforeWriteProvider(IntentContractProviderMixin):
    def __init__(self):
        self.tool_loop_calls = 0

    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        self.tool_loop_calls += 1
        raise AgentToolLoopError("Agent tool loop exceeded maxToolRounds", tool_events=tool_registry.events)


class RuntimeErrorProvider(IntentContractProviderMixin):
    def run_tool_loop(self, context, tool_registry, max_tool_rounds=5, max_tool_calls_per_round=3):
        raise RuntimeError("unexpected runtime failure")


def test_health_command_returns_valid_json(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER_CHAIN", raising=False)
    get_settings.cache_clear()
    exit_code = main(["health", "--json", "--state-dir", str(tmp_path / "runs")])
    payload = _json_stdout(capsys)

    assert exit_code == 0, payload
    assert payload["schemaVersion"] == "ai-runtime-health-v1"
    assert payload["status"] in {"ok", "degraded"}
    assert payload["database"]["initialized"] is True
    assert payload["databasePath"].endswith(".db")
    assert payload["databaseExists"] is True
    assert isinstance(payload["preferenceMemoryRows"], int)
    assert isinstance(payload["sessionMemoryRows"], int)
    assert payload["database"]["databasePath"] == payload["databasePath"]
    assert payload["database"]["preferenceMemoryRows"] == payload["preferenceMemoryRows"]
    assert payload["artifactDirectory"]["writable"] is True
    assert "ddgs" in payload["providerStatus"]["tools"]["search"]["providerChain"]
    assert "secret" not in json.dumps(payload).lower()
    assert "sk-" not in json.dumps(payload).lower()


def test_cli_parser_matches_console_script_name():
    parser = build_parser()
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"

    assert parser.prog == "trip-agent"
    assert 'trip-agent = "src.cli.agent_cli:main"' in pyproject.read_text(encoding="utf-8")
    try:
        parser.parse_args(["run", "--input", "北京一天", "--mock-map-rate-limit-stage", "immediate"])
    except SystemExit as error:
        assert error.code != 0
    else:
        raise AssertionError("mock map rate-limit flags must stay eval-only, not trip-agent run")


def test_deepseek_tool_smoke_uses_strict_compiled_schema(capsys, monkeypatch):
    class FakeDeepSeekProvider:
        def __init__(self):
            self.api_key = "test-key"
            self.model = "deepseek-test"
            self.base_url = "https://api.deepseek.com"
            self.tool_strict_mode = False

        def _post_json(self, payload):
            function = payload["tools"][0]["function"]
            assert function["name"] == "patch_itinerary"
            assert function["strict"] is True
            assert payload["tool_choice"]["function"]["name"] == "patch_itinerary"
            assert payload["thinking"] == {"type": "disabled"}
            return {
                "id": "deepseek-smoke-test",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_smoke",
                                    "type": "function",
                                    "function": {
                                        "name": "patch_itinerary",
                                        "arguments": json.dumps(
                                            {
                                                "baseVersionId": "ver_smoke",
                                                "operations": [{"op": "remove_segment", "segmentId": "seg_smoke"}],
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
            }

    monkeypatch.setattr("src.cli.agent_cli.DeepSeekAgentProvider", FakeDeepSeekProvider)
    exit_code = main(["deepseek-tool-smoke", "--tool", "patch_itinerary", "--strict", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    assert payload["strict"] is True
    assert payload["endpointMode"] == "beta"
    assert payload["argumentsValid"] is True
    assert payload["toolCallReceived"] is True
    assert payload["toolCallCount"] == 1


def test_deepseek_no_arg_tool_smoke_uses_closed_empty_arguments(capsys, monkeypatch):
    class FakeDeepSeekProvider:
        def __init__(self):
            self.api_key = "test-key"
            self.model = "deepseek-test"
            self.base_url = "https://api.deepseek.com"
            self.tool_strict_mode = False

        def _post_json(self, payload):
            function = payload["tools"][0]["function"]
            assert function["name"] == "read_itinerary"
            assert function["strict"] is True
            assert "parameters" not in function
            return {
                "id": "deepseek-no-arg-smoke-test",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_read",
                                    "type": "function",
                                    "function": {"name": "read_itinerary", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
            }

    monkeypatch.setattr("src.cli.agent_cli.DeepSeekAgentProvider", FakeDeepSeekProvider)
    exit_code = main(["deepseek-tool-smoke", "--tool", "read_itinerary", "--strict", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    assert payload["argumentKeys"] == []
    assert payload["toolCallCount"] == 1


def test_eval_command_reads_scenario_file_and_reports_artifact_metrics(
    tmp_path, capsys, goal_aligned_runtime_initial_plan
):
    scenario_file = tmp_path / "quality_scenarios.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "mock_runtime_quality_plumbing",
                        "city": "北京",
                        "input": _route_ready_input(
                            "帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"
                        ),
                        "expectations": {"mustCreateTimeline": True, "minDays": 1},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "eval",
            "--scenario-file",
            str(scenario_file),
            "--state-dir",
            str(tmp_path / "eval-runs"),
            "--json",
            "--mock-providers",
        ]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["schemaVersion"] == "trip-agent-quality-eval-v1"
    assert payload["status"] == "passed"
    assert payload["summary"] == {"total": 1, "passed": 1, "failed": 0}
    result = payload["results"][0]
    assert result["scenarioId"] == "mock_runtime_quality_plumbing"
    assert result["metrics"]["activeVersionCreated"] is True
    assert Path(result["artifactPath"]).exists()


def test_web_search_smoke_outputs_provider_chain_diagnostics(capsys, monkeypatch):
    class FakeResilientProvider:
        def search(self, query: str, count: int = 5, freshness: str = "oneYear") -> WebSearchResponse:
            return WebSearchResponse(
                query=query,
                results=[
                    WebSearchItem(
                        title="北京大学 2026 国庆预约官方公告",
                        url="https://www.pku.edu.cn/notice/2026",
                        snippet="2026年国庆期间校园参观需提前预约。",
                        source_name="北京大学",
                        confidence=0.82,
                        credibility_rank="official",
                        provider_name="brave-web-search",
                    )
                ],
                provider_name="chained-web-search",
                provider_diagnostics=[
                    {"providerName": "tavily", "status": "failed", "reason": "timeout", "resultCount": 0},
                    {"providerName": "brave-web-search", "status": "success", "reason": "ok", "resultCount": 1},
                ],
                attempted_providers=["tavily", "brave-web-search"],
                successful_providers=["brave-web-search"],
                failed_providers=["tavily"],
            )

    monkeypatch.setattr("src.cli.agent_cli.ResilientWebSearchProvider", FakeResilientProvider)

    exit_code = main(["web-search-smoke", "--query", "北京大学 2026 国庆 预约 官方公告", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["schemaVersion"] == "trip-web-search-smoke-v1"
    assert payload["status"] == "passed"
    assert payload["attemptedProviders"] == ["tavily", "brave-web-search"]
    assert payload["successfulProviders"] == ["brave-web-search"]
    assert payload["acceptedSourceCount"] == 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "test-key" not in serialized
    assert "key=" not in serialized.lower()


def test_web_search_smoke_offline_fake_uses_search_provider_key_for_bocha(monkeypatch, capsys):
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "")
    monkeypatch.setenv("SEARCH_PROVIDER_KEY", "search-provider-key")
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)
    get_settings.cache_clear()

    try:
        exit_code = main(
            [
                "web-search-smoke",
                "--query",
                "北京大学 国庆 预约 官方公告",
                "--provider",
                "bocha",
                "--offline-fake",
                "--json",
            ]
        )
        payload = _json_stdout(capsys)
    finally:
        get_settings.cache_clear()

    assert exit_code == 0
    assert payload["status"] == "passed"
    assert payload["providerChain"] == ["bocha"]
    bocha = payload["configDiagnostics"]["providers"][0]
    assert bocha["providerName"] == "bocha-web-search"
    assert bocha["configured"] is True
    assert bocha["envAliases"]["WEB_SEARCH_API_KEY"] == "empty"
    assert bocha["envAliases"]["SEARCH_PROVIDER_KEY"] == "present"
    assert payload["diagnostics"][0]["providerName"] == "bocha-web-search"
    assert payload["topResults"][0]["providerName"] == "bocha-web-search"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "search-provider-key" not in serialized
    assert "key=" not in serialized.lower()


def test_web_search_reset_command_returns_clear_counts(capsys):
    exit_code = main(["web-search-reset", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["schemaVersion"] == "trip-web-search-reset-v1"
    assert payload["status"] == "cleared"
    assert isinstance(payload["providerInstances"], int)
    assert isinstance(payload["cacheEntriesCleared"], int)
    assert isinstance(payload["circuitEntriesCleared"], int)


def test_judge_run_command_reports_artifact_verdict(tmp_path, capsys):
    artifact = tmp_path / "run_001"
    artifact.mkdir()
    (artifact / "final_response.json").write_text(
        json.dumps({"status": "success"}, ensure_ascii=False), encoding="utf-8"
    )
    (artifact / "planning_steps.jsonl").write_text(
        json.dumps({"metadata": {"providerDebug": [{"classifiedReason": "http_429"}]}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (artifact / "tool_events.jsonl").write_text(
        json.dumps(
            {"metadata": {"providerDiagnostics": [{"providerName": "cheetah-duckduckgo-html-search"}]}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact / "verifier_report.json").write_text(json.dumps({"passed": True}, ensure_ascii=False), encoding="utf-8")

    exit_code = main(["judge-run", "--artifact", str(artifact), "--evaluator-model", "gpt-5.4-mini", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["schemaVersion"] == "trip-agent-judge-run-v1"
    assert payload["status"] == "passed"
    assert payload["evaluatorModel"] == "gpt-5.4-mini"
    assert payload["checks"]["providerDiagnosticsVisible"] is True
    assert payload["verdict"]["approved"] is True


def test_eval_metrics_read_web_search_provider_diagnostics():
    artifact = {
        "itinerarySnapshot": {
            "days": [],
            "poiRiskAlerts": [
                {
                    "segmentId": "seg_1",
                    "sources": [
                        {
                            "type": "riskSearchDiagnostics",
                            "query": "北京大学 2026 国庆 官方公告 预约 限流",
                            "queryLength": 26,
                            "acceptedSourceCount": 1,
                            "riskStatusReason": "search_success_degraded",
                        },
                        {
                            "type": "webSearchProviderDiagnostics",
                            "query": "北京大学 2026 国庆 官方公告 预约 限流",
                            "attemptedProviders": ["tavily", "brave-web-search"],
                            "successfulProviders": ["brave-web-search"],
                            "failedProviders": ["tavily"],
                            "skippedProviders": ["searxng"],
                            "providerDiagnostics": [
                                {"providerName": "tavily", "status": "failed", "reason": "timeout"},
                                {
                                    "providerName": "brave-web-search",
                                    "status": "success",
                                    "reason": "ok",
                                    "resultCount": 1,
                                },
                            ],
                        },
                    ],
                }
            ],
        },
        "context": {},
        "planningSteps": [],
        "toolEvents": [],
        "verifierReport": {},
    }

    metrics = AgentQualityEvaluator(None)._metrics(artifact, {})

    assert metrics["webSearchAttemptedProviderCount"] == 2
    assert metrics["webSearchSuccessfulProviderCount"] == 1
    assert metrics["webSearchAcceptedSourceCount"] == 1
    assert metrics["webSearchProviderDiagnosticsPresent"] is True
    assert metrics["riskSearchHasAtLeastOneConfiguredStableProvider"] is True
    assert metrics["riskSearchQueryTooLongCount"] == 0
    assert metrics["riskSearchDuplicateQueryCount"] == 0


def test_eval_command_supports_multi_turn_scenarios(tmp_path, capsys, goal_aligned_runtime_initial_plan):
    scenario_file = tmp_path / "quality_scenarios_turns.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "retry_turn_quality_plumbing",
                        "city": "北京",
                        "turns": [
                            {
                                "input": _route_ready_input(
                                    "帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"
                                )
                            },
                            {"input": "重新构建"},
                        ],
                        "expectations": {
                            "activeVersionEventuallyCreated": True,
                            "secondTurnDoesNotAskForTripDates": True,
                            "noRawProviderRateLimitAsOnlyReply": True,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "eval",
            "--scenario-file",
            str(scenario_file),
            "--state-dir",
            str(tmp_path / "eval-runs"),
            "--json",
            "--mock-providers",
        ]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    result = payload["results"][0]
    assert result["scenarioId"] == "retry_turn_quality_plumbing"
    assert result["metrics"]["turnCount"] == 2
    assert result["metrics"]["activeVersionEventuallyCreated"] is True
    assert len(result["turnResults"]) == 2
    assert Path(result["turnResults"][1]["artifactPath"]).exists()


def test_eval_command_scores_inline_web_search_mock_artifact(tmp_path, capsys):
    scenario_file = tmp_path / "quality_scenarios_web_search.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "risk_search_brave_success_after_tavily_fail",
                        "mockArtifact": {
                            "context": {},
                            "itinerarySnapshot": {
                                "days": [
                                    {
                                        "dayNumber": 1,
                                        "segments": [{"id": "seg_pku", "kind": "visit", "poi": {"name": "北京大学"}}],
                                    }
                                ],
                                "poiRiskAlerts": [
                                    {
                                        "segmentId": "seg_pku",
                                        "status": "degraded",
                                        "sources": [
                                            {
                                                "type": "riskSearchDiagnostics",
                                                "query": "北京 北京大学 2026 国庆 官方公告 预约 限流",
                                                "queryLength": 31,
                                                "acceptedSourceCount": 1,
                                                "riskStatusReason": "search_success_degraded",
                                            },
                                            {
                                                "type": "webSearchProviderDiagnostics",
                                                "attemptedProviders": ["tavily", "brave-web-search"],
                                                "successfulProviders": ["brave-web-search"],
                                                "failedProviders": ["tavily"],
                                                "skippedProviders": [],
                                                "providerDiagnostics": [
                                                    {"providerName": "tavily", "status": "failed", "reason": "timeout"},
                                                    {
                                                        "providerName": "brave-web-search",
                                                        "status": "success",
                                                        "reason": "ok",
                                                        "resultCount": 1,
                                                    },
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                        },
                        "expectations": {
                            "riskSearchProviderDiagnosticsMustBePresent": True,
                            "riskSearchMustAttemptAtLeastOneStableProvider": True,
                            "riskSearchAcceptedSourceCountAtLeast": 1,
                            "riskSearchQueryMaxChars": 180,
                            "riskSearchDuplicateQueryMaxCount": 0,
                            "riskSearchMustSkipOrdinaryMeals": True,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        ["eval", "--scenario-file", str(scenario_file), "--state-dir", str(tmp_path / "eval-runs"), "--json"]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    result = payload["results"][0]
    assert result["runtimeStatus"] == "mock_artifact"
    assert result["metrics"]["webSearchProviderDiagnosticsPresent"] is True
    assert result["metrics"]["webSearchSuccessfulProviderCount"] == 1
    assert Path(result["artifactPath"]).exists()


def test_eval_command_scores_timeline_precision_and_meal_cost_mock_artifact(tmp_path, capsys):
    scenario_file = tmp_path / "quality_scenarios_timeline_precision.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "timeline_precision_edit_fill_day2_dinner",
                        "mockArtifact": {
                            "context": {
                                "timelinePrecisionExpectations": {
                                    "targetDayNumber": 2,
                                    "unchangedDaySegmentIds": {"1": ["seg_d1_campus", "seg_d1_night"]},
                                }
                            },
                            "itinerarySnapshot": {
                                "days": [
                                    {
                                        "dayNumber": 1,
                                        "segments": [
                                            {
                                                "id": "seg_d1_campus",
                                                "kind": "visit",
                                                "startTime": "09:00",
                                                "poi": {"name": "北京大学"},
                                            },
                                            {
                                                "id": "seg_d1_night",
                                                "kind": "visit",
                                                "startTime": "19:30",
                                                "poi": {"name": "景山公园夜景"},
                                            },
                                        ],
                                    },
                                    {
                                        "dayNumber": 2,
                                        "segments": [
                                            {
                                                "id": "seg_d2_campus",
                                                "kind": "visit",
                                                "startTime": "15:00",
                                                "poi": {"name": "清华大学"},
                                            },
                                            {
                                                "id": "seg_d2_dinner",
                                                "kind": "meal",
                                                "startTime": "18:00",
                                                "estimatedCost": 240,
                                                "notes": "costBasis=per_person; costPerPerson=120; partySize=2; totalCost=240; costSource=budget_policy",
                                                "poi": {
                                                    "name": "晚餐 当地特色美食",
                                                    "sourceNote": "groundingStatus：waiting_for_poi_grounding",
                                                },
                                            },
                                            {
                                                "id": "seg_d2_night",
                                                "kind": "visit",
                                                "startTime": "19:30",
                                                "poi": {"name": "奥林匹克塔夜景"},
                                            },
                                        ],
                                    },
                                ]
                            },
                            "planningSteps": [
                                {
                                    "type": "timeline_edit",
                                    "metadata": {
                                        "resultPreview": {
                                            "timelineCommand": {"operation": "replace_or_fill", "dayNumber": 2},
                                            "globalReorder": False,
                                        }
                                    },
                                }
                            ],
                        },
                        "expectations": {
                            "timelinePrecisionEditFillDay2Dinner": True,
                            "mealCostPerPersonDisplay": True,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        ["eval", "--scenario-file", str(scenario_file), "--state-dir", str(tmp_path / "eval-runs"), "--json"]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    result = payload["results"][0]
    assert result["scenarioId"] == "timeline_precision_edit_fill_day2_dinner"
    assert result["metrics"]["timelineEditEventPresent"] is True
    assert result["metrics"]["targetDayDinnerCount"] == 1
    assert result["metrics"]["mealCostPerPersonMismatchCount"] == 0


def test_eval_command_scores_local_edit_tool_and_amap_budget_metrics(tmp_path, capsys):
    scenario_file = tmp_path / "quality_scenarios_local_edit_budget.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "second_turn_food_edit_no_tool_loop_exceeded",
                        "mockArtifact": {
                            "itinerarySnapshot": {
                                "days": [
                                    {
                                        "dayNumber": 1,
                                        "segments": [
                                            {
                                                "id": "seg_d1_campus",
                                                "kind": "visit",
                                                "startTime": "09:00",
                                                "poi": {"name": "北京大学"},
                                            },
                                            {
                                                "id": "seg_d1_meal",
                                                "kind": "meal",
                                                "startTime": "18:00",
                                                "poi": {"name": "北京烤鸭餐厅"},
                                            },
                                            {
                                                "id": "seg_d1_night",
                                                "kind": "visit",
                                                "startTime": "19:30",
                                                "poi": {"name": "景山公园夜景"},
                                            },
                                        ],
                                    }
                                ]
                            },
                            "planningSteps": [
                                {
                                    "type": "timeline_edit",
                                    "metadata": {
                                        "resultPreview": {
                                            "timelineCommand": {
                                                "scope": "meal",
                                                "operation": "replace_or_fill",
                                                "dayNumber": 1,
                                            },
                                            "changedScope": "day1_meal_only",
                                            "globalReorder": False,
                                            "toolLoopEntered": False,
                                            "webSearchCalls": 0,
                                            "ticketLookupCalls": 0,
                                            "amapWeatherCalls": 0,
                                            "operationCount": 1,
                                            "amapCallBudget": {
                                                "usedPlaceText": 0,
                                                "usedPlaceAround": 3,
                                                "usedRoute": 0,
                                                "usedTotalExternal": 3,
                                                "cacheHitCount": 1,
                                                "skippedBecauseBudget": 0,
                                            },
                                        }
                                    },
                                }
                            ],
                        },
                        "expectations": {
                            "mustUseDeterministicTimelineCommand": True,
                            "mustNotContainFailure": "Agent tool loop exceeded",
                            "mustNotEnterToolLoop": True,
                            "maxToolRoundsUsed": 0,
                            "maxWebSearchCalls": 0,
                            "maxTicketLookupCalls": 0,
                            "maxAmapWeatherCalls": 0,
                            "maxAmapPoiExternalCalls": 4,
                            "maxAmapPoiAroundExternalCalls": 4,
                            "modifiedScope": "day1_meal_only",
                        },
                    },
                    {
                        "id": "amap_budget_prevents_rate_limit_spike",
                        "mockArtifact": {
                            "planningSteps": [
                                {
                                    "type": "collect_candidates",
                                    "metadata": {
                                        "resultPreview": {
                                            "amapCallBudget": {
                                                "used": {
                                                    "usedTotalExternal": 16,
                                                    "usedPlaceText": 6,
                                                    "usedPlaceAround": 8,
                                                    "usedRoute": 2,
                                                },
                                                "cacheHitCount": 5,
                                                "skippedBecauseBudget": 3,
                                            }
                                        }
                                    },
                                }
                            ]
                        },
                        "expectations": {
                            "maxAmapPoiExternalCalls": 16,
                            "minAmapSkippedBecauseBudget": 1,
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        ["eval", "--scenario-file", str(scenario_file), "--state-dir", str(tmp_path / "eval-runs"), "--json"]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["status"] == "passed"
    food_result = payload["results"][0]
    assert food_result["metrics"]["deterministicTimelineCommand"] is True
    assert food_result["metrics"]["webSearchCallCount"] == 0
    assert food_result["metrics"]["amapPoiExternalCallCount"] == 3
    budget_result = payload["results"][1]
    assert budget_result["metrics"]["amapSkippedBecauseBudget"] == 3


def test_eval_command_recovered_rate_limit_preserves_requirements_with_a_safe_terminal(tmp_path, capsys):
    scenario_file = tmp_path / "quality_scenarios_rate_limit_resume.json"
    scenario_file.write_text(
        json.dumps(
            {
                "schemaVersion": "trip-agent-quality-scenarios-v1",
                "scenarios": [
                    {
                        "id": "retry_after_rate_limit_reuses_previous_requirements",
                        "city": "北京",
                        "turns": [
                            {
                                "input": _route_ready_input(
                                    "今年国庆参观北京高校两日游，每晚都看北京夜景。"
                                    "10月1日到2日，2天，中等预算，1人，公交地铁优先。"
                                    "想要体验当地特色美食"
                                ),
                                "mockMapProvider": {"rateLimitAtStage": "immediate"},
                            },
                            {
                                "input": "重新构建",
                                "mockMapProvider": {
                                    "recover": True,
                                    "trustedPoiFixtures": True,
                                    "trustedFoodForDeterministicEdit": True,
                                    "mockRouteRefresh": True,
                                    "deterministicSingleCandidate": True,
                                },
                            },
                        ],
                        "expectations": {
                            "secondTurnDoesNotAskForTripDates": True,
                            "effectiveUserMessageContainsOriginalTrip": True,
                            "noRawProviderRateLimitAsOnlyReply": True,
                            "rateLimitDoesNotCreateUnassignedPoolNoise": True,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "eval",
            "--scenario-file",
            str(scenario_file),
            "--state-dir",
            str(tmp_path / "eval-runs"),
            "--json",
        ]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 0, payload
    assert payload["status"] == "passed"
    result = payload["results"][0]
    assert result["metrics"]["turnCount"] == 2
    terminal_status = result["metrics"]["terminalStatus"]
    assert terminal_status in {"draft_pending_grounding", "candidate_refresh_required"}
    if terminal_status == "candidate_refresh_required":
        assert result["turnResults"][1]["metrics"]["activeVersionChanged"] is False
    else:
        assert result["turnResults"][1]["metrics"]["activeVersionChanged"] is True
    assert result["metrics"]["effectiveUserMessageContainsOriginalTrip"] is True
    assert result["metrics"]["rateLimitDoesNotCreateUnassignedPoolNoise"] is True
    assert Path(result["turnResults"][0]["artifactPath"]).exists()
    assert Path(result["turnResults"][1]["artifactPath"]).exists()


def test_init_session_returns_json(capsys):
    exit_code = main(["init-session", "--city", "北京", "--title", "AI 接手测试", "--json"])
    payload = _json_stdout(capsys)

    assert exit_code == 0
    assert payload["sessionId"]
    assert payload["activePlanId"]
    assert payload["city"] == "北京"


def test_run_with_mock_providers_creates_artifact_directory(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    exit_code = main(
        [
            "run",
            "--input",
            "想出去玩",
            "--city",
            "北京",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    payload = _json_stdout(capsys)
    run_dirs = _run_dirs(state_dir)

    assert exit_code == 3
    assert len(run_dirs) == 1
    assert payload["sessionId"]
    assert payload["status"] == "needs_confirmation"
    assert payload["artifactPath"] == str(run_dirs[0])
    for name in _expected_artifact_files():
        assert (run_dirs[0] / name).exists()


def test_final_response_json_includes_session_status_and_artifact_path(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(["run", "--input", "想出去玩", "--state-dir", str(state_dir), "--json", "--mock-providers"])
    _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]
    final_response = json.loads((run_dir / "final_response.json").read_text(encoding="utf-8"))

    assert final_response["sessionId"]
    assert final_response["status"] == "needs_confirmation"
    assert final_response["artifactPath"] == f"artifact://{run_dir.name}"
    assert "artifactPathAbsolute" not in final_response


def test_run_debug_final_response_includes_runtime_debug(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
            "--debug",
        ]
    )
    payload = _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]
    final_response = json.loads((run_dir / "final_response.json").read_text(encoding="utf-8"))

    assert payload["debug"]["toolLoopEntered"] is False
    assert payload["debug"]["webSearchCalls"] == 0
    assert "toolLoopEntered" in final_response["debug"]


def test_run_with_mock_providers_can_write_draft_itinerary(tmp_path, capsys, goal_aligned_runtime_initial_plan):
    state_dir = tmp_path / "runs"
    exit_code = main(
        [
            "run",
            "--input",
            _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
            "--city",
            "北京",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    payload = _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]
    final_response = json.loads((run_dir / "final_response.json").read_text(encoding="utf-8"))
    snapshot = json.loads((run_dir / "session_snapshot.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["status"] == "draft_pending_grounding"
    assert payload["terminalStatus"] == "draft_pending_grounding"
    assert payload["activeVersionId"]
    assert final_response["activeVersionId"] == payload["activeVersionId"]
    assert snapshot["active_version"]["id"] == payload["activeVersionId"]
    assert snapshot["itinerary_patches"][0]["validation_status"] == "accepted"


def test_run_with_input_file_and_output_file(tmp_path, capsys, goal_aligned_runtime_initial_plan):
    input_file = tmp_path / "input.txt"
    output_file = tmp_path / "final.json"
    input_file.write_text(
        _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "run",
            "--input-file",
            str(input_file),
            "--state-dir",
            str(tmp_path / "runs"),
            "--output",
            str(output_file),
            "--json",
            "--mock-providers",
        ]
    )
    stdout_payload = _json_stdout(capsys)
    file_payload = json.loads(output_file.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert stdout_payload["status"] == "draft_pending_grounding"
    assert stdout_payload["terminalStatus"] == "draft_pending_grounding"
    assert stdout_payload["activeVersionChanged"] is True
    assert file_payload["status"] == "draft_pending_grounding"
    assert file_payload["terminalStatus"] == "draft_pending_grounding"
    assert file_payload["activeVersionChanged"] is True
    assert file_payload["activeVersionId"] == stdout_payload["activeVersionId"]


def test_artifact_jsonl_files_are_valid_json(tmp_path, capsys, goal_aligned_runtime_initial_plan):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]

    assert {path.name for path in run_dir.iterdir() if path.is_file()} == _expected_artifact_files()
    decision = json.loads((run_dir / "agent_decision.json").read_text(encoding="utf-8"))
    assert set(decision["rawDecision"]) == {
        "schemaVersion",
        "primaryAction",
        "actionDirective",
    }
    assert decision["rawDecision"]["primaryAction"] == "draft_itinerary"
    assert decision["rawDecision"]["schemaVersion"] == "agent-decision-v3"
    gated = decision["gatedDecision"]
    assert gated["decisionSummary"]
    assert gated["reasonCodes"]
    assert gated["stopCondition"]["type"]
    assert gated["effectiveWriteRisk"] == "high"
    planning_events = [
        json.loads(line)
        for line in (run_dir / "planning_steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decision_event = next(item for item in planning_events if item["type"] == "agent_decision")
    assert decision_event["metadata"]["primaryAction"] == "draft_itinerary"
    assert decision_event["metadata"]["effectiveWriteRisk"] == "high"
    assert decision_event["metadata"]["effectiveTools"] == ["patch_itinerary", "resolve_poi"]
    assert decision_event["metadata"]["proposedExecutionRoute"] == "staged_initial_pipeline"
    assert decision_event["metadata"]["decisionDurationMs"] >= 0
    for name in (
        "planning_steps.jsonl",
        "tool_events.jsonl",
        "patches.jsonl",
        "errors.jsonl",
        "agent_observations.jsonl",
        "agent_decisions.jsonl",
        "agent_action_outcomes.jsonl",
    ):
        for line in (run_dir / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                assert json.loads(line)


def test_replay_artifact_success(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    replay_output = tmp_path / "replay.json"
    exit_code = main(
        [
            "replay",
            "--artifact",
            run_payload["artifactPath"],
            "--output",
            str(replay_output),
            "--json",
        ]
    )
    replay_payload = _json_stdout(capsys)

    assert exit_code == 0
    assert replay_payload["status"] == "success"
    assert replay_payload["activeVersionId"] == run_payload["activeVersionId"]
    assert json.loads(replay_output.read_text(encoding="utf-8")) == replay_payload


def test_replay_artifact_does_not_open_database(tmp_path, capsys, monkeypatch):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)

    def fail_open_db():
        raise AssertionError("replay must not open SQLite")

    monkeypatch.setattr("src.cli.agent_cli._open_db", fail_open_db)
    exit_code = main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    replay_payload = _json_stdout(capsys)

    assert exit_code == 0
    assert replay_payload["status"] == "success"


def test_replay_fails_when_required_json_file_is_missing(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    (Path(run_payload["artifactPath"]) / "context.json").unlink()

    exit_code = main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    replay_payload = _json_stdout(capsys)

    assert exit_code == 1
    assert replay_payload["status"] == "failed"
    assert any(error["file"] == "context.json" for error in replay_payload["errors"])


def test_replay_fails_when_jsonl_line_is_invalid(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    with (Path(run_payload["artifactPath"]) / "tool_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    exit_code = main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    replay_payload = _json_stdout(capsys)

    assert exit_code == 1
    assert replay_payload["status"] == "failed"
    assert any(error["file"] == "tool_events.jsonl" for error in replay_payload["errors"])


def test_replay_fails_when_active_version_mismatches_snapshot(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    final_path = Path(run_payload["artifactPath"]) / "final_response.json"
    final_response = json.loads(final_path.read_text(encoding="utf-8"))
    final_response["activeVersionId"] = "ver_mismatch"
    final_path.write_text(json.dumps(final_response, ensure_ascii=False), encoding="utf-8")

    exit_code = main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    replay_payload = _json_stdout(capsys)

    assert exit_code == 1
    assert replay_payload["status"] == "failed"
    assert any("activeVersionId" in error["message"] for error in replay_payload["errors"])


def test_export_state_success(tmp_path, capsys, goal_aligned_runtime_initial_plan):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    output_file = tmp_path / "state.json"
    exit_code = main(["export-state", "--session-id", run_payload["sessionId"], "--output", str(output_file), "--json"])
    export_payload = _json_stdout(capsys)
    file_payload = json.loads(output_file.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert export_payload["readOnly"] is True
    assert export_payload["sessionId"] == run_payload["sessionId"]
    assert export_payload["active_itinerary_snapshot"]
    assert file_payload["sessionId"] == run_payload["sessionId"]


def test_invalid_input_writes_errors_jsonl_and_returns_non_zero(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    exit_code = main(["run", "--input", "", "--state-dir", str(state_dir), "--json", "--mock-providers"])
    payload = _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]
    errors = (run_dir / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()

    assert exit_code != 0
    assert payload["status"] == "failed"
    assert errors
    assert json.loads(errors[0])["errorCode"] == "invalid_input"


def test_provider_unavailable_writes_artifact_without_fake_success(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    exit_code = main(["run", "--input", "北京一天", "--state-dir", str(state_dir), "--json"])
    payload = _json_stdout(capsys)
    run_dir = _run_dirs(state_dir)[0]
    errors = [json.loads(line) for line in (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
    snapshot = json.loads((run_dir / "session_snapshot.json").read_text(encoding="utf-8"))

    assert exit_code == 4
    assert payload["status"] == "provider_unavailable"
    assert payload["terminalStatus"] == "provider_unavailable"
    assert payload["activeVersionChanged"] is False
    assert payload["activeVersionId"] is None
    assert errors[0]["stage"] == "provider_preflight"
    assert errors[0]["errorCode"] == "provider_unavailable"
    assert snapshot["conversation_session"] is None
    assert (run_dir / "final_response.json").exists()
    assert (run_dir / "README.md").exists()


def test_invalid_patch_returns_validation_failed_and_preserves_active_version(
    tmp_path, goal_aligned_runtime_initial_plan
):
    success, success_exit = _run_with_provider(
        tmp_path,
        RuntimeMockAgentProvider(),
        _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
    )
    before_version = success.active_version_id
    failed, failed_exit = _run_with_provider(
        tmp_path,
        InvalidPatchProvider(),
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
        session_id=success.session_id,
    )
    run_dir = Path(failed.artifact_path)
    errors = [json.loads(line) for line in (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
    patches = [json.loads(line) for line in (run_dir / "patches.jsonl").read_text(encoding="utf-8").splitlines()]
    verifier = json.loads((run_dir / "verifier_report.json").read_text(encoding="utf-8"))

    assert success_exit == 0
    assert failed_exit == 2
    assert failed.status == "validation_failed"
    assert _active_version_id(success.session_id) == before_version
    assert errors[-1]["errorCode"] == "validation_failed"
    assert verifier
    assert any(patch["validation_status"] == "rejected" for patch in patches)
    assert any(patch["validation_errors_json"] for patch in patches if patch["validation_status"] == "rejected")


def test_stale_base_version_returns_stale_status_and_replayable_artifact(tmp_path, goal_aligned_runtime_initial_plan):
    success, _ = _run_with_provider(
        tmp_path,
        RuntimeMockAgentProvider(),
        _route_ready_input("帮我安排2026年10月1日北京一日游，1人，预算500，公共交通，必去故宫博物院和景山公园"),
    )
    before_version = success.active_version_id
    stale, stale_exit = _run_with_provider(
        tmp_path,
        StalePatchProvider(),
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
        session_id=success.session_id,
    )
    run_dir = Path(stale.artifact_path)
    errors = [json.loads(line) for line in (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
    replay = TripAgentRuntime.replay_artifact(stale.artifact_path)

    assert stale_exit == 5
    assert stale.status == "stale_version"
    assert _active_version_id(success.session_id) == before_version
    assert errors[-1]["errorCode"] == "stale_base_version"
    assert replay["status"] == "success"


@pytest.mark.xfail(
    reason="initial ordinary-language drafts no longer enter the legacy generic tool loop",
    strict=True,
)
def test_tool_loop_overrun_after_verified_write_returns_partial_success(tmp_path):
    final, exit_code = _run_with_provider(
        tmp_path,
        OverrunAfterWriteProvider(),
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
    )
    snapshot = json.loads((Path(final.artifact_path) / "session_snapshot.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert final.status == "partial_success"
    assert final.active_version_id
    assert snapshot["conversation_session"]["active_version_id"] == final.active_version_id


@pytest.mark.xfail(
    reason="initial ordinary-language drafts no longer enter the legacy generic tool loop",
    strict=True,
)
def test_success_with_rejected_candidate_diagnostics_stays_success(tmp_path):
    final, exit_code = _run_with_provider(
        tmp_path,
        SuccessWithRejectedDiagnosticsProvider(),
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
    )

    assert exit_code == 0
    assert final.status == "success"
    assert final.active_version_id


def test_legacy_tool_loop_overrun_provider_is_not_called_without_persisted_target(tmp_path):
    provider = OverrunBeforeWriteProvider()
    final, exit_code = _run_with_provider(
        tmp_path,
        provider,
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
    )

    assert provider.tool_loop_calls == 0
    assert exit_code == 3
    assert final.status == "needs_confirmation"
    assert final.terminal_status == "needs_confirmation"
    assert final.active_version_id is None
    assert final.active_version_changed is False


@pytest.mark.xfail(
    reason="initial ordinary-language drafts no longer enter the legacy generic tool loop",
    strict=True,
)
def test_runtime_error_returns_failed_artifact_without_traceback_by_default(tmp_path):
    final, exit_code = _run_with_provider(
        tmp_path,
        RuntimeErrorProvider(),
        "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
    )
    run_dir = Path(final.artifact_path)
    errors = [json.loads(line) for line in (run_dir / "errors.jsonl").read_text(encoding="utf-8").splitlines()]

    assert exit_code == 1
    assert final.status == "failed"
    assert final.active_version_id is None
    for name in (
        "manifest.json",
        "input.json",
        "final_response.json",
        "session_snapshot.json",
        "errors.jsonl",
        "README.md",
    ):
        assert (run_dir / name).exists()
    assert errors[-1]["errorCode"] == "runtime_failed"
    assert "traceback" not in errors[-1]


def test_inspect_is_read_only(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(["run", "--input", "想出去玩", "--state-dir", str(state_dir), "--json", "--mock-providers"])
    run_payload = _json_stdout(capsys)
    session_id = run_payload["sessionId"]
    before_count = _count_turns(session_id)

    exit_code = main(["inspect", "--session-id", session_id, "--json"])
    inspect_payload = _json_stdout(capsys)
    after_count = _count_turns(session_id)

    assert exit_code == 0
    assert inspect_payload["readOnly"] is True
    assert inspect_payload["sessionId"] == session_id
    assert inspect_payload["activePlanId"]
    assert inspect_payload["latestPlanningRun"]["run_type"] == "agent_clarification"
    assert before_count == after_count


def test_inspect_export_and_replay_are_read_only(tmp_path, capsys):
    state_dir = tmp_path / "runs"
    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    run_payload = _json_stdout(capsys)
    before = _state_counts(run_payload["sessionId"])

    main(["inspect", "--session-id", run_payload["sessionId"], "--json"])
    _json_stdout(capsys)
    main(["export-state", "--session-id", run_payload["sessionId"], "--json"])
    _json_stdout(capsys)
    main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    _json_stdout(capsys)
    after = _state_counts(run_payload["sessionId"])

    assert before == after


def test_secret_values_not_written_to_stdout_artifact_export_or_replay(tmp_path, capsys, monkeypatch):
    secret = "sk-testsecretvalue1234567890"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setenv("MAP_PROVIDER_KEY", secret)
    get_settings.cache_clear()
    state_dir = tmp_path / "runs"

    main(
        [
            "run",
            "--input",
            "北京一天，明天出发，1人，预算500，公共交通，轻松一点",
            "--state-dir",
            str(state_dir),
            "--json",
            "--mock-providers",
        ]
    )
    stdout = capsys.readouterr().out
    run_payload = json.loads(stdout)
    run_dir = _run_dirs(state_dir)[0]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in run_dir.iterdir() if path.is_file())
    main(["export-state", "--session-id", run_payload["sessionId"], "--json"])
    export_stdout = capsys.readouterr().out
    main(["replay", "--artifact", run_payload["artifactPath"], "--json"])
    replay_stdout = capsys.readouterr().out

    assert secret not in stdout
    assert secret not in combined
    assert secret not in export_stdout
    assert secret not in replay_stdout
    assert "[redacted]" in combined


def test_run_does_not_require_frontend(tmp_path, capsys):
    exit_code = main(
        [
            "run",
            "--input",
            "想出去玩",
            "--state-dir",
            str(tmp_path / "runs"),
            "--json",
            "--mock-providers",
        ]
    )
    payload = _json_stdout(capsys)

    assert exit_code == 3
    assert payload["sessionId"]


def test_recorded_amap_replay_uses_production_poi_and_route_parsers_without_network(monkeypatch):
    import socket

    from src.models.poi import POI
    from src.runtime.recorded_amap_replay import recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService

    def forbid_network(*_args, **_kwargs):
        raise AssertionError("recorded AMap replay must not open a network socket")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture) as replay:
        service = MapPoiService()
        tsinghua = service.search("北京", "清华大学", "campus", limit=5).pois[0]
        pku = service.search("北京", "北京大学", "campus", limit=5).pois[0]

        def poi(item) -> POI:
            return POI(
                id=item.id,
                amap_id=item.id,
                name=item.name,
                city=item.city,
                category=item.category,
                latitude=item.latitude,
                longitude=item.longitude,
                source=item.source,
                confidence=item.confidence,
                type=item.type,
                district=item.district,
                address=item.address,
            )

        route = RouteService()._build_amap_route("recorded_test", 1, 1, poi(tsinghua), poi(pku), "transit")

    assert tsinghua.name == "清华大学"
    assert pku.name == "北京大学"
    assert tsinghua.longitude != pku.longitude
    assert route.distance_meters == 2406
    assert route.duration_seconds == 2230
    assert route.polyline
    assert [item["endpoint"] for item in replay.requests] == [
        "/v3/place/text",
        "/v3/place/text",
        "/v3/direction/transit/integrated",
    ]


def test_recorded_amap_replay_unknown_request_fails_closed():
    from src.runtime.recorded_amap_replay import RecordedAmapReplayError, recorded_amap_replay_scope
    from src.services.map_poi_service import MapPoiService

    fixture = Path("backend/evals/fixtures/beijing_amap_sanitized_recording.json")
    with recorded_amap_replay_scope(fixture):
        with pytest.raises(RecordedAmapReplayError) as caught:
            MapPoiService().search("北京", "未录制的地点", "campus", limit=5)

    assert "recorded AMap request not found" in str(caught.value)


def _quality_mutation_artifact() -> dict:
    segments = [
        {
            "id": "campus_1",
            "kind": "visit",
            "startTime": "09:00",
            "endTime": "11:00",
            "poi": {
                "name": "清华大学",
                "source": "amap-place-search",
                "category": "campus",
                "longitude": 116.32,
                "latitude": 40.00,
            },
            "notes": "campus",
        },
        {
            "id": "campus_2",
            "kind": "visit",
            "startTime": "12:00",
            "endTime": "14:00",
            "poi": {
                "name": "北京大学",
                "source": "amap-place-search",
                "category": "campus",
                "longitude": 116.31,
                "latitude": 39.99,
            },
            "notes": "campus",
        },
    ]
    return {
        "context": {},
        "itinerarySnapshot": {
            "hardConstraints": {
                "campusTier": {"value": "985", "expectedCount": 2, "source": "request", "relaxed": False}
            },
            "days": [{"dayNumber": 1, "segments": segments}],
            "routeOptions": [
                {
                    "fromSegmentId": "campus_1",
                    "toSegmentId": "campus_2",
                    "isSelected": True,
                    "mode": "transit",
                    "durationSeconds": 1200,
                    "distanceMeters": 2500,
                }
            ],
        },
        "planningSteps": [{"metadata": {"resultPreview": {"routeStatus": "route_ready"}}}],
        "toolEvents": [],
        "sessionSnapshot": {},
        "verifierReport": {},
    }


def test_quality_eval_mutations_reject_non985_missing_pair_and_route_event_mismatch():
    evaluator = AgentQualityEvaluator(None)

    non985 = _quality_mutation_artifact()
    non985["itinerarySnapshot"]["days"][0]["segments"][1]["poi"]["name"] = "北京语言大学"
    metrics = evaluator._metrics(non985, {})
    failures = evaluator._expectation_failures(metrics, {"non985CampusCount": 0, "hardConstraintMustNotRelax": True})
    assert any("non985CampusCount mismatch" in item for item in failures)

    missing_pair = _quality_mutation_artifact()
    missing_pair["itinerarySnapshot"]["routeOptions"] = []
    metrics = evaluator._metrics(missing_pair, {})
    failures = evaluator._expectation_failures(
        metrics, {"requiredRouteLegCount": 1, "coveredRouteLegCount": 1, "missingRouteLegCount": 0}
    )
    assert any("coveredRouteLegCount mismatch" in item for item in failures)
    assert any("route-ready event disagrees" in item for item in failures)

    event_mismatch = _quality_mutation_artifact()
    event_mismatch["planningSteps"][0]["metadata"]["resultPreview"]["routeStatus"] = "route_partial"
    metrics = evaluator._metrics(event_mismatch, {})
    failures = evaluator._expectation_failures(
        metrics, {"requiredRouteLegCount": 1, "coveredRouteLegCount": 1, "missingRouteLegCount": 0}
    )
    assert any("route-ready event disagrees" in item for item in failures)


def test_quality_eval_mutations_reject_coffee_dinner_and_unrelaxed_family_mismatch():
    evaluator = AgentQualityEvaluator(None)
    artifact = _quality_mutation_artifact()
    artifact["itinerarySnapshot"]["days"][0]["segments"].append(
        {
            "id": "dinner",
            "kind": "meal",
            "startTime": "18:00",
            "endTime": "19:00",
            "poi": {
                "name": "校园咖啡馆",
                "source": "amap-place-search",
                "category": "food",
                "longitude": 116.33,
                "latitude": 40.01,
            },
            "notes": "晚餐; mealFamilyMismatch=true",
        }
    )
    metrics = evaluator._metrics(artifact, {})
    failures = evaluator._expectation_failures(metrics, {"coffeeUsedAsDinnerCount": 0, "familyMismatchSlotCount": 0})
    assert any("coffeeUsedAsDinnerCount mismatch" in item for item in failures)
    assert any("familyMismatchSlotCount mismatch" in item for item in failures)


def test_runtime_status_preserves_structured_local_option_needs_confirmation(monkeypatch):
    runtime = object.__new__(TripAgentRuntime)
    monkeypatch.setattr(runtime, "_turn_payload", lambda _turn_id: {})
    response = SimpleNamespace(
        pending_poi_candidates=[],
        terminal_status="needs_confirmation",
        assistant_turn=SimpleNamespace(id="turn_waiting", content="", status="active"),
        warnings=[],
        planning_steps=[],
        tool_events=[],
        version=None,
        planning_run=None,
    )

    assert runtime._status_from_response(response) == "needs_confirmation"


def test_runtime_status_keeps_successful_version_when_soft_pending_candidates_remain(monkeypatch):
    runtime = object.__new__(TripAgentRuntime)
    monkeypatch.setattr(runtime, "_turn_payload", lambda _turn_id: {})
    response = SimpleNamespace(
        pending_poi_candidates=[SimpleNamespace(id="cand_soft_pending")],
        terminal_status="success",
        assistant_turn=SimpleNamespace(id="turn_committed", content="", status="active"),
        warnings=[],
        planning_steps=[],
        tool_events=[],
        version=SimpleNamespace(id="ver_committed"),
        planning_run=None,
    )

    assert runtime._status_from_response(response) == "success"


def test_runtime_status_requires_confirmation_for_pending_candidates_without_version(monkeypatch):
    runtime = object.__new__(TripAgentRuntime)
    monkeypatch.setattr(runtime, "_turn_payload", lambda _turn_id: {})
    response = SimpleNamespace(
        pending_poi_candidates=[SimpleNamespace(id="cand_unresolved")],
        terminal_status="success",
        assistant_turn=SimpleNamespace(id="turn_pending", content="", status="active"),
        warnings=[],
        planning_steps=[],
        tool_events=[],
        version=None,
        planning_run=None,
    )

    assert runtime._status_from_response(response) == "needs_confirmation"


def test_runtime_status_preserves_explicit_confirmation_even_when_version_is_present(monkeypatch):
    runtime = object.__new__(TripAgentRuntime)
    monkeypatch.setattr(runtime, "_turn_payload", lambda _turn_id: {})
    response = SimpleNamespace(
        pending_poi_candidates=[],
        terminal_status="needs_confirmation",
        assistant_turn=SimpleNamespace(id="turn_waiting_after_version", content="", status="active"),
        warnings=[],
        planning_steps=[],
        tool_events=[],
        version=SimpleNamespace(id="ver_existing"),
        planning_run=None,
    )

    assert runtime._status_from_response(response) == "needs_confirmation"


def test_runtime_extracts_staged_basic_verifier_result_preview():
    runtime = object.__new__(TripAgentRuntime)
    events = [
        {
            "type": "basic_verifier",
            "label": "运行本地 basic verifier",
            "metadata": {
                "resultPreview": {
                    "passed": True,
                    "hardFailures": [],
                    "softFailures": [],
                    "checks": [{"name": "hard_requirement_coverage", "status": "passed"}],
                }
            },
        }
    ]

    report = runtime._verifier_report(events)

    assert runtime._is_verifier_event(events[0]) is True
    assert report["passed"] is True
    assert report["checks"][0]["name"] == "hard_requirement_coverage"


def test_runtime_prefers_post_enrichment_verifiers_over_pre_route_soft_failures():
    runtime = object.__new__(TripAgentRuntime)
    events = [
        {
            "type": "basic_verifier",
            "metadata": {"resultPreview": {"passed": True, "softFailures": ["route pending"], "checks": []}},
        },
        {
            "type": "deterministic_enrichment",
            "metadata": {
                "resultPreview": {
                    "state": "route_ready",
                    "routeCoverage": {"requiredLegCount": 8, "coveredLegCount": 8, "missingLegCount": 0},
                    "mapVerifier": {
                        "passed": True,
                        "hardFailures": [],
                        "softFailures": [],
                        "checks": [{"name": "map"}],
                    },
                    "routeVerifier": {
                        "passed": True,
                        "hardFailures": [],
                        "softFailures": [],
                        "checks": [{"name": "route"}],
                    },
                    "scheduleVerifier": {
                        "passed": True,
                        "hardFailures": [],
                        "softFailures": [],
                        "checks": [{"name": "schedule"}],
                    },
                }
            },
        },
    ]

    report = runtime._verifier_report(events)

    assert report["passed"] is True
    assert report["softFailures"] == []
    assert report["routeCoverage"]["coveredLegCount"] == 8
    assert [item["name"] for item in report["checks"]] == ["map", "route", "schedule"]
