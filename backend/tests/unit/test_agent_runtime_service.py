import json
from json import JSONDecodeError
from urllib.error import HTTPError, URLError

import pytest
from fastapi import HTTPException

from src.services.agent_runtime_service import (
    AgentRunContext,
    AgentRuntimeLimits,
    ProviderCircuitBreaker,
    classify_provider_error,
    compact_agent_context,
)


def test_compact_agent_context_preserves_current_contract() -> None:
    turns = [
        {"role": "user", "content": f"old turn {index}", "turnIndex": index, "itineraryVersionId": f"v_{index}"}
        for index in range(20)
    ]
    context = {
        "latestUserMessage": "把故宫放到下午",
        "activeConversationTurns": turns,
        "currentItinerarySnapshot": {"activeVersionId": "ver_current", "title": "北京行程", "days": []},
        "requiredToolSequence": ["read_itinerary", "patch_itinerary"],
        "timelineWriteContract": {"mustWrite": True},
    }

    compacted = compact_agent_context(context, AgentRuntimeLimits(max_context_turns=6, max_context_chars=2000))

    assert compacted["latestUserMessage"] == "把故宫放到下午"
    assert compacted["requiredToolSequence"] == ["read_itinerary", "patch_itinerary"]
    assert compacted["timelineWriteContract"] == {"mustWrite": True}
    assert compacted["currentItinerarySnapshot"]["activeVersionId"] == "ver_current"
    assert len(compacted["activeConversationTurns"]) == 6
    assert compacted["compactedTurnSummary"]["compactedTurnCount"] == 14


def test_classify_provider_errors() -> None:
    timeout = classify_provider_error(TimeoutError("timed out"))
    auth = classify_provider_error(HTTPException(status_code=401, detail="bad api key"))
    invalid_json = classify_provider_error(ValueError("response was not valid JSON"))
    real_json_error = classify_provider_error(JSONDecodeError("Expecting value", "", 0))
    bad_request = classify_provider_error(HTTPError("https://example.test", 400, "Bad Request", {}, None))
    rate_limit = classify_provider_error(HTTPError("https://example.test", 429, "Too Many Requests", {}, None))
    server_error = classify_provider_error(HTTPError("https://example.test", 503, "Unavailable", {}, None))
    network = classify_provider_error(URLError("temporary failure"))
    context_too_long = classify_provider_error(ValueError("context length is too large"))
    unknown = classify_provider_error(ValueError("unexpected provider failure"))

    assert timeout.category == "timeout"
    assert timeout.retryable is True
    assert auth.category == "auth"
    assert auth.retryable is False
    assert invalid_json.category == "invalid_json"
    assert real_json_error.category == "invalid_json"
    assert bad_request.category == "bad_request"
    assert bad_request.retryable is False
    assert rate_limit.category == "rate_limit"
    assert rate_limit.retryable is True
    assert server_error.category == "provider_server"
    assert server_error.retryable is True
    assert network.category == "network"
    assert network.retryable is True
    assert context_too_long.category == "context_too_long"
    assert context_too_long.should_compact is True
    assert unknown.category == "unknown"
    assert unknown.retryable is False


def test_agent_run_context_provider_context_compacts_and_exposes_limits() -> None:
    context = {
        "latestUserMessage": "今天只改故宫时间",
        "activeConversationTurns": [
            {"role": "user", "content": f"turn {index}", "turnIndex": index}
            for index in range(8)
        ],
        "currentItinerarySnapshot": {"activeVersionId": "ver_current", "days": []},
    }
    run_context = AgentRunContext(
        session={"id": "sess_1"},
        request_context=context,
        source_turn_id="turn_1",
        active_version_id="ver_current",
        runtime_limits=AgentRuntimeLimits(max_tool_rounds=2, max_tool_calls_per_round=1, max_context_turns=3),
    )

    provider_context = run_context.provider_context()

    assert provider_context["latestUserMessage"] == "今天只改故宫时间"
    assert provider_context["currentItinerarySnapshot"]["activeVersionId"] == "ver_current"
    assert len(provider_context["activeConversationTurns"]) == 3
    assert provider_context["runtimeLimits"] == {
        "maxToolRounds": 2,
        "maxToolCallsPerRound": 1,
        "maxContextTurns": 3,
        "maxRunSeconds": 45,
        "maxToolSeconds": 20,
        "maxPatchSeconds": 12,
    }
    assert provider_context["runtimeDeadlineMonotonic"] > 0


def test_compact_agent_context_trims_large_secondary_context_before_warning() -> None:
    context = {
        "latestUserMessage": "保留当前请求",
        "activeConversationTurns": [{"role": "user", "content": "current"}],
        "currentItinerarySnapshot": {
            "activeVersionId": "ver_current",
            "itineraryPlan": {
                "id": "plan_1",
                "city": "北京",
                "days": [
                    {
                        "id": f"day_{day}",
                        "dayNumber": day,
                        "segments": [
                            {
                                "id": f"seg_{day}_{index}",
                                "startTime": "09:00",
                                "poi": {"name": "故宫" + "x" * 200},
                                "transportMode": "walk",
                            }
                            for index in range(20)
                        ],
                    }
                    for day in range(1, 8)
                ],
            },
        },
        "candidateMapPois": [{"name": "候选" + "x" * 1000, "secret": "not-secret"} for _ in range(20)],
        "pendingAmapPoiCandidates": [{"query": "候选" + "y" * 1000} for _ in range(20)],
        "selectedSkills": [{"text": "skill" * 1000} for _ in range(20)],
        "skillContext": "skill context " * 1000,
        "requiredToolSequence": ["read_itinerary", "patch_itinerary"],
    }

    compacted = compact_agent_context(context, AgentRuntimeLimits(max_context_turns=4, max_context_chars=2500))

    assert compacted["latestUserMessage"] == "保留当前请求"
    assert compacted["requiredToolSequence"] == ["read_itinerary", "patch_itinerary"]
    assert compacted["currentItinerarySnapshot"]["activeVersionId"] == "ver_current"
    assert len(compacted["candidateMapPois"]) <= 9
    assert len(compacted["skillContext"]) <= 1203
    assert "contextCompactionWarning" in compacted or len(json.dumps(compacted, ensure_ascii=False, default=str)) <= 2500


def test_provider_circuit_breaker_opens_and_recovers_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr("src.services.agent_runtime_service.time.monotonic", lambda: now)
    breaker = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=10)

    breaker.record_failure("deepseek:test")
    breaker.assert_allowed("deepseek:test")
    breaker.record_failure("deepseek:test")
    with pytest.raises(HTTPException):
        breaker.assert_allowed("deepseek:test")

    now = 111.0
    breaker.assert_allowed("deepseek:test")
    breaker.record_failure("deepseek:test")
    breaker.record_success("deepseek:test")
    breaker.assert_allowed("deepseek:test")
