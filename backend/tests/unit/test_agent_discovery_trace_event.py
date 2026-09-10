from __future__ import annotations

import hashlib
import json

from src.core.database import get_db
from src.services.agent_service import AgentService
from src.services.portfolio_candidate_discovery_service import (
    PortfolioCandidateDiscoveryService,
)
from src.services.planning_run_service import PlanningRunService


def test_agent_discovery_event_persists_only_allowlisted_trace_summary():
    discovery_service = PortfolioCandidateDiscoveryService()
    metrics = {
        "candidateDiscoveryMs": 12.5,
        "webDiscoveryMs": 4.0,
        "amapGroundingMs": 8.5,
        "webSearchCount": 1,
        "providerPayload": {"url": "https://secret.example", "snippet": "leak"},
        "prompt": "hidden prompt",
        "responseHeaders": {"authorization": "Bearer secret"},
        "discoveryProvenance": {"localPath": r"C:\Users\person\trace.json"},
        "reasonCodes": [
            "discovery_completed",
            "https://secret.example/reason",
        ],
        "webDiscoveryAttempts": [
            {
                "query": "北京 高校",
                "providerName": "search-provider",
                "providerStatus": "success",
                "status": "grounded",
                "reasonCodes": ["web_seed_grounded"],
                "durationMs": 4.0,
                "webDurationMs": 1.5,
                "amapGroundingMs": 2.5,
                "resultCount": 2,
                "seedCount": 1,
                "scope": {
                    "briefId": "brief_1",
                    "poolId": "pool_1",
                    "planningSlotId": "slot_1",
                    "dayNumber": 1,
                    "sourceGoalId": "goal_campus",
                },
                "seedGroundings": [
                    {
                        "seedName": "清华大学",
                        "providerName": "amap-place-search",
                        "status": "grounded",
                        "durationMs": 2.5,
                        "candidateCount": 1,
                        "selectedCandidates": [
                            {"amapId": "B000A", "name": "清华大学"}
                        ],
                        "url": "https://secret.example/seed",
                    }
                ],
                "selectedCandidates": [
                    {
                        "amapId": "B000A",
                        "name": "清华大学",
                        "scope": {
                            "briefId": "brief_1",
                            "poolId": "pool_1",
                            "planningSlotId": "slot_1",
                            "dayNumber": 1,
                            "sourceGoalId": "goal_campus",
                        },
                        "providerPayload": {"secret": True},
                    }
                ],
                "responseHeaders": {"cookie": "secret"},
            }
        ],
        "discoveryByBrief": [
            {
                "briefId": "brief_1",
                "poolCount": 1,
                "candidateCount": 2,
                "webSearchCount": 1,
                "amapGroundingCount": 1,
                "webDiscoveryMs": 4.0,
                "amapGroundingMs": 2.5,
                "reasonCodes": ["brief_discovery_complete"],
                "prompt": "hidden",
            }
        ],
    }

    preview = discovery_service.planning_event_preview(metrics)
    service = object.__new__(AgentService)
    service._pipeline_event_perf = None
    service.portfolio_candidate_discovery_service = discovery_service
    events: list[dict] = []
    service._append_pipeline_event(
        events,
        "portfolio_candidate_discovery",
        "补充检索地点候选",
        "completed",
        "候选检索完成。",
        preview,
    )

    result_preview = events[0]["metadata"]["resultPreview"]
    assert {key: result_preview[key] for key in preview} == preview
    assert preview["webDiscoveryAttempts"][0]["scope"] == {
        "briefId": "brief_1",
        "poolId": "pool_1",
        "planningSlotId": "slot_1",
        "dayNumber": 1,
        "sourceGoalId": "goal_campus",
    }
    assert preview["webDiscoveryAttempts"][0]["selectedCandidates"] == [
        {
            "amapId": "B000A",
            "name": "清华大学",
            "scope": {
                "briefId": "brief_1",
                "poolId": "pool_1",
                "planningSlotId": "slot_1",
                "dayNumber": 1,
                "sourceGoalId": "goal_campus",
            },
        }
    ]
    assert preview["webDiscoveryAttempts"][0]["queryFingerprint"] == (
        hashlib.sha256("北京 高校".encode("utf-8")).hexdigest()
    )
    serialized = json.dumps(events, ensure_ascii=False)
    for forbidden in (
        "providerPayload",
        "prompt",
        "responseHeaders",
        "discoveryProvenance",
        "https://",
        "secret.example",
        "北京 高校",
        "authorization",
        r"C:\\Users",
    ):
        assert forbidden not in serialized

    events[0]["metadata"]["resultPreview"]["reasonCodes"].append(
        "authorization:secret"
    )
    tool_calls = service._planning_tool_calls_from_tool_events(events)
    connections = get_db()
    db_connection = next(connections)
    try:
        run = PlanningRunService(db_connection).create_run(
            "agent_message",
            tool_calls_override=tool_calls,
        )
        persisted = json.loads(
            db_connection.execute(
                "SELECT tool_calls_json FROM planning_runs WHERE id = ?",
                (run.id,),
            ).fetchone()[0]
        )
    finally:
        connections.close()
    trace_summary = persisted[0]["traceSummary"]
    assert trace_summary["sequence"] == 1
    assert trace_summary["webDiscoveryAttempts"][0]["scope"] == {
        "briefId": "brief_1",
        "poolId": "pool_1",
        "planningSlotId": "slot_1",
        "dayNumber": 1,
        "sourceGoalId": "goal_campus",
    }
    assert trace_summary["webDiscoveryAttempts"][0]["seedGroundings"][0][
        "selectedCandidates"
    ] == [{"amapId": "B000A", "name": "清华大学"}]
    assert "traceSummary" not in run.model_dump(by_alias=True)["toolCalls"][0]
    persisted_serialized = json.dumps(persisted, ensure_ascii=False)
    for forbidden in (
        "providerPayload",
        "prompt",
        "responseHeaders",
        "discoveryProvenance",
        "https://",
        "secret.example",
        "authorization",
        "authorization:secret",
        r"C:\\Users",
    ):
        assert forbidden not in persisted_serialized


