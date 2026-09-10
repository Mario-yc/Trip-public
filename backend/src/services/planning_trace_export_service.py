from __future__ import annotations

import json
from hashlib import sha256
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from src.services.agent_choice_trace_service import build_structured_choice_trace


_SCOPE_KEYS = (
    ("creativeBriefId", "briefId"),
    ("briefId", "briefId"),
    ("poolId", "poolId"),
    ("planningSlotId", "planningSlotId"),
    ("dayNumber", "dayNumber"),
    ("sourceGoalId", "sourceGoalId"),
)
_COUNT_KEYS = (
    "candidateCount",
    "candidateCounts",
    "selectedAnchorCount",
    "finalSnapshotAnchorCount",
    "routeAnchorSelectedCount",
    "matchedAnchorCount",
    "unmatchedAnchorCount",
)
_METRIC_KEYS = (
    "controllerCalls",
    "controllerDurationMs",
    "candidateDiscoveryMs",
    "webDiscoveryMs",
    "amapGroundingMs",
    "routePreflightMs",
    "repairMs",
    "verifierMs",
    "workerQueueMs",
    "connectDurationMs",
    "ttfbDurationMs",
    "readDurationMs",
    "webSearchCount",
    "amapTextCount",
    "amapAroundCount",
    "amapRouteCount",
    "maxConcurrentBriefWorkers",
    "providerCallCount",
    "compiledSearchProfileCount",
    "distinctSearchProfileFingerprintCount",
    "familySearchProfileCoverageCount",
    "familySearchSemanticMismatchCount",
    "genericScenicCollapseCount",
    "profileCoverageShortcutHitCount",
    "invalidCoverageShortcutCount",
    "semanticCandidateAcceptedCount",
    "semanticCandidateRejectedCount",
    "familySpecificAmapCandidateCount",
    "webSeedGroundedCandidateCount",
    "duplicateExcludedBeforeRouteCount",
    "routePreflightAvoidedBySemanticFilterCount",
)
_DELTA_KEYS = ("versionDelta", "patchDelta", "routeWriteDelta")
_MAX_WEB_DISCOVERY_ATTEMPTS = 64
_MAX_WEB_DISCOVERY_SEEDS_PER_ATTEMPT = 6
_MAX_WEB_DISCOVERY_CANDIDATES_PER_SEED = 4
_TRACE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_REASON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_DOTENV_PATTERN = re.compile(r"(?<![A-Za-z0-9_])\.env(?:\b|[._-])", re.IGNORECASE)
_CREDENTIAL_TEXT_PATTERN = re.compile(
    r"(?:authorization|proxy[-_]?authorization|bearer\s+\S+|"
    r"(?:x[-_])?api[-_ ]?key|apikey|password|passwd|credential|"
    r"client[-_]?secret|secret|set[-_]?cookie|cookie|"
    r"(?:access|refresh|auth|api)?[-_]?token\s*[:=])",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r")|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{2,}\."
    r"[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_FORBIDDEN_URI_SCHEME_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}:(?=\S)|www\.|//",
    re.IGNORECASE,
)
_FORBIDDEN_REASON_CODE_SCHEME_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]{0,31}:(?=\S)",
    re.IGNORECASE,
)
_EMBEDDED_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'(<\[{=:])(?:"
    r"[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+|~[\\/]|"
    r"/(?!/)[^\s\"'<>]+"
    r")",
    re.IGNORECASE,
)
_IMAGE_REFERENCE_PATTERN = re.compile(
    r"(?:^|[\s\"'(<\[{=:])[^\s\"'<>]*\."
    r"(?:avif|bmp|gif|ico|jpe?g|png|svg|webp)"
    r"(?:[?#][^\s\"'<>]*)?(?:$|[\s\"')>\]}])",
    re.IGNORECASE,
)


