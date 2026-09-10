from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any


BASE_VERIFIER_PATH = Path(__file__).with_name("verify_live_simple_direction_e2e.py")
BASE_SPEC = importlib.util.spec_from_file_location(
    "verify_live_simple_direction_e2e_base",
    BASE_VERIFIER_PATH,
)
assert BASE_SPEC is not None and BASE_SPEC.loader is not None
base = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(base)

TRUSTED_DEEPSEEK_ENDPOINT_HOST = base.TRUSTED_DEEPSEEK_ENDPOINT_HOST
AMAP_ID_PATTERN = base.AMAP_ID_PATTERN
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
TZ_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check the live Simple Direction frontier browser journey "
            "against isolated SQLite and persisted frontier evidence."
        )
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--journey-result", required=True, type=Path)
    parser.add_argument("--run-summary", type=Path)
    parser.add_argument("--expected-git-commit", default="")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--deepseek-endpoint-host",
        default="api.deepseek.com",
        help="Launcher-fixed DeepSeek endpoint host; host only, never a URL.",
    )
    return parser.parse_args()


def artifact_commit_errors(
    *,
    journey: dict[str, Any],
    run_summary: dict[str, Any],
    expected_git_commit: str,
) -> list[str]:
    errors: list[str] = []
    expected = str(expected_git_commit or "").strip().lower()
    journey_commit = str(journey.get("gitCommit") or "").strip().lower()
    summary_commit = str(run_summary.get("gitCommit") or "").strip().lower()
    if not GIT_COMMIT_RE.fullmatch(expected):
        errors.append("expected_git_commit_invalid")
    if not GIT_COMMIT_RE.fullmatch(journey_commit):
        errors.append("journey_git_commit_invalid")
    if not GIT_COMMIT_RE.fullmatch(summary_commit):
        errors.append("run_summary_git_commit_invalid")
    if GIT_COMMIT_RE.fullmatch(expected):
        if journey_commit != expected:
            errors.append("journey_git_commit_mismatch")
        if summary_commit != expected:
            errors.append("run_summary_git_commit_mismatch")
    if journey_commit != summary_commit:
        errors.append("artifact_git_commit_mismatch")
    return sorted(set(errors))


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def route_row_count(connection: sqlite3.Connection, *, plan_id: str) -> int:
    if not plan_id or not table_exists(connection, "route_options"):
        return 0
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM route_options WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def response_choice_options(turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in base.json_list(base.json_object(turn.get("response")).get("choiceOptions"))
        if isinstance(item, dict)
    ]


def proposal_title(snapshot: dict[str, Any]) -> str:
    return str(
        snapshot.get("displayTitle")
        or snapshot.get("title")
        or base.json_object(snapshot.get("portfolioTitleGeneration")).get("title")
        or ""
    ).strip()


def normalize_title(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", str(value or ""))
        if re.fullmatch(r"[\u3400-\u9fff]", character)
    )


def bigram_jaccard(left: str, right: str) -> float:
    def grams(value: str) -> set[str]:
        return {
            f"{value[index]}{value[index + 1]}"
            for index in range(max(0, len(value) - 1))
        }

    left_grams = grams(left)
    right_grams = grams(right)
    union = left_grams | right_grams
    if not union:
        return 0.0
    return len(left_grams & right_grams) / len(union)


def ready_title_errors(titles: list[str]) -> list[str]:
    errors: list[str] = []
    normalized = [normalize_title(title) for title in titles if str(title).strip()]
    if len(set(normalized)) != len(normalized):
        errors.append("ready_title_duplicate")
    for left_index, left in enumerate(normalized):
        for right in normalized[left_index + 1 :]:
            if not left or not right:
                errors.append("ready_title_empty")
                continue
            if left[:2] == right[:2]:
                errors.append("ready_title_common_prefix")
            if left[-2:] == right[-2:]:
                errors.append("ready_title_common_suffix")
            if bigram_jaccard(left, right) >= 0.5:
                errors.append("ready_title_bigram_overlap")
    return sorted(set(errors))


def segment_independence(segment: dict[str, Any]) -> dict[str, Any]:
    poi = base.json_object(segment.get("poi"))
    direct = base.json_object(poi.get("experienceIndependenceEvidence"))
    if direct:
        return direct
    semantic = base.semantic_metadata(segment)
    constraints = base.json_object(semantic.get("scheduleConstraints"))
    return base.json_object(constraints.get("experienceIndependenceEvidence"))


def segment_physical_group(segment: dict[str, Any]) -> str:
    poi = base.json_object(segment.get("poi"))
    independence = segment_independence(segment)
    return (
        str(independence.get("physicalGroupId") or "").strip().upper()
        or str(poi.get("parentPoiId") or "").strip().upper()
        or str(poi.get("indoorParentPoiId") or "").strip().upper()
        or str(poi.get("amapId") or "").strip().upper()
    )


def park_segment_errors(
    segment: dict[str, Any],
    *,
    campus_groups: set[str],
) -> list[str]:
    errors: list[str] = []
    independence = segment_independence(segment)
    poi = base.json_object(segment.get("poi"))
    evidence = base.json_object(independence.get("evidence"))
    physical_group = str(independence.get("physicalGroupId") or "").strip().upper()
    receipt = str(evidence.get("providerQueryReceiptFingerprint") or "").strip()
    queried_at = str(evidence.get("providerQueriedAt") or "").strip()
    category_decision = str(evidence.get("providerCategoryDecision") or "").strip()
    if str(independence.get("status") or "") != "standalone_verified":
        errors.append("park_not_standalone_verified")
    if not physical_group or not AMAP_ID_PATTERN.fullmatch(physical_group):
        errors.append("park_physical_group_invalid")
    if physical_group and physical_group in campus_groups:
        errors.append("park_physical_group_reuses_campus")
    if not FINGERPRINT_RE.fullmatch(receipt):
        errors.append("park_provider_receipt_missing")
    if not queried_at or not TZ_TIMESTAMP_RE.fullmatch(queried_at):
        errors.append("park_provider_queried_at_missing")
    if category_decision != "accepted":
        errors.append("park_provider_category_not_accepted")
    if str(poi.get("source") or "") != "amap-place-search":
        errors.append("park_source_not_amap_place_search")
    return errors