def test_search_profile_compile_event_keeps_safe_family_metrics_and_strips_payloads():
    service = object.__new__(AgentService)
    service.portfolio_candidate_discovery_service = PortfolioCandidateDiscoveryService()
    event = {
        "id": "compile_experience_search_profile",
        "type": "compile_experience_search_profile",
        "metadata": {
            "resultPreview": {
                "compiledSearchProfileCount": 2,
                "distinctSearchProfileFingerprintCount": 2,
                "familySearchProfileCoverageCount": 2,
                "familySearchSemanticMismatchCount": 0,
                "genericScenicCollapseCount": 0,
                "profileMetrics": [
                    {
                        "briefId": "brief_1",
                        "poolId": "pool_local_life",
                        "planningSlotId": "slot_1",
                        "experienceFamily": "local_life",
                        "activityMode": "observe_walk",
                        "searchProfileFingerprint": "a" * 64,
                        "queryPlanCount": 4,
                        "fallbackLevel": 3,
                        "excludedPhysicalPoiCount": 2,
                        "queryModes": ["amap_text", "amap_around", "web_seed_then_amap"],
                        "providerKeys": ["amap", "web"],
                        "keyword": "secret semantic query",
                        "providerPayload": {"url": "https://secret.example"},
                    }
                ],
                "prompt": "hidden prompt",
            }
        },
    }

    summary = service._planning_trace_summary_from_tool_event(event, sequence=4)

    assert summary["compiledSearchProfileCount"] == 2
    assert summary["distinctSearchProfileFingerprintCount"] == 2
    assert summary["familySearchProfileCoverageCount"] == 2
    assert summary["genericScenicCollapseCount"] == 0
    assert summary["profileMetrics"] == [
        {
            "briefId": "brief_1",
            "poolId": "pool_local_life",
            "planningSlotId": "slot_1",
            "experienceFamily": "local_life",
            "activityMode": "observe_walk",
            "searchProfileFingerprint": "a" * 64,
            "queryPlanCount": 4,
            "fallbackLevel": 3,
            "excludedPhysicalPoiCount": 2,
            "queryModes": ["amap_text", "amap_around", "web_seed_then_amap"],
            "providerKeys": ["amap", "web"],
        }
    ]
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "secret semantic query" not in serialized
    assert "providerPayload" not in serialized
    assert "https://" not in serialized
    assert "hidden prompt" not in serialized


def test_search_profile_compile_preview_reports_family_coverage_without_keywords():
    profile = {
        "profileId": "profile_local_life",
        "profileFingerprint": "b" * 64,
        "experienceFamily": "local_life",
        "activityMode": "observe_walk",
        "originalExperienceFamily": "local_life",
        "excludedPhysicalPoiIds": ["B0001", "B0002"],
        "queryPlans": [
            {
                "mode": "amap_text",
                "fallbackLevel": 0,
                "providerCategoryKeys": ["local_service", "market"],
                "keyword": "must not leak",
            },
            {
                "mode": "web_seed_then_amap",
                "fallbackLevel": 3,
                "providerCategoryKeys": ["local_service"],
                "keyword": "must not leak either",
            },
        ],
    }

    preview = AgentService._experience_search_profile_trace_preview(
        [
            {
                "briefId": "brief_1",
                "poolId": "pool_1",
                "requiredSlotIds": ["slot_1"],
                "optionalExperienceFamily": "local_life",
                "searchProfile": profile,
            }
        ]
    )

    assert preview["compiledSearchProfileCount"] == 1
    assert preview["distinctSearchProfileFingerprintCount"] == 1
    assert preview["familySearchProfileCoverageCount"] == 1
    assert preview["familySearchSemanticMismatchCount"] == 0
    assert preview["genericScenicCollapseCount"] == 0
    assert preview["profileMetrics"][0]["providerKeys"] == [
        "local_service",
        "market",
    ]
    serialized = json.dumps(preview, ensure_ascii=False)
    assert "must not leak" not in serialized
