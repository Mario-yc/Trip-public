from datetime import datetime, timezone
from time import perf_counter
from typing import Optional

from src.api.schemas.agent import AgentPlanningEventResponse


HARNESS_STAGES = ("plan", "resolve_poi", "apply_patch", "verify", "respond")


class AgentHarnessTrace:
    def __init__(self, session_id: str, turn_id: Optional[str] = None):
        self.session_id = session_id
        self.turn_id = turn_id

    def event(
        self,
        stage: str,
        label: str,
        status: str = "completed",
        detail: str = "",
        provider_name: Optional[str] = None,
        tool_provider: Optional[str] = None,
        fallback_used: bool = False,
        failure_reason: Optional[str] = None,
        duration_ms: int = 0,
        metadata: Optional[dict] = None,
    ) -> AgentPlanningEventResponse:
        return AgentPlanningEventResponse(
            type=stage,
            label=label,
            status=status,
            detail=detail,
            sessionId=self.session_id,
            turnId=self.turn_id,
            providerName=provider_name,
            toolProvider=tool_provider or provider_name,
            fallbackUsed=fallback_used,
            failureReason=failure_reason,
            durationMs=max(0, int(duration_ms)),
            metadata=metadata or {},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def plan(self, detail: str, metadata: Optional[dict] = None) -> AgentPlanningEventResponse:
        return self.event(
            "plan",
            "Agent plan/context",
            detail=detail,
            provider_name="agent-service",
            metadata=metadata,
        )

    def resolve_poi(
        self,
        status: str,
        detail: str,
        duration_ms: int = 0,
        failure_reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> AgentPlanningEventResponse:
        return self.event(
            "resolve_poi",
            "AMap POI grounding",
            status=status,
            detail=detail,
            provider_name="amap",
            tool_provider="poi_resolution_service",
            failure_reason=failure_reason,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    def apply_patch(
        self,
        status: str,
        detail: str,
        duration_ms: int = 0,
        failure_reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> AgentPlanningEventResponse:
        return self.event(
            "apply_patch",
            "Versioned itinerary patch",
            status=status,
            detail=detail,
            provider_name="sqlite",
            tool_provider="itinerary_patch_service",
            failure_reason=failure_reason,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    def respond(self, status: str, detail: str, metadata: Optional[dict] = None) -> AgentPlanningEventResponse:
        return self.event(
            "respond",
            "Agent response",
            status=status,
            detail=detail,
            provider_name="agent-service",
            metadata=metadata,
        )

    def verify(
        self,
        status: str,
        detail: str,
        duration_ms: int = 0,
        failure_reason: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> AgentPlanningEventResponse:
        return self.event(
            "verify",
            "Rule verifier",
            status=status,
            detail=detail,
            provider_name="agent-verifier-service",
            tool_provider="agent_verifier_service",
            failure_reason=failure_reason,
            duration_ms=duration_ms,
            metadata=metadata,
        )


class TraceTimer:
    def __init__(self):
        self.started_at = perf_counter()

    def elapsed_ms(self) -> int:
        return int((perf_counter() - self.started_at) * 1000)