def route_evidence_errors(snapshot: dict[str, Any]) -> list[str]:
    errors = list(base.compact_route_evidence_errors(snapshot))
    audit = base.json_object(snapshot.get("simpleOpenRouteAssignment"))
    topology = base.json_object(audit.get("topologyEvidence"))
    if audit.get("schemaVersion") != "simple-open-route-evidence-v2":
        return sorted(set(errors))
    if (
        audit.get("decisionSource")
        != "bounded_candidate_geometry_then_provider_final_legs"
    ):
        errors.append("route_decision_source_invalid")
    if topology.get("evidenceSource") != "bounded_candidate_geometry":
        errors.append("route_topology_evidence_source_invalid")
    if topology.get("geometryUsedAsRouteFeasibilityEvidence") is not False:
        errors.append("route_geometry_used_as_provider_evidence")
    expected = [
        item
        for item in base.json_list(audit.get("expectedPairs"))
        if isinstance(item, dict)
    ]
    verified = [
        item
        for item in base.json_list(audit.get("verifiedPairs"))
        if isinstance(item, dict)
    ]
    if audit.get("routeCoverageComplete") is True and len(expected) != 4:
        errors.append("route_expected_pair_count_not_4")
    if audit.get("routeCoverageComplete") is True and len(verified) != 4:
        errors.append("route_verified_pair_count_not_4")
    for pair in verified:
        provider = str(pair.get("provider") or pair.get("source") or "").strip()
        queried_at = str(pair.get("queriedAt") or "").strip()
        provider_fingerprint = str(pair.get("providerEvidenceFingerprint") or "").strip()
        if provider != "amap-route":
            errors.append("route_pair_provider_not_amap")
        if not queried_at or not TZ_TIMESTAMP_RE.fullmatch(queried_at):
            errors.append("route_pair_queried_at_invalid")
        if not FINGERPRINT_RE.fullmatch(provider_fingerprint):
            errors.append("route_pair_provider_fingerprint_invalid")
    return sorted(set(errors))


def frontier_status_errors(
    frontier: dict[str, Any],
    *,
    expected_planning_root_id: str = "",
) -> list[str]:
    errors: list[str] = []
    if frontier.get("schemaVersion") != "simple-direction-frontier-v1":
        errors.append("frontier_schema_invalid")
        return errors
    qualification = str(frontier.get("qualificationEvidenceFingerprint") or "")
    if not FINGERPRINT_RE.fullmatch(qualification):
        errors.append("frontier_qualification_fingerprint_invalid")
    planning_root_id = str(frontier.get("planningRootId") or "")
    request_fingerprint = str(frontier.get("requestContractFingerprint") or "")
    frontier_fingerprint = str(frontier.get("frontierFingerprint") or "")
    if not planning_root_id:
        errors.append("frontier_planning_root_missing")
    if expected_planning_root_id and planning_root_id != expected_planning_root_id:
        errors.append("frontier_planning_root_mismatch")
    if not FINGERPRINT_RE.fullmatch(request_fingerprint):
        errors.append("frontier_request_fingerprint_invalid")
    if not FINGERPRINT_RE.fullmatch(frontier_fingerprint):
        errors.append("frontier_fingerprint_invalid")
    execution_profile = base.json_object(frontier.get("executionProfile"))
    maximum_pages = int(execution_profile.get("maxPagesPerQuery") or 0)
    page_offset = int(execution_profile.get("pageOffset") or 0)
    if not 1 <= maximum_pages <= 10 or not 1 <= page_offset <= 25:
        errors.append("frontier_execution_profile_invalid")
    if not FINGERPRINT_RE.fullmatch(str(execution_profile.get("profileFingerprint") or "")):
        errors.append("frontier_execution_profile_fingerprint_invalid")
    remaining_entities = int(frontier.get("remainingQualifiedEntityCount") or 0)
    remaining_pages = int(frontier.get("remainingPoiPageCount") or 0)
    status = str(frontier.get("frontierStatus") or "")
    entities = [
        item
        for item in base.json_list(frontier.get("qualifiedEntityFrontier"))
        if isinstance(item, dict)
    ]
    valid_entity_states = {
        "untried",
        "grounding_pending",
        "grounded",
        "assigned_partial",
        "used_ready",
        "rejected",
    }
    entity_fingerprints: set[str] = set()
    for entity in entities:
        fingerprint = str(entity.get("evidenceEntityFingerprint") or "")
        if not FINGERPRINT_RE.fullmatch(fingerprint):
            errors.append("frontier_entity_fingerprint_invalid")
        if fingerprint in entity_fingerprints:
            errors.append("frontier_entity_fingerprint_duplicate")
        entity_fingerprints.add(fingerprint)
        state = str(entity.get("state") or "")
        if state not in valid_entity_states:
            errors.append("frontier_entity_state_invalid")
        canonical_amap_id = str(entity.get("canonicalAmapId") or "").strip().upper()
        if state in {"grounded", "assigned_partial", "used_ready"} and not AMAP_ID_PATTERN.fullmatch(
            canonical_amap_id
        ):
            errors.append("frontier_grounded_entity_amap_id_invalid")
        attempted_pages = [int(value) for value in base.json_list(entity.get("attemptedPages"))]
        if any(page < 1 or page > maximum_pages for page in attempted_pages):
            errors.append("frontier_entity_page_out_of_profile")
        if any(
            not FINGERPRINT_RE.fullmatch(str(value))
            for value in base.json_list(entity.get("attemptedQueryFingerprints"))
        ):
            errors.append("frontier_entity_query_fingerprint_invalid")
    slot_frontiers = base.json_object(frontier.get("slotFrontiers"))
    provider_pending = (
        str(frontier.get("terminalBlockingLayer") or "") == "provider"
        or any(str(item.get("state") or "") == "grounding_pending" for item in entities)
        or any(
            isinstance(item, dict)
            and str(item.get("lastProviderOutcome") or "") == "failure"
            for item in slot_frontiers.values()
        )
    )
    if status == "provider_pending" and not provider_pending:
        errors.append("frontier_provider_pending_without_pending_evidence")
    if status == "qualification_exhausted" and remaining_entities > 0:
        errors.append("frontier_false_exhaustion_remaining_entities")
    if status in {"poi_exhausted", "route_feasible_exhausted"} and (
        remaining_entities > 0 or remaining_pages > 0
    ):
        errors.append("frontier_false_exhaustion_remaining_budget")
    if status == "has_more" and remaining_entities <= 0 and remaining_pages <= 0:
        errors.append("frontier_has_more_without_remaining_budget")
    return sorted(set(errors))


