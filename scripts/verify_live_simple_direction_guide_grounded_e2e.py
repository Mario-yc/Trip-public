from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


GUIDE_GROUNDED_REQUEST = "参考攻略建议的地点，生成新的方案"
SCHEMA_VERSION = "trip-simple-direction-guide-grounded-live-v1"
JOURNEY_MODE = "simple_direction_guide"
PLAYWRIGHT_SPEC = "e2e/simple-direction-guide-grounded-user-journey.spec.ts"
OFFICIAL_DEEPSEEK_HOST = "api.deepseek.com"
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
REQUIRED_SOURCE_PATHS = {
    PLAYWRIGHT_SPEC,
    "e2e/support/simple-direction-live-state.ts",
    "e2e/support/clarification-card-selectors.ts",
    "backend/src/services/conversation_intent_router.py",
    "backend/src/services/agent_service.py",
    "backend/src/services/conversation_service.py",
    "backend/src/services/guide_continuation_requirement_service.py",
    "backend/src/services/simple_direction_execution_evidence_service.py",
    "backend/src/services/simple_open_itinerary_executor.py",
    "backend/src/services/simple_open_direction_service.py",
    "backend/src/services/travel_guide_advice_service.py",
    "frontend/src/services/apiClient.ts",
    "frontend/src/state/planComparisonPreview.ts",
    "frontend/src/components/comparison/PlanComparison.tsx",
    "scripts/verify_live_simple_direction_guide_grounded_e2e.py",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the one-shot real guide-grounded Simple Direction journey."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--journey-result", required=True, type=Path)
    parser.add_argument("--run-summary", required=True, type=Path)
    parser.add_argument("--expected-git-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--deepseek-endpoint-host", required=True)
    return parser.parse_args()


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def check(condition: bool, code: str, failures: list[str]) -> None:
    if not condition:
        failures.append(code)