class PlanningTraceExportService:
    """Projects a single planning run into a safe, read-only diagnostic export."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def export(
        self,
        *,
        session_id: str,
        assistant_turn_id: str,
        planning_run_id: str,
    ) -> dict[str, Any]:
        turn = self._db.execute(
            """SELECT id, planning_run_id, agent_response_json, created_at
            FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant'""",
            (assistant_turn_id, session_id),
        ).fetchone()
        if turn is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "planning_trace_turn_not_found", "message": "未找到可导出的规划结果。"},
            )
        if str(turn["planning_run_id"] or "") != planning_run_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "planning_trace_scope_mismatch", "message": "当前调试详情与规划运行记录不一致。"},
            )

        planning_run = self._db.execute(
            """SELECT id, run_type, tool_calls_json, created_at
            FROM planning_runs WHERE id = ?""",
            (planning_run_id,),
        ).fetchone()
        if planning_run is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "planning_trace_not_found", "message": "未找到对应的规划运行记录。"},
            )

        response = _json_object(turn["agent_response_json"])
        tool_calls = _json_list(planning_run["tool_calls_json"])
        phases = _project_phases(response, tool_calls)
        request_turn = self._db.execute(
            """SELECT * FROM conversation_turns
            WHERE session_id = ? AND planning_run_id = ? AND role = 'user'
            ORDER BY turn_index DESC LIMIT 1""",
            (session_id, planning_run_id),
        ).fetchone()
        request_turn_id = str(request_turn["id"] or "") if request_turn is not None else ""
        choices = self._project_choice_executions(
            session_id,
            assistant_turn_id,
            request_turn_id=request_turn_id,
        )
        continuation_scope = dict(
            choices[0].get("continuationScope") or {}
        ) if choices else {}
        if request_turn is not None:
            choice_trace = build_structured_choice_trace(self._db, request_turn)
            continuation_scope.update(_project_continuation_scope(choice_trace or {}))
        portfolios = self._project_portfolios(
            session_id,
            assistant_turn_id,
            root_portfolio_id=str(continuation_scope.get("rootPortfolioId") or ""),
        )

        return {
            "schemaVersion": "trip-planning-trace-v1",
            "scope": {
                "sessionId": _safe_trace_identifier(session_id),
                "assistantTurnId": _safe_trace_identifier(assistant_turn_id),
                "planningRunId": _safe_trace_identifier(planning_run_id),
            },
            "planningRun": {
                "runType": _safe_trace_text(planning_run["run_type"]),
                "createdAt": _safe_trace_timestamp(planning_run["created_at"]),
                "assistantTurnCreatedAt": _safe_trace_timestamp(turn["created_at"]),
            },
            "continuationScope": continuation_scope,
            "phases": phases,
            "portfolio": portfolios,
            "choiceExecutions": choices,
            "writeDeltas": _write_deltas(response, choices),
        }

    def _project_portfolios(
        self,
        session_id: str,
        assistant_turn_id: str,
        *,
        root_portfolio_id: str = "",
    ) -> dict[str, Any]:
        rows = self._db.execute(
            """SELECT id, status, expected_base_version_id, selected_proposal_id,
                      dominant_proposal_id, summary_json, failure_reason
            FROM agent_plan_portfolios
            WHERE session_id = ?
              AND (source_assistant_turn_id = ? OR (? != '' AND id = ?))
            ORDER BY created_at ASC""",
            (session_id, assistant_turn_id, root_portfolio_id, root_portfolio_id),
        ).fetchall()
        portfolio_items: list[dict[str, Any]] = []
        proposal_items: list[dict[str, Any]] = []
        for row in rows:
            summary = _json_object(row["summary_json"])
            visible_ids = summary.get("visibleProposalIds")
            portfolio_items.append(
                _drop_none(
                    {
                        "id": _safe_trace_identifier(row["id"]),
                        "status": _safe_trace_text(row["status"]),
                        "expectedBaseVersionId": _safe_trace_identifier(row["expected_base_version_id"]),
                        "selectedProposalId": _safe_trace_identifier(row["selected_proposal_id"]),
                        "dominantProposalId": _safe_trace_identifier(row["dominant_proposal_id"]),
                        "visibleProposalCount": len(visible_ids) if isinstance(visible_ids, list) else 0,
                        "briefGenerationState": _project_brief_generation_state(
                            summary.get("briefGenerationState")
                        ),
                        "reasonCodes": _reason_codes(row["failure_reason"]),
                    }
                )
            )
            proposals = self._db.execute(
                """SELECT id, choice_id, rank_index, status, brief_json, score_json, verifier_json
                FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY rank_index ASC""",
                (row["id"],),
            ).fetchall()
            for proposal in proposals:
                brief = _json_object(proposal["brief_json"])
                score = _json_object(proposal["score_json"])
                verifier = _json_object(proposal["verifier_json"])
                proposal_items.append(
                    _drop_none(
                        {
                            "id": _safe_trace_identifier(proposal["id"]),
                            "portfolioId": _safe_trace_identifier(row["id"]),
                            "choiceId": _safe_trace_identifier(proposal["choice_id"]),
                            "rankIndex": _integer_or_none(proposal["rank_index"]),
                            "status": _safe_trace_text(proposal["status"]),
                            "scope": _scope_from(brief),
                            "hardConstraintPassed": _bool_or_none(score.get("hardConstraintPassed")),
                            "verifierPassed": _bool_or_none(verifier.get("passed")),
                            "reasonCodes": _reason_codes(
                                verifier.get("reasonCode"),
                                verifier.get("reasonCodes"),
                            ),
                        }
                    )
                )
        return {"portfolios": portfolio_items, "proposals": proposal_items}

    def _project_choice_executions(
        self,
        session_id: str,
        assistant_turn_id: str,
        *,
        request_turn_id: str = "",
    ) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT id, source_turn_id, choice_id, action, status,
                      expected_base_version_id, result_version_id, attempt,
                      continuation_json, checkpoint_fingerprint, outcome_json, error_json
            FROM agent_choice_executions
            WHERE session_id = ?
              AND (source_turn_id = ? OR execution_turn_id = ? OR request_turn_id = ?)
            ORDER BY CASE WHEN (? != '' AND request_turn_id = ?) THEN 0 ELSE 1 END,
                     created_at ASC""",
            (
                session_id,
                assistant_turn_id,
                assistant_turn_id,
                request_turn_id,
                request_turn_id,
                request_turn_id,
            ),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            continuation = _json_object(row["continuation_json"])
            outcome = _json_object(row["outcome_json"])
            error = _json_object(row["error_json"])
            expansion = str(continuation.get("kind") or "") == "expand_partial_portfolio"
            continuation_scope = _project_continuation_scope(
                {
                    **continuation,
                    **outcome,
                    "sourceAssistantTurnId": row["source_turn_id"],
                    "requestChoiceId": row["choice_id"],
                    "persistedChoiceId": row["choice_id"],
                    "executionId": row["id"],
                    "executionStatus": row["status"],
                    "executionAction": row["action"],
                    "expectedBaseVersionId": continuation.get("expectedBaseVersionId")
                    or row["expected_base_version_id"],
                    "checkpointFingerprint": row["checkpoint_fingerprint"],
                    "controllerCalled": False if expansion else outcome.get("controllerCalled"),
                    "executionRoute": "controller_choice_resume"
                    if expansion
                    else outcome.get("executionRoute"),
                }
            )
            items.append(
                _drop_none(
                    {
                        "id": _safe_trace_identifier(row["id"]),
                        "choiceId": _safe_trace_identifier(row["choice_id"]),
                        "action": _safe_trace_text(row["action"]),
                        "status": _safe_trace_text(row["status"]),
                        "attempt": _integer_or_none(row["attempt"]),
                        "expectedBaseVersionId": _safe_trace_identifier(row["expected_base_version_id"]),
                        "resultVersionId": _safe_trace_identifier(row["result_version_id"]),
                        "scope": _scope_from(continuation),
                        "continuationScope": continuation_scope,
                        "writeDeltas": _deltas(outcome),
                        "reasonCodes": _reason_codes(
                            outcome.get("reasonCode"),
                            outcome.get("reasonCodes"),
                            error.get("reasonCode"),
                            error.get("reasonCodes"),
                        ),
                    }
                )
            )
        return items


def _project_continuation_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "sourceAssistantTurnId",
        "requestChoiceId",
        "persistedChoiceId",
        "executionId",
        "planningSelectionRootTurnId",
        "rootPortfolioId",
        "requestContractFingerprint",
        "expectedBaseVersionId",
        "checkpointFingerprint",
        "focusBriefId",
        "cursorFocusBriefId",
        "requestedFocusBriefId",
        "resolvedFocusBriefId",
        "resolvedDirectionSignature",
        "nextBriefId",
    ):
        if (safe_value := _safe_trace_identifier(value.get(key))) is not None:
            result[key] = safe_value
    for key in (
        "executionStatus",
        "executionAction",
        "expansionFocusMode",
        "executionRoute",
    ):
        if (safe_value := _safe_trace_text(value.get(key))) is not None:
            result[key] = safe_value
    for key in (
        "cursorAdvanced",
        "cursorExhausted",
        "controllerCalled",
    ):
        if (safe_value := _bool_or_none(value.get(key))) is not None:
            result[key] = safe_value
    return result


def _project_phases(response: Mapping[str, Any], tool_calls: list[Any]) -> list[dict[str, Any]]:
    raw_events: list[Any] = []
    for key in ("planningSteps", "toolEvents"):
        value = response.get(key)
        if isinstance(value, list):
            raw_events.extend(value)
    raw_events.extend(tool_calls)

    projected: list[tuple[int | None, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for source_index, value in enumerate(raw_events):
        event = value if isinstance(value, Mapping) else {}
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        trace_summary = (
            event.get("traceSummary") if isinstance(event.get("traceSummary"), Mapping) else {}
        )
        source = dict(event)
        source.update(metadata)
        preview = metadata.get("resultPreview") if isinstance(metadata.get("resultPreview"), Mapping) else {}
        staging = preview.get("staging") if isinstance(preview.get("staging"), Mapping) else {}
        diagnostic_source = _diagnostic_projection_source(source, preview, trace_summary)
        phase = _safe_trace_text(source.get("phase") or source.get("type") or source.get("toolName"))
        if phase is None:
            phase = "planning_event"
        executed_at = _safe_trace_timestamp(
            source.get("timestamp")
            or source.get("queriedAt")
            or source.get("createdAt")
            or trace_summary.get("executedAt")
        )
        sequence = _number_or_none(source.get("sequence") or trace_summary.get("sequence"))
        item = _drop_none(
            {
                "phase": phase,
                "executedAt": executed_at,
                "sequence": int(sequence) if sequence is not None else None,
                "status": _safe_trace_text(source.get("status")),
                "durationMs": _number_or_none(
                    source.get("durationMs") or trace_summary.get("durationMs")
                ),
                "scope": _scope_from(diagnostic_source),
                "candidateCounts": _numeric_fields(diagnostic_source, _COUNT_KEYS),
                "callMetrics": _controller_metrics(diagnostic_source),
                "briefMetrics": _project_brief_metrics(diagnostic_source.get("briefMetrics")),
                "profileMetrics": _project_search_profile_metrics(
                    diagnostic_source.get("profileMetrics")
                ),
                "webDiscoveryAttempts": _project_web_discovery_attempts(
                    diagnostic_source.get("webDiscoveryAttempts")
                ),
                "reasonCodes": _reason_codes(
                    source.get("reasonCode"),
                    source.get("reasonCodes"),
                    preview.get("reasonCode"),
                    preview.get("reasonCodes"),
                    staging.get("reasonCode"),
                    staging.get("reasonCodes"),
                    trace_summary.get("reasonCode"),
                    trace_summary.get("reasonCodes"),
                ),
            }
        )
        fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        projected.append((int(sequence) if sequence is not None else None, source_index, item))

    # `executedAt` is source-reported diagnostic data and may be coarse or
    # clock-skewed. Preserve an explicit server sequence when present, then
    # stable source capture order; do not claim timestamps define chronology.
    projected.sort(
        key=lambda row: (
            row[0] is None,
            row[0] if row[0] is not None else row[1],
            row[1],
        )
    )
    phases: list[dict[str, Any]] = []
    for capture_order, (_sequence, _source_index, item) in enumerate(projected):
        phases.append(
            {
                "captureOrder": capture_order,
                "exportSequence": capture_order + 1,
                **item,
            }
        )
    return phases


def _controller_metrics(source: Mapping[str, Any]) -> dict[str, int | float]:
    metrics = _numeric_fields(source, _METRIC_KEYS)
    _merge_controller_metrics(metrics, source.get("controllerPerformance"))
    return metrics


def _diagnostic_projection_source(
    source: Mapping[str, Any],
    preview: Mapping[str, Any],
    trace_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose only fixed diagnostic keys from persisted trace summaries."""

    allowed = {
        *(source_key for source_key, _target_key in _SCOPE_KEYS),
        *_COUNT_KEYS,
        *_METRIC_KEYS,
        "briefMetrics",
        "profileMetrics",
        "controllerPerformance",
        "webDiscoveryAttempts",
    }
    projected: dict[str, Any] = {}
    for key in allowed:
        if key in source:
            projected[key] = source[key]
        elif key in preview:
            projected[key] = preview[key]
        elif key in trace_summary:
            projected[key] = trace_summary[key]
    return projected