def frontier_attempt_errors(
    attempts: dict[str, Any],
    *,
    expected_request_fingerprint: str,
) -> list[str]:
    errors: list[str] = []
    for execution_id, raw_record in attempts.items():
        record = base.json_object(raw_record)
        attempt = base.json_object(record.get("attempt"))
        status = str(record.get("status") or "")
        if not str(execution_id).strip() or str(record.get("executionId") or "") != str(execution_id):
            errors.append("frontier_attempt_execution_identity_invalid")
        if record.get("schemaVersion") != "simple-direction-frontier-claim-v1":
            errors.append(f"frontier_attempt_claim_schema_invalid:{execution_id}")
        if str(record.get("requestContractFingerprint") or "") != expected_request_fingerprint:
            errors.append(f"frontier_attempt_request_fingerprint_mismatch:{execution_id}")
        if status not in {"reconciled", "provider_pending"}:
            errors.append(f"frontier_attempt_not_terminal:{execution_id}")
        if attempt.get("schemaVersion") != "simple-direction-frontier-attempt-v1":
            errors.append(f"frontier_attempt_schema_invalid:{execution_id}")
        if str(attempt.get("requestContractFingerprint") or "") != expected_request_fingerprint:
            errors.append(f"frontier_attempt_material_request_mismatch:{execution_id}")
        for field in ("frontierFingerprint", "attemptFingerprint"):
            if not FINGERPRINT_RE.fullmatch(str(attempt.get(field) or "")):
                errors.append(f"frontier_attempt_{field}_invalid:{execution_id}")
        assignments = [
            item
            for item in base.json_list(attempt.get("campusAssignments"))
            if isinstance(item, dict)
        ]
        if int(attempt.get("requestedCampusSlotCount") or 0) != len(assignments):
            errors.append(f"frontier_attempt_assignment_count_invalid:{execution_id}")
        for assignment in assignments:
            if not FINGERPRINT_RE.fullmatch(
                str(assignment.get("evidenceEntityFingerprint") or "")
            ):
                errors.append(f"frontier_attempt_entity_fingerprint_invalid:{execution_id}")
            for field in ("queryScopeFingerprint", "queryFingerprint"):
                if not FINGERPRINT_RE.fullmatch(str(assignment.get(field) or "")):
                    errors.append(f"frontier_attempt_{field}_invalid:{execution_id}")
            if int(assignment.get("page") or 0) < 1 or int(assignment.get("offset") or 0) < 1:
                errors.append(f"frontier_attempt_query_page_invalid:{execution_id}")
        outcomes = [
            item for item in base.json_list(record.get("outcomes")) if isinstance(item, dict)
        ]
        if status == "reconciled" and len(outcomes) != len(assignments):
            errors.append(f"frontier_attempt_outcomes_incomplete:{execution_id}")
        if status == "provider_pending" and str(record.get("blockingLayer") or "") != "provider":
            errors.append(f"frontier_attempt_provider_blocker_missing:{execution_id}")
        if status == "provider_pending" and not str(record.get("reasonCode") or ""):
            errors.append(f"frontier_attempt_provider_reason_missing:{execution_id}")
    return sorted(set(errors))


