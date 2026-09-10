from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, PrivateAttr


class PlanningToolCallResponse(BaseModel):
    id: str
    tool_name: str = Field(alias="toolName")
    status: str
    provider_name: str = Field(default="", alias="providerName")
    source_name: str = Field(default="", alias="sourceName")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    queried_at: str = Field(default="", alias="queriedAt")
    confidence: float = 0
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    failure_reason: Optional[str] = Field(default=None, alias="failureReason")
    user_visible_caveat: str = Field(default="", alias="userVisibleCaveat")
    summary: str = ""
    _trace_summary: dict[str, Any] = PrivateAttr(default_factory=dict)

    model_config = {"populate_by_name": True}


class SourceAssessmentResponse(BaseModel):
    source_name: str = Field(alias="sourceName")
    source_url: Optional[str] = Field(default=None, alias="sourceUrl")
    credibility_rank: str = Field(alias="credibilityRank")
    credibility_label: str = Field(alias="credibilityLabel")
    provider_name: str = Field(alias="providerName")
    confidence: float = 0
    fallback_used: bool = Field(default=False, alias="fallbackUsed")
    conflict_detected: bool = Field(default=False, alias="conflictDetected")
    conflict_reason: str = Field(default="", alias="conflictReason")
    recommendation: str = ""

    model_config = {"populate_by_name": True}


class FeasibilityIssueResponse(BaseModel):
    code: str
    severity: str
    dimension: str
    message: str
    recommendation: str
    affected_day_id: Optional[str] = Field(default=None, alias="affectedDayId")
    affected_segment_id: Optional[str] = Field(default=None, alias="affectedSegmentId")
    evidence: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class LocalReplanSuggestionResponse(BaseModel):
    id: str
    issue_code: str = Field(alias="issueCode")
    action_type: str = Field(alias="actionType")
    summary: str
    rationale: str
    requires_confirmation: bool = Field(default=True, alias="requiresConfirmation")
    operations: list[dict] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class FeasibilityReportResponse(BaseModel):
    score: int
    risk_level: str = Field(alias="riskLevel")
    route_status: str = Field(
        default="pending_provider_verification",
        alias="routeStatus",
    )
    issues: list[FeasibilityIssueResponse] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    local_replan_suggestions: list[LocalReplanSuggestionResponse] = Field(
        default_factory=list,
        alias="localReplanSuggestions",
    )
    preference_alignment: str = Field(default="", alias="preferenceAlignment")
    checked_at: datetime = Field(alias="checkedAt")

    model_config = {"populate_by_name": True}


class PlanningRunResponse(BaseModel):
    id: str
    run_type: str = Field(alias="runType")
    user_input: str = Field(default="", alias="userInput")
    preference_summary: str = Field(default="", alias="preferenceSummary")
    itinerary_plan_id: Optional[str] = Field(default=None, alias="itineraryPlanId")
    itinerary_version_id: Optional[str] = Field(default=None, alias="itineraryVersionId")
    understood_requirements: dict = Field(default_factory=dict, alias="understoodRequirements")
    constraint_summary: list[dict] = Field(default_factory=list, alias="constraintSummary")
    tool_calls: list[PlanningToolCallResponse] = Field(default_factory=list, alias="toolCalls")
    source_assessments: list[SourceAssessmentResponse] = Field(default_factory=list, alias="sourceAssessments")
    feasibility_report: Optional[FeasibilityReportResponse] = Field(default=None, alias="feasibilityReport")
    final_summary: str = Field(default="", alias="finalSummary")
    created_at: datetime = Field(alias="createdAt")

    model_config = {"populate_by_name": True}
