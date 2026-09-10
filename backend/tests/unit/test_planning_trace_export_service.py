from __future__ import annotations

import json

from src.services.agent_service import AgentService
from src.services.planning_trace_export_service import (
    _project_brief_metrics,
    _project_web_discovery_attempts,
)


def test_web_discovery_projection_drops_nonfinite_and_negative_metrics():
    projected = _project_web_discovery_attempts(
        [
            {
                "query": "北京 高校 官方 地点",
                "providerName": "recorded-web",
                "providerStatus": "success",
                "status": "unresolved",
                "durationMs": float("nan"),
                "webDurationMs": float("inf"),
                "amapGroundingMs": -1,
                "resultCount": -2,
                "scope": {
                    "briefId": "brief-one",
                    "poolId": "pool-one",
                    "planningSlotId": "slot-one",
                    "dayNumber": 1,
                },
                "seedGroundings": [
                    {
                        "seedName": "清华大学",
                        "providerName": "amap-place-search",
                        "status": "unresolved",
                        "durationMs": float("nan"),
                        "candidateCount": -3,
                    }
                ],
            }
        ]
    )

    assert len(projected) == 1
    attempt = projected[0]
    for key in ("durationMs", "webDurationMs", "amapGroundingMs", "resultCount"):
        assert key not in attempt
    assert "durationMs" not in attempt["seedGroundings"][0]
    assert "candidateCount" not in attempt["seedGroundings"][0]
    json.dumps(projected, allow_nan=False)


def test_route_schedule_projection_exception_survives_both_safe_trace_projections():
    metrics = [
        {
            "briefId": "fallback_1_culture_deep_dive",
            "status": "failed",
            "routeProviderState": "ok",
            "verifierPassed": False,
            "draftVerifierPassed": True,
            "routeQualityIssueDetails": [
                {
                    "code": "route_schedule_projection_failed",
                    "workerFailureClass": "ValueError",
                    "sanitizedWorkerFailureMessage": "route window cannot be projected",
                    "workerTimedOut": False,
                    "workerTimeoutMs": 12000,
                    "Authorization": "must-not-leak",
                    "providerPayload": {"token": "must-not-leak"},
                }
            ],
        }
    ]

    expected = {
        "code": "route_schedule_projection_failed",
        "workerFailureClass": "ValueError",
        "sanitizedWorkerFailureMessage": "route window cannot be projected",
        "workerTimedOut": False,
        "workerTimeoutMs": 12000,
    }
    exported = _project_brief_metrics(metrics)
    assistant_trace = AgentService._planning_trace_brief_metrics(metrics)

    assert exported[0]["routeQualityIssueDetails"] == [expected]
    assert assistant_trace[0]["routeQualityIssueDetails"] == [expected]
    assert exported[0]["draftVerifierPassed"] is True
    assert assistant_trace[0]["draftVerifierPassed"] is True
    assert "Authorization" not in json.dumps(exported)
    assert "providerPayload" not in json.dumps(assistant_trace)