def continuation_execution_errors(
    *,
    expected_capabilities: list[dict[str, Any]],
    choice_rows: list[sqlite3.Row],
    turns_by_id: dict[str, dict[str, Any]],
    session_id: str,
    frontier_attempts: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected_by_pair = {
        (
            str(item.get("sourceAssistantTurnId") or ""),
            str(item.get("choiceId") or ""),
        ): item
        for item in expected_capabilities
        if str(item.get("sourceAssistantTurnId") or "")
        and str(item.get("choiceId") or "")
    }
    actual_pairs: list[tuple[str, str]] = []
    for row in choice_rows:
        if str(row["id"] or "") not in frontier_attempts:
            errors.append(f"continuation_frontier_attempt_missing:{row['id']}")
        source_turn_id = str(row["source_turn_id"] or "")
        choice_id = str(row["choice_id"] or "")
        actual_pairs.append((source_turn_id, choice_id))
        if str(row["session_id"] or "") != session_id:
            errors.append(f"continuation_session_mismatch:{row['id']}")
        source_turn = turns_by_id.get(source_turn_id)
        if not isinstance(source_turn, dict) or source_turn.get("role") != "assistant":
            errors.append(f"continuation_source_turn_invalid:{row['id']}")
            continue
        matching_option = next(
            (
                option
                for option in response_choice_options(source_turn)
                if str(option.get("action") or "") == "continue_plan_expansion"
                and str(option.get("id") or option.get("choiceId") or "") == choice_id
            ),
            None,
        )
        if matching_option is None:
            errors.append(f"continuation_source_choice_missing:{row['id']}")
        else:
            expected = expected_by_pair.get((source_turn_id, choice_id), {})
            required_identity = {
                "action": "continue_plan_expansion",
                "kind": "simple_direction_more_plans",
                "scopeKind": "comparison",
                "sourceAssistantTurnId": source_turn_id,
                "planningSelectionRootTurnId": str(
                    expected.get("planningSelectionRootTurnId") or ""
                ),
                "rootPortfolioId": str(expected.get("rootPortfolioId") or ""),
                "requestContractFingerprint": str(
                    expected.get("requestContractFingerprint") or ""
                ),
            }
            for field, expected_value in required_identity.items():
                actual_value = str(matching_option.get(field) or "")
                if not expected_value or actual_value != expected_value:
                    errors.append(f"continuation_source_scope_invalid:{row['id']}:{field}")
        request_turn = turns_by_id.get(str(row["request_turn_id"] or ""))
        if not isinstance(request_turn, dict) or request_turn.get("role") != "user":
            errors.append(f"continuation_request_turn_invalid:{row['id']}")
        else:
            request = base.json_object(request_turn.get("request"))
            context = base.json_object(request.get("context"))
            selected = base.json_object(
                context.get("selectedAgentChoice") or request.get("selectedAgentChoice")
            )
            if set(selected) - {"sourceAssistantTurnId", "choiceId"}:
                errors.append(f"continuation_request_choice_not_identity_only:{row['id']}")
            if str(selected.get("sourceAssistantTurnId") or "") != source_turn_id:
                errors.append(f"continuation_request_source_identity_invalid:{row['id']}")
            if str(selected.get("choiceId") or "") != choice_id:
                errors.append(f"continuation_request_choice_identity_invalid:{row['id']}")
        execution_turn = turns_by_id.get(str(row["execution_turn_id"] or ""))
        if not isinstance(execution_turn, dict) or execution_turn.get("role") != "assistant":
            errors.append(f"continuation_execution_turn_invalid:{row['id']}")
        outcome = base.json_object(row["outcome_json"])
        for key in ("versionDelta", "patchDelta", "routeWriteDelta"):
            if int(outcome.get(key) or 0) != 0:
                errors.append(f"continuation_non_zero_write:{row['id']}:{key}")
    if len(actual_pairs) != len(set(actual_pairs)):
        errors.append("continuation_exactly_once_violated")
    if set(expected_by_pair) != set(actual_pairs):
        errors.append("continuation_capability_identity_mismatch")
    return sorted(set(errors))


def round_progress_errors(rounds: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    previous_proposal_ids: set[str] = set()
    for expected_index, round_item in enumerate(rounds):
        round_index_raw = round_item.get("roundIndex")
        round_index = (
            int(round_index_raw)
            if not isinstance(round_index_raw, bool) and round_index_raw is not None
            else -1
        )
        if round_index != expected_index:
            errors.append("round_index_sequence_invalid")
        summary = base.json_object(round_item.get("comparisonSummary"))
        proposal_ids = [
            str(item)
            for item in base.json_list(round_item.get("proposalIds"))
            if str(item)
        ]
        ready_ids = [
            str(item)
            for item in base.json_list(round_item.get("readyProposalIds"))
            if str(item)
        ]
        partial_ids = [
            str(item)
            for item in base.json_list(round_item.get("partialProposalIds"))
            if str(item)
        ]
        new_ids = [
            str(item)
            for item in base.json_list(round_item.get("newProposalIds"))
            if str(item)
        ]
        ui = base.json_object(round_item.get("ui"))
        if set(new_ids) & seen:
            errors.append("round_new_proposal_repeated")
        seen.update(new_ids)
        if not previous_proposal_ids.issubset(set(proposal_ids)):
            errors.append("round_visible_proposals_not_monotonic")
        previous_proposal_ids = set(proposal_ids)
        if int(summary.get("adoptionReadyCount") or 0) != len(ready_ids):
            errors.append("round_ready_count_mismatch")
        if int(summary.get("repairablePartialCount") or 0) != len(partial_ids):
            errors.append("round_partial_count_mismatch")
        if int(ui.get("readyCardCount") or 0) != len(ready_ids):
            errors.append("round_ui_ready_count_mismatch")
        if int(ui.get("partialCardCount") or 0) != len(partial_ids):
            errors.append("round_ui_partial_count_mismatch")
    return sorted(set(errors))


def proposal_snapshot_errors(
    *,
    proposal_id: str,
    snapshot: dict[str, Any],
    prior_proposal_ids: list[str],
    require_ready: bool,
    verifier: dict[str, Any],
    generation_lineage: dict[str, Any],
    frontier_attempts: dict[str, Any],
    expected_request_fingerprint: str,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    title_generation = base.json_object(snapshot.get("portfolioTitleGeneration"))
    attempt_count = int(title_generation.get("attemptCount") or 0)
    max_attempts = int(title_generation.get("maxAttempts") or 0)

    raw_segments = [
        segment
        for day in base.json_list(snapshot.get("days"))
        if isinstance(day, dict)
        for segment in base.json_list(day.get("segments"))
        if isinstance(segment, dict)
    ]
    campus_groups: list[str] = []
    park_groups: list[str] = []
    for segment in raw_segments:
        intent_type = str(base.semantic_metadata(segment).get("intentType") or "")
        if intent_type == "campus_visit":
            group = segment_physical_group(segment)
            if not group or not AMAP_ID_PATTERN.fullmatch(group):
                errors.append(f"proposal_campus_group_invalid:{proposal_id}")
            campus_groups.append(group)
    campus_group_set = {group for group in campus_groups if group}
    for segment in raw_segments:
        if str(base.semantic_metadata(segment).get("intentType") or "") != "park":
            continue
        park_groups.append(segment_physical_group(segment))
        if require_ready:
            for error in park_segment_errors(segment, campus_groups=campus_group_set):
                errors.append(f"proposal_park:{proposal_id}:{error}")

    novelty = base.json_object(snapshot.get("simpleDirectionNoveltyEvidence"))
    if novelty.get("schemaVersion") != "simple-direction-novelty-v3":
        errors.append(f"proposal_novelty_schema_invalid:{proposal_id}")
    if novelty.get("passed") is not True:
        errors.append(f"proposal_novelty_not_passed:{proposal_id}")
    if novelty.get("campusAssignmentsGrounded") is not True:
        errors.append(f"proposal_campus_assignments_not_grounded:{proposal_id}")
    frontier_attempt_fingerprint = str(
        novelty.get("frontierAttemptFingerprint") or ""
    )
    if not FINGERPRINT_RE.fullmatch(frontier_attempt_fingerprint):
        errors.append(f"proposal_frontier_attempt_fingerprint_invalid:{proposal_id}")
    frontier_execution_id = str(
        generation_lineage.get("frontierExecutionId") or ""
    ).strip()
    persisted_claim = base.json_object(frontier_attempts.get(frontier_execution_id))
    persisted_attempt = base.json_object(persisted_claim.get("attempt"))
    persisted_attempt_fingerprint = str(
        persisted_attempt.get("attemptFingerprint") or ""
    )
    lineage_attempt_fingerprint = str(
        generation_lineage.get("frontierAttemptFingerprint") or ""
    )
    lineage_request_fingerprint = str(
        generation_lineage.get("requestContractFingerprint") or ""
    )
    if not frontier_execution_id:
        errors.append(f"proposal_frontier_execution_id_missing:{proposal_id}")
    elif not persisted_claim:
        errors.append(f"proposal_frontier_execution_not_persisted:{proposal_id}")
    elif str(persisted_claim.get("executionId") or "") != frontier_execution_id:
        errors.append(f"proposal_frontier_execution_identity_mismatch:{proposal_id}")
    if lineage_request_fingerprint != expected_request_fingerprint:
        errors.append(f"proposal_lineage_request_fingerprint_mismatch:{proposal_id}")
    if persisted_claim and str(
        persisted_claim.get("requestContractFingerprint") or ""
    ) != expected_request_fingerprint:
        errors.append(f"proposal_claim_request_fingerprint_mismatch:{proposal_id}")
    if persisted_attempt and str(
        persisted_attempt.get("requestContractFingerprint") or ""
    ) != expected_request_fingerprint:
        errors.append(f"proposal_attempt_request_fingerprint_mismatch:{proposal_id}")
    if not FINGERPRINT_RE.fullmatch(lineage_attempt_fingerprint):
        errors.append(f"proposal_lineage_attempt_fingerprint_invalid:{proposal_id}")
    if persisted_claim and (
        lineage_attempt_fingerprint != persisted_attempt_fingerprint
        or lineage_attempt_fingerprint != frontier_attempt_fingerprint
    ):
        errors.append(f"proposal_frontier_attempt_fingerprint_mismatch:{proposal_id}")
    comparison_ids = [
        str(base.json_object(item).get("priorProposalId") or "")
        for item in base.json_list(novelty.get("comparisons"))
    ]
    if prior_proposal_ids and set(comparison_ids) != set(prior_proposal_ids):
        errors.append(f"proposal_novelty_history_incomplete:{proposal_id}")

    blocker_codes: list[str] = []
    route_audit = base.json_object(snapshot.get("simpleOpenRouteAssignment"))
    if str(route_audit.get("failureReason") or ""):
        blocker_codes.append(str(route_audit.get("failureReason") or ""))
    for slot in base.pending_slots(snapshot):
        reason = str(base.json_object(slot).get("reasonCode") or "")
        if reason:
            blocker_codes.append(reason)
    for blocker in base.json_list(verifier.get("blockingReasons")):
        if isinstance(blocker, dict):
            reason = str(blocker.get("reasonCode") or blocker.get("code") or "")
        else:
            reason = str(blocker or "")
        if reason:
            blocker_codes.append(reason)

    if require_ready:
        for error in route_evidence_errors(snapshot):
            errors.append(f"proposal_route:{proposal_id}:{error}")
        if len(campus_groups) != 2:
            errors.append(f"proposal_campus_count_invalid:{proposal_id}:{len(campus_groups)}")
        if len(campus_group_set) != len(campus_groups):
            errors.append(f"proposal_campus_group_duplicate:{proposal_id}")
        if len(park_groups) != 2:
            errors.append(f"proposal_park_count_invalid:{proposal_id}:{len(park_groups)}")
        if attempt_count < 0 or attempt_count > 2:
            errors.append(f"proposal_title_attempt_count_invalid:{proposal_id}")
        if max_attempts != 2:
            errors.append(f"proposal_title_max_attempts_invalid:{proposal_id}")
        if not str(title_generation.get("titleDecisionSource") or ""):
            errors.append(f"proposal_title_decision_source_missing:{proposal_id}")
        title = normalize_title(proposal_title(snapshot))
        if not 6 <= len(title) <= 18:
            errors.append(f"proposal_title_length_invalid:{proposal_id}")
        title_evidence = base.json_object(snapshot.get("portfolioTitleEvidence"))
        used_signal = str(
            title_generation.get("usedTitleSignal")
            or title_evidence.get("usedTitleSignal")
            or ""
        ).strip()
        if not used_signal or normalize_title(used_signal) not in title:
            errors.append(f"proposal_title_signal_missing:{proposal_id}")
        if novelty.get("readyNoveltyPassed") is not True:
            errors.append(f"proposal_ready_novelty_not_passed:{proposal_id}")
        single_new_fallback = novelty.get("singleNewAnchorFallback") is True
        if novelty.get("twoNewAnchorsRequired") is True and int(
            novelty.get("newQualifiedEntityCount") or 0
        ) < 2:
            errors.append(f"proposal_two_new_anchor_count_invalid:{proposal_id}")
        if single_new_fallback and int(novelty.get("newQualifiedEntityCount") or 0) != 1:
            errors.append(f"proposal_single_new_anchor_count_invalid:{proposal_id}")
        for comparison in [
            item
            for item in base.json_list(novelty.get("comparisons"))
            if isinstance(item, dict)
        ]:
            if comparison.get("passedForStorage") is not True:
                errors.append(f"proposal_comparison_storage_not_passed:{proposal_id}")
            if comparison.get("campusRotationPassed") is not True:
                errors.append(f"proposal_comparison_campus_rotation_not_passed:{proposal_id}")
            minimum_changed_campuses = 1 if single_new_fallback else 2
            if int(comparison.get("changedCampusDayCount") or 0) < minimum_changed_campuses:
                errors.append(f"proposal_comparison_campus_change_shortfall:{proposal_id}")
            if comparison.get("standaloneNoveltyPassed") is not True or int(
                comparison.get("totalStandaloneChangedCount") or 0
            ) < 2:
                errors.append(f"proposal_comparison_standalone_change_shortfall:{proposal_id}")
            day_evidence = [
                item
                for item in base.json_list(comparison.get("standaloneDayEvidence"))
                if isinstance(item, dict)
            ]
            if len(day_evidence) != 2 or any(
                int(item.get("changedCount") or 0) < 1 for item in day_evidence
            ):
                errors.append(f"proposal_comparison_daily_change_shortfall:{proposal_id}")
            if comparison.get("orderedPairNoveltyPassed") is not True or int(
                comparison.get("changedOrderedPairCount") or 0
            ) < 1:
                errors.append(f"proposal_comparison_ordered_pair_unchanged:{proposal_id}")
        if blocker_codes:
            errors.append(f"proposal_ready_has_blocker:{proposal_id}")
    else:
        if verifier.get("confirmationPassed") is True:
            errors.append(f"partial_confirmation_passed:{proposal_id}")
        if novelty.get("repairablePartialAccepted") is not True:
            errors.append(f"partial_novelty_not_repairable:{proposal_id}")
        for comparison in [
            item
            for item in base.json_list(novelty.get("comparisons"))
            if isinstance(item, dict)
        ]:
            if comparison.get("partialIdentityPassed") is not True or comparison.get(
                "passedForStorage"
            ) is not True:
                errors.append(f"partial_identity_not_passed:{proposal_id}")
        if not blocker_codes:
            errors.append(f"partial_missing_concrete_blocker:{proposal_id}")

    return sorted(set(errors)), {
        "title": proposal_title(snapshot),
        "campusGroups": campus_groups,
        "parkGroups": park_groups,
        "confirmationPassed": verifier.get("confirmationPassed") is True,
        "requireReady": require_ready,
        "frontierExecutionId": frontier_execution_id,
        "blockerCodes": sorted(set(blocker_codes)),
    }


def report_with_failures(
    *,
    failures: list[str],
    session_id: str,
    normalized_deepseek_host: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "passed": not failures,
        "failures": sorted(set(failures)),
        "sessionId": session_id,
        "deepSeekEndpointHost": normalized_deepseek_host,
        "allowedDeepSeekEndpointHost": TRUSTED_DEEPSEEK_ENDPOINT_HOST,
        **details,
    }


def verify_database(
    database: Path,
    journey: dict[str, Any],
    *,
    deepseek_endpoint_host: str = TRUSTED_DEEPSEEK_ENDPOINT_HOST,
    pre_adoption_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_deepseek_host, endpoint_failure = base.deepseek_endpoint_preflight(
        deepseek_endpoint_host
    )
    if endpoint_failure is not None:
        return endpoint_failure
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    failures: list[str] = []
    session_id = str(journey.get("sessionId") or "")
    try:
        session = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        base.check(session is not None, "session_not_found", failures)
        if session is None:
            return report_with_failures(
                failures=failures,
                session_id=session_id,
                normalized_deepseek_host=normalized_deepseek_host,
                details={},
            )
        plan_id = str(session["active_plan_id"] or "")
        turn_rows = connection.execute(
            "SELECT id, turn_index, role, status, parent_turn_id, itinerary_version_id, "
            "content, agent_request_json, agent_response_json, created_at "
            "FROM conversation_turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,),
        ).fetchall()
        turns = [base.turn_payload(row) for row in turn_rows]
        turns_by_id = {turn["id"]: turn for turn in turns}

        journey_status = str(journey.get("status") or "")
        exploration = base.json_object(journey.get("exploration"))
        frontier_summary = base.json_object(exploration.get("frontierEvidence"))
        portfolio_id = str(exploration.get("finalPortfolioId") or "")
        expected_planning_root_id = str(exploration.get("finalPlanningRootId") or "")
        portfolio_row = connection.execute(
            "SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
            (portfolio_id, session_id),
        ).fetchone()
        base.check(portfolio_row is not None, "portfolio_not_found", failures)
        if portfolio_row is None:
            return report_with_failures(
                failures=failures,
                session_id=session_id,
                normalized_deepseek_host=normalized_deepseek_host,
                details={},
            )
        portfolio_summary = base.json_object(portfolio_row["summary_json"])
        frontier = base.json_object(portfolio_summary.get("simpleDirectionFrontier"))
        for error in frontier_status_errors(
            frontier,
            expected_planning_root_id=expected_planning_root_id,
        ):
            failures.append(error)
        request_fingerprint = str(frontier.get("requestContractFingerprint") or "")
        attempts = base.json_object(portfolio_summary.get("simpleDirectionFrontierAttempts"))
        failures.extend(
            frontier_attempt_errors(
                attempts,
                expected_request_fingerprint=request_fingerprint,
            )
        )
        if frontier_summary:
            if (
                str(frontier.get("qualificationEvidenceFingerprint") or "")
                != str(frontier_summary.get("qualificationEvidenceFingerprint") or "")
            ):
                failures.append("frontier_qualification_fingerprint_mismatch")
            if str(frontier.get("frontierStatus") or "") != str(
                frontier_summary.get("frontierStatus") or ""
            ):
                failures.append("frontier_status_mismatch")

        proposal_rows = connection.execute(
            "SELECT * FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY rank_index, created_at",
            (portfolio_id,),
        ).fetchall()
        proposal_by_id = {str(row["id"]): row for row in proposal_rows}
        turns_source_ids = set(turns_by_id)

        proposal_summaries: list[dict[str, Any]] = []
        ready_titles: list[str] = []
        ready_pairs: set[tuple[str, ...]] = set()
        seen_frontier_execution_ids: set[str] = set()
        prior_proposal_ids: list[str] = []
        for row in proposal_rows:
            proposal_id = str(row["id"] or "")
            snapshot = base.json_object(row["snapshot_json"])
            row_keys = set(row.keys())
            persisted_verifier = (
                base.json_object(row["verifier_json"])
                if "verifier_json" in row_keys
                else {}
            )
            snapshot_verifier = base.json_object(snapshot.get("portfolioVerifier"))
            verifier = persisted_verifier or snapshot_verifier
            lineage = base.json_object(row["generation_lineage_json"])
            status = str(row["status"] or "")
            ready_status = status in {"adoption_ready", "committed"}
            require_ready = ready_status and verifier.get("confirmationPassed") is True
            if ready_status != require_ready:
                failures.append(f"proposal_ready_status_verifier_mismatch:{proposal_id}")
            errors, summary = proposal_snapshot_errors(
                proposal_id=proposal_id,
                snapshot=snapshot,
                prior_proposal_ids=prior_proposal_ids,
                require_ready=require_ready,
                verifier=verifier,
                generation_lineage=lineage,
                frontier_attempts=attempts,
                expected_request_fingerprint=request_fingerprint,
            )
            failures.extend(errors)
            summary["proposalId"] = proposal_id
            summary["status"] = status
            proposal_summaries.append(summary)
            if require_ready:
                ready_titles.append(summary["title"])
                pair = tuple(summary["campusGroups"])
                if pair in ready_pairs:
                    failures.append(f"ready_campus_pair_duplicate:{proposal_id}")
                ready_pairs.add(pair)
            frontier_execution_id = str(summary.get("frontierExecutionId") or "")
            if frontier_execution_id:
                if frontier_execution_id in seen_frontier_execution_ids:
                    failures.append(
                        f"proposal_frontier_execution_reused:{proposal_id}"
                    )
                seen_frontier_execution_ids.add(frontier_execution_id)
            source_turn_id = str(lineage.get("sourceAssistantTurnId") or "")
            if source_turn_id and source_turn_id not in turns_source_ids:
                failures.append(f"proposal_source_turn_missing:{proposal_id}")
            prior_proposal_ids.append(proposal_id)

        expected_proposal_ids = {
            str(base.json_object(item).get("proposalId") or "")
            for item in base.json_list(journey.get("proposals"))
            if str(base.json_object(item).get("proposalId") or "")
        }
        if expected_proposal_ids and expected_proposal_ids != set(proposal_by_id):
            failures.append("journey_database_proposal_ids_mismatch")
        failures.extend(ready_title_errors(ready_titles))

        comparison_summary = base.json_object(portfolio_summary.get("comparisonSummary"))
        computed_ready_count = sum(
            1 for item in proposal_summaries if item.get("requireReady") is True
        )
        computed_partial_count = len(proposal_summaries) - computed_ready_count
        if int(comparison_summary.get("adoptionReadyCount") or 0) != computed_ready_count:
            failures.append("comparison_summary_ready_count_mismatch")
        if int(comparison_summary.get("repairablePartialCount") or 0) != computed_partial_count:
            failures.append("comparison_summary_partial_count_mismatch")
        if int(comparison_summary.get("remainingQualifiedEntityCount") or 0) != int(
            frontier.get("remainingQualifiedEntityCount") or 0
        ):
            failures.append("comparison_summary_remaining_entity_count_mismatch")
        if str(comparison_summary.get("frontierStatus") or "") != str(
            frontier.get("frontierStatus") or ""
        ):
            failures.append("comparison_summary_frontier_status_mismatch")

        controller_evidence: list[dict[str, Any]] = []
        assistant_external_counts: list[dict[str, Any]] = []
        for turn in turns:
            if turn.get("role") != "assistant":
                continue
            response = base.json_object(turn.get("response"))
            decision_evidence = base.deepseek_decision_evidence(
                response,
                endpoint_host=normalized_deepseek_host,
            )
            if decision_evidence.get("verified") is True:
                controller_evidence.append(decision_evidence)
            counts = base.external_call_counts(response)
            assistant_external_counts.append({"turnId": turn.get("id"), **counts})
            if int(counts.get("amapPlaceText") or 0) > 6:
                failures.append(f"amap_place_budget_exceeded:{turn.get('id')}")
            if int(counts.get("amapRoute") or 0) > 4:
                failures.append(f"amap_route_budget_exceeded:{turn.get('id')}")
        if not controller_evidence:
            failures.append("deepseek_verified_controller_decision_missing")

        rounds = [
            item
            for item in base.json_list(base.json_object(journey.get("exploration")).get("rounds"))
            if isinstance(item, dict)
        ]
        failures.extend(round_progress_errors(rounds))

        continuation_expected = [
            {
                "sourceAssistantTurnId": str(base.json_object(item.get("continueCapability")).get("sourceAssistantTurnId") or ""),
                "choiceId": str(base.json_object(item.get("continueCapability")).get("choiceId") or ""),
                "planningSelectionRootTurnId": str(
                    base.json_object(item.get("continueCapability")).get(
                        "planningSelectionRootTurnId"
                    )
                    or ""
                ),
                "rootPortfolioId": str(
                    base.json_object(item.get("continueCapability")).get("rootPortfolioId")
                    or ""
                ),
                "requestContractFingerprint": str(
                    base.json_object(item.get("continueCapability")).get(
                        "requestContractFingerprint"
                    )
                    or ""
                ),
            }
            for item in rounds
            if item.get("continueClicked") is True
            and base.json_object(item.get("continueCapability"))
        ]
        continuation_rows = connection.execute(
            "SELECT * FROM agent_choice_executions "
            "WHERE session_id = ? AND action = 'continue_plan_expansion' AND status = 'succeeded' "
            "ORDER BY created_at",
            (session_id,),
        ).fetchall()
        failures.extend(
            continuation_execution_errors(
                expected_capabilities=continuation_expected,
                choice_rows=continuation_rows,
                turns_by_id=turns_by_id,
                session_id=session_id,
                frontier_attempts=attempts,
            )
        )
        expected_continuation_clicks = int(
            base.json_object(journey.get("boundedExecution")).get("continuationClicks")
            or 0
        )
        if expected_continuation_clicks != len(continuation_expected):
            failures.append("journey_continuation_click_count_mismatch")

        version_rows = connection.execute(
            "SELECT * FROM itinerary_versions WHERE session_id = ? ORDER BY version_number",
            (session_id,),
        ).fetchall()
        patch_rows = connection.execute(
            "SELECT * FROM itinerary_patches WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        choice_count = connection.execute(
            "SELECT COUNT(*) AS count FROM agent_choice_executions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        actual_choice_count = int(choice_count["count"] if choice_count is not None else 0)
        actual_route_count = route_row_count(connection, plan_id=plan_id)

        if journey_status == "journey_failed_before_contract_completion":
            blocker_codes: list[str] = []
            if str(frontier.get("frontierStatus") or "") == "provider_pending":
                blocker_codes.append("frontier_provider_pending")
            for summary in proposal_summaries:
                row = proposal_by_id.get(summary["proposalId"])
                snapshot = base.json_object(row["snapshot_json"]) if row is not None else {}
                route_audit = base.json_object(snapshot.get("simpleOpenRouteAssignment"))
                if str(route_audit.get("failureReason") or ""):
                    blocker_codes.append(str(route_audit.get("failureReason") or ""))
                for slot in base.pending_slots(snapshot):
                    reason_code = str(base.json_object(slot).get("reasonCode") or "")
                    if reason_code:
                        blocker_codes.append(reason_code)
            if not blocker_codes:
                failures.append("degraded_branch_missing_concrete_blocker")
            if str(session["active_version_id"] or ""):
                failures.append("degraded_branch_active_version_present")
            if version_rows or patch_rows or actual_route_count:
                failures.append("degraded_branch_formal_writes_present")
            return report_with_failures(
                failures=failures,
                session_id=session_id,
                normalized_deepseek_host=normalized_deepseek_host,
                details={
                    "journeyStatus": journey_status,
                    "frontierStatus": frontier.get("frontierStatus"),
                    "blockers": sorted(set(blocker_codes)),
                    "writes": {
                        "versionCount": len(version_rows),
                        "patchCount": len(patch_rows),
                        "routeCount": actual_route_count,
                    },
                    "proposals": proposal_summaries,
                    "controllerEvidenceCount": len(controller_evidence),
                    "assistantExternalCallCounts": assistant_external_counts,
                },
            )

        if journey_status != "success":
            failures.append("journey_status_invalid")
        terminal_frontier_statuses = {
            "qualification_exhausted",
            "poi_exhausted",
            "route_feasible_exhausted",
        }
        frontier_converged = computed_ready_count >= 3 or str(
            frontier.get("frontierStatus") or ""
        ) in terminal_frontier_statuses
        bounded_execution = base.json_object(journey.get("boundedExecution"))
        if bounded_execution.get("frontierConverged") is not True or not frontier_converged:
            failures.append("frontier_live_acceptance_not_converged")
        if (
            str(bounded_execution.get("stopReason") or "")
            == "max_continuation_rounds_reached"
            and computed_ready_count < 3
        ):
            failures.append("frontier_bounded_rounds_exhausted_before_convergence")

        pre_adoption = base.json_object(journey.get("preAdoption"))
        if pre_adoption.get("zeroFormalWrites") is not True:
            failures.append("pre_adoption_zero_write_missing")
        if base.json_object(pre_adoption.get("counts")).get("itineraryVersionCount") not in {0, None}:
            failures.append("pre_adoption_version_count_not_zero")
        if base.json_object(pre_adoption.get("counts")).get("patchCount") not in {0, None}:
            failures.append("pre_adoption_patch_count_not_zero")
        if base.json_object(pre_adoption.get("counts")).get("formalRouteWriteCount") not in {0, None}:
            failures.append("pre_adoption_route_count_not_zero")
        if pre_adoption_bundle is None:
            failures.append("pre_adoption_debug_bundle_missing")
        else:
            sections = base.json_object(pre_adoption_bundle.get("sections"))
            artifact_counts = {
                "itineraryVersionCount": len(base.json_list(sections.get("ITINERARY_VERSIONS"))),
                "patchCount": len(base.json_list(sections.get("PATCHES"))),
                "formalRouteWriteCount": len(base.json_list(sections.get("ROUTE_EVIDENCE"))),
            }
            for key, value in artifact_counts.items():
                if value != 0:
                    failures.append(f"pre_adoption_artifact_{key}_not_zero")
                journey_value = int(base.json_object(pre_adoption.get("counts")).get(key) or 0)
                if journey_value != value:
                    failures.append(f"pre_adoption_artifact_{key}_mismatch")

        adoption = base.json_object(journey.get("adoption"))
        adoption_rows = connection.execute(
            "SELECT * FROM agent_choice_executions "
            "WHERE session_id = ? AND action = 'select_plan_proposal' AND status = 'succeeded' "
            "ORDER BY created_at",
            (session_id,),
        ).fetchall()
        adoption_status = str(adoption.get("status") or "")
        if adoption_status == "confirmed_and_exactly_once_replayed":
            if len(adoption_rows) != 1:
                failures.append(f"adoption_execution_count:{len(adoption_rows)}")
            proposal_id = str(adoption.get("proposalId") or "")
            proposal_row = proposal_by_id.get(proposal_id)
            if proposal_row is None:
                failures.append("adoption_proposal_missing")
            elif adoption_rows:
                failures.extend(
                    base.adoption_execution_binding_errors(
                        adoption_rows[0],
                        proposal_row,
                        turns_by_id,
                        session_id=session_id,
                        expected_version_id=str(adoption.get("firstActiveVersionId") or ""),
                    )
                )
            if str(session["active_version_id"] or "") != str(adoption.get("firstActiveVersionId") or ""):
                failures.append("adoption_final_active_version_mismatch")
            if str(adoption.get("replayActiveVersionId") or "") != str(
                adoption.get("firstActiveVersionId") or ""
            ):
                failures.append("adoption_replay_active_version_mismatch")
            final_counts = base.json_object(adoption.get("finalCounts"))
            post_counts = base.json_object(adoption.get("postAdoptionCounts"))
            if final_counts != post_counts:
                failures.append("adoption_replay_write_growth_detected")
            if int(final_counts.get("itineraryVersionCount") or 0) != len(version_rows):
                failures.append("adoption_version_count_mismatch")
            if int(final_counts.get("patchCount") or 0) != len(patch_rows):
                failures.append("adoption_patch_count_mismatch")
            if int(final_counts.get("formalRouteWriteCount") or 0) != actual_route_count:
                failures.append("adoption_route_count_mismatch")
            if int(final_counts.get("choiceExecutionCount") or 0) != actual_choice_count:
                failures.append("adoption_choice_count_mismatch")
            if len(version_rows) != 1:
                failures.append(f"adoption_version_count_not_exactly_one:{len(version_rows)}")
            if portfolio_row["selected_proposal_id"] != proposal_id:
                failures.append("adoption_portfolio_selected_proposal_mismatch")
            for row in proposal_rows:
                row_id = str(row["id"] or "")
                row_status = str(row["status"] or "")
                if row_id == proposal_id and row_status != "committed":
                    failures.append("adoption_selected_proposal_not_committed")
                if row_id != proposal_id and row_status == "committed":
                    failures.append(f"adoption_unselected_proposal_committed:{row_id}")
        elif adoption_rows:
            failures.append("unexpected_adoption_execution_present")

        return report_with_failures(
            failures=failures,
            session_id=session_id,
            normalized_deepseek_host=normalized_deepseek_host,
            details={
                "journeyStatus": "success",
                "frontierStatus": frontier.get("frontierStatus"),
                "proposalCount": len(proposal_rows),
                "continuationExecutionCount": len(continuation_rows),
                "adoptionExecutionCount": len(adoption_rows),
                "writes": {
                    "versionCount": len(version_rows),
                    "patchCount": len(patch_rows),
                    "routeCount": actual_route_count,
                    "choiceExecutionCount": actual_choice_count,
                },
                "proposals": proposal_summaries,
                "controllerEvidenceCount": len(controller_evidence),
                "assistantExternalCallCounts": assistant_external_counts,
            },
        )
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    normalized_deepseek_host, endpoint_failure = base.deepseek_endpoint_preflight(
        args.deepseek_endpoint_host
    )
    if endpoint_failure is not None:
        args.output.write_text(
            json.dumps(endpoint_failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1
    journey = json.loads(args.journey_result.read_text(encoding="utf-8"))
    run_summary = (
        base.json_object(
            json.loads(args.run_summary.read_text(encoding="utf-8"))
        )
        if isinstance(args.run_summary, Path) and args.run_summary.is_file()
        else {}
    )
    commit_failures = artifact_commit_errors(
        journey=base.json_object(journey),
        run_summary=run_summary,
        expected_git_commit=str(args.expected_git_commit or ""),
    )
    if commit_failures:
        report = {
            "passed": False,
            "failures": commit_failures,
            "gitCommit": str(args.expected_git_commit or "").strip().lower(),
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 1
    artifacts = base.json_object(journey.get("artifacts"))
    pre_adoption_name = str(artifacts.get("preAdoptionDebugBundle") or "")
    pre_adoption_bundle: dict[str, Any] | None = None
    if pre_adoption_name:
        artifact_path = args.journey_result.parent / pre_adoption_name
        if artifact_path.is_file():
            pre_adoption_bundle = base.json_object(
                json.loads(artifact_path.read_text(encoding="utf-8"))
            )
    report = verify_database(
        args.database,
        journey,
        deepseek_endpoint_host=normalized_deepseek_host,
        pre_adoption_bundle=pre_adoption_bundle,
    )
    report["gitCommit"] = str(args.expected_git_commit or "").strip().lower()
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