def _project_search_profile_metrics(value: Any) -> list[dict[str, Any]]:
    """Project fixed, bounded profile lineage without query text or payloads."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:16]:
        if not isinstance(raw, Mapping):
            continue
        fingerprint = _safe_trace_identifier(
            raw.get("searchProfileFingerprint")
        )
        if fingerprint is None:
            continue
        item: dict[str, Any] = {
            "searchProfileFingerprint": fingerprint,
        }
        for key in (
            "briefId",
            "poolId",
            "planningSlotId",
            "searchProfileId",
            "experienceFamily",
            "activityMode",
        ):
            if (safe_value := _safe_trace_identifier(raw.get(key))) is not None:
                item[key] = safe_value
        item.update(
            _numeric_fields(
                raw,
                (
                    "queryPlanCount",
                    "fallbackLevel",
                    "excludedPhysicalPoiCount",
                ),
            )
        )
        for key in ("queryModes", "providerKeys"):
            raw_values = raw.get(key)
            if not isinstance(raw_values, list):
                continue
            safe_values = [
                safe_value
                for entry in raw_values[:12]
                for safe_value in [_safe_trace_identifier(entry)]
                if safe_value is not None
            ]
            if safe_values:
                item[key] = safe_values
        result.append(item)
    return result


def _project_brief_metrics(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    metric_keys = (
        "durationMs",
        "candidateCount",
        "candidateDiscoveryMs",
        "webDiscoveryMs",
        "amapGroundingMs",
        "routePreflightMs",
        "routePreflightInvocationCount",
        "routePreflightCallCount",
        "repairMs",
        "repairCallCount",
        "verifierMs",
        "webSearchCount",
        "amapTextCount",
        "amapAroundCount",
        "amapRouteCount",
    )
    result: list[dict[str, Any]] = []
    for raw in value[:8]:
        if not isinstance(raw, Mapping):
            continue
        brief_id = _safe_trace_text(raw.get("briefId"))
        if brief_id is None:
            continue
        item = {
            "briefId": brief_id,
            "briefIndex": _number_or_none(raw.get("briefIndex")),
            "status": _safe_trace_text(raw.get("status")),
            "verifierPassed": _bool_or_none(raw.get("verifierPassed")),
            "draftVerifierPassed": _bool_or_none(raw.get("draftVerifierPassed")),
            "routeProviderState": _safe_trace_text(raw.get("routeProviderState")),
            "repairAttempted": _bool_or_none(raw.get("repairAttempted")),
            **_numeric_fields(raw, metric_keys),
            "reasonCodes": _reason_codes(raw.get("reasonCodes")),
        }
        issue_details = project_route_quality_issue_details(
            raw.get("routeQualityIssueDetails")
        )
        if issue_details:
            item["routeQualityIssueDetails"] = issue_details
        result.append(_drop_none(item))
    return result


def project_route_quality_issue_details(value: Any) -> list[dict[str, Any]]:
    """Project only the safe route-worker exception contract."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:12]:
        if not isinstance(raw, Mapping):
            continue
        item = _drop_none(
            {
                "code": _safe_trace_identifier(raw.get("code")),
                "dayNumber": _number_or_none(raw.get("dayNumber")),
                "segmentId": _safe_trace_identifier(
                    raw.get("segmentId") or raw.get("mealSegmentId")
                ),
                "previousSegmentId": _safe_trace_identifier(
                    raw.get("previousSegmentId")
                ),
                "nextSegmentId": _safe_trace_identifier(raw.get("nextSegmentId")),
                "generalizedCostDelta": _signed_number_or_none(
                    raw.get("generalizedCostDelta")
                ),
                "detourRatio": _signed_number_or_none(raw.get("detourRatio")),
                "addedDistanceMeters": _number_or_none(
                    raw.get("addedDistanceMeters")
                ),
                "addedDurationMinutes": _number_or_none(
                    raw.get("addedDurationMinutes")
                ),
                "timeWindowFeasible": _bool_or_none(raw.get("timeWindowFeasible")),
                "assessmentBasis": _safe_trace_identifier(
                    raw.get("assessmentBasis")
                ),
                "workerFailureClass": _safe_trace_identifier(
                    raw.get("workerFailureClass")
                ),
                "sanitizedWorkerFailureMessage": _safe_trace_text(
                    raw.get("sanitizedWorkerFailureMessage")
                ),
                "workerTimedOut": _bool_or_none(raw.get("workerTimedOut")),
                "workerTimeoutMs": _number_or_none(raw.get("workerTimeoutMs")),
            }
        )
        if item:
            result.append(item)
    return result