def canonical_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def guide_evidence_fingerprint(
    *, query_fingerprint: str, source_fingerprints: list[str]
) -> str:
    encoded = json.dumps(
        {
            "queryFingerprint": query_fingerprint,
            "sourceFingerprints": source_fingerprints,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def artifact_errors(
    *,
    journey: dict[str, Any],
    run_summary: dict[str, Any],
    expected_git_commit: str,
    deepseek_endpoint_host: str,
) -> list[str]:
    failures: list[str] = []
    expected = str(expected_git_commit or "").strip().lower()
    journey_commit = str(journey.get("gitCommit") or "").strip().lower()
    summary_commit = str(run_summary.get("gitCommit") or "").strip().lower()
    check(bool(GIT_COMMIT_RE.fullmatch(expected)), "expected_git_commit_invalid", failures)
    check(
        bool(GIT_COMMIT_RE.fullmatch(journey_commit)),
        "journey_git_commit_invalid",
        failures,
    )
    check(
        bool(GIT_COMMIT_RE.fullmatch(summary_commit)),
        "run_summary_git_commit_invalid",
        failures,
    )
    check(journey_commit == expected, "journey_git_commit_mismatch", failures)
    check(summary_commit == expected, "run_summary_git_commit_mismatch", failures)
    check(journey_commit == summary_commit, "artifact_git_commit_mismatch", failures)
    check(journey.get("schemaVersion") == SCHEMA_VERSION, "journey_schema_invalid", failures)
    check(run_summary.get("journeyMode") == JOURNEY_MODE, "journey_mode_invalid", failures)
    check(
        str(run_summary.get("playwrightSpec") or "").replace("\\", "/")
        == PLAYWRIGHT_SPEC,
        "playwright_spec_invalid",
        failures,
    )
    check(run_summary.get("providerMode") == "default", "provider_mode_not_real", failures)
    check(
        run_summary.get("initialPlanningMode") == "simple_open_v1",
        "initial_planning_mode_invalid",
        failures,
    )
    check(run_summary.get("databaseIsIsolated") is True, "database_not_isolated", failures)
    check(run_summary.get("seededFromDatabase") is None, "seed_database_forbidden", failures)
    check(
        str(deepseek_endpoint_host or "").strip().lower() == OFFICIAL_DEEPSEEK_HOST
        and str(run_summary.get("deepSeekEndpointHost") or "").strip().lower()
        == OFFICIAL_DEEPSEEK_HOST,
        "deepseek_endpoint_not_official",
        failures,
    )
    check(
        run_summary.get("sourceAttributionVerified") is True,
        "source_attribution_not_verified",
        failures,
    )
    required = {
        str(item).replace("\\", "/")
        for item in run_summary.get("requiredSourcePaths") or []
        if str(item)
    }
    check(
        REQUIRED_SOURCE_PATHS.issubset(required),
        "required_source_allowlist_incomplete",
        failures,
    )
    attribution_checks = [
        item
        for item in run_summary.get("sourceAttributionChecks") or []
        if isinstance(item, dict)
    ]
    attribution_phases = [str(item.get("phase") or "") for item in attribution_checks]
    check(
        attribution_phases in (["preflight"], ["preflight", "post_journey", "final"]),
        "source_attribution_phase_sequence_invalid",
        failures,
    )
    for item in attribution_checks:
        phase = str(item.get("phase") or "unknown")
        check(item.get("verified") is True, f"source_attribution_failed:{phase}", failures)
        check(item.get("headMatches") is True, f"source_head_mismatch:{phase}", failures)
        check(
            item.get("trackedSourceClean") is True,
            f"source_tracked_tree_dirty:{phase}",
            failures,
        )
        results = [
            result
            for result in item.get("requiredSourceResults") or []
            if isinstance(result, dict)
        ]
        result_paths = {
            str(result.get("path") or "").replace("\\", "/") for result in results
        }
        check(
            REQUIRED_SOURCE_PATHS.issubset(result_paths),
            f"required_source_results_incomplete:{phase}",
            failures,
        )
        check(
            all(result.get("verified") is True for result in results),
            f"required_source_result_failed:{phase}",
            failures,
        )
    readiness = json_object(journey.get("providerReadiness"))
    check(readiness.get("mode") == "default", "browser_provider_mode_not_real", failures)
    check(readiness.get("agentConfigured") is True, "browser_deepseek_not_configured", failures)
    check(readiness.get("amapConfigured") is True, "browser_amap_not_configured", failures)
    check(readiness.get("browserMapEnabled") is True, "browser_amap_js_not_enabled", failures)
    check(
        "deepseek" in str(readiness.get("agentProviderName") or "").casefold(),
        "browser_agent_provider_not_deepseek",
        failures,
    )
    return sorted(set(failures))


def _choice_pair(value: Any) -> dict[str, str]:
    pair = json_object(value)
    return {
        "sourceAssistantTurnId": str(pair.get("sourceAssistantTurnId") or ""),
        "choiceId": str(pair.get("choiceId") or ""),
    }


def _selected_pair(request: dict[str, Any]) -> dict[str, str]:
    selected = json_object(request.get("selectedAgentChoice"))
    if not selected:
        selected = json_object(json_object(request.get("context")).get("selectedAgentChoice"))
    return {
        "sourceAssistantTurnId": str(selected.get("sourceAssistantTurnId") or ""),
        "choiceId": str(selected.get("choiceId") or ""),
    }


def _turn_payload(row: dict[str, Any]) -> dict[str, Any]:
    return json_object(row.get("agent_response_json"))


def _turn_request(row: dict[str, Any]) -> dict[str, Any]:
    return json_object(row.get("agent_request_json"))


def _execution_outcome(row: dict[str, Any]) -> dict[str, Any]:
    return json_object(row.get("outcome_json"))


def _zero_write_payload(payload: dict[str, Any]) -> bool:
    return all(
        int(payload.get(key) or 0) == 0
        for key in (
            "newProposalDelta",
            "proposalWriteDelta",
            "versionDelta",
            "patchDelta",
            "routeWriteDelta",
        )
    )


def _guide_usage_errors(usage: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = int(usage.get("requiredMinimum") or 0)
    used_places = [item for item in usage.get("usedPlaces") or [] if isinstance(item, dict)]
    if usage.get("schemaVersion") != "guide-evidence-usage-v1":
        errors.append("guide_usage_schema_invalid")
    if usage.get("status") != "satisfied":
        errors.append("guide_usage_not_satisfied")
    if not SHA256_RE.fullmatch(str(usage.get("evidenceFingerprint") or "")):
        errors.append("guide_usage_evidence_fingerprint_invalid")
    if not SHA256_RE.fullmatch(str(usage.get("requirementFingerprint") or "")):
        errors.append("guide_usage_requirement_fingerprint_invalid")
    if required < 1 or len(used_places) < required:
        errors.append("guide_usage_minimum_not_met")
    if not used_places or not all(
        item.get("routeVerified") is True and str(item.get("amapPoiId") or "")
        for item in used_places
    ):
        errors.append("guide_usage_route_verified_place_missing")
    return errors


def _real_deepseek_controller_evidence(turns: list[dict[str, Any]]) -> bool:
    for turn in turns:
        for container in walk_objects(_turn_payload(turn)):
            performance = container.get("controllerPerformance")
            raw_decisions = container.get("providerRawDecisions")
            if not isinstance(performance, list) or not isinstance(raw_decisions, list):
                continue
            successful = [
                item
                for item in performance
                if isinstance(item, dict)
                and item.get("providerInvoked") is True
                and item.get("captureState") == "completed"
                and item.get("responseHeadersReceived") is True
                and int(item.get("httpStatus") or 0) == 200
                and int(item.get("responseBytes") or 0) > 0
            ]
            valid_decisions = [
                item
                for item in raw_decisions
                if isinstance(item, dict)
                and str(item.get("schemaVersion") or "")
                and str(item.get("primaryAction") or "")
            ]
            if (
                container.get("source") == "controller"
                and container.get("decisionPath") == "full"
                and container.get("controllerFullCalled") is True
                and container.get("controllerLiteCalled") is False
                and container.get("controllerSucceeded") is True
                and container.get("accepted") is True
                and successful
                and len(successful) == len(raw_decisions) == len(valid_decisions)
            ):
                return True
    return False


def _full_controller_stop_evidence(response: dict[str, Any]) -> bool:
    stop_steps = [
        item
        for item in response.get("planningSteps") or []
        if isinstance(item, dict) and item.get("type") == "agent_stop"
    ]
    return len(stop_steps) == 1 and (
        lambda metadata: (
            int(metadata.get("controllerFullCallCount") or 0) == 1
            and int(metadata.get("controllerLiteCallCount") or 0) == 0
            and metadata.get("plannerCalled") is True
        )
    )(json_object(stop_steps[0].get("metadata")))


def _real_amap_guide_grounding_evidence(
    response: dict[str, Any], used_amap_ids: set[str]
) -> bool:
    bound: set[str] = set()
    for step in response.get("planningSteps") or []:
        if not isinstance(step, dict):
            continue
        metadata = json_object(step.get("metadata"))
        selected_id = str(metadata.get("selectedAmapId") or "").upper()
        if (
            step.get("type") == "simple_open_tool_call"
            and step.get("providerName") == "amap-place-search"
            and step.get("status") == "completed"
            and metadata.get("providerOutcome") == "success"
            and metadata.get("cacheHit") is False
            and int(metadata.get("resultCount") or 0) > 0
            and selected_id in used_amap_ids
        ):
            bound.add(selected_id)
    return bool(used_amap_ids) and used_amap_ids.issubset(bound)


def _adjacent_segment_pairs(snapshot: dict[str, Any]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for day in snapshot.get("days") or []:
        if not isinstance(day, dict):
            continue
        anchors: list[str] = []
        for segment in day.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            metadata = json_object(segment.get("semanticMetadata"))
            poi = json_object(segment.get("poi"))
            if metadata.get("routeAnchor") is True and str(poi.get("amapId") or ""):
                segment_id = str(segment.get("id") or "")
                if segment_id:
                    anchors.append(segment_id)
        result.update(zip(anchors, anchors[1:]))
    return result


def verify_records(
    *,
    journey: dict[str, Any],
    run_summary: dict[str, Any],
    records: dict[str, Any],
    expected_git_commit: str,
    deepseek_endpoint_host: str,
) -> dict[str, Any]:
    failures = artifact_errors(
        journey=journey,
        run_summary=run_summary,
        expected_git_commit=expected_git_commit,
        deepseek_endpoint_host=deepseek_endpoint_host,
    )
    check(
        str(journey.get("guideGroundedRequest") or "") == GUIDE_GROUNDED_REQUEST,
        "guide_grounded_request_not_exact",
        failures,
    )
    opaque = json_object(journey.get("opaqueChoices"))
    ordinary_pair = _choice_pair(opaque.get("ordinaryContinuation"))
    guide_pair = _choice_pair(opaque.get("guideSearch"))
    continuation_pair = _choice_pair(opaque.get("guideContinuation"))
    selection_pair = _choice_pair(opaque.get("proposalSelection"))
    pairs = [ordinary_pair, guide_pair, continuation_pair, selection_pair]
    check(
        all(pair["sourceAssistantTurnId"] and pair["choiceId"] for pair in pairs),
        "opaque_choice_identity_missing",
        failures,
    )
    check(
        len({(pair["sourceAssistantTurnId"], pair["choiceId"]) for pair in pairs}) == 4,
        "opaque_choice_identity_reused",
        failures,
    )

    session_id = str(journey.get("sessionId") or "")
    proposal_id = str(journey.get("proposalId") or "")
    active_version_id = str(journey.get("activeVersionId") or "")
    sessions = records.get("sessions") or []
    turns = records.get("turns") or []
    portfolios = records.get("portfolios") or []
    proposals = records.get("proposals") or []
    executions = records.get("executions") or []
    versions = records.get("versions") or []
    patches = records.get("patches") or []
    routes = records.get("routes") or []
    check(len(sessions) == 1, f"session_count_not_one:{len(sessions)}", failures)
    session = sessions[0] if len(sessions) == 1 else {}
    check(str(session.get("id") or "") == session_id, "session_identity_mismatch", failures)
    check(
        str(session.get("active_version_id") or "") == active_version_id and bool(active_version_id),
        "active_version_identity_mismatch",
        failures,
    )
    turns_by_id = {str(turn.get("id") or ""): turn for turn in turns}
    proposals_by_id = {str(item.get("id") or ""): item for item in proposals}

    def exact_execution(pair: dict[str, str], action: str) -> dict[str, Any]:
        matches = [
            row
            for row in executions
            if str(row.get("source_turn_id") or "") == pair["sourceAssistantTurnId"]
            and str(row.get("choice_id") or "") == pair["choiceId"]
            and str(row.get("action") or "") == action
        ]
        check(
            len(matches) == 1,
            f"choice_execution_count:{action}:{len(matches)}",
            failures,
        )
        return matches[0] if len(matches) == 1 else {}

    ordinary_execution = exact_execution(ordinary_pair, "continue_plan_expansion")
    guide_execution = exact_execution(guide_pair, "search_travel_guide_advice")
    continuation_execution = exact_execution(continuation_pair, "continue_plan_expansion")
    selection_execution = exact_execution(selection_pair, "select_plan_proposal")
    check(
        {
            str(row.get("id") or "")
            for row in executions
            if row.get("action") == "continue_plan_expansion"
        }
        == {
            str(ordinary_execution.get("id") or ""),
            str(continuation_execution.get("id") or ""),
        },
        "continuation_execution_set_not_exactly_two",
        failures,
    )
    check(
        len([row for row in executions if row.get("action") == "select_plan_proposal"]) == 1,
        "proposal_selection_not_exactly_once",
        failures,
    )

    ordinary_request_turn = turns_by_id.get(
        str(ordinary_execution.get("request_turn_id") or ""), {}
    )
    ordinary_turn_id = str(ordinary_execution.get("execution_turn_id") or "")
    ordinary_turn = turns_by_id.get(ordinary_turn_id, {})
    ordinary_payload = _turn_payload(ordinary_turn)
    ordinary_outcome = _execution_outcome(ordinary_execution)
    ordinary_browser = json_object(journey.get("ordinaryContinuationEvidence"))
    initial_proposal_ids = {
        str(item) for item in ordinary_browser.get("initialProposalIds") or [] if str(item)
    }
    ordinary_new_ids = {
        str(item) for item in ordinary_browser.get("newProposalIds") or [] if str(item)
    }
    baseline_ids = {
        str(item) for item in journey.get("baselineProposalIds") or [] if str(item)
    }
    check(
        ordinary_execution.get("status") == "succeeded"
        and not str(ordinary_execution.get("result_version_id") or ""),
        "ordinary_continuation_not_succeeded_or_wrote_version",
        failures,
    )
    check(
        _selected_pair(_turn_request(ordinary_request_turn)) == ordinary_pair,
        "persisted_ordinary_continuation_choice_identity_mismatch",
        failures,
    )
    ordinary_source_choices = [
        choice
        for choice in _turn_payload(
            turns_by_id.get(ordinary_pair["sourceAssistantTurnId"], {})
        ).get("choiceOptions")
        or []
        if isinstance(choice, dict)
        and str(choice.get("id") or choice.get("choiceId") or "")
        == ordinary_pair["choiceId"]
        and str(choice.get("action") or "") == "continue_plan_expansion"
        and str(choice.get("kind") or "") == "simple_direction_more_plans"
    ]
    check(
        len(ordinary_source_choices) == 1,
        "ordinary_continuation_source_choice_invalid",
        failures,
    )
    check(
        _zero_write_payload(ordinary_payload)
        and _zero_write_payload(ordinary_outcome)
        and ordinary_payload.get("mode") == "simple_open_direction_proposal"
        and ordinary_payload.get("workflowMode") == "simple_direction_v1"
        and int(ordinary_payload.get("proposalDelta") or 0) == 1
        and int(ordinary_outcome.get("proposalDelta") or 0) == 1,
        "ordinary_continuation_delta_or_mode_invalid",
        failures,
    )
    check(
        str(ordinary_browser.get("sourceAssistantTurnId") or "")
        == ordinary_pair["sourceAssistantTurnId"]
        and str(ordinary_browser.get("assistantTurnId") or "") == ordinary_turn_id
        and int(ordinary_browser.get("proposalDelta") or 0) == 1
        and int(ordinary_browser.get("versionDelta") or 0) == 0
        and int(ordinary_browser.get("patchDelta") or 0) == 0
        and int(ordinary_browser.get("routeWriteDelta") or 0) == 0,
        "browser_database_ordinary_continuation_mismatch",
        failures,
    )
    check(
        bool(initial_proposal_ids)
        and len(ordinary_new_ids) == 1
        and baseline_ids == initial_proposal_ids | ordinary_new_ids
        and len(baseline_ids) >= 2,
        "ordinary_second_proposal_sequence_invalid",
        failures,
    )
    check(
        guide_pair["sourceAssistantTurnId"] == ordinary_turn_id,
        "guide_search_not_offered_by_ordinary_second_proposal_turn",
        failures,
    )
    ordinary_proposal = proposals_by_id.get(next(iter(ordinary_new_ids), ""), {})
    ordinary_lineage = json_object(ordinary_proposal.get("generation_lineage_json"))
    ordinary_snapshot = json_object(ordinary_proposal.get("snapshot_json"))
    ordinary_source_choice = ordinary_source_choices[0] if len(ordinary_source_choices) == 1 else {}
    ordinary_request_fingerprint = str(
        ordinary_source_choice.get("requestContractFingerprint") or ""
    )
    check(
        bool(ordinary_proposal)
        and str(ordinary_lineage.get("frontierExecutionId") or "")
        == str(ordinary_execution.get("id") or "")
        and str(ordinary_lineage.get("sourceAssistantTurnId") or "")
        == ordinary_turn_id
        and str(ordinary_lineage.get("workflowMode") or "")
        == "simple_direction_v1"
        and bool(SHA256_RE.fullmatch(ordinary_request_fingerprint))
        and str(ordinary_lineage.get("requestContractFingerprint") or "")
        == ordinary_request_fingerprint
        and int(ordinary_lineage.get("itineraryWriteCount") or 0) == 0
        and not json_object(ordinary_snapshot.get("guideContinuationRequirement"))
        and not str(ordinary_lineage.get("guideEvidenceFingerprint") or ""),
        "ordinary_second_proposal_lineage_invalid",
        failures,
    )

    guide_request_turn = turns_by_id.get(
        str(guide_execution.get("request_turn_id") or ""), {}
    )
    check(
        _selected_pair(_turn_request(guide_request_turn)) == guide_pair,
        "persisted_guide_search_choice_identity_mismatch",
        failures,
    )
    guide_source_choices = [
        choice
        for choice in ordinary_payload.get("choiceOptions") or []
        if isinstance(choice, dict)
        and str(choice.get("id") or choice.get("choiceId") or "")
        == guide_pair["choiceId"]
        and str(choice.get("action") or "") == "search_travel_guide_advice"
    ]
    check(
        len(guide_source_choices) == 1,
        "guide_search_source_choice_invalid",
        failures,
    )

    guide_outcome = _execution_outcome(guide_execution)
    check(guide_execution.get("status") == "succeeded", "guide_search_not_succeeded", failures)
    check(not str(guide_execution.get("result_version_id") or ""), "guide_search_wrote_version", failures)
    check(_zero_write_payload(guide_outcome), "guide_search_nonzero_write_delta", failures)
    check(
        int(guide_outcome.get("proposalDelta") or 0) == 0,
        "guide_search_proposal_delta_nonzero",
        failures,
    )
    guide_turn_id = str(guide_execution.get("execution_turn_id") or "")
    guide_turn = turns_by_id.get(guide_turn_id, {})
    guide_payload = _turn_payload(guide_turn)
    guide_advice = json_object(guide_payload.get("guideAdvice"))
    source_refs = [item for item in guide_advice.get("sourceRefs") or [] if isinstance(item, dict)]
    source_fingerprints = [str(item.get("sourceFingerprint") or "") for item in source_refs]
    guide_fingerprint = str(guide_advice.get("evidenceFingerprint") or "")
    check(guide_payload.get("mode") == "travel_guide_advice", "guide_turn_mode_invalid", failures)
    check(guide_advice.get("status") == "completed", "guide_advice_not_completed", failures)
    check(bool(source_refs) and all(source_fingerprints), "guide_source_refs_missing", failures)
    successful_web_providers = [
        str(item) for item in guide_advice.get("successfulProviders") or [] if str(item)
    ]
    check(
        1 <= int(guide_advice.get("queryCount") or 0) <= 2
        and bool(successful_web_providers)
        and all(
            not any(token in provider.casefold() for token in ("mock", "recorded", "static"))
            for provider in successful_web_providers
        ),
        "real_web_search_evidence_missing",
        failures,
    )
    check(
        guide_fingerprint
        == guide_evidence_fingerprint(
            query_fingerprint=str(guide_advice.get("queryFingerprint") or ""),
            source_fingerprints=source_fingerprints,
        ),
        "guide_evidence_fingerprint_mismatch",
        failures,
    )
    browser_guide_evidence = json_object(journey.get("guideEvidence"))
    check(
        str(browser_guide_evidence.get("sourceAssistantTurnId") or "") == guide_turn_id
        and str(browser_guide_evidence.get("queryFingerprint") or "")
        == str(guide_advice.get("queryFingerprint") or "")
        and str(browser_guide_evidence.get("evidenceFingerprint") or "")
        == guide_fingerprint
        and int(browser_guide_evidence.get("sourceCount") or 0) == len(source_refs)
        and int(browser_guide_evidence.get("placeHintCount") or 0)
        == len([item for item in guide_advice.get("placeHints") or [] if isinstance(item, dict)]),
        "browser_guide_turn_identity_mismatch",
        failures,
    )

    continuation_request_turn_id = str(continuation_execution.get("request_turn_id") or "")
    continuation_request_turn = turns_by_id.get(continuation_request_turn_id, {})
    check(
        str(continuation_request_turn.get("content") or "") == GUIDE_GROUNDED_REQUEST,
        "persisted_guide_request_not_exact",
        failures,
    )
    check(
        _selected_pair(_turn_request(continuation_request_turn)) == continuation_pair,
        "persisted_continuation_choice_identity_mismatch",
        failures,
    )
    continuation_outcome = _execution_outcome(continuation_execution)
    continuation_completion = json_object(continuation_outcome.get("completionEvidence"))
    if not continuation_completion and continuation_outcome.get("passed") is True:
        continuation_completion = continuation_outcome
    continuation_turn_id = str(continuation_execution.get("execution_turn_id") or "")
    continuation_turn = turns_by_id.get(continuation_turn_id, {})
    continuation_payload = _turn_payload(continuation_turn)
    check(
        continuation_execution.get("status") == "succeeded",
        "guide_continuation_not_succeeded",
        failures,
    )
    check(
        not str(continuation_execution.get("result_version_id") or ""),
        "guide_continuation_wrote_version",
        failures,
    )
    check(_zero_write_payload(continuation_payload), "guide_continuation_response_nonzero_write", failures)
    check(_zero_write_payload(continuation_outcome), "guide_continuation_outcome_nonzero_write", failures)
    check(
        continuation_payload.get("mode") == "simple_open_direction_proposal"
        and continuation_payload.get("workflowMode") == "simple_direction_v1"
        and int(continuation_payload.get("proposalDelta") or 0) == 1,
        "guide_continuation_proposal_delta_invalid",
        failures,
    )
    check(
        continuation_completion.get("passed") is True
        and continuation_completion.get("reason")
        == "simple_direction_execution_evidence_verified",
        "guide_continuation_completion_not_verified",
        failures,
    )
    check(
        _real_deepseek_controller_evidence([continuation_turn]),
        "real_deepseek_controller_evidence_missing",
        failures,
    )
    check(
        _full_controller_stop_evidence(continuation_payload),
        "guide_continuation_full_controller_metrics_invalid",
        failures,
    )

    proposal = proposals_by_id.get(proposal_id, {})
    check(bool(proposal), "selected_proposal_missing", failures)
    expected_proposal_ids = baseline_ids | {proposal_id}
    check(
        set(proposals_by_id) == expected_proposal_ids
        and len(proposals) == len(expected_proposal_ids),
        "guide_continuation_proposal_count_not_one",
        failures,
    )
    check(
        str(proposal.get("choice_id") or "") == selection_pair["choiceId"],
        "proposal_selection_choice_mismatch",
        failures,
    )
    check(proposal.get("status") == "committed", "proposal_not_committed", failures)
    portfolio_id = str(proposal.get("portfolio_id") or "")
    portfolio = next(
        (item for item in portfolios if str(item.get("id") or "") == portfolio_id),
        {},
    )
    check(bool(portfolio), "proposal_portfolio_missing", failures)
    check(
        str(portfolio.get("selected_proposal_id") or "") == proposal_id,
        "portfolio_selected_proposal_mismatch",
        failures,
    )
    check(proposal_id not in baseline_ids, "guide_proposal_not_new", failures)
    check(
        baseline_ids.issubset(proposals_by_id) and len(proposals) >= len(baseline_ids) + 1,
        "proposal_delta_not_persisted",
        failures,
    )

    proposal_snapshot = json_object(proposal.get("snapshot_json"))
    proposal_verifier = json_object(proposal.get("verifier_json"))
    proposal_evidence = json_object(proposal.get("evidence_json"))
    proposal_lineage = json_object(proposal.get("generation_lineage_json"))
    requirement = json_object(proposal_snapshot.get("guideContinuationRequirement"))
    usage = json_object(proposal_verifier.get("guideEvidenceUsage"))
    for error in _guide_usage_errors(usage):
        check(False, error, failures)
    check(
        json_object(proposal_evidence.get("guideEvidenceUsage")) == usage,
        "proposal_guide_usage_evidence_mismatch",
        failures,
    )
    requirement_without_fingerprint = {
        key: value for key, value in requirement.items() if key != "requirementFingerprint"
    }
    requirement_fingerprint = str(requirement.get("requirementFingerprint") or "")
    check(
        requirement.get("schemaVersion") == "guide-continuation-requirement-v1"
        and requirement_fingerprint == canonical_fingerprint(requirement_without_fingerprint),
        "guide_requirement_fingerprint_mismatch",
        failures,
    )
    check(
        str(requirement.get("evidenceFingerprint") or "") == guide_fingerprint
        and str(usage.get("evidenceFingerprint") or "") == guide_fingerprint
        and str(usage.get("requirementFingerprint") or "") == requirement_fingerprint,
        "guide_requirement_usage_binding_mismatch",
        failures,
    )
    check(
        str(proposal_lineage.get("frontierExecutionId") or "")
        == str(continuation_execution.get("id") or "")
        and str(proposal_lineage.get("sourceAssistantTurnId") or "")
        == continuation_turn_id
        and str(proposal_lineage.get("guideContinuationRequirementFingerprint") or "")
        == requirement_fingerprint
        and str(proposal_lineage.get("guideEvidenceSourceAssistantTurnId") or "")
        == guide_turn_id
        and str(proposal_lineage.get("guideChoiceExecutionId") or "")
        == str(guide_execution.get("id") or "")
        and str(proposal_lineage.get("guideEvidenceFingerprint") or "")
        == guide_fingerprint
        and int(proposal_lineage.get("itineraryWriteCount") or 0) == 0,
        "guide_proposal_generation_lineage_mismatch",
        failures,
    )
    check(
        json_object(continuation_payload.get("guideEvidenceUsage")) == usage
        and json_object(json_object(journey.get("proposalEvidence")).get("guideEvidenceUsage"))
        == usage,
        "browser_database_guide_usage_mismatch",
        failures,
    )
    browser_proposal = json_object(journey.get("proposalEvidence"))
    check(
        str(browser_proposal.get("assistantTurnId") or "") == continuation_turn_id
        and int(browser_proposal.get("proposalDelta") or 0) == 1
        and int(browser_proposal.get("versionDelta") or 0) == 0
        and int(browser_proposal.get("patchDelta") or 0) == 0
        and int(browser_proposal.get("routeWriteDelta") or 0) == 0
        and int(browser_proposal.get("controllerFullCallCount") or 0) == 1
        and int(browser_proposal.get("controllerLiteCallCount") or 0) == 0
        and browser_proposal.get("plannerCalled") is True,
        "browser_database_proposal_turn_mismatch",
        failures,
    )

    used_places = [item for item in usage.get("usedPlaces") or [] if isinstance(item, dict)]
    used_amap_ids = {str(item.get("amapPoiId") or "").upper() for item in used_places}
    guide_segment_amap_ids: set[str] = set()
    for item in walk_objects(proposal_snapshot.get("days") or []):
        guide = json_object(json_object(item.get("scheduleConstraints")).get("guideEvidence"))
        if guide.get("verificationStatus") == "verified_amap_grounding":
            guide_segment_amap_ids.add(str(guide.get("amapPoiId") or "").upper())
    route_assignment = json_object(proposal_snapshot.get("simpleOpenRouteAssignment"))
    verified_route_amap_ids = {
        str(pair.get(field) or "").upper()
        for pair in route_assignment.get("verifiedPairs") or []
        if isinstance(pair, dict)
        for field in ("fromAmapId", "toAmapId")
        if str(pair.get(field) or "")
    }
    check(
        bool(used_amap_ids)
        and used_amap_ids.issubset(guide_segment_amap_ids)
        and used_amap_ids.issubset(verified_route_amap_ids),
        "guide_used_place_not_bound_to_grounded_verified_route",
        failures,
    )
    check(
        _real_amap_guide_grounding_evidence(continuation_payload, used_amap_ids),
        "real_amap_guide_grounding_evidence_missing",
        failures,
    )
    verified_pairs = [
        pair
        for pair in route_assignment.get("verifiedPairs") or []
        if isinstance(pair, dict)
    ]
    check(
        bool(verified_pairs)
        and all(
            "amap" in str(pair.get("provider") or "").casefold()
            and int(pair.get("durationSeconds") or 0) > 0
            and int(pair.get("distanceMeters") or 0) > 0
            and bool(
                SHA256_RE.fullmatch(str(pair.get("providerEvidenceFingerprint") or ""))
            )
            for pair in verified_pairs
        ),
        "real_amap_route_evidence_missing",
        failures,
    )

    check(
        selection_execution.get("status") == "succeeded"
        and str(selection_execution.get("result_version_id") or "") == active_version_id,
        "proposal_selection_not_committed",
        failures,
    )
    selection_request_turn = turns_by_id.get(
        str(selection_execution.get("request_turn_id") or ""), {}
    )
    check(
        _selected_pair(_turn_request(selection_request_turn)) == selection_pair,
        "persisted_selection_choice_identity_mismatch",
        failures,
    )
    selection_source_turn = turns_by_id.get(selection_pair["sourceAssistantTurnId"], {})
    matching_selection_choices = [
        choice
        for choice in _turn_payload(selection_source_turn).get("choiceOptions") or []
        if isinstance(choice, dict)
        and str(choice.get("id") or choice.get("choiceId") or "") == selection_pair["choiceId"]
        and str(choice.get("action") or "") == "select_plan_proposal"
        and str(choice.get("proposalId") or "") == proposal_id
    ]
    check(
        len(matching_selection_choices) == 1,
        "selection_source_choice_not_exact",
        failures,
    )
    selection_outcome = _execution_outcome(selection_execution)
    check(
        int(selection_outcome.get("versionDelta") or 0) == 1
        and int(selection_outcome.get("patchDelta") or 0) == 1
        and int(selection_outcome.get("routeWriteDelta") or 0) >= 1,
        "selection_persisted_deltas_invalid",
        failures,
    )
    check(len(versions) == 1, f"formal_version_count:{len(versions)}", failures)
    check(len(patches) == 1, f"formal_patch_count:{len(patches)}", failures)
    version = versions[0] if len(versions) == 1 else {}
    patch = patches[0] if len(patches) == 1 else {}
    selection_result_turn_id = str(selection_execution.get("execution_turn_id") or "")
    check(
        str(version.get("id") or "") == active_version_id
        and str(version.get("session_id") or "") == session_id
        and str(version.get("source_turn_id") or "") == selection_result_turn_id,
        "formal_version_source_lineage_mismatch",
        failures,
    )
    check(
        str(patch.get("result_version_id") or "") == active_version_id
        and str(patch.get("session_id") or "") == session_id
        and str(patch.get("source_turn_id") or "") == selection_result_turn_id
        and str(patch.get("validation_status") or "") in {"accepted", "passed"},
        "formal_patch_source_lineage_mismatch",
        failures,
    )
    version_snapshot = json_object(version.get("snapshot_json"))
    check(
        version_snapshot.get("days") == proposal_snapshot.get("days")
        and json_object(version_snapshot.get("guideContinuationRequirement")) == requirement,
        "adopted_version_not_exact_guide_proposal_material",
        failures,
    )
    expected_route_pairs = _adjacent_segment_pairs(version_snapshot)
    selected_routes = [row for row in routes if int(row.get("is_selected") or 0) == 1]
    persisted_route_pairs = {
        (str(row.get("from_segment_id") or ""), str(row.get("to_segment_id") or ""))
        for row in selected_routes
    }
    check(
        bool(expected_route_pairs)
        and persisted_route_pairs == expected_route_pairs
        and all(
            "amap" in str(row.get("provider") or "").casefold()
            and int(row.get("duration_seconds") or 0) > 0
            and int(row.get("distance_meters") or 0) > 0
            for row in selected_routes
        ),
        "formal_route_lineage_or_provider_evidence_invalid",
        failures,
    )
    check(
        len(routes) == int(selection_outcome.get("routeWriteDelta") or 0),
        "formal_route_write_delta_mismatch",
        failures,
    )
    adoption_browser = json_object(journey.get("adoptionEvidence"))
    check(
        str(adoption_browser.get("assistantTurnId") or "") == selection_result_turn_id
        and str(adoption_browser.get("activeVersionId") or "") == active_version_id
        and str(adoption_browser.get("selectedProposalId") or "") == proposal_id
        and int(adoption_browser.get("versionDelta") or 0) == 1
        and int(adoption_browser.get("patchDelta") or 0) == 1
        and int(adoption_browser.get("routeWriteDelta") or 0) == len(routes)
        and adoption_browser.get("reloadPreservedActiveVersion") is True,
        "browser_database_adoption_delta_mismatch",
        failures,
    )

    return {
        "schemaVersion": "trip-simple-direction-guide-grounded-verification-v1",
        "passed": not failures,
        "failures": sorted(set(failures)),
        "sessionId": session_id,
        "proposalId": proposal_id,
        "activeVersionId": active_version_id,
        "guideChoiceExecutionId": str(guide_execution.get("id") or ""),
        "continuationChoiceExecutionId": str(continuation_execution.get("id") or ""),
        "selectionChoiceExecutionId": str(selection_execution.get("id") or ""),
        "guideEvidenceFingerprint": guide_fingerprint,
        "guideRequirementFingerprint": requirement_fingerprint,
        "routeVerifiedGuidePlaceCount": len(used_places),
        "formalDeltas": {
            "version": len(versions),
            "patch": len(patches),
            "route": len(routes),
        },
    }


def _rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def verify_database(
    *,
    database_path: Path,
    journey: dict[str, Any],
    run_summary: dict[str, Any],
    expected_git_commit: str,
    deepseek_endpoint_host: str,
) -> dict[str, Any]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        session_id = str(journey.get("sessionId") or "")
        sessions = _rows(
            connection,
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session_id,),
        )
        turns = _rows(
            connection,
            "SELECT * FROM conversation_turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,),
        )
        portfolios = _rows(
            connection,
            "SELECT * FROM agent_plan_portfolios WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        portfolio_ids = [str(row.get("id") or "") for row in portfolios]
        proposals: list[dict[str, Any]] = []
        if portfolio_ids:
            placeholders = ",".join("?" for _ in portfolio_ids)
            proposals = _rows(
                connection,
                f"SELECT * FROM agent_plan_proposals WHERE portfolio_id IN ({placeholders}) ORDER BY created_at",
                tuple(portfolio_ids),
            )
        executions = _rows(
            connection,
            "SELECT * FROM agent_choice_executions WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        versions = _rows(
            connection,
            "SELECT * FROM itinerary_versions WHERE session_id = ? ORDER BY version_number",
            (session_id,),
        )
        patches = _rows(
            connection,
            "SELECT * FROM itinerary_patches WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        plan_id = str(versions[0].get("plan_id") or "") if versions else ""
        routes = (
            _rows(
                connection,
                "SELECT * FROM route_options WHERE plan_id = ? ORDER BY sort_order, id",
                (plan_id,),
            )
            if plan_id
            else []
        )
    finally:
        connection.close()
    return verify_records(
        journey=journey,
        run_summary=run_summary,
        records={
            "sessions": sessions,
            "turns": turns,
            "portfolios": portfolios,
            "proposals": proposals,
            "executions": executions,
            "versions": versions,
            "patches": patches,
            "routes": routes,
        },
        expected_git_commit=expected_git_commit,
        deepseek_endpoint_host=deepseek_endpoint_host,
    )


def main() -> int:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        journey = json.loads(args.journey_result.read_text(encoding="utf-8"))
        run_summary = json.loads(args.run_summary.read_text(encoding="utf-8"))
        if not isinstance(journey, dict) or not isinstance(run_summary, dict):
            raise ValueError("artifact_payload_not_object")
        report = verify_database(
            database_path=args.database,
            journey=journey,
            run_summary=run_summary,
            expected_git_commit=args.expected_git_commit,
            deepseek_endpoint_host=args.deepseek_endpoint_host,
        )
    except Exception as error:  # pragma: no cover - CLI safety boundary
        report = {
            "schemaVersion": "trip-simple-direction-guide-grounded-verification-v1",
            "passed": False,
            "failures": [f"verification_exception:{type(error).__name__}"],
        }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
