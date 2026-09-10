from src.services.agent_harness_trace_service import AgentHarnessTrace, HARNESS_STAGES


def test_trace_event_contains_required_stage_fields():
    trace = AgentHarnessTrace("sess_trace", "turn_trace")

    event = trace.apply_patch(
        "completed",
        "Applied versioned patch.",
        duration_ms=12,
        metadata={"versionId": "ver_trace"},
    )
    payload = event.model_dump(by_alias=True)

    assert HARNESS_STAGES == ("plan", "resolve_poi", "apply_patch", "verify", "respond")
    assert payload["type"] == "apply_patch"
    assert payload["status"] == "completed"
    assert payload["sessionId"] == "sess_trace"
    assert payload["turnId"] == "turn_trace"
    assert payload["providerName"] == "sqlite"
    assert payload["toolProvider"] == "itinerary_patch_service"
    assert payload["fallbackUsed"] is False
    assert payload["failureReason"] is None
    assert payload["durationMs"] == 12
    assert payload["metadata"] == {"versionId": "ver_trace"}
