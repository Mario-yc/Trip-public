from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


RuntimeStatus = Literal[
    "running",
    "success",
    "partial_success",
    "draft_pending_grounding",
    "candidate_refresh_required",
    "no_safe_action",
    "needs_confirmation",
    "validation_failed",
    "provider_unavailable",
    "stale_version",
    "failed",
]


class RuntimeErrorRecord(BaseModel):
    error_code: str = Field(alias="errorCode")
    message: str
    stage: str
    timestamp: str
    traceback: Optional[str] = None

    model_config = {"populate_by_name": True}


class RuntimeRunOptions(BaseModel):
    input: str
    city: str = "北京"
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    state_dir: str = Field(default=".ai-runs", alias="stateDir")
    json_output: bool = Field(default=True, alias="json")
    mock_providers: bool = Field(default=False, alias="mockProviders")
    mock_map_provider: dict[str, Any] = Field(default_factory=dict, alias="mockMapProvider")
    selected_agent_choice: Optional[dict[str, Any]] = Field(default=None, alias="selectedAgentChoice")
    baseline_ref: str = Field(default="preview/agent-mvp-foundation-20260629-agentcli", alias="baselineRef")
    debug: bool = False

    model_config = {"populate_by_name": True}


class RuntimeFinalResponse(BaseModel):
    status: RuntimeStatus
    terminal_status: str = Field(default="", alias="terminalStatus")
    active_version_changed: bool = Field(default=False, alias="activeVersionChanged")
    session_id: Optional[str] = Field(default=None, alias="sessionId")
    active_plan_id: Optional[str] = Field(default=None, alias="activePlanId")
    active_version_id: Optional[str] = Field(default=None, alias="activeVersionId")
    assistant_reply: str = Field(default="", alias="assistantReply")
    warnings: list[str] = Field(default_factory=list)
    pending_poi_candidates: list[dict] = Field(default_factory=list, alias="pendingPoiCandidates")
    artifact_path: str = Field(default="", alias="artifactPath")
    artifact_path_absolute: str = Field(default="", alias="artifactPathAbsolute")
    next_actions: list[str] = Field(default_factory=list, alias="nextActions")
    debug: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}