def _project_web_discovery_attempts(value: Any) -> list[dict[str, Any]]:
    """Project bounded, scope-complete discovery summaries from planning events.

    This deliberately does not recurse through provider diagnostics or candidate
    provenance. Those internal objects may contain URLs, snippets, headers, or
    payloads and are never part of the export contract.
    """

    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for raw in value[:_MAX_WEB_DISCOVERY_ATTEMPTS]:
        if not isinstance(raw, Mapping):
            continue
        scope = _scope_from(raw.get("scope") if isinstance(raw.get("scope"), Mapping) else raw)
        if not all(scope.get(key) is not None for key in ("briefId", "poolId", "planningSlotId", "dayNumber")):
            continue
        seed_groundings = _project_web_seed_groundings(raw.get("seedGroundings"))
        selected_candidates = _project_web_selected_candidates(
            raw.get("selectedCandidates"),
            expected_scope=scope,
            include_scope=True,
        )
        projected.append(
            _drop_none(
                {
                    "queryFingerprint": _query_fingerprint(raw),
                    "providerName": _safe_trace_text(raw.get("providerName")),
                    "providerStatus": _safe_trace_text(raw.get("providerStatus")),
                    "status": _safe_trace_text(raw.get("status")),
                    "reasonCodes": _reason_codes(raw.get("reasonCode"), raw.get("reasonCodes")),
                    "durationMs": _number_or_none(raw.get("durationMs")),
                    "webDurationMs": _number_or_none(raw.get("webDurationMs")),
                    "amapGroundingMs": _number_or_none(raw.get("amapGroundingMs")),
                    "resultCount": _number_or_none(raw.get("resultCount")),
                    "seedCount": _number_or_none(raw.get("seedCount")),
                    "seedRecordsTruncated": _bool_or_none(raw.get("seedRecordsTruncated")),
                    "reused": _bool_or_none(raw.get("reused")),
                    "scope": scope,
                    "seedGroundings": seed_groundings,
                    "selectedCandidates": selected_candidates,
                }
            )
        )
    return projected


def _project_web_seed_groundings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for raw in value[:_MAX_WEB_DISCOVERY_SEEDS_PER_ATTEMPT]:
        if not isinstance(raw, Mapping):
            continue
        seed_name = _safe_trace_text(raw.get("seedName"))
        if seed_name is None:
            continue
        projected.append(
            _drop_none(
                {
                    "seedName": seed_name,
                    "providerName": _safe_trace_text(raw.get("providerName")),
                    "status": _safe_trace_text(raw.get("status")),
                    "reasonCodes": _reason_codes(raw.get("reasonCode"), raw.get("reasonCodes")),
                    "durationMs": _number_or_none(raw.get("durationMs")),
                    "candidateCount": _number_or_none(raw.get("candidateCount")),
                    "selectedCandidates": _project_web_selected_candidates(
                        raw.get("selectedCandidates"),
                        expected_scope=None,
                        include_scope=False,
                    ),
                }
            )
        )
    return projected


def _project_web_selected_candidates(
    value: Any,
    *,
    expected_scope: Mapping[str, Any] | None,
    include_scope: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected: list[dict[str, Any]] = []
    for raw in value[:_MAX_WEB_DISCOVERY_CANDIDATES_PER_SEED]:
        if not isinstance(raw, Mapping):
            continue
        amap_id = _safe_trace_identifier(raw.get("amapId"))
        name = _safe_trace_text(raw.get("name"))
        if amap_id is None or name is None:
            continue
        item: dict[str, Any] = {"amapId": amap_id, "name": name}
        if include_scope:
            candidate_scope = _scope_from(
                raw.get("scope") if isinstance(raw.get("scope"), Mapping) else raw
            )
            if expected_scope is None or candidate_scope != dict(expected_scope):
                continue
            item["scope"] = candidate_scope
        projected.append(item)
    return projected


def _project_brief_generation_state(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:8]:
        if not isinstance(raw, Mapping):
            continue
        brief_id = _safe_trace_text(raw.get("briefId"))
        if brief_id is None:
            continue
        result.append(
            _drop_none(
                {
                    "briefId": brief_id,
                    "order": _number_or_none(raw.get("order")),
                    "status": _safe_trace_text(raw.get("status")),
                    "resultType": _safe_trace_text(raw.get("resultType")),
                    "reasonCodes": _reason_codes(raw.get("reasonCodes")),
                }
            )
        )
    return result


def _write_deltas(response: Mapping[str, Any], choices: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    values = {key: 0 for key in _DELTA_KEYS}
    for candidate in (
        response.get("timelineMutationOutcome"),
        response.get("choiceExecutionOutcome"),
    ):
        if isinstance(candidate, Mapping):
            _merge_deltas(values, candidate)
    for choice in choices:
        write_deltas = choice.get("writeDeltas")
        if isinstance(write_deltas, Mapping):
            _merge_deltas(values, write_deltas)
    return values


def _merge_deltas(target: dict[str, int], source: Mapping[str, Any]) -> None:
    for key in _DELTA_KEYS:
        value = _number_or_none(source.get(key))
        if value is not None:
            target[key] = max(target[key], int(value))


def _deltas(value: Mapping[str, Any]) -> dict[str, int]:
    result = {key: 0 for key in _DELTA_KEYS}
    _merge_deltas(result, value)
    return result


def _merge_controller_metrics(target: dict[str, int | float], value: Any) -> None:
    if not isinstance(value, list):
        return
    controller_calls = 0
    for item in value:
        if not isinstance(item, Mapping):
            continue
        controller_calls += 1
        for key in ("workerQueueMs", "connectDurationMs", "ttfbDurationMs", "readDurationMs", "durationMs"):
            duration = _number_or_none(item.get(key))
            if duration is None:
                continue
            target_key = "controllerDurationMs" if key == "durationMs" else key
            target[target_key] = target.get(target_key, 0) + duration
    if controller_calls:
        target["controllerCalls"] = controller_calls

def _scope_from(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source_key, target_key in _SCOPE_KEYS:
        candidate = value.get(source_key)
        if candidate is not None and target_key not in result:
            if target_key == "dayNumber":
                numeric = _number_or_none(candidate)
                if numeric is not None:
                    result[target_key] = int(numeric)
            elif (safe_value := _safe_trace_identifier(candidate)) is not None:
                result[target_key] = safe_value
    return result


def _numeric_fields(value: Mapping[str, Any], keys: Iterable[str]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for key in keys:
        numeric = _number_or_none(value.get(key))
        if numeric is not None:
            result[key] = numeric
    return result


def _safe_trace_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 64
        or _contains_forbidden_trace_text(normalized)
    ):
        return None
    if "T" not in normalized:
        return None
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalized

def _safe_trace_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 160 or any(ord(character) < 32 for character in normalized):
        return None
    if _contains_forbidden_trace_text(normalized):
        return None
    return normalized


def _safe_trace_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _TRACE_IDENTIFIER_PATTERN.fullmatch(normalized):
        return None
    if _contains_forbidden_trace_text(normalized):
        return None
    return normalized


def _query_fingerprint(value: Mapping[str, Any]) -> str | None:
    existing = _safe_trace_identifier(value.get("queryFingerprint"))
    if existing is not None and re.fullmatch(r"[0-9a-f]{64}", existing):
        return existing
    query = _safe_trace_text(value.get("query"))
    if query is None:
        return None
    return sha256(query.encode("utf-8")).hexdigest()


def _contains_forbidden_trace_text(value: str) -> bool:
    return bool(
        "://" in value
        or "\\" in value
        or _DOTENV_PATTERN.search(value)
        or _CREDENTIAL_TEXT_PATTERN.search(value)
        or _SECRET_VALUE_PATTERN.search(value)
        or _FORBIDDEN_URI_SCHEME_PATTERN.search(value)
        or _EMBEDDED_ABSOLUTE_PATH_PATTERN.search(value)
        or _IMAGE_REFERENCE_PATTERN.search(value)
    )


def _safe_reason_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _REASON_CODE_PATTERN.fullmatch(normalized):
        return None
    if _contains_forbidden_reason_code_text(normalized):
        return None
    return normalized


def _contains_forbidden_reason_code_text(value: str) -> bool:
    """Protect structured codes without treating their colon delimiters as URIs."""

    return bool(
        "\\" in value
        or "//" in value
        or _DOTENV_PATTERN.search(value)
        or _CREDENTIAL_TEXT_PATTERN.search(value)
        or _SECRET_VALUE_PATTERN.search(value)
        or _FORBIDDEN_REASON_CODE_SCHEME_PATTERN.search(value)
        or _EMBEDDED_ABSOLUTE_PATH_PATTERN.search(value)
        or _IMAGE_REFERENCE_PATTERN.search(value)
    )


def _reason_codes(*values: Any) -> list[str]:
    """Sanitize only values supplied by explicitly allowlisted call-site paths."""

    codes: list[str] = []
    for value in values:
        candidates = value if isinstance(value, (list, tuple)) else (value,)
        for candidate in candidates:
            safe_code = _safe_reason_code(candidate)
            if safe_code is not None and safe_code not in codes:
                codes.append(safe_code)
    return codes


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return list(value) if isinstance(value, list) else []


def _number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return value if math.isfinite(number) and number >= 0 else None
    return None


def _signed_number_or_none(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    return None


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}
