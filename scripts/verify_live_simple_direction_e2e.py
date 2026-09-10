from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


AMAP_ID_PATTERN = re.compile(r"^B[0-9A-Z]{8,31}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
CONTRADICTORY_EDIT_PATTERN = re.compile(
    r"没有得到可安全执行的唯一结果|当前活动版本保持不变|no_safe_action",
    re.IGNORECASE,
)
CLARIFICATION_DIMENSIONS = (
    "night_view.cardinality",
    "night_view.experience_mode",
)
CLARIFICATION_SEMANTIC_FIELDS = (
    "frequency",
    "experienceFamilies",
)
CLARIFICATION_FIELD_BY_DIMENSION = dict(
    zip(CLARIFICATION_DIMENSIONS, CLARIFICATION_SEMANTIC_FIELDS)
)
TRUSTED_DEEPSEEK_ENDPOINT_HOST = "api.deepseek.com"
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check the live sequential Simple Direction browser journey "
            "against isolated SQLite."
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


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition and message not in failures:
        failures.append(message)


def canonical_json_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def artifact_commit_errors(
    *,
    journey: dict[str, Any],
    run_summary: dict[str, Any],
    expected_git_commit: str,
) -> list[str]:
    expected = str(expected_git_commit or "").strip().lower()
    journey_commit = str(journey.get("gitCommit") or "").strip().lower()
    summary_commit = str(run_summary.get("gitCommit") or "").strip().lower()
    errors: list[str] = []
    if GIT_COMMIT_PATTERN.fullmatch(expected) is None:
        errors.append("expected_git_commit_invalid")
    if GIT_COMMIT_PATTERN.fullmatch(journey_commit) is None:
        errors.append("journey_git_commit_invalid")
    if GIT_COMMIT_PATTERN.fullmatch(summary_commit) is None:
        errors.append("run_summary_git_commit_invalid")
    if journey_commit != expected:
        errors.append("journey_git_commit_mismatch")
    if summary_commit != expected:
        errors.append("run_summary_git_commit_mismatch")
    if journey_commit != summary_commit:
        errors.append("artifact_git_commit_mismatch")
    return sorted(set(errors))


def detour_semantic_errors(value: Any) -> list[str]:
    semantic = value if isinstance(value, dict) else {}
    errors: list[str] = []
    if not isinstance(value, dict) or set(semantic) not in (
        {"detourTolerance"},
        {"detourTolerance", "adjacentLegConstraint"},
    ):
        errors.append("semantic_fields_invalid")
        return errors
    tolerance = semantic.get("detourTolerance")
    if not isinstance(tolerance, dict) or set(tolerance) != {
        "maxGeneralizedCostDelta",
        "maxDetourRatio",
    }:
        errors.append("detour_tolerance_fields_invalid")
        return errors
    delta = tolerance.get("maxGeneralizedCostDelta")
    ratio = tolerance.get("maxDetourRatio")
    if (
        not isinstance(delta, (int, float))
        or isinstance(delta, bool)
        or not math.isfinite(float(delta))
        or float(delta) <= 0
    ):
        errors.append("max_generalized_cost_delta_invalid")
    if (
        not isinstance(ratio, (int, float))
        or isinstance(ratio, bool)
        or not math.isfinite(float(ratio))
        or not 0 <= float(ratio) <= 1
    ):
        errors.append("max_detour_ratio_invalid")
    if "adjacentLegConstraint" in semantic:
        adjacent = semantic.get("adjacentLegConstraint")
        if not isinstance(adjacent, dict) or set(adjacent) != {
            "candidateSearchRadiusMeters",
            "maxProviderTravelMinutes",
        }:
            errors.append("adjacent_leg_fields_invalid")
        else:
            radius = adjacent.get("candidateSearchRadiusMeters")
            minutes = adjacent.get("maxProviderTravelMinutes")
            if (
                not isinstance(radius, (int, float))
                or isinstance(radius, bool)
                or not math.isfinite(float(radius))
                or not 100 <= float(radius) <= 50000
            ):
                errors.append("candidate_search_radius_invalid")
            if (
                not isinstance(minutes, (int, float))
                or isinstance(minutes, bool)
                or not math.isfinite(float(minutes))
                or not 1 <= float(minutes) <= 480
            ):
                errors.append("max_provider_travel_minutes_invalid")
    return errors


def detour_option_identity_evidence(
    *,
    source_checkpoint: dict[str, Any],
    source_turn_id: str,
    artifact_selection: dict[str, Any],
    selected_agent_choice: dict[str, Any],
    submitted_selection: dict[str, Any],
    resolved_checkpoint: dict[str, Any],
    request_contract: dict[str, Any],
) -> dict[str, Any]:
    dimension = "route_decision.detour_tolerance"
    errors: list[str] = []
    source_fingerprint = str(source_checkpoint.get("fingerprint") or "")
    source_unsigned = {
        key: value for key, value in source_checkpoint.items() if key != "fingerprint"
    }
    source_lineage = {
        "checkpointId": str(source_checkpoint.get("checkpointId") or ""),
        "checkpointFingerprint": source_fingerprint,
        "requestFingerprint": str(source_checkpoint.get("requestFingerprint") or ""),
        "planningRootId": str(source_checkpoint.get("planningRootId") or ""),
        "sourceAssistantTurnId": str(
            source_checkpoint.get("sourceAssistantTurnId") or ""
        ),
    }
    if (
        source_checkpoint.get("schemaVersion") != "clarification-checkpoint-v2"
        or source_checkpoint.get("status") != "awaiting_answer"
        or not all(source_lineage.values())
        or source_lineage["sourceAssistantTurnId"] != source_turn_id
        or source_fingerprint != canonical_json_fingerprint(source_unsigned)
        or source_lineage["requestFingerprint"]
        != canonical_json_fingerprint(request_contract)
    ):
        errors.append("source_checkpoint_identity_invalid")

    questions = [
        item
        for item in json_list(source_checkpoint.get("questions"))
        if isinstance(item, dict) and item.get("dimensionId") == dimension
    ]
    if len(questions) != 1:
        errors.append("source_question_identity_invalid")
        return {
            "verified": False,
            "errors": errors,
            **source_lineage,
            "dimensionId": dimension,
        }
    question = questions[0]
    if question.get("allowFreeText") is not False:
        errors.append("source_question_free_text_unexpected")
    options = [
        item for item in json_list(question.get("options")) if isinstance(item, dict)
    ]
    if not 2 <= len(options) <= 3:
        errors.append("source_option_count_invalid")
    option_ids = [str(item.get("id") or "") for item in options]
    if any(not option_id or option_id.strip() != option_id for option_id in option_ids):
        errors.append("source_option_identity_invalid")
    if len(set(option_ids)) != len(option_ids):
        errors.append("source_option_identity_duplicate")
    semantic_fingerprints: list[str] = []
    for option in options:
        option_id = str(option.get("id") or "")
        semantic = option.get("semanticValue")
        semantic_errors = detour_semantic_errors(semantic)
        if semantic_errors:
            errors.extend(
                f"source_option_{option_id or 'missing'}_{error}"
                for error in semantic_errors
            )
        elif isinstance(semantic, dict):
            semantic_fingerprints.append(canonical_json_fingerprint(semantic))
    if len(set(semantic_fingerprints)) != len(semantic_fingerprints):
        errors.append("source_option_semantic_duplicate")

    artifact_lineage = {
        key: str(artifact_selection.get(key) or "") for key in source_lineage
    }
    allowed_artifact_fields = {
        *source_lineage,
        "dimensionId",
        "optionId",
        "semanticValue",
        "submissionMode",
    }
    if set(artifact_selection) != allowed_artifact_fields:
        errors.append("artifact_fields_invalid")
    if artifact_lineage != source_lineage:
        errors.append("artifact_checkpoint_lineage_mismatch")
    if (
        artifact_selection.get("dimensionId") != dimension
        or artifact_selection.get("submissionMode") != "persisted_option"
    ):
        errors.append("artifact_submission_contract_invalid")
    selected_option_id = str(artifact_selection.get("optionId") or "")
    matching_options = [
        item for item in options if str(item.get("id") or "") == selected_option_id
    ]
    if len(matching_options) != 1:
        errors.append("artifact_option_not_uniquely_source_signed")
        source_semantic: dict[str, Any] = {}
    else:
        source_semantic = json_object(matching_options[0].get("semanticValue"))
    artifact_semantic = json_object(artifact_selection.get("semanticValue"))
    if artifact_semantic != source_semantic:
        errors.append("artifact_semantic_value_mismatch")

    allowed_submission_fields = {"dimensionId", "optionId", "manualValue"}
    if (
        set(submitted_selection) - allowed_submission_fields
        or submitted_selection.get("dimensionId") != dimension
        or str(submitted_selection.get("optionId") or "") != selected_option_id
        or submitted_selection.get("manualValue") not in (None, "")
    ):
        errors.append("browser_submission_identity_invalid")
    selected_option = json_object(selected_agent_choice.get("option"))
    allowed_signed_option_fields = {
        "id",
        "index",
        "label",
        "kind",
        "action",
        "scopeKind",
        "checkpointId",
        "checkpointFingerprint",
        "sourceUserTurnId",
        "planningSelectionRootTurnId",
        "allowsManualInput",
        "sourceAssistantTurnId",
    }
    if set(selected_option) != allowed_signed_option_fields:
        errors.append("submitted_capability_fields_invalid")
    if (
        str(selected_agent_choice.get("sourceAssistantTurnId") or "")
        != source_lineage["sourceAssistantTurnId"]
        or str(selected_option.get("checkpointId") or "")
        != source_lineage["checkpointId"]
        or str(selected_option.get("checkpointFingerprint") or "")
        != source_lineage["checkpointFingerprint"]
        or str(selected_option.get("planningSelectionRootTurnId") or "")
        != source_lineage["planningRootId"]
    ):
        errors.append("submitted_checkpoint_lineage_mismatch")

    resolved_unsigned = {
        key: value for key, value in resolved_checkpoint.items() if key != "fingerprint"
    }
    if any(
        str(resolved_checkpoint.get(source_key) or "") != source_lineage[evidence_key]
        for source_key, evidence_key in (
            ("checkpointId", "checkpointId"),
            ("requestFingerprint", "requestFingerprint"),
            ("planningRootId", "planningRootId"),
            ("sourceAssistantTurnId", "sourceAssistantTurnId"),
        )
    ) or str(
        resolved_checkpoint.get("fingerprint") or ""
    ) != canonical_json_fingerprint(resolved_unsigned):
        errors.append("resolved_checkpoint_lineage_invalid")
    resolved_answers = [
        item
        for item in json_list(resolved_checkpoint.get("resolvedAnswers"))
        if isinstance(item, dict) and item.get("dimensionId") == dimension
    ]
    if len(resolved_answers) != 1:
        errors.append("resolved_answer_identity_invalid")
    else:
        resolved_answer = resolved_answers[0]
        required_resolved_answer_fields = {
            "dimensionId",
            "semanticValue",
            "source",
            "sourceUserTurnId",
            "optionId",
            "label",
        }
        if not (
            required_resolved_answer_fields.issubset(resolved_answer)
            and set(resolved_answer)
            <= required_resolved_answer_fields | {"manualValue"}
        ):
            errors.append("resolved_answer_fields_invalid")
        if (
            resolved_answer.get("source") != "structured_option"
            or str(resolved_answer.get("optionId") or "") != selected_option_id
            or resolved_answer.get("manualValue") not in (None, "")
        ):
            errors.append("resolved_answer_source_invalid")
        if json_object(resolved_answer.get("semanticValue")) != source_semantic:
            errors.append("resolved_answer_semantic_value_mismatch")

    return {
        "verified": not errors,
        "errors": errors,
        **source_lineage,
        "dimensionId": dimension,
        "optionId": selected_option_id,
        "semanticValue": source_semantic,
        "submissionMode": artifact_selection.get("submissionMode"),
    }


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_time_window(value: Any, start: Any, end: Any) -> bool:
    parsed = parse_timestamp(value)
    parsed_start = parse_timestamp(start)
    parsed_end = parse_timestamp(end)
    return bool(
        parsed is not None
        and parsed_start is not None
        and parsed_end is not None
        and parsed_start <= parsed <= parsed_end
    )


def route_contract_ready(snapshot: dict[str, Any]) -> bool:
    contract = json_object(snapshot.get("routeDecisionContract"))
    return bool(
        contract.get("schemaVersion") == "route-decision-contract-v2"
        and contract.get("status") == "ready"
        and contract.get("missingFields") == []
        and json_object(contract.get("mobilityProfile"))
        and json_object(contract.get("detourTolerance"))
        and json_object(contract.get("adjacentLegConstraint"))
        == {
            "candidateSearchRadiusMeters": 5000,
            "maxProviderTravelMinutes": 45,
        }
        and json_object(contract.get("topologyConstraint"))
        == {"maxBacktrackRatio": 0.15}
        and str(contract.get("fingerprint") or "")
    )


def compact_route_evidence_errors(snapshot: dict[str, Any]) -> list[str]:
    contract = json_object(snapshot.get("routeDecisionContract"))
    audit = json_object(snapshot.get("simpleOpenRouteAssignment"))
    expected = [json_object(item) for item in json_list(audit.get("expectedPairs"))]
    verified = [json_object(item) for item in json_list(audit.get("verifiedPairs"))]
    expected_identities = [_route_pair_identity(item) for item in expected]
    verified_identities = [_route_pair_identity(item) for item in verified]
    actual_identities = derived_route_pair_identities(snapshot)
    errors: list[str] = []
    if audit.get("schemaVersion") != "simple-open-route-evidence-v2":
        errors.append("route_evidence_schema_invalid")
    if str(audit.get("routeContractFingerprint") or "") != str(
        contract.get("fingerprint") or ""
    ):
        errors.append("route_contract_fingerprint_mismatch")
    if audit.get("routeCoverageComplete") is not True:
        errors.append("route_coverage_incomplete")
    if audit.get("adjacentLegCompliance") != "verified":
        errors.append("adjacent_leg_not_verified")
    if audit.get("topologyCompliance") != "verified":
        errors.append("topology_not_verified")
    if audit.get("providerBaselineCompared") is not False:
        errors.append("provider_baseline_state_invalid")
    if audit.get("detourCompliance") != "not_evaluated":
        errors.append("detour_state_invalid")
    if any(
        identity is None for identity in [*expected_identities, *verified_identities]
    ):
        errors.append("route_pair_identity_invalid")
    normalized_expected = [
        identity for identity in expected_identities if identity is not None
    ]
    normalized_verified = [
        identity for identity in verified_identities if identity is not None
    ]
    if (
        not normalized_expected
        or normalized_expected != actual_identities
        or normalized_verified != actual_identities
        or len(set(normalized_expected)) != len(normalized_expected)
        or len(set(normalized_verified)) != len(normalized_verified)
        or len(set(actual_identities)) != len(actual_identities)
    ):
        errors.append("route_pair_identity_mismatch")
    if any(
        not pair[0]
        or not pair[1]
        or not finite_number(item.get("durationSeconds"))
        or float(item.get("durationSeconds") or 0) <= 0
        or float(item.get("durationSeconds") or 0) > 45 * 60
        or not finite_number(item.get("distanceMeters"))
        or float(item.get("distanceMeters") or 0) <= 0
        or str(item.get("transportMode") or "") not in {"transit", "public_transit"}
        for pair, item in zip(
            [
                (str(value.get("fromAmapId") or ""), str(value.get("toAmapId") or ""))
                for value in verified
            ],
            verified,
        )
    ):
        errors.append("verified_route_pair_invalid")
    return errors


def _route_pair_identity(
    item: dict[str, Any],
) -> tuple[int, int, str, str, str, str] | None:
    try:
        day_number = int(item.get("dayNumber") or 0)
        pair_ordinal = int(item.get("pairOrdinal") or 0)
    except (TypeError, ValueError):
        return None
    from_segment_id = str(
        item.get("fromSegmentId") or item.get("from_segment_id") or ""
    ).strip()
    to_segment_id = str(
        item.get("toSegmentId") or item.get("to_segment_id") or ""
    ).strip()
    from_amap_id = (
        str(item.get("fromAmapId") or item.get("from_amap_id") or "").strip().upper()
    )
    to_amap_id = (
        str(item.get("toAmapId") or item.get("to_amap_id") or "").strip().upper()
    )
    if (
        day_number <= 0
        or pair_ordinal <= 0
        or not from_segment_id
        or not to_segment_id
        or AMAP_ID_PATTERN.fullmatch(from_amap_id) is None
        or AMAP_ID_PATTERN.fullmatch(to_amap_id) is None
    ):
        return None
    return (
        day_number,
        pair_ordinal,
        from_segment_id,
        to_segment_id,
        from_amap_id,
        to_amap_id,
    )


def route_provider_evidence_fingerprint(item: dict[str, Any]) -> str:
    identity = _route_pair_identity(item)
    if identity is None:
        return ""
    duration = item.get("durationSeconds")
    distance = item.get("distanceMeters")
    if (
        not finite_number(duration)
        or float(duration) <= 0
        or not finite_number(distance)
        or float(distance) <= 0
    ):
        return ""
    normalized_mode = (
        str(item.get("mode") or item.get("transportMode") or "")
        .strip()
        .casefold()
        .replace("-", "_")
        .replace(" ", "_")
    )
    normalized_mode = {
        "walk": "walking",
        "public_transit": "transit",
        "public_transport": "transit",
        "bus": "transit",
        "subway": "transit",
        "metro": "transit",
        "rail": "transit",
        "bike": "bicycling",
        "cycling": "bicycling",
        "self_drive": "driving",
    }.get(normalized_mode, normalized_mode)
    material = {
        "dayNumber": identity[0],
        "pairOrdinal": identity[1],
        "fromSegmentId": identity[2],
        "toSegmentId": identity[3],
        "fromAmapId": identity[4],
        "toAmapId": identity[5],
        "transportMode": normalized_mode,
        "durationSeconds": float(duration),
        "distanceMeters": float(distance),
        "provider": str(item.get("provider") or item.get("source") or ""),
        "queriedAt": str(item.get("queriedAt") or ""),
    }
    return canonical_json_fingerprint(material)


def _real_amap_poi(value: Any) -> bool:
    poi = json_object(value)
    return bool(
        AMAP_ID_PATTERN.fullmatch(str(poi.get("amapId") or "").strip().upper())
        and poi.get("source") == "amap-place-search"
        and finite_number(poi.get("latitude"))
        and finite_number(poi.get("longitude"))
    )


def _is_materialized_route_segment(segment: dict[str, Any]) -> bool:
    return bool(
        str(segment.get("kind") or "") not in {"pending", "placeholder"}
        and _real_amap_poi(segment.get("poi"))
    )


def _is_route_target_segment(segment: dict[str, Any]) -> bool:
    if not _is_materialized_route_segment(segment):
        return False
    semantic = semantic_metadata(segment)
    if "requiresRouteEdge" in semantic:
        return semantic.get("requiresRouteEdge") is True
    return True


def derived_route_pair_identities(
    snapshot: dict[str, Any],
) -> list[tuple[int, int, str, str, str, str]]:
    pairs: list[tuple[int, int, str, str, str, str]] = []
    for day_index, day in enumerate(json_list(snapshot.get("days")), start=1):
        if not isinstance(day, dict):
            continue
        try:
            day_number = int(day.get("dayNumber") or day_index)
        except (TypeError, ValueError):
            continue
        route_targets = [
            segment
            for segment in json_list(day.get("segments"))
            if isinstance(segment, dict) and _is_route_target_segment(segment)
        ]
        segment_ids = [
            str(segment.get("id") or "").strip() for segment in route_targets
        ]
        if any(not segment_id for segment_id in segment_ids):
            continue
        for pair_ordinal, (left, right) in enumerate(
            zip(route_targets, route_targets[1:]), start=1
        ):
            left_poi = json_object(left.get("poi"))
            right_poi = json_object(right.get("poi"))
            pairs.append(
                (
                    day_number,
                    pair_ordinal,
                    str(left.get("id") or "").strip(),
                    str(right.get("id") or "").strip(),
                    str(left_poi.get("amapId") or "").strip().upper(),
                    str(right_poi.get("amapId") or "").strip().upper(),
                )
            )
    return pairs


def semantic_metadata(segment: dict[str, Any]) -> dict[str, Any]:
    metadata = json_object(segment.get("semanticMetadata"))
    for key in (
        "completionRequired",
        "dayCompletionRequired",
        "goalId",
        "sourceGoalId",
        "occurrenceId",
        "groundingStatus",
        "intentSlotId",
        "intentType",
        "planningSlotId",
        "poolId",
        "rawNeed",
        "required",
        "requirementLevel",
        "requiresRouteEdge",
        "routeAnchor",
        "lineageAuthority",
        "userExplicit",
    ):
        if key not in metadata and key in segment:
            metadata[key] = segment.get(key)
    return metadata


def finite_number(value: Any) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def canonical_poi_errors(poi: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    amap_id = str(poi.get("amapId") or "")
    latitude = poi.get("latitude")
    longitude = poi.get("longitude")
    grounding = str(metadata.get("groundingStatus") or poi.get("groundingStatus") or "")
    if not AMAP_ID_PATTERN.fullmatch(amap_id):
        errors.append("canonical_amap_id_invalid")
    if not finite_number(latitude) or not (-90 <= float(latitude) <= 90):
        errors.append("canonical_latitude_invalid")
    if not finite_number(longitude) or not (-180 <= float(longitude) <= 180):
        errors.append("canonical_longitude_invalid")
    if poi.get("source") != "amap-place-search":
        errors.append("canonical_source_invalid")
    if grounding not in {"verified_amap", "provisional"}:
        errors.append("materialized_grounding_invalid")
    if grounding == "unresolved" or poi.get("needsConcretePoi") is True:
        errors.append("unresolved_skeleton_materialized")
    return errors


def pending_slots(container: dict[str, Any]) -> list[dict[str, Any]]:
    raw = container.get("portfolioPendingSlots")
    if not isinstance(raw, list):
        raw = container.get("pendingSlots")
    return [item for item in json_list(raw) if isinstance(item, dict)]


def pending_lineage(slot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(slot.get("id") or ""),
        "dayNumber": int(slot.get("dayNumber") or 0),
        "startTime": str(slot.get("startTime") or ""),
        "endTime": str(slot.get("endTime") or ""),
        "timeWindow": str(slot.get("timeWindow") or ""),
        "intentType": str(slot.get("intentType") or ""),
        "goalId": str(slot.get("goalId") or ""),
        "sourceGoalId": str(slot.get("sourceGoalId") or ""),
        "occurrenceId": str(slot.get("occurrenceId") or ""),
        "planningSlotId": str(slot.get("planningSlotId") or ""),
        "poolId": str(slot.get("poolId") or ""),
        "reasonCode": str(slot.get("reasonCode") or ""),
        "requirementLevel": str(slot.get("requirementLevel") or ""),
        "groundingStatus": str(slot.get("groundingStatus") or ""),
        "simpleDirectionProviderExhausted": slot.get(
            "simpleDirectionProviderExhausted"
        ),
        "simpleDirectionRequirementLineageConflict": slot.get(
            "simpleDirectionRequirementLineageConflict"
        ),
        "futureRouteAnchor": slot.get("futureRouteAnchor"),
        "routeAnchorExpected": slot.get("routeAnchorExpected"),
    }


def _authoritative_explicit_every_day_meal_requirements(
    request_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for item in json_list(request_contract.get("requiredIntents")):
        if not isinstance(item, dict):
            continue
        if str(item.get("intentType") or "") not in {"meal", "local_food", "food"}:
            continue
        if (
            item.get("userExplicit") is not True
            or str(item.get("distributionPolicy") or "") != "every_allowed_day"
            or str(item.get("cardinalitySource") or "") != "explicit_every_day"
        ):
            continue
        try:
            allowed_days = sorted(
                {
                    int(value)
                    for value in json_list(item.get("allowedDayNumbers"))
                    if not isinstance(value, bool) and int(value) > 0
                }
            )
        except (TypeError, ValueError):
            continue
        goal_id = str(item.get("goalId") or "").strip()
        if goal_id and allowed_days:
            requirements.append({"goalId": goal_id, "allowedDayNumbers": allowed_days})
    return requirements


def _minutes_of_day(value: str) -> int | None:
    if TIME_PATTERN.fullmatch(value) is None:
        return None
    return int(value[:2]) * 60 + int(value[3:])


def _is_completion_required_pending_slot(
    slot: dict[str, Any], *, goal_id: str, day_number: int
) -> bool:
    lineage = pending_lineage(slot)
    raw_allowed_days = slot.get("allowedDayNumbers")
    if not isinstance(raw_allowed_days, list):
        return False
    try:
        allowed_days = {
            int(value)
            for value in raw_allowed_days
            if not isinstance(value, bool) and int(value) > 0
        }
    except (TypeError, ValueError):
        return False
    return bool(
        str(lineage["intentType"]) in {"meal", "local_food", "food"}
        and lineage["dayNumber"] == day_number
        and day_number in allowed_days
        and lineage["goalId"] == goal_id
        and lineage["sourceGoalId"] == goal_id
        and lineage["occurrenceId"] == f"occ:{goal_id}:day:{day_number}"
        and slot.get("userExplicit") is True
        and str(slot.get("distributionPolicy") or "") == "every_allowed_day"
        and str(slot.get("cardinalitySource") or "") == "explicit_every_day"
        and str(slot.get("lineageAuthority") or "")
        in {
            "goal_occurrence_compiler",
            "simple_open_request_contract_every_day_meal",
        }
        and slot.get("simpleDirectionRequirementLineageConflict") is not True
    )


def explicit_every_day_meal_errors(
    snapshot: dict[str, Any], request_contract: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    pending = pending_slots(snapshot)
    segments = snapshot_segments(snapshot)
    requirements = _authoritative_explicit_every_day_meal_requirements(request_contract)
    for requirement in requirements:
        goal_id = str(requirement["goalId"])
        for day_number in requirement["allowedDayNumbers"]:
            day_matches = []
            for item in segments:
                if int(item.get("dayNumber") or 0) != int(day_number):
                    continue
                semantic = json_object(item.get("semantic"))
                poi = json_object(item.get("poi"))
                if not _real_amap_poi(poi):
                    continue
                if str(
                    semantic.get("intentType") or poi.get("intentType") or ""
                ) not in {
                    "meal",
                    "local_food",
                    "food",
                }:
                    continue
                if str(semantic.get("goalId") or "") != goal_id:
                    continue
                if str(semantic.get("sourceGoalId") or goal_id) != goal_id:
                    continue
                if (
                    str(semantic.get("occurrenceId") or "")
                    != f"occ:{goal_id}:day:{day_number}"
                ):
                    continue
                if (
                    semantic.get("completionRequired") is not None
                    and semantic.get("completionRequired") is not True
                ):
                    errors.append(
                        f"explicit_every_day_meal_materialized_completion_required_invalid:{goal_id}:{day_number}"
                    )
                start_minutes = _minutes_of_day(str(item.get("startTime") or ""))
                if start_minutes is None or not 11 * 60 <= start_minutes < 14 * 60:
                    errors.append(
                        f"explicit_every_day_meal_not_at_noon:{goal_id}:{day_number}"
                    )
                day_matches.append(item)
            if any(
                _is_completion_required_pending_slot(
                    slot,
                    goal_id=goal_id,
                    day_number=int(day_number),
                )
                for slot in pending
            ):
                errors.append(
                    f"explicit_every_day_meal_pending_completion_required:{goal_id}:{day_number}"
                )
            if len(day_matches) != 1:
                errors.append(
                    f"explicit_every_day_meal_occurrence_count_invalid:{goal_id}:{day_number}:{len(day_matches)}"
                )
    return sorted(set(errors))


def typed_provider_exhausted_night_errors(slot: dict[str, Any]) -> list[str]:
    lineage = pending_lineage(slot)
    errors: list[str] = []
    for field in (
        "id",
        "startTime",
        "endTime",
        "timeWindow",
        "goalId",
        "sourceGoalId",
        "planningSlotId",
        "poolId",
        "reasonCode",
    ):
        if not lineage[field]:
            errors.append(f"pending_{field}_missing")
    if lineage["dayNumber"] <= 0:
        errors.append("pending_dayNumber_invalid")
    if not TIME_PATTERN.fullmatch(lineage["startTime"]):
        errors.append("pending_startTime_invalid")
    if not TIME_PATTERN.fullmatch(lineage["endTime"]):
        errors.append("pending_endTime_invalid")
    expected_window = f"{lineage['startTime']}-{lineage['endTime']}"
    if lineage["timeWindow"] != expected_window:
        errors.append("pending_timeWindow_inconsistent")
    if lineage["intentType"] != "night_view":
        errors.append("pending_intent_not_night_view")
    if lineage["goalId"] != lineage["sourceGoalId"]:
        errors.append("pending_goal_lineage_mismatch")
    if lineage["requirementLevel"] not in {"required", "hard"}:
        errors.append("pending_requirement_not_required")
    if lineage["groundingStatus"] != "unresolved":
        errors.append("pending_grounding_not_unresolved")
    if lineage["simpleDirectionProviderExhausted"] is not True:
        errors.append("pending_provider_exhausted_flag_missing")
    if lineage["simpleDirectionRequirementLineageConflict"] is not False:
        errors.append("pending_lineage_conflict_not_false")
    if lineage["futureRouteAnchor"] is not True:
        errors.append("pending_future_route_anchor_missing")
    if lineage["routeAnchorExpected"] is not True:
        errors.append("pending_route_anchor_expected_missing")
    if isinstance(slot.get("poi"), dict):
        errors.append("pending_slot_contains_materialized_poi")
    return errors


def provider_exhausted_required_night_slots(
    container: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        slot
        for slot in pending_slots(container)
        if slot.get("intentType") == "night_view"
        and slot.get("requirementLevel") in {"required", "hard"}
        and slot.get("simpleDirectionProviderExhausted") is True
    ]


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _hard_night_requirements(request_contract: dict[str, Any]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for item in json_list(request_contract.get("requiredIntents")):
        if not isinstance(item, dict):
            continue
        if str(item.get("intentType") or "") != "night_view":
            continue
        if str(item.get("requirementLevel") or "") not in {"required", "hard"}:
            continue
        goal_id = str(item.get("goalId") or "").strip()
        count = next(
            (
                candidate
                for candidate in (
                    _positive_int(item.get("requiredMin")),
                    _positive_int(item.get("minCount")),
                    _positive_int(item.get("requestedCount")),
                    _positive_int(item.get("target")),
                )
                if candidate > 0
            ),
            0,
        )
        if goal_id and count > 0:
            requirements.append({"goalId": goal_id, "requiredCount": count})
    return requirements


def _materialized_hard_night_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for day_index, day in enumerate(json_list(snapshot.get("days")), start=1):
        if not isinstance(day, dict):
            continue
        day_number = int(day.get("dayNumber") or day_index)
        for segment in json_list(day.get("segments")):
            if not isinstance(segment, dict):
                continue
            metadata = semantic_metadata(segment)
            poi = json_object(segment.get("poi"))
            intent_type = str(metadata.get("intentType") or poi.get("intentType") or "")
            if intent_type != "night_view" or not poi:
                continue
            grounding = str(
                metadata.get("groundingStatus") or poi.get("groundingStatus") or ""
            )
            records.append(
                {
                    "goalId": str(
                        metadata.get("goalId") or metadata.get("sourceGoalId") or ""
                    ),
                    "sourceGoalId": str(metadata.get("sourceGoalId") or ""),
                    "occurrenceId": str(metadata.get("occurrenceId") or ""),
                    "planningSlotId": str(metadata.get("planningSlotId") or ""),
                    "poolId": str(metadata.get("poolId") or ""),
                    "dayNumber": day_number,
                    "startTime": str(segment.get("startTime") or ""),
                    "endTime": str(segment.get("endTime") or ""),
                    "amapId": str(poi.get("amapId") or "").upper(),
                    "name": str(poi.get("name") or ""),
                    "source": str(poi.get("source") or ""),
                    "groundingStatus": grounding,
                    "canonicalErrors": canonical_poi_errors(poi, metadata),
                }
            )
    return records


def hard_night_occurrence_evidence(
    snapshot: dict[str, Any], request_contract: dict[str, Any]
) -> dict[str, Any]:
    requirements = _hard_night_requirements(request_contract)
    materialized = _materialized_hard_night_records(snapshot)
    pending = provider_exhausted_required_night_slots(snapshot)
    pending_records = [pending_lineage(slot) for slot in pending]
    errors: list[str] = []
    expected_goal_ids = {item["goalId"] for item in requirements}
    if not requirements:
        errors.append("hard_night_requirement_contract_missing")

    for record in materialized:
        goal_id = record["goalId"]
        if not goal_id:
            errors.append("hard_night_materialized_goal_lineage_missing")
        elif goal_id not in expected_goal_ids:
            errors.append(f"hard_night_unexpected_goal_lineage:{goal_id}")
        if not record["sourceGoalId"]:
            errors.append(
                f"hard_night_materialized_source_goal_missing:{goal_id or 'missing'}"
            )
        elif record["sourceGoalId"] != goal_id:
            errors.append(f"hard_night_materialized_source_goal_mismatch:{goal_id}")
        if not record["planningSlotId"]:
            errors.append(
                f"hard_night_materialized_planning_slot_missing:{goal_id or 'missing'}"
            )
        if not record["poolId"]:
            errors.append(
                f"hard_night_materialized_pool_missing:{goal_id or 'missing'}"
            )
        for error in record["canonicalErrors"]:
            errors.append(
                f"hard_night_materialized_identity:{goal_id or 'missing'}:{error}"
            )
        if not record["occurrenceId"]:
            errors.append(
                f"hard_night_materialized_occurrence_lineage_missing:{goal_id or 'missing'}"
            )

    for slot, record in zip(pending, pending_records):
        goal_id = record["goalId"]
        if not goal_id or goal_id not in expected_goal_ids:
            errors.append(f"hard_night_unexpected_goal_lineage:{goal_id or 'missing'}")
        for error in typed_provider_exhausted_night_errors(slot):
            errors.append(
                f"hard_night_pending_typed_contract:{goal_id or 'missing'}:{error}"
            )
        if not record["occurrenceId"]:
            errors.append(
                f"hard_night_pending_occurrence_lineage_missing:{goal_id or 'missing'}"
            )

    for requirement in requirements:
        goal_id = requirement["goalId"]
        required_count = requirement["requiredCount"]
        goal_materialized = [item for item in materialized if item["goalId"] == goal_id]
        goal_pending = [item for item in pending_records if item["goalId"] == goal_id]
        represented_count = len(goal_materialized) + len(goal_pending)
        if represented_count < required_count:
            for ordinal in range(represented_count + 1, required_count + 1):
                errors.append(f"hard_night_occurrence_missing:{goal_id}:{ordinal}")
        elif represented_count > required_count:
            code = (
                "hard_night_occurrence_dual_representation"
                if goal_materialized and goal_pending and required_count == 1
                else "hard_night_occurrence_overrepresented"
            )
            errors.append(f"{code}:{goal_id}:{required_count}")

        materialized_occurrences = {
            item["occurrenceId"] or item["planningSlotId"] for item in goal_materialized
        }
        pending_occurrences = {
            item["occurrenceId"] or item["planningSlotId"] for item in goal_pending
        }
        all_occurrences = [
            item["occurrenceId"] or item["planningSlotId"]
            for item in [*goal_materialized, *goal_pending]
        ]
        for occurrence in sorted(set(all_occurrences)):
            if occurrence and all_occurrences.count(occurrence) > 1:
                errors.append(f"hard_night_occurrence_duplicate:{goal_id}:{occurrence}")
        for occurrence in sorted(materialized_occurrences & pending_occurrences):
            errors.append(f"hard_night_occurrence_dual_lineage:{goal_id}:{occurrence}")

    branch = "missing"
    if materialized and not pending:
        branch = "materialized"
    elif pending and not materialized:
        branch = "provider_exhausted_pending"
    elif materialized and pending:
        branch = "mixed"
    return {
        "errors": sorted(set(errors)),
        "branch": branch,
        "requiredOccurrenceCount": sum(item["requiredCount"] for item in requirements),
        "coveredOccurrenceCount": len(materialized) + len(pending_records),
        "materialized": [
            {key: value for key, value in item.items() if key != "canonicalErrors"}
            for item in materialized
        ],
        "providerExhaustedPending": pending_records,
    }


def hard_night_evidence_core(evidence: dict[str, Any]) -> str:
    return json.dumps(
        {
            "branch": evidence.get("branch"),
            "requiredOccurrenceCount": evidence.get("requiredOccurrenceCount"),
            "coveredOccurrenceCount": evidence.get("coveredOccurrenceCount"),
            "materialized": evidence.get("materialized") or [],
            "providerExhaustedPending": evidence.get("providerExhaustedPending") or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def hard_night_identity_core(evidence: dict[str, Any]) -> dict[str, Any]:
    materialized_keys = (
        "goalId",
        "sourceGoalId",
        "occurrenceId",
        "planningSlotId",
        "poolId",
        "dayNumber",
        "startTime",
        "endTime",
        "amapId",
        "name",
        "source",
        "groundingStatus",
    )
    pending_keys = (
        "id",
        "goalId",
        "sourceGoalId",
        "occurrenceId",
        "planningSlotId",
        "poolId",
        "dayNumber",
        "startTime",
        "endTime",
        "timeWindow",
        "reasonCode",
        "groundingStatus",
    )
    return {
        "branch": evidence.get("branch"),
        "materialized": [
            {key: item.get(key) for key in materialized_keys}
            for item in json_list(evidence.get("materialized"))
            if isinstance(item, dict)
        ],
        "providerExhaustedPending": [
            {key: item.get(key) for key in pending_keys}
            for item in json_list(evidence.get("providerExhaustedPending"))
            if isinstance(item, dict)
        ],
    }


def lineage_fingerprint(slots: list[dict[str, Any]]) -> list[str]:
    return sorted(
        json.dumps(pending_lineage(slot), ensure_ascii=False, sort_keys=True)
        for slot in slots
    )


def snapshot_quality(snapshot: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    day_anchor_counts: dict[str, int] = {}
    materialized_poi_count = 0
    days = [day for day in json_list(snapshot.get("days")) if isinstance(day, dict)]
    if not days:
        errors.append("snapshot_days_missing")
    for day_index, day in enumerate(days, start=1):
        day_number = int(day.get("dayNumber") or day_index)
        anchor_count = 0
        segments = [
            segment
            for segment in json_list(day.get("segments"))
            if isinstance(segment, dict)
        ]
        for segment_index, segment in enumerate(segments):
            metadata = semantic_metadata(segment)
            poi = json_object(segment.get("poi"))
            grounding = str(metadata.get("groundingStatus") or "")
            label = f"day{day_number}_segment{segment_index + 1}"
            if poi:
                materialized_poi_count += 1
                for error in canonical_poi_errors(poi, metadata):
                    errors.append(f"{label}:{error}")
            elif metadata.get("routeAnchor") is True or grounding in {
                "verified_amap",
                "provisional",
                "unresolved",
            }:
                errors.append(f"{label}:materialized_route_anchor_poi_missing")
            if grounding == "unresolved":
                errors.append(f"{label}:unresolved_skeleton_materialized")
            if (
                metadata.get("routeAnchor") is True
                and grounding == "verified_amap"
                and poi
                and not canonical_poi_errors(poi, metadata)
            ):
                anchor_count += 1
        day_anchor_counts[str(day_number)] = anchor_count
        if anchor_count < 1:
            errors.append(f"day{day_number}:verified_amap_anchor_missing")
    return {
        "errors": errors,
        "dayAnchorCounts": day_anchor_counts,
        "materializedPoiCount": materialized_poi_count,
    }


def standard_two_day_daily_completion_evidence(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Verify the live standard two-day contract without repairing its snapshot.

    The live journey is intentionally fixed to a standard-paced two-day request.
    Its server-authored density target and daily-completion lineage are therefore
    acceptance evidence, not advisory UI metadata.  A pending supplement never
    counts as the materialized Day 2 stop.
    """

    errors: list[str] = []
    days = [day for day in json_list(snapshot.get("days")) if isinstance(day, dict)]
    day_by_number: dict[int, dict[str, Any]] = {}
    for day_index, day in enumerate(days, start=1):
        try:
            day_number = int(day.get("dayNumber") or day_index)
        except (TypeError, ValueError):
            continue
        if day_number > 0 and day_number not in day_by_number:
            day_by_number[day_number] = day

    if str(snapshot.get("simpleOpenExecutionProfile") or "") != "simple_open_v1":
        errors.append("simple_open_execution_profile_invalid")
    pace_class = str(
        json_object(
            json_object(snapshot.get("routeDecisionContract")).get("mobilityProfile")
        ).get("paceClass")
        or ""
    )
    if pace_class != "standard":
        errors.append(f"standard_pace_contract_invalid:{pace_class or 'missing'}")
    if set(day_by_number) != {1, 2}:
        errors.append(
            "standard_two_day_calendar_invalid:"
            + ",".join(str(value) for value in sorted(day_by_number))
        )

    raw_targets = snapshot.get("desiredDensityAnchorTargets")
    targets: dict[str, int] = {}
    if isinstance(raw_targets, dict):
        for raw_day, raw_target in raw_targets.items():
            if (
                not isinstance(raw_day, str)
                or not raw_day.isdigit()
                or str(int(raw_day)) != raw_day
                or isinstance(raw_target, bool)
                or not isinstance(raw_target, int)
                or raw_target < 0
            ):
                targets = {}
                break
            targets[raw_day] = raw_target
    if set(targets) != {"1", "2"}:
        errors.append("authoritative_daily_anchor_targets_invalid")
    if targets.get("2", 0) < 3:
        errors.append(
            f"day2_authoritative_anchor_target_below_three:{targets.get('2', 0)}"
        )

    actuals: dict[str, int] = {}
    for day_number, day in sorted(day_by_number.items()):
        actuals[str(day_number)] = sum(
            1
            for segment in json_list(day.get("segments"))
            if isinstance(segment, dict)
            and semantic_metadata(segment).get("routeAnchor") is True
            and semantic_metadata(segment).get("groundingStatus") == "verified_amap"
            and _real_amap_poi(segment.get("poi"))
        )
    for day_number in (1, 2):
        target = targets.get(str(day_number))
        actual = actuals.get(str(day_number), 0)
        if target is not None and actual != target:
            errors.append(f"day{day_number}_anchor_target_mismatch:{target}:{actual}")

    day_2_segments = [
        segment
        for segment in json_list(day_by_number.get(2, {}).get("segments"))
        if isinstance(segment, dict)
    ]
    day_2_completion_candidates = [
        segment
        for segment in day_2_segments
        if semantic_metadata(segment).get("lineageAuthority")
        == "simple_open_daily_completion_policy"
        or semantic_metadata(segment).get("dayCompletionRequired") is True
    ]
    materialized_completions = [
        segment
        for segment in day_2_completion_candidates
        if semantic_metadata(segment).get("groundingStatus") == "verified_amap"
        and _real_amap_poi(segment.get("poi"))
    ]
    if len(materialized_completions) != 1:
        errors.append(
            f"day2_daily_completion_materialized_count_invalid:{len(materialized_completions)}"
        )

    pending_completion = [
        slot
        for slot in pending_slots(snapshot)
        if int(slot.get("dayNumber") or 0) == 2
        and (
            str(slot.get("lineageAuthority") or "")
            == "simple_open_daily_completion_policy"
            or slot.get("dayCompletionRequired") is True
        )
    ]
    if pending_completion:
        errors.append("day2_daily_completion_pending")

    completion = (
        materialized_completions[0] if len(materialized_completions) == 1 else {}
    )
    completion_metadata = semantic_metadata(completion) if completion else {}
    if completion:
        if (
            completion_metadata.get("lineageAuthority")
            != "simple_open_daily_completion_policy"
        ):
            errors.append("day2_daily_completion_lineage_authority_invalid")
        if completion_metadata.get("dayCompletionRequired") is not True:
            errors.append("day2_daily_completion_required_flag_missing")
        if completion_metadata.get("completionRequired") is not False:
            errors.append("day2_daily_completion_user_completion_flag_invalid")
        if completion_metadata.get("userExplicit") is not False:
            errors.append("day2_daily_completion_user_explicit_flag_invalid")
        goal_id = str(completion_metadata.get("goalId") or "")
        if (
            goal_id != "goal_daily_completion_day_2"
            or str(completion_metadata.get("sourceGoalId") or "") != goal_id
            or str(completion_metadata.get("occurrenceId") or "")
            != f"occ:{goal_id}:day:2"
            or not str(completion_metadata.get("planningSlotId") or "")
        ):
            errors.append("day2_daily_completion_occurrence_lineage_invalid")

    campus_segments = [
        segment
        for segment in day_2_segments
        if str(semantic_metadata(segment).get("intentType") or "") == "campus_visit"
        and _is_route_target_segment(segment)
    ]
    meal_segments = [
        segment
        for segment in day_2_segments
        if str(semantic_metadata(segment).get("intentType") or "")
        in {"meal", "local_food", "food"}
        and _is_route_target_segment(segment)
    ]
    if len(campus_segments) != 1:
        errors.append(f"day2_campus_route_anchor_count_invalid:{len(campus_segments)}")
    if len(meal_segments) != 1:
        errors.append(f"day2_noon_meal_route_anchor_count_invalid:{len(meal_segments)}")

    route_sequence: list[dict[str, Any]] = [
        segment for segment in day_2_segments if _is_route_target_segment(segment)
    ]
    if len(campus_segments) == len(meal_segments) == len(materialized_completions) == 1:
        campus = campus_segments[0]
        meal = meal_segments[0]
        completion = materialized_completions[0]
        semantic_sequence = [campus, meal, completion]
        sequence_indices = [
            route_sequence.index(segment) if segment in route_sequence else -1
            for segment in semantic_sequence
        ]
        start_minutes = [
            _minutes_of_day(
                str(segment.get("startTime") or segment.get("start_time") or "")
            )
            for segment in semantic_sequence
        ]
        end_minutes = [
            _minutes_of_day(
                str(segment.get("endTime") or segment.get("end_time") or "")
            )
            for segment in semantic_sequence
        ]
        order_valid = bool(
            sequence_indices == sorted(sequence_indices)
            and sequence_indices == [0, 1, 2]
            and all(value is not None for value in [*start_minutes, *end_minutes])
            and start_minutes[0] < start_minutes[1] < start_minutes[2]  # type: ignore[operator]
            and 11 * 60 <= start_minutes[1] < 14 * 60  # type: ignore[operator]
            and 14 * 60 <= start_minutes[2] < 18 * 60  # type: ignore[operator]
            and end_minutes[0] <= start_minutes[1]  # type: ignore[operator]
            and end_minutes[1] <= start_minutes[2]  # type: ignore[operator]
            and 45
            <= (
                minutes_between(
                    str(completion.get("startTime") or ""),
                    str(completion.get("endTime") or ""),
                )
                or 0
            )
            <= 90
        )
        if not order_valid:
            errors.append("day2_semantic_route_order_invalid")

        required_pair_identities = [
            (
                2,
                pair_ordinal,
                str(left.get("id") or ""),
                str(right.get("id") or ""),
                str(json_object(left.get("poi")).get("amapId") or "").upper(),
                str(json_object(right.get("poi")).get("amapId") or "").upper(),
            )
            for pair_ordinal, (left, right) in enumerate(
                zip(semantic_sequence, semantic_sequence[1:]), start=1
            )
        ]
        verified_pairs = [
            json_object(item)
            for item in json_list(
                json_object(snapshot.get("simpleOpenRouteAssignment")).get(
                    "verifiedPairs"
                )
            )
        ]
        verified_by_identity = {
            identity: item
            for item in verified_pairs
            if (identity := _route_pair_identity(item)) is not None
        }
        for identity in required_pair_identities:
            route = verified_by_identity.get(identity)
            if route is None:
                errors.append(f"day2_daily_completion_route_pair_missing:{identity[1]}")
                continue
            fingerprint = str(route.get("providerEvidenceFingerprint") or "")
            if (
                route.get("provider") != "amap-webservice"
                or parse_timestamp(route.get("queriedAt")) is None
                or re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None
                or fingerprint != route_provider_evidence_fingerprint(route)
            ):
                errors.append(
                    f"day2_daily_completion_route_provider_evidence_invalid:{identity[1]}"
                )

    for route_error in compact_route_evidence_errors(snapshot):
        errors.append(f"daily_completion_route_evidence:{route_error}")

    errors = sorted(set(errors))
    return {
        "verified": not errors,
        "errors": errors,
        "paceClass": pace_class,
        "dayAnchorTargets": targets,
        "dayAnchorActuals": actuals,
        "day2CompletionSegmentId": str(completion.get("id") or "")
        if completion
        else None,
        "day2CompletionPlanningSlotId": (
            str(completion_metadata.get("planningSlotId") or "") if completion else None
        ),
        "day2VerifiedRoutePairCount": sum(
            1
            for item in json_list(
                json_object(snapshot.get("simpleOpenRouteAssignment")).get(
                    "verifiedPairs"
                )
            )
            if isinstance(item, dict) and int(item.get("dayNumber") or 0) == 2
        ),
    }


def minutes_between(start: str, end: str) -> int | None:
    if not TIME_PATTERN.fullmatch(start) or not TIME_PATTERN.fullmatch(end):
        return None
    start_minutes = int(start[:2]) * 60 + int(start[3:])
    end_minutes = int(end[:2]) * 60 + int(end[3:])
    if end_minutes < start_minutes:
        end_minutes += 24 * 60
    return end_minutes - start_minutes


def poi_core(poi: dict[str, Any]) -> dict[str, Any]:
    return {
        key: poi.get(key)
        for key in (
            "id",
            "amapId",
            "name",
            "type",
            "category",
            "city",
            "district",
            "address",
            "latitude",
            "longitude",
            "source",
            "groundingStatus",
            "intentType",
        )
    }


def segment_contract(
    segment: dict[str, Any], day_number: int, segment_index: int
) -> dict[str, Any]:
    metadata = semantic_metadata(segment)
    start = str(segment.get("startTime") or segment.get("start_time") or "")
    end = str(segment.get("endTime") or segment.get("end_time") or "")
    return {
        "dayNumber": day_number,
        "segmentIndex": segment_index,
        "id": str(segment.get("id") or ""),
        "kind": str(segment.get("kind") or ""),
        "startTime": start,
        "endTime": end,
        "durationMinutes": minutes_between(start, end),
        "transportMode": segment.get("transportMode"),
        "semantic": {
            key: metadata.get(key)
            for key in (
                "completionRequired",
                "dayCompletionRequired",
                "goalId",
                "groundingStatus",
                "intentSlotId",
                "intentType",
                "occurrenceId",
                "planningSlotId",
                "poolId",
                "rawNeed",
                "required",
                "requirementLevel",
                "requiresRouteEdge",
                "routeAnchor",
                "lineageAuthority",
                "sourceGoalId",
                "userExplicit",
            )
        },
        "poi": poi_core(json_object(segment.get("poi"))),
    }


def snapshot_segments(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for day_index, day in enumerate(json_list(snapshot.get("days")), start=1):
        if not isinstance(day, dict):
            continue
        day_number = int(day.get("dayNumber") or day_index)
        for segment_index, segment in enumerate(
            json_list(day.get("segments")), start=1
        ):
            if isinstance(segment, dict):
                result.append(segment_contract(segment, day_number, segment_index))
    return result


def proposal_commit_readiness_evidence(
    snapshot: dict[str, Any],
    *,
    adoption_ready: bool,
) -> dict[str, Any]:
    """Fail closed before adoption without repairing proposal schedule data."""

    base: dict[str, Any] = {
        "schemaVersion": "trip-proposal-commit-readiness-v1",
        "evaluated": adoption_ready is True,
        "verified": True,
        "adoptionReady": adoption_ready is True,
        "materializedSegmentCount": 0,
        "validSegmentCount": 0,
        "invalidSegmentCount": 0,
        "derivedDurationCount": 0,
        "failureCounts": {},
        "failures": [],
    }
    if adoption_ready is not True:
        return base

    failures_by_segment: dict[tuple[int, int], dict[str, Any]] = {}
    intervals_by_day: dict[int, list[dict[str, int]]] = {}
    materialized_count = 0
    derived_duration_count = 0

    def add_failure(
        *,
        day_number: int,
        segment_index: int,
        code: str,
        conflicts_with_segment_index: int | None = None,
    ) -> None:
        key = (day_number, segment_index)
        detail = failures_by_segment.setdefault(
            key,
            {
                "dayNumber": day_number,
                "segmentIndex": segment_index,
                "codes": [],
            },
        )
        if code not in detail["codes"]:
            detail["codes"].append(code)
        if conflicts_with_segment_index is not None:
            detail["conflictsWithSegmentIndex"] = conflicts_with_segment_index

    for day_index, day in enumerate(json_list(snapshot.get("days")), start=1):
        if not isinstance(day, dict):
            continue
        day_number = int(day.get("dayNumber") or day_index)
        for segment_index, segment in enumerate(
            json_list(day.get("segments")), start=1
        ):
            if not isinstance(segment, dict):
                continue
            materialized_count += 1
            start = str(segment.get("startTime") or segment.get("start_time") or "")
            end = str(segment.get("endTime") or segment.get("end_time") or "")
            start_valid = TIME_PATTERN.fullmatch(start) is not None
            end_valid = TIME_PATTERN.fullmatch(end) is not None
            if not start_valid:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="start_time_format_invalid",
                )
            if not end_valid:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="end_time_format_invalid",
                )
            raw_duration = segment.get("durationMinutes")
            raw_duration_supplied = raw_duration is not None
            raw_duration_valid = (
                isinstance(raw_duration, int)
                and not isinstance(raw_duration, bool)
                and raw_duration > 0
            )
            if raw_duration_supplied and not raw_duration_valid:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="duration_minutes_not_positive",
                )
            if not start_valid or not end_valid:
                continue
            start_minutes = int(start[:2]) * 60 + int(start[3:])
            end_minutes = int(end[:2]) * 60 + int(end[3:])
            duration_minutes = end_minutes - start_minutes
            if end_minutes <= start_minutes:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="end_time_not_after_start",
                )
            if duration_minutes <= 0:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="duration_minutes_not_positive",
                )
                continue
            if not raw_duration_supplied:
                derived_duration_count += 1
            elif raw_duration_valid and raw_duration != duration_minutes:
                add_failure(
                    day_number=day_number,
                    segment_index=segment_index,
                    code="duration_minutes_mismatch",
                )
            intervals_by_day.setdefault(day_number, []).append(
                {
                    "segmentIndex": segment_index,
                    "startMinutes": start_minutes,
                    "endMinutes": end_minutes,
                }
            )

    for day_number, intervals in intervals_by_day.items():
        ordered = sorted(
            intervals,
            key=lambda item: (
                item["startMinutes"],
                item["endMinutes"],
                item["segmentIndex"],
            ),
        )
        active: dict[str, int] | None = None
        for interval in ordered:
            if active is not None and interval["startMinutes"] < active["endMinutes"]:
                add_failure(
                    day_number=day_number,
                    segment_index=interval["segmentIndex"],
                    code="same_day_overlap",
                    conflicts_with_segment_index=active["segmentIndex"],
                )
                if interval["endMinutes"] > active["endMinutes"]:
                    active = interval
                continue
            active = interval

    failures = [failures_by_segment[key] for key in sorted(failures_by_segment)]
    failure_counts: dict[str, int] = {}
    for detail in failures:
        detail["codes"] = sorted(detail["codes"])
        for code in detail["codes"]:
            failure_counts[code] = failure_counts.get(code, 0) + 1

    invalid_count = len(failures)
    return {
        **base,
        "verified": invalid_count == 0,
        "materializedSegmentCount": materialized_count,
        "validSegmentCount": materialized_count - invalid_count,
        "invalidSegmentCount": invalid_count,
        "derivedDurationCount": derived_duration_count,
        "failureCounts": dict(sorted(failure_counts.items())),
        "failures": failures,
    }


def record_proposal_commit_readiness_failures(
    *,
    proposal_id: str,
    stage: str,
    evidence: dict[str, Any],
    failures: list[str],
) -> None:
    for detail in json_list(evidence.get("failures")):
        if not isinstance(detail, dict):
            continue
        day_number = int(detail.get("dayNumber") or 0)
        segment_index = int(detail.get("segmentIndex") or 0)
        for code in json_list(detail.get("codes")):
            check(
                False,
                "proposal_commit_readiness:"
                f"{proposal_id}:{stage}:day_{day_number}:"
                f"segment_{segment_index}:{str(code)}",
                failures,
            )


def snapshot_core(snapshot: dict[str, Any]) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    for day_index, day in enumerate(json_list(snapshot.get("days")), start=1):
        if not isinstance(day, dict):
            continue
        day_number = int(day.get("dayNumber") or day_index)
        days.append(
            {
                "dayNumber": day_number,
                "date": day.get("date"),
                "title": day.get("title"),
                "segments": [
                    segment_contract(segment, day_number, segment_index)
                    for segment_index, segment in enumerate(
                        json_list(day.get("segments")), start=1
                    )
                    if isinstance(segment, dict)
                ],
            }
        )
    return {
        "city": snapshot.get("city"),
        "title": snapshot.get("title"),
        "status": snapshot.get("status"),
        "workflowMode": snapshot.get("workflowMode"),
        "routeDecisionContract": json_object(snapshot.get("routeDecisionContract")),
        "days": days,
        "pendingLineage": lineage_fingerprint(pending_slots(snapshot)),
    }


def business_signature(snapshot: dict[str, Any]) -> str:
    segments = [
        {
            "dayNumber": item["dayNumber"],
            "segmentIndex": item["segmentIndex"],
            "kind": item["kind"],
            "startTime": item["startTime"],
            "endTime": item["endTime"],
            "amapId": item["poi"].get("amapId"),
            "intentType": item["semantic"].get("intentType"),
            "planningSlotId": item["semantic"].get("planningSlotId"),
        }
        for item in snapshot_segments(snapshot)
    ]
    return json.dumps(
        {
            "segments": segments,
            "pendingLineage": lineage_fingerprint(pending_slots(snapshot)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def proposal_projection_identity_core(snapshot: dict[str, Any]) -> dict[str, Any]:
    segments = [
        {
            "dayNumber": item["dayNumber"],
            "segmentIndex": item["segmentIndex"],
            "id": item["id"],
            "kind": item["kind"],
            "startTime": item["startTime"],
            "endTime": item["endTime"],
            "amapId": str(item["poi"].get("amapId") or ""),
            "intentType": str(item["semantic"].get("intentType") or ""),
            "planningSlotId": str(item["semantic"].get("planningSlotId") or ""),
        }
        for item in snapshot_segments(snapshot)
    ]
    pending = sorted(
        (
            {
                "id": str(slot.get("id") or ""),
                "goalId": str(slot.get("goalId") or ""),
                "occurrenceId": str(slot.get("occurrenceId") or ""),
                "planningSlotId": str(slot.get("planningSlotId") or ""),
                "dayNumber": int(slot.get("dayNumber") or 0),
                "startTime": str(slot.get("startTime") or ""),
                "endTime": str(slot.get("endTime") or ""),
                "intentType": str(slot.get("intentType") or ""),
                "reasonCode": str(slot.get("reasonCode") or ""),
            }
            for slot in pending_slots(snapshot)
        ),
        key=lambda item: (
            item["goalId"],
            item["occurrenceId"],
            item["planningSlotId"],
        ),
    )
    return {"segments": segments, "pending": pending}


def response_steps(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in json_list(response.get("planningSteps"))
        if isinstance(step, dict)
    ]


def external_call_counts(response: dict[str, Any]) -> dict[str, int]:
    steps = response_steps(response)
    text_steps = [
        step
        for step in steps
        if step.get("type") == "simple_open_tool_call"
        and step.get("providerName") == "amap-place-search"
    ]
    route_event_count = sum(
        1
        for step in steps
        if step.get("type") == "proposal_route_leg_started"
        and json_object(step.get("metadata")).get("cached") is not True
    )
    route_deltas: list[int] = [int(response.get("routeWriteDelta") or 0)]
    for step in steps:
        metadata = json_object(step.get("metadata"))
        preview = json_object(metadata.get("resultPreview"))
        route_deltas.extend(
            [
                int(metadata.get("routeWriteDelta") or 0),
                int(preview.get("routeWriteDelta") or 0),
            ]
        )
    route_count = max([route_event_count, *route_deltas])
    text_count = len(text_steps)
    return {
        "amapPlaceText": text_count,
        "amapRoute": route_count,
        "amapExternal": text_count + route_count,
    }


def route_alternative_budget_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct per-day alternative Provider calls from the persisted audit."""

    audit = json_object(snapshot.get("simpleOpenRouteAssignment"))
    attempts = [
        item
        for item in json_list(audit.get("topologyCandidateAttempts"))
        if isinstance(item, dict)
    ]
    errors: list[str] = []
    prior_provider_count = 0
    alternative_by_day: dict[int, int] = {}
    observed_provider_counter = False
    for index, attempt in enumerate(attempts):
        try:
            candidate_rank = int(attempt.get("candidateRank"))
            day_number = int(attempt.get("dayNumber"))
        except (TypeError, ValueError):
            errors.append(f"route_attempt_identity_invalid:{index}")
            continue
        if candidate_rank <= 0 or day_number <= 0:
            errors.append(f"route_attempt_identity_invalid:{index}")
            continue
        raw_count = attempt.get("providerAttemptCountAfter")
        if raw_count is None:
            status = str(attempt.get("status") or "")
            day_count = attempt.get("dayAlternativeProviderAttemptCount")
            day_limit = attempt.get("dayAlternativeProviderAttemptLimit")
            if day_count is not None or day_limit is not None:
                try:
                    parsed_count = int(day_count)
                    parsed_limit = int(day_limit)
                except (TypeError, ValueError):
                    errors.append(f"day_alternative_budget_marker_invalid:{index}")
                else:
                    if parsed_limit != 2 or parsed_count < 0 or parsed_count > parsed_limit:
                        errors.append(
                            f"day_alternative_budget_marker_exceeded:{index}:{parsed_count}:{parsed_limit}"
                        )
            elif status != "not_attempted_route_budget_insufficient":
                errors.append(f"route_provider_counter_missing:{index}:{status}")
            continue
        try:
            provider_count = int(raw_count)
        except (TypeError, ValueError):
            errors.append(f"route_provider_counter_invalid:{index}")
            continue
        observed_provider_counter = True
        if provider_count < prior_provider_count:
            errors.append(
                f"route_provider_counter_regressed:{index}:{prior_provider_count}:{provider_count}"
            )
            prior_provider_count = provider_count
            continue
        delta = provider_count - prior_provider_count
        prior_provider_count = provider_count
        if candidate_rank > 1 and delta:
            alternative_by_day[day_number] = alternative_by_day.get(day_number, 0) + delta

    try:
        persisted_total = int(audit.get("routeProviderAttemptCount") or 0)
    except (TypeError, ValueError):
        persisted_total = -1
        errors.append("route_provider_attempt_total_invalid")
    if not 0 <= persisted_total <= 8:
        errors.append(f"route_provider_attempt_budget_exceeded:{persisted_total}")
    if persisted_total > 0 and not observed_provider_counter:
        errors.append("route_provider_attempt_audit_missing")
    if observed_provider_counter and prior_provider_count != persisted_total:
        errors.append(
            f"route_provider_attempt_total_mismatch:{prior_provider_count}:{persisted_total}"
        )
    for day_number, count in sorted(alternative_by_day.items()):
        if count > 2:
            errors.append(f"day_alternative_route_budget_exceeded:{day_number}:{count}")
    return {
        "verified": not errors,
        "errors": sorted(set(errors)),
        "routeProviderAttemptCount": persisted_total,
        "alternativeProviderAttemptCountByDay": {
            str(day_number): count for day_number, count in sorted(alternative_by_day.items())
        },
    }


def _request_source_material_ids(request: dict[str, Any]) -> list[str]:
    direct = json_list(request.get("sourceMaterialIds"))
    nested = json_list(json_object(request.get("context")).get("sourceMaterialIds"))
    return [str(item) for item in [*direct, *nested] if str(item)]


def social_link_database_evidence(
    connection: sqlite3.Connection,
    journey: dict[str, Any],
) -> dict[str, Any]:
    artifact = json_object(journey.get("socialLinkEvidence"))
    public = json_object(artifact.get("public"))
    restricted = json_object(artifact.get("restricted"))
    main_session_id = str(journey.get("sessionId") or "")
    errors: list[str] = []
    session_evidence: dict[str, Any] = {}

    public_session_id = str(public.get("sessionId") or "")
    restricted_session_id = str(restricted.get("sessionId") or "")
    if (
        not public_session_id
        or not restricted_session_id
        or len({main_session_id, public_session_id, restricted_session_id}) != 3
    ):
        errors.append("social_link_session_identity_invalid")

    for label, expected_status, item in (
        ("public", "succeeded", public),
        ("restricted", "needs_user_material", restricted),
    ):
        material_id = str(item.get("sourceMaterialId") or "")
        session_id = str(item.get("sessionId") or "")
        material = (
            connection.execute(
                "SELECT id, kind, raw_text, link_url, metadata_json FROM source_materials WHERE id = ?",
                (material_id,),
            ).fetchone()
            if material_id
            else None
        )
        session = (
            connection.execute(
                "SELECT id, active_plan_id, active_version_id FROM conversation_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session_id
            else None
        )
        if material is None:
            errors.append(f"social_link_material_missing:{label}")
            continue
        if session is None:
            errors.append(f"social_link_session_missing:{label}")
            continue
        metadata = json_object(material["metadata_json"])
        raw_text = str(material["raw_text"] or "")
        content_fingerprint = str(metadata.get("contentFingerprint") or "")
        if material["kind"] != "social_link":
            errors.append(f"social_link_material_kind_invalid:{label}")
        if str(material["link_url"] or "") != str(item.get("url") or ""):
            errors.append(f"social_link_url_binding_invalid:{label}")
        if metadata.get("fetchStatus") != expected_status:
            errors.append(f"social_link_fetch_status_invalid:{label}")
        if metadata.get("provider") != "xiaohongshu_public_html":
            errors.append(f"social_link_provider_invalid:{label}")

        user_turn_rows = connection.execute(
            "SELECT id, agent_request_json FROM conversation_turns "
            "WHERE session_id = ? AND role = 'user' ORDER BY turn_index",
            (session_id,),
        ).fetchall()
        bound_requests = [
            (str(row["id"]), json_object(row["agent_request_json"]))
            for row in user_turn_rows
            if material_id in _request_source_material_ids(json_object(row["agent_request_json"]))
        ]
        bound_turn_ids = [turn_id for turn_id, _request in bound_requests]
        if len(bound_turn_ids) != 1:
            errors.append(f"social_link_request_binding_invalid:{label}:{len(bound_turn_ids)}")

        version_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )
        portfolio_rows = connection.execute(
            "SELECT id FROM agent_plan_portfolios WHERE session_id = ?", (session_id,)
        ).fetchall()
        proposal_count = 0
        if portfolio_rows:
            placeholders = ",".join("?" for _ in portfolio_rows)
            proposal_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id IN ({placeholders})",
                    tuple(str(row["id"]) for row in portfolio_rows),
                ).fetchone()[0]
            )

        if label == "public":
            if len(raw_text) <= 40:
                errors.append("social_link_public_text_missing")
            if re.fullmatch(r"[0-9a-f]{64}", content_fingerprint) is None:
                errors.append("social_link_public_fingerprint_invalid")
            if not (str(session["active_version_id"] or "") or proposal_count > 0):
                errors.append("social_link_public_not_converted")
            if str(item.get("fetchStatus") or "") != expected_status:
                errors.append("social_link_public_artifact_status_invalid")
            bound_request = bound_requests[0][1] if len(bound_requests) == 1 else {}
            request_contract = json_object(bound_request.get("requestIntentContract"))
            source_hints = [
                hint
                for hint in json_list(request_contract.get("sourceMaterialGoalHints"))
                if isinstance(hint, dict)
            ]
            if (
                not source_hints
                or any(str(hint.get("sourceMaterialId") or "") != material_id for hint in source_hints)
            ):
                errors.append("social_link_public_goal_hint_lineage_invalid")
            expected_evidence_fingerprint = hashlib.sha256(
                json.dumps(
                    [{"id": material_id, "contentFingerprint": content_fingerprint}],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                str(request_contract.get("sourceMaterialEvidenceFingerprint") or "")
                != expected_evidence_fingerprint
            ):
                errors.append("social_link_public_evidence_fingerprint_mismatch")

            public_snapshots: list[dict[str, Any]] = []
            active_version_id = str(session["active_version_id"] or "")
            if active_version_id:
                version_row = connection.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
                    (active_version_id, session_id),
                ).fetchone()
                if version_row is not None:
                    public_snapshots.append(json_object(version_row["snapshot_json"]))
            if portfolio_rows:
                placeholders = ",".join("?" for _ in portfolio_rows)
                public_snapshots.extend(
                    json_object(row["snapshot_json"])
                    for row in connection.execute(
                        f"SELECT snapshot_json FROM agent_plan_proposals WHERE portfolio_id IN ({placeholders})",
                        tuple(str(row["id"]) for row in portfolio_rows),
                    ).fetchall()
                )
            public_segments = [
                segment
                for snapshot in public_snapshots
                for segment in snapshot_segments(snapshot)
            ]
            if not public_segments or any(
                AMAP_ID_PATTERN.fullmatch(str(segment["poi"].get("amapId") or "")) is None
                for segment in public_segments
            ):
                errors.append("social_link_public_amap_grounding_invalid")
        else:
            if raw_text:
                errors.append("social_link_restricted_text_should_be_empty")
            if content_fingerprint:
                errors.append("social_link_restricted_fingerprint_should_be_empty")
            if not str(metadata.get("failureReason") or ""):
                errors.append("social_link_restricted_failure_reason_missing")
            patch_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
            )
            mutation_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM timeline_mutation_transactions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            portfolio_count = len(portfolio_rows)
            if version_count or patch_count or mutation_count or portfolio_count or proposal_count:
                errors.append(
                    "social_link_restricted_formal_write:"
                    f"versions={version_count}:patches={patch_count}:mutations={mutation_count}:"
                    f"portfolios={portfolio_count}:proposals={proposal_count}"
                )

        session_evidence[label] = {
            "sessionId": session_id,
            "sourceMaterialId": material_id,
            "fetchStatus": str(metadata.get("fetchStatus") or ""),
            "rawTextLength": len(raw_text),
            "contentFingerprintPresent": bool(content_fingerprint),
            "requestTurnIds": bound_turn_ids,
            "activeVersionId": str(session["active_version_id"] or ""),
            "versionCount": version_count,
            "proposalCount": proposal_count,
            **(
                {
                    "patchCount": patch_count,
                    "mutationCount": mutation_count,
                    "portfolioCount": portfolio_count,
                }
                if label == "restricted"
                else {}
            ),
        }

    return {
        "verified": not errors,
        "errors": sorted(set(errors)),
        "sessions": session_evidence,
    }


def normalize_endpoint_host(value: Any) -> str:
    """Return a lower-case host[:port] without accepting URL-shaped input."""

    raw = str(value or "").strip()
    if (
        not raw
        or "://" in raw
        or any(character in raw for character in "/?#@")
        or any(character.isspace() for character in raw)
    ):
        return ""
    try:
        parsed = urlsplit(f"//{raw}")
        hostname = str(parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return ""
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port is not None else hostname


def deepseek_endpoint_preflight(
    endpoint_host: Any,
) -> tuple[str, dict[str, Any] | None]:
    normalized_endpoint = normalize_endpoint_host(endpoint_host)
    reason_code = ""
    if not normalized_endpoint:
        reason_code = "deepseek_endpoint_host_invalid"
    elif normalized_endpoint != TRUSTED_DEEPSEEK_ENDPOINT_HOST:
        reason_code = "deepseek_endpoint_host_not_allowed"
    if not reason_code:
        return normalized_endpoint, None
    return (
        normalized_endpoint,
        {
            "passed": False,
            "failures": [reason_code],
            "deepSeekEndpointHost": normalized_endpoint,
            "allowedDeepSeekEndpointHost": TRUSTED_DEEPSEEK_ENDPOINT_HOST,
        },
    )


def _successful_controller_attempt(item: dict[str, Any]) -> bool:
    return bool(
        item.get("providerInvoked") is True
        and item.get("captureState") == "completed"
        and item.get("responseHeadersReceived") is True
        and item.get("httpStatus") == 200
        and _positive_int(item.get("responseBytes")) > 0
    )


def deepseek_decision_evidence(
    response: dict[str, Any],
    *,
    endpoint_host: Any,
) -> dict[str, Any]:
    """Bind one accepted controller decision to its own successful attempts.

    The endpoint is a launcher-owned invariant because the current planning-run
    telemetry does not persist the request URL.  Only the normalized host is
    carried into the evidence artifact.
    """

    normalized_endpoint = normalize_endpoint_host(endpoint_host)
    if not normalized_endpoint:
        return {
            "verified": False,
            "reasonCode": "deepseek_endpoint_host_invalid",
            "endpointHost": normalized_endpoint,
            "attempts": [],
        }
    if normalized_endpoint != TRUSTED_DEEPSEEK_ENDPOINT_HOST:
        return {
            "verified": False,
            "reasonCode": "deepseek_endpoint_host_not_allowed",
            "endpointHost": normalized_endpoint,
            "attempts": [],
        }

    saw_bound_attempt = False
    saw_unaccepted_bound_attempt = False
    for container in walk_json(response):
        performance = container.get("controllerPerformance")
        raw_decisions = container.get("providerRawDecisions")
        if not isinstance(performance, list) or not isinstance(raw_decisions, list):
            continue
        successful_attempts = [
            (index, item)
            for index, item in enumerate(performance)
            if isinstance(item, dict) and _successful_controller_attempt(item)
        ]
        valid_raw_decisions = [
            item
            for item in raw_decisions
            if isinstance(item, dict)
            and str(item.get("schemaVersion") or "").strip()
            and str(item.get("primaryAction") or "").strip()
        ]
        if (
            not successful_attempts
            or len(successful_attempts) != len(raw_decisions)
            or len(valid_raw_decisions) != len(raw_decisions)
        ):
            continue
        saw_bound_attempt = True
        accepted_controller = bool(
            container.get("source") == "controller"
            and container.get("decisionPath") in {"full", "lite"}
            and container.get("controllerSucceeded") is True
            and container.get("accepted") is True
        )
        if not accepted_controller:
            saw_unaccepted_bound_attempt = True
            continue
        decision_id = str(container.get("decisionId") or "")
        attempts = []
        for raw_index, ((performance_index, attempt), raw) in enumerate(
            zip(successful_attempts, valid_raw_decisions)
        ):
            attempts.append(
                {
                    "decisionId": decision_id,
                    "performanceAttemptIndex": performance_index,
                    "rawDecisionIndex": raw_index,
                    "callKind": str(attempt.get("callKind") or ""),
                    "captureState": str(attempt.get("captureState") or ""),
                    "httpStatus": attempt.get("httpStatus"),
                    "rawDecisionSchemaVersion": str(raw.get("schemaVersion") or ""),
                    "rawPrimaryAction": str(raw.get("primaryAction") or ""),
                    "endpointHost": normalized_endpoint,
                }
            )
        return {
            "verified": True,
            "reasonCode": "deepseek_accepted_controller_decision_verified",
            "endpointHost": normalized_endpoint,
            "attempts": attempts,
        }

    reason = "deepseek_attempt_decision_binding_missing"
    if saw_bound_attempt and saw_unaccepted_bound_attempt:
        reason = "deepseek_accepted_controller_decision_missing"
    return {
        "verified": False,
        "reasonCode": reason,
        "endpointHost": normalized_endpoint,
        "attempts": [],
    }


def _materialized_amap_lineage(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for segment in snapshot_segments(snapshot):
        amap_id = str(segment["poi"].get("amapId") or "").upper()
        source = str(segment["poi"].get("source") or "")
        slot_id = str(segment["semantic"].get("planningSlotId") or "")
        if source == "amap-place-search" and AMAP_ID_PATTERN.fullmatch(amap_id):
            records.append({"amapId": amap_id, "planningSlotId": slot_id})
    return records


def real_amap_text_evidence(
    response: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Require a non-cache successful search selected into this proposal."""

    materialized = _materialized_amap_lineage(snapshot)
    materialized_pairs = {
        (item["amapId"], item["planningSlotId"]) for item in materialized
    }
    materialized_ids = {item["amapId"] for item in materialized}
    bound_ids: set[str] = set()
    errors: list[str] = []
    events: list[dict[str, Any]] = []
    for step_index, step in enumerate(response_steps(response)):
        if (
            step.get("type") != "simple_open_tool_call"
            or step.get("providerName") != "amap-place-search"
        ):
            continue
        metadata = json_object(step.get("metadata"))
        selected_id = str(metadata.get("selectedAmapId") or "").upper()
        slot_id = str(metadata.get("slotKey") or "")
        query_fingerprint = str(metadata.get("queryFingerprint") or "")
        strict_success = bool(
            step.get("status") == "completed"
            and metadata.get("providerOutcome") == "success"
            and metadata.get("cacheHit") is False
            and _positive_int(metadata.get("resultCount")) > 0
            and re.fullmatch(r"[0-9a-fA-F]{16}", query_fingerprint)
            and AMAP_ID_PATTERN.fullmatch(selected_id)
            and slot_id
        )
        events.append(
            {
                "stepIndex": step_index,
                "status": str(step.get("status") or ""),
                "providerOutcome": str(metadata.get("providerOutcome") or ""),
                "cacheHit": metadata.get("cacheHit"),
                "resultCount": metadata.get("resultCount"),
                "queryFingerprint": query_fingerprint,
                "selectedAmapId": selected_id,
                "slotKey": slot_id,
                "strictSuccess": strict_success,
            }
        )
        if not strict_success:
            continue
        if selected_id not in materialized_ids:
            errors.append(f"amap_search_identity_not_materialized:{selected_id}")
            continue
        if (selected_id, slot_id) not in materialized_pairs:
            errors.append(f"amap_search_slot_not_materialized:{selected_id}:{slot_id}")
            continue
        bound_ids.add(selected_id)

    for amap_id in sorted(materialized_ids - bound_ids):
        errors.append(f"amap_materialized_identity_without_live_search:{amap_id}")
    return {
        "verified": not errors and bool(materialized_ids) and bool(bound_ids),
        "errors": sorted(set(errors)),
        "materializedAmapIds": sorted(materialized_ids),
        "boundAmapIds": sorted(bound_ids),
        "events": events,
    }


def current_direction_adjacent_search_evidence(
    response: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Bind every route-local meal/park search to this proposal's day seed."""

    errors: list[str] = []
    mismatch_occurrences = sum(
        1
        for item in walk_json(response)
        if str(item.get("reasonCode") or "")
        == "simple_direction_claimed_adjacent_scope_mismatch"
    )
    if mismatch_occurrences:
        errors.append(f"claimed_adjacent_scope_mismatch_present:{mismatch_occurrences}")

    segments = snapshot_segments(snapshot)
    campus_ids_by_day: dict[int, list[str]] = {}
    for segment in segments:
        if str(segment["semantic"].get("intentType") or "") != "campus_visit":
            continue
        amap_id = str(segment["poi"].get("amapId") or "").strip().upper()
        if (
            segment["semantic"].get("groundingStatus") == "verified_amap"
            and segment["poi"].get("source") == "amap-place-search"
            and AMAP_ID_PATTERN.fullmatch(amap_id)
        ):
            campus_ids_by_day.setdefault(int(segment["dayNumber"]), []).append(amap_id)

    targets = [
        segment
        for segment in segments
        if (
            str(segment["semantic"].get("intentType") or "")
            in {"meal", "local_food", "food", "park"}
            or segment["semantic"].get("dayCompletionRequired") is True
            or str(segment["semantic"].get("lineageAuthority") or "")
            == "simple_open_daily_completion_policy"
        )
        and segment["semantic"].get("groundingStatus") == "verified_amap"
        and segment["poi"].get("source") == "amap-place-search"
        and AMAP_ID_PATTERN.fullmatch(
            str(segment["poi"].get("amapId") or "").strip().upper()
        )
    ]
    tool_steps = [
        step
        for step in response_steps(response)
        if step.get("type") == "simple_open_tool_call"
        and step.get("providerName") == "amap-place-search"
    ]
    bindings: list[dict[str, Any]] = []
    for target in targets:
        day_number = int(target["dayNumber"])
        slot_id = str(target["semantic"].get("planningSlotId") or "")
        selected_id = str(target["poi"].get("amapId") or "").strip().upper()
        campus_ids = sorted(set(campus_ids_by_day.get(day_number, [])))
        if len(campus_ids) != 1:
            errors.append(
                f"adjacent_search_current_day_campus_count_invalid:{day_number}:{len(campus_ids)}"
            )
            continue
        matching_steps = []
        for step in tool_steps:
            metadata = json_object(step.get("metadata"))
            if (
                str(metadata.get("slotKey") or "") == slot_id
                and str(metadata.get("selectedAmapId") or "").strip().upper()
                == selected_id
                and step.get("status") == "completed"
                and metadata.get("providerOutcome") == "success"
                and metadata.get("cacheHit") is False
                and _positive_int(metadata.get("resultCount")) > 0
            ):
                matching_steps.append((step, metadata))
        if len(matching_steps) != 1:
            errors.append(
                f"adjacent_search_live_call_count_invalid:{slot_id}:{len(matching_steps)}"
            )
            continue
        _step, metadata = matching_steps[0]
        expected_seed = campus_ids[0]
        actual_seed = str(metadata.get("daySeedAmapId") or "").strip().upper()
        scope_fingerprint = str(metadata.get("queryScopeFingerprint") or "")
        anchor_amap_id = str(metadata.get("anchorAmapId") or "").strip().upper()
        radius = _positive_int(metadata.get("radiusMeters"))
        if metadata.get("searchScope") != "nearby_low_detour":
            errors.append(f"adjacent_search_scope_invalid:{slot_id}")
        if actual_seed != expected_seed:
            errors.append(
                f"adjacent_search_day_seed_mismatch:{slot_id}:{expected_seed}:{actual_seed or 'missing'}"
            )
        if AMAP_ID_PATTERN.fullmatch(anchor_amap_id) is None:
            errors.append(f"adjacent_search_anchor_amap_id_invalid:{slot_id}")
        if re.fullmatch(r"[0-9a-fA-F]{64}", scope_fingerprint) is None:
            errors.append(f"adjacent_search_scope_fingerprint_invalid:{slot_id}")
        if not 0 < radius <= 5000:
            errors.append(f"adjacent_search_radius_invalid:{slot_id}:{radius}")
        bindings.append(
            {
                "dayNumber": day_number,
                "planningSlotId": slot_id,
                "selectedAmapId": selected_id,
                "currentCampusAmapId": expected_seed,
                "daySeedAmapId": actual_seed,
                "anchorAmapId": anchor_amap_id,
                "queryScopeFingerprint": scope_fingerprint,
                "providerCalled": True,
            }
        )

    if not targets:
        errors.append("adjacent_search_materialized_targets_missing")
    errors = sorted(set(errors))
    return {
        "verified": not errors,
        "errors": errors,
        "mismatchReasonCodeCount": mismatch_occurrences,
        "targetCount": len(targets),
        "bindings": bindings,
    }


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def adoption_execution_binding_errors(
    execution: Any,
    proposal: Any,
    turns_by_id: dict[str, dict[str, Any]],
    *,
    session_id: str,
    expected_version_id: str,
) -> list[str]:
    execution_id = str(_row_value(execution, "id") or "missing")
    choice_id = str(_row_value(execution, "choice_id") or "")
    proposal_id = str(_row_value(proposal, "id") or "")
    proposal_choice_id = str(_row_value(proposal, "choice_id") or "")
    errors: list[str] = []
    if str(_row_value(execution, "session_id") or "") != session_id:
        errors.append(f"adoption_session_mismatch:{execution_id}")
    if choice_id != proposal_choice_id or not choice_id:
        errors.append(f"adoption_choice_not_proposal_choice:{execution_id}")
    if str(_row_value(execution, "result_version_id") or "") != expected_version_id:
        errors.append(f"adoption_result_version_mismatch:{execution_id}")

    source_turn_id = str(_row_value(execution, "source_turn_id") or "")
    source_turn = turns_by_id.get(source_turn_id)
    source_options = (
        json_list(json_object(source_turn.get("response")).get("choiceOptions"))
        if isinstance(source_turn, dict)
        else []
    )
    source_choice_found = any(
        isinstance(option, dict)
        and str(option.get("id") or option.get("choiceId") or "") == choice_id
        and option.get("action") == "select_plan_proposal"
        and str(option.get("proposalId") or "") == proposal_id
        for option in source_options
    )
    if (
        not isinstance(source_turn, dict)
        or source_turn.get("role") != "assistant"
        or not source_choice_found
    ):
        errors.append(f"adoption_source_choice_missing:{execution_id}")

    request_turn = turns_by_id.get(str(_row_value(execution, "request_turn_id") or ""))
    if not isinstance(request_turn, dict) or request_turn.get("role") != "user":
        errors.append(f"adoption_request_turn_invalid:{execution_id}")
    execution_turn = turns_by_id.get(
        str(_row_value(execution, "execution_turn_id") or "")
    )
    if (
        not isinstance(execution_turn, dict)
        or execution_turn.get("role") != "assistant"
    ):
        errors.append(f"adoption_execution_turn_invalid:{execution_id}")
    return sorted(set(errors))


def planning_external_evidence(response: dict[str, Any]) -> list[str]:
    """Reject POI/route work before clarification, while allowing Controller calls."""

    evidence: list[str] = []
    for step in response_steps(response):
        step_type = str(step.get("type") or "")
        provider = str(step.get("providerName") or "")
        if (
            step_type == "simple_open_tool_call"
            or step_type == "proposal_route_leg_started"
            or provider
            in {
                "amap-place-search",
                "route_service",
            }
        ):
            evidence.append(f"{step_type}:{provider}")
    return sorted(set(evidence))


def comparison_projection(response: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    for projection in json_list(response.get("comparisonProjections")):
        if (
            isinstance(projection, dict)
            and str(projection.get("proposalId") or projection.get("id") or "")
            == proposal_id
        ):
            return projection
    return {}


def request_intent_contract(response: dict[str, Any]) -> dict[str, Any]:
    direct = json_object(response.get("requestIntentContract"))
    if direct:
        return direct
    pipeline = json_object(response.get("pipelineContext"))
    return json_object(pipeline.get("requestIntentContract"))


def turn_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "turnIndex": int(row["turn_index"]),
        "role": str(row["role"]),
        "status": str(row["status"] or ""),
        "parentTurnId": row["parent_turn_id"],
        "itineraryVersionId": row["itinerary_version_id"],
        "content": str(row["content"] or ""),
        "request": json_object(row["agent_request_json"]),
        "response": json_object(row["agent_response_json"]),
        "createdAt": row["created_at"],
    }


def _continuation_attempt(
    portfolio_summary: dict[str, Any], execution_id: str
) -> tuple[dict[str, Any], str]:
    for key, kind in (
        ("simpleDirectionFrontierAttempts", "frontier"),
        ("simpleDirectionCompatibilityAttempts", "compatibility"),
    ):
        raw = portfolio_summary.get(key)
        if isinstance(raw, dict):
            direct = raw.get(execution_id)
            if isinstance(direct, dict):
                return direct, kind
            values = raw.values()
        elif isinstance(raw, list):
            values = raw
        else:
            continue
        for value in values:
            if (
                isinstance(value, dict)
                and str(
                    value.get("executionId") or value.get("frontierExecutionId") or ""
                )
                == execution_id
            ):
                return value, kind
    return {}, ""


def _continuation_choices(turn: dict[str, Any] | None) -> list[dict[str, Any]]:
    response = json_object(turn.get("response")) if isinstance(turn, dict) else {}
    return [
        option
        for option in json_list(response.get("choiceOptions"))
        if isinstance(option, dict)
        and option.get("action") == "continue_plan_expansion"
    ]


def continuation_execution_evidence(
    *,
    browser_evidence: Any,
    execution_rows: Iterable[Any],
    turns_by_id: dict[str, dict[str, Any]],
    portfolio_summary: dict[str, Any],
) -> dict[str, Any]:
    browser_items = [
        item for item in json_list(browser_evidence) if isinstance(item, dict)
    ]
    rows = list(execution_rows)
    errors: list[str] = []
    evidence_rows: list[dict[str, Any]] = []
    terminal_no_progress_count = 0
    if not browser_items:
        errors.append("continuation_browser_evidence_missing")
    if len(browser_items) != len(rows):
        errors.append("continuation_execution_count_mismatch")

    browser_by_choice: dict[str, dict[str, Any]] = {}
    for item in browser_items:
        choice_id = str(item.get("choiceId") or "")
        if not choice_id:
            errors.append("continuation_browser_choice_id_missing")
        elif choice_id in browser_by_choice:
            errors.append(f"continuation_browser_choice_duplicate:{choice_id}")
        else:
            browser_by_choice[choice_id] = item

    seen_execution_ids: set[str] = set()
    seen_execution_choices: set[str] = set()
    for row in rows:
        execution_id = str(_row_value(row, "id") or "")
        row_session_id = str(_row_value(row, "session_id") or "")
        choice_id = str(_row_value(row, "choice_id") or "")
        source_turn_id = str(_row_value(row, "source_turn_id") or "")
        source_user_turn_id = str(_row_value(row, "source_user_turn_id") or "")
        request_turn_id = str(_row_value(row, "request_turn_id") or "")
        result_turn_id = str(_row_value(row, "execution_turn_id") or "")
        browser = browser_by_choice.get(choice_id, {})
        outcome = json_object(_row_value(row, "outcome_json"))
        nested_completion = json_object(outcome.get("completionEvidence"))
        flat_direct_completion = bool(
            outcome.get("passed") is True
            and outcome.get("reason") == "simple_direction_execution_evidence_verified"
        )
        flat_legacy_completion = bool(
            outcome.get("passed") is True
            and outcome.get("reconciledFrom") == "failed_retryable/no_material_progress"
            and outcome.get("boundedAttemptConsumed") is True
            and outcome.get("reason")
            in {
                "new_simple_direction_proposal",
                "simple_direction_frontier_advanced_without_proposal",
            }
        )
        completion = (
            nested_completion
            if nested_completion
            else (outcome if flat_direct_completion or flat_legacy_completion else {})
        )
        attempt, attempt_bucket = _continuation_attempt(portfolio_summary, execution_id)
        source_turn = turns_by_id.get(source_turn_id)
        request_turn = turns_by_id.get(request_turn_id)
        result_turn = turns_by_id.get(result_turn_id)
        result_response = (
            json_object(result_turn.get("response"))
            if isinstance(result_turn, dict)
            else {}
        )
        source_choices = _continuation_choices(source_turn)
        source_choice = next(
            (
                item
                for item in source_choices
                if str(item.get("choiceId") or item.get("id") or "") == choice_id
            ),
            {},
        )
        result_choices = _continuation_choices(result_turn)

        def add(code: str) -> None:
            errors.append(f"{code}:{execution_id or choice_id or 'unknown'}")

        if not execution_id or execution_id in seen_execution_ids:
            add("continuation_execution_id_invalid")
        seen_execution_ids.add(execution_id)
        if not choice_id or choice_id in seen_execution_choices:
            add("continuation_execution_choice_invalid")
        seen_execution_choices.add(choice_id)
        if not browser:
            add("continuation_browser_choice_unbound")
        if (
            _row_value(row, "action") != "continue_plan_expansion"
            or _row_value(row, "status") != "succeeded"
            or str(_row_value(row, "result_version_id") or "")
        ):
            add("continuation_execution_contract_invalid")
        if not isinstance(source_turn, dict) or not source_choice:
            add("continuation_source_capability_missing")
        if not isinstance(request_turn, dict) or request_turn.get("role") != "user":
            add("continuation_request_turn_invalid")
        if not isinstance(result_turn, dict) or result_turn.get("role") != "assistant":
            add("continuation_result_turn_invalid")

        deltas = {
            key: int(outcome.get(key) or 0)
            for key in ("versionDelta", "patchDelta", "routeWriteDelta")
        }
        completion_deltas = {
            key: int(completion.get(key) or 0)
            for key in ("versionDelta", "patchDelta", "routeWriteDelta")
        }
        if (
            outcome.get("zeroWrite") is not True
            or (
                bool(nested_completion)
                and outcome.get("boundedAttemptConsumed") is not True
            )
            or any(deltas.values())
            or completion.get("zeroWrite") is not True
            or any(completion_deltas.values())
        ):
            add("continuation_zero_write_contract_invalid")
        if completion.get("passed") is not True:
            add("continuation_completion_evidence_failed")
        if (
            not flat_legacy_completion
            and str(completion.get("reason") or "")
            != "simple_direction_execution_evidence_verified"
            or str(completion.get("sessionId") or "") != row_session_id
        ):
            add("continuation_completion_scope_invalid")

        identity_values = {
            "choiceId": choice_id,
            "sourceAssistantTurnId": source_turn_id,
            "sourceUserTurnId": source_user_turn_id,
            "requestTurnId": request_turn_id,
            "assistantTurnId": result_turn_id,
            "planningSelectionRootTurnId": str(
                browser.get("planningSelectionRootTurnId") or ""
            ),
            "rootPortfolioId": str(browser.get("rootPortfolioId") or ""),
            "requestContractFingerprint": str(
                browser.get("requestContractFingerprint") or ""
            ),
        }
        for key, expected in identity_values.items():
            if key == "sourceUserTurnId":
                continue
            if str(completion.get(key) or "") != expected:
                add(f"continuation_completion_{key}_mismatch")
        if str(completion.get("frontierExecutionId") or "") != execution_id:
            add("continuation_completion_execution_mismatch")
        attempt_kind = str(completion.get("attemptKind") or "")
        if attempt_kind not in {"frontier", "compatibility"}:
            add("continuation_attempt_kind_invalid")
        elif attempt_kind != attempt_bucket:
            add("continuation_attempt_kind_bucket_mismatch")
        attempt_fingerprint = str(completion.get("frontierAttemptFingerprint") or "")
        if re.fullmatch(r"[0-9a-f]{64}", attempt_fingerprint) is None:
            add("continuation_attempt_fingerprint_invalid")

        source_identity = {
            "sourceAssistantTurnId": source_turn_id,
            "sourceUserTurnId": source_user_turn_id,
            "planningSelectionRootTurnId": identity_values[
                "planningSelectionRootTurnId"
            ],
            "rootPortfolioId": identity_values["rootPortfolioId"],
            "requestContractFingerprint": identity_values["requestContractFingerprint"],
        }
        for key, expected in source_identity.items():
            if str(browser.get(key) or "") != expected:
                add(f"continuation_browser_{key}_mismatch")
            if source_choice and str(source_choice.get(key) or "") != expected:
                add(f"continuation_source_{key}_mismatch")
        expected_source_choice_id = (
            "simple_direction_continue_"
            + hashlib.sha256(
                f"{identity_values['rootPortfolioId']}:{source_turn_id}".encode("utf-8")
            ).hexdigest()[:24]
        )
        if source_choice and (
            choice_id != expected_source_choice_id
            or source_choice.get("kind") != "simple_direction_more_plans"
            or source_choice.get("scopeKind") != "comparison"
            or source_choice.get("workflowMode") != "simple_direction_v1"
            or str(source_choice.get("expectedBaseVersionId") or "")
            != str(browser.get("activeVersionBefore") or "")
        ):
            add("continuation_source_choice_shape_invalid")
        view_resolution = json_object(result_response.get("viewResolution"))
        if (
            result_response.get("mode") != "simple_open_direction_proposal"
            or result_response.get("workflowMode") != "simple_direction_v1"
            or str(result_response.get("planningSelectionRootTurnId") or "")
            != identity_values["planningSelectionRootTurnId"]
            or str(result_response.get("rootPortfolioId") or "")
            != identity_values["rootPortfolioId"]
            or result_response.get("frontierAttemptConsumed") is not True
            or any(
                int(result_response.get(key) or 0) != 0
                for key in ("versionDelta", "patchDelta", "routeWriteDelta")
            )
        ):
            add("continuation_result_response_contract_invalid")
        if (
            view_resolution.get("schemaVersion") != "agent-view-resolution-v1"
            or view_resolution.get("resolutionSource")
            != "server_validated_opaque_choice"
            or view_resolution.get("resolvedAction") != "generate_new_direction"
            or str(view_resolution.get("planningSelectionRootTurnId") or "")
            != identity_values["planningSelectionRootTurnId"]
            or str(view_resolution.get("rootPortfolioId") or "")
            != identity_values["rootPortfolioId"]
        ):
            add("continuation_result_view_resolution_invalid")
        if str(browser.get("workflowMode") or "") != "simple_direction_v1":
            add("continuation_browser_workflow_mode_invalid")
        if int(browser.get("streamRequestDelta") or 0) != 1:
            add("continuation_browser_stream_delta_invalid")
        if str(browser.get("preFrontierStatus") or "") != "has_more":
            add("continuation_browser_pre_frontier_invalid")
        if str(browser.get("activeVersionBefore") or "") != str(
            browser.get("activeVersionAfter") or ""
        ):
            add("continuation_browser_active_version_changed")

        if not attempt:
            add("continuation_frontier_attempt_missing")
        else:
            attempt_identity = {
                "executionId": execution_id,
                "requestContractFingerprint": identity_values[
                    "requestContractFingerprint"
                ],
            }
            if attempt_kind == "compatibility":
                attempt_identity.update(
                    {
                        "choiceId": choice_id,
                        "sourceAssistantTurnId": source_turn_id,
                        "requestTurnId": request_turn_id,
                        "resultAssistantTurnId": result_turn_id,
                        "planningSelectionRootTurnId": identity_values[
                            "planningSelectionRootTurnId"
                        ],
                        "rootPortfolioId": identity_values["rootPortfolioId"],
                    }
                )
            for key, expected in attempt_identity.items():
                actual = attempt.get(key)
                if key == "executionId" and actual is None:
                    actual = attempt.get("frontierExecutionId")
                if str(actual or "") != expected:
                    add(f"continuation_attempt_{key}_mismatch")
            persisted_attempt_fingerprint = str(
                attempt.get("attemptFingerprint")
                or json_object(attempt.get("attempt")).get("attemptFingerprint")
                or ""
            )
            if persisted_attempt_fingerprint != attempt_fingerprint:
                add("continuation_attempt_fingerprint_mismatch")

        frontier_status = str(completion.get("frontierStatus") or "")
        proposal_delta = int(completion.get("proposalDelta") or 0)
        if (
            str(outcome.get("frontierStatus") or "") != frontier_status
            or int(outcome.get("proposalDelta") or 0) != proposal_delta
            or (
                attempt_kind == "compatibility"
                and attempt
                and str(attempt.get("frontierStatus") or "") != frontier_status
            )
            or (
                attempt_kind == "compatibility"
                and attempt
                and int(attempt.get("proposalDelta") or 0) != proposal_delta
            )
            or str(result_response.get("frontierExecutionId") or "") != execution_id
            or str(result_response.get("frontierStatus") or "") != frontier_status
            or int(result_response.get("proposalDelta") or 0) != proposal_delta
        ):
            add("continuation_frontier_result_mismatch")

        attempt_status = str(attempt.get("status") or "")
        progress = json_object(attempt.get("progress"))
        if attempt_status == "no_progress":
            terminal_no_progress_count += 1
            reason_code = str(attempt.get("reasonCode") or attempt.get("reason") or "")
            if (
                proposal_delta != 0
                or progress.get("madeProgress") is not False
                or reason_code != "no_progress_no_query_candidate_or_route_delta"
                or frontier_status == "has_more"
            ):
                add("continuation_no_progress_contract_invalid")
            if (
                result_choices
                or bool(browser.get("postCapabilityAvailable"))
                or str(browser.get("postChoiceId") or "")
            ):
                add("continuation_no_progress_choice_reissued")
            if (
                str(browser.get("postFrontierStatus") or "") != frontier_status
                or str(browser.get("postStopReason") or "")
                != f"frontier_terminal:{frontier_status}"
            ):
                add("continuation_no_progress_browser_terminal_mismatch")
        else:
            frontier_material = json_object(attempt.get("attempt"))
            frontier_advanced = bool(attempt.get("resultFrontierFingerprint")) and str(
                attempt.get("resultFrontierFingerprint") or ""
            ) != str(frontier_material.get("frontierFingerprint") or "")
            made_progress = (
                progress.get("madeProgress") is True
                or proposal_delta > 0
                or (attempt_kind == "frontier" and frontier_advanced)
            )
            if attempt_status != "reconciled" or not made_progress:
                add("continuation_progress_contract_invalid")
            if frontier_status == "has_more":
                next_choice_id = str(completion.get("nextChoiceId") or "")
                if len(result_choices) != 1:
                    add("continuation_next_choice_count_invalid")
                next_choice = result_choices[0] if len(result_choices) == 1 else {}
                actual_next_choice_id = str(
                    next_choice.get("choiceId") or next_choice.get("id") or ""
                )
                expected_next_choice_id = (
                    "simple_direction_continue_"
                    + hashlib.sha256(
                        f"{identity_values['rootPortfolioId']}:{result_turn_id}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:24]
                )
                if (
                    not next_choice_id
                    or next_choice_id != expected_next_choice_id
                    or actual_next_choice_id != next_choice_id
                    or next_choice.get("kind") != "simple_direction_more_plans"
                    or next_choice.get("scopeKind") != "comparison"
                    or next_choice.get("workflowMode") != "simple_direction_v1"
                    or str(next_choice.get("sourceAssistantTurnId") or "")
                    != result_turn_id
                    or str(next_choice.get("sourceUserTurnId") or "")
                    != identity_values["sourceUserTurnId"]
                    or str(next_choice.get("planningSelectionRootTurnId") or "")
                    != identity_values["planningSelectionRootTurnId"]
                    or str(next_choice.get("rootPortfolioId") or "")
                    != identity_values["rootPortfolioId"]
                    or str(next_choice.get("requestContractFingerprint") or "")
                    != identity_values["requestContractFingerprint"]
                    or str(next_choice.get("expectedBaseVersionId") or "")
                    != str(browser.get("activeVersionAfter") or "")
                    or browser.get("postCapabilityAvailable") is not True
                    or str(browser.get("postChoiceId") or "") != next_choice_id
                    or str(browser.get("postSourceAssistantTurnId") or "")
                    != result_turn_id
                    or str(browser.get("postFrontierStatus") or "") != frontier_status
                    or str(browser.get("postRequestContractFingerprint") or "")
                    != identity_values["requestContractFingerprint"]
                ):
                    add("continuation_next_choice_binding_invalid")
            elif (
                completion.get("nextChoiceId") is not None
                or result_choices
                or bool(browser.get("postCapabilityAvailable"))
                or str(browser.get("postChoiceId") or "")
                or str(browser.get("postStopReason") or "")
                != f"frontier_terminal:{frontier_status}"
            ):
                add("continuation_terminal_choice_contract_invalid")

        evidence_rows.append(
            {
                "executionId": execution_id,
                "choiceId": choice_id,
                "attemptStatus": attempt_status,
                "frontierStatus": frontier_status,
                "proposalDelta": proposal_delta,
                "zeroWrite": not any(deltas.values()),
                "attemptFingerprint": attempt_fingerprint,
            }
        )

    return {
        "verified": not errors,
        "errors": sorted(set(errors)),
        "browserCount": len(browser_items),
        "executionCount": len(rows),
        "terminalNoProgressCount": terminal_no_progress_count,
        "executions": evidence_rows,
    }


def detour_option_identity_from_sqlite(
    *,
    connection: sqlite3.Connection,
    choice_execution: sqlite3.Row | dict[str, Any],
    journey_batch: dict[str, Any],
) -> dict[str, Any]:
    dimension = "route_decision.detour_tolerance"
    source_turn_id = str(_row_value(choice_execution, "source_turn_id") or "")
    request_turn_id = str(_row_value(choice_execution, "request_turn_id") or "")
    rows = connection.execute(
        "SELECT id, role, agent_request_json, agent_response_json "
        "FROM conversation_turns WHERE id IN (?, ?)",
        (source_turn_id, request_turn_id),
    ).fetchall()
    rows_by_id = {str(row["id"]): row for row in rows}
    source_turn = rows_by_id.get(source_turn_id)
    request_turn = rows_by_id.get(request_turn_id)
    preflight_errors: list[str] = []
    if source_turn is None or str(source_turn["role"] or "") != "assistant":
        preflight_errors.append("sqlite_source_turn_invalid")
    if request_turn is None or str(request_turn["role"] or "") != "user":
        preflight_errors.append("sqlite_request_turn_invalid")
    if preflight_errors:
        return {
            "verified": False,
            "errors": preflight_errors,
            "dimensionId": dimension,
            "sourceAssistantTurnId": source_turn_id,
        }

    source_response = json_object(source_turn["agent_response_json"])
    request_context = json_object(request_turn["agent_request_json"])
    selected = json_object(request_context.get("selectedAgentChoice"))
    submitted = [
        item
        for item in json_list(selected.get("batchSelections"))
        if isinstance(item, dict) and item.get("dimensionId") == dimension
    ]
    artifact = [
        item
        for item in json_list(journey_batch.get("selections"))
        if isinstance(item, dict) and item.get("dimensionId") == dimension
    ]
    if len(submitted) != 1 or len(artifact) != 1:
        return {
            "verified": False,
            "errors": ["sqlite_detour_selection_identity_invalid"],
            "dimensionId": dimension,
            "sourceAssistantTurnId": source_turn_id,
        }
    return detour_option_identity_evidence(
        source_checkpoint=json_object(source_response.get("clarificationCheckpoint")),
        source_turn_id=source_turn_id,
        artifact_selection=artifact[0],
        selected_agent_choice=selected,
        submitted_selection=submitted[0],
        resolved_checkpoint=json_object(request_context.get("clarificationCheckpoint")),
        request_contract=json_object(request_context.get("requestIntentContract")),
    )


def find_offer_turn(
    proposal_id: str,
    proposal_row: sqlite3.Row,
    turns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    lineage = json_object(proposal_row["generation_lineage_json"])
    source_assistant_id = str(lineage.get("sourceAssistantTurnId") or "")
    for turn in turns:
        if turn["id"] == source_assistant_id:
            return turn
    for turn in turns:
        response = turn["response"]
        if response.get(
            "mode"
        ) == "simple_open_direction_proposal" and comparison_projection(
            response, proposal_id
        ):
            return turn
    return None


def prior_user_turn(
    offer_turn: dict[str, Any],
    proposal_row: sqlite3.Row,
    turns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    lineage = json_object(proposal_row["generation_lineage_json"])
    source_user_id = str(lineage.get("sourceUserTurnId") or "")
    for turn in turns:
        if turn["id"] == source_user_id:
            return turn
    candidates = [
        turn
        for turn in turns
        if turn["role"] == "user" and turn["turnIndex"] < offer_turn["turnIndex"]
    ]
    return candidates[-1] if candidates else None


def verify_database(
    database: Path,
    journey: dict[str, Any],
    *,
    deepseek_endpoint_host: str = TRUSTED_DEEPSEEK_ENDPOINT_HOST,
) -> dict[str, Any]:
    normalized_deepseek_host, endpoint_failure = deepseek_endpoint_preflight(
        deepseek_endpoint_host
    )
    if endpoint_failure is not None:
        return endpoint_failure
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    failures: list[str] = []

    session_id = str(journey.get("sessionId") or "")
    proposal_a = str(journey.get("proposalAId") or "")
    proposal_b = str(journey.get("proposalBId") or "")
    versions = json_object(journey.get("versions"))
    version_a_confirmed = str(versions.get("aConfirmed") or "")
    version_a_edited = str(versions.get("aEdited") or "")
    version_b_confirmed = str(versions.get("bConfirmed") or "")
    version_a_restored = str(versions.get("aRestored") or "")
    expected_version_ids = {
        version_a_confirmed,
        version_a_edited,
        version_b_confirmed,
        version_a_restored,
    }
    check(
        len(expected_version_ids) == 4 and "" not in expected_version_ids,
        "journey_version_identity_invalid",
        failures,
    )
    check(
        proposal_a != "" and proposal_b != "" and proposal_a != proposal_b,
        "journey_proposal_identity_invalid",
        failures,
    )

    session = connection.execute(
        "SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    check(session is not None, "session_not_found", failures)
    if session is None:
        connection.close()
        return {"passed": False, "failures": failures, "sessionId": session_id}
    plan_id = str(session["active_plan_id"] or "")
    check(
        str(session["active_version_id"] or "") == version_a_restored,
        "final_active_version_not_restored_a",
        failures,
    )

    turn_rows = connection.execute(
        "SELECT id, turn_index, role, status, parent_turn_id, itinerary_version_id, "
        "content, agent_request_json, agent_response_json, created_at "
        "FROM conversation_turns WHERE session_id = ? ORDER BY turn_index",
        (session_id,),
    ).fetchall()
    turns = [turn_payload(row) for row in turn_rows]

    turns_by_id = {turn["id"]: turn for turn in turns}
    initial_user_turn = next((turn for turn in turns if turn["role"] == "user"), {})
    initial_request_contract = json_object(
        json_object(initial_user_turn.get("request")).get("requestIntentContract")
    )
    route_default_contract = json_object(
        initial_request_contract.get("routeDecisionContract")
    )
    editable_defaults = [
        item
        for item in json_list(initial_request_contract.get("editableDefaults"))
        if isinstance(item, dict)
    ]
    default_dimensions = [str(item.get("dimensionId") or "") for item in editable_defaults]
    check(
        route_default_contract.get("status") == "ready"
        and route_default_contract.get("mobilityProfileSource")
        == "server_safe_default_v1"
        and route_default_contract.get("detourToleranceSource")
        == "server_safe_default_v1"
        and json_object(route_default_contract.get("mobilityProfile"))
        == {"transportMode": "transit", "paceClass": "standard"}
        and json_object(route_default_contract.get("detourTolerance"))
        == {"maxGeneralizedCostDelta": 35.0, "maxDetourRatio": 0.35},
        "route_safe_defaults_invalid",
        failures,
    )
    check(
        default_dimensions
        == [
            "route_decision.mobility_profile",
            "route_decision.detour_tolerance",
        ]
        and all(
            item.get("source") == "server_safe_default_v1"
            and item.get("editable") is True
            and item.get("userConfirmed") is False
            for item in editable_defaults
        ),
        f"route_editable_defaults_invalid:{default_dimensions}",
        failures,
    )
    journey_clarification = json_object(journey.get("clarificationBatchEvidence"))
    journey_batches = [
        item
        for item in json_list(journey_clarification.get("batches"))
        if isinstance(item, dict)
    ]
    journey_dimensions = [
        str(item) for item in json_list(journey_clarification.get("dimensions"))
    ]
    journey_manual_dimensions = [
        str(item) for item in json_list(journey_clarification.get("manualDimensions"))
    ]
    clarification_turns = []
    for turn in turns:
        checkpoint = json_object(turn["response"].get("clarificationCheckpoint"))
        if (
            turn["role"] == "assistant"
            and checkpoint.get("schemaVersion") == "clarification-checkpoint-v2"
            and checkpoint.get("status") == "awaiting_answer"
        ):
            clarification_turns.append(turn)
    check(bool(clarification_turns), "clarification_batch_turn_missing", failures)
    check(
        len(clarification_turns)
        == int(journey_clarification.get("batchSubmitCount") or 0)
        == len(journey_batches),
        (
            "clarification_batch_count_mismatch:"
            f"db={len(clarification_turns)}:journey={journey_clarification.get('batchSubmitCount')}"
        ),
        failures,
    )
    actual_dimensions: list[str] = []
    clarification_external_evidence: list[dict[str, Any]] = []
    batch_dimensions_by_turn: dict[str, list[str]] = {}
    expected_resolved_dimensions: set[str] = set()
    for index, turn in enumerate(clarification_turns):
        checkpoint = json_object(turn["response"].get("clarificationCheckpoint"))
        questions = [
            item
            for item in json_list(checkpoint.get("questions"))
            if isinstance(item, dict)
        ]
        check(
            checkpoint.get("submissionMode") == "batch_atomic"
            and 1 <= len(questions) <= 3,
            f"clarification_batch_contract_invalid:{turn['id']}",
            failures,
        )
        actual_resolved = set(
            str(item) for item in json_list(checkpoint.get("resolvedDimensions"))
        )
        check(
            actual_resolved == expected_resolved_dimensions,
            f"clarification_resolved_prefix_mismatch:{turn['id']}",
            failures,
        )
        submit_options = [
            item
            for item in json_list(turn["response"].get("choiceOptions"))
            if isinstance(item, dict)
            and item.get("action") == "submit_clarification_batch"
            and item.get("kind") == "clarification_batch_submit"
        ]
        check(
            len(submit_options) == 1
            and submit_options[0].get("scopeKind") == "clarification"
            and str(submit_options[0].get("checkpointId") or "")
            == str(checkpoint.get("checkpointId") or "")
            and str(submit_options[0].get("checkpointFingerprint") or "")
            == str(checkpoint.get("fingerprint") or ""),
            f"clarification_submit_capability_invalid:{turn['id']}",
            failures,
        )
        batch_dimensions: list[str] = []
        for question in questions:
            dimension = str(question.get("dimensionId") or "")
            semantic_field = CLARIFICATION_FIELD_BY_DIMENSION.get(dimension, "")
            semantic_values = [
                json_object(option.get("semanticValue"))
                for option in json_list(question.get("options"))
                if isinstance(option, dict)
            ]
            check(
                bool(dimension)
                and dimension not in actual_dimensions
                and bool(semantic_field)
                and any(semantic_field in value for value in semantic_values),
                f"clarification_semantic_field_missing:{dimension}:{semantic_field}",
                failures,
            )
            if dimension == "route_decision.detour_tolerance":
                check(
                    question.get("allowFreeText") is False,
                    f"clarification_detour_free_text_unexpected:{turn['id']}",
                    failures,
                )
            actual_dimensions.append(dimension)
            batch_dimensions.append(dimension)
        batch_dimensions_by_turn[turn["id"]] = batch_dimensions
        expected_resolved_dimensions.update(batch_dimensions)
        if index < len(journey_batches):
            journey_batch = journey_batches[index]
            check(
                str(journey_batch.get("checkpointId") or "")
                == str(checkpoint.get("checkpointId") or "")
                and str(journey_batch.get("checkpointFingerprint") or "")
                == str(checkpoint.get("fingerprint") or "")
                and str(journey_batch.get("requestFingerprint") or "")
                == str(checkpoint.get("requestFingerprint") or "")
                and str(journey_batch.get("planningRootId") or "")
                == str(checkpoint.get("planningRootId") or "")
                and str(journey_batch.get("sourceAssistantTurnId") or "") == turn["id"]
                and str(journey_batch.get("sourceTurnId") or "") == turn["id"]
                and [str(item) for item in json_list(journey_batch.get("dimensions"))]
                == batch_dimensions
                and int(journey_batch.get("streamRequestDelta") or 0) == 1,
                f"clarification_browser_batch_mismatch:{turn['id']}",
                failures,
            )
        evidence = planning_external_evidence(turn["response"])
        if evidence:
            clarification_external_evidence.append(
                {"turnId": turn["id"], "evidence": evidence}
            )
    check(
        tuple(actual_dimensions) == CLARIFICATION_DIMENSIONS,
        f"clarification_order:{actual_dimensions}",
        failures,
    )
    check(
        journey_dimensions == actual_dimensions,
        f"clarification_browser_dimensions:{journey_dimensions}",
        failures,
    )
    check(
        journey_manual_dimensions == [],
        f"clarification_browser_manual_dimensions:{journey_manual_dimensions}",
        failures,
    )
    check(
        not clarification_external_evidence,
        "clarification_preflight_external_call_detected",
        failures,
    )

    clarification_choices = connection.execute(
        "SELECT * FROM agent_choice_executions WHERE session_id = ? "
        "AND action = 'submit_clarification_batch' ORDER BY created_at",
        (session_id,),
    ).fetchall()
    check(
        len(clarification_choices) == len(clarification_turns),
        f"clarification_choice_execution_count:{len(clarification_choices)}",
        failures,
    )
    clarification_choice_evidence: list[dict[str, Any]] = []
    manual_normalization_evidence: list[dict[str, Any]] = []
    detour_option_identity_rows: list[dict[str, Any]] = []
    for index, row in enumerate(clarification_choices):
        outcome = json_object(row["outcome_json"])
        source_turn_id = str(row["source_turn_id"] or "")
        expected_batch_dimensions = batch_dimensions_by_turn.get(source_turn_id, [])
        source_turn = turns_by_id.get(source_turn_id)
        proposal_delta = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_plan_proposals proposal "
                    "JOIN agent_plan_portfolios portfolio ON portfolio.id = proposal.portfolio_id "
                    "WHERE portfolio.session_id = ? "
                    "AND proposal.created_at > ? AND proposal.created_at <= ?",
                    (session_id, source_turn["createdAt"], row["created_at"]),
                ).fetchone()[0]
            )
            if isinstance(source_turn, dict)
            else -1
        )
        deltas = {
            key: int(outcome.get(key) or 0)
            for key in ("versionDelta", "patchDelta", "routeWriteDelta")
        }
        deltas["proposalDelta"] = proposal_delta
        journey_batch = journey_batches[index] if index < len(journey_batches) else {}
        request_turn = turns_by_id.get(str(row["request_turn_id"] or ""))
        request_context = (
            request_turn["request"] if isinstance(request_turn, dict) else {}
        )
        selected = json_object(request_context.get("selectedAgentChoice"))
        selections = [
            item
            for item in json_list(selected.get("batchSelections"))
            if isinstance(item, dict)
        ]
        selection_dimensions = [
            str(item.get("dimensionId") or "") for item in selections
        ]
        check(
            selection_dimensions == expected_batch_dimensions
            and len(set(selection_dimensions)) == len(selection_dimensions),
            f"clarification_batch_selection_coverage:{row['id']}:{selection_dimensions}",
            failures,
        )
        resolved_checkpoint = json_object(
            request_context.get("clarificationCheckpoint")
        )
        resolved_answers = {
            str(item.get("dimensionId") or ""): item
            for item in json_list(resolved_checkpoint.get("resolvedAnswers"))
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        manual_dimensions = [
            str(item.get("dimensionId") or "")
            for item in selections
            if str(item.get("manualValue") or "").strip()
        ]
        for selection in selections:
            dimension = str(selection.get("dimensionId") or "")
            answer = json_object(resolved_answers.get(dimension))
            expected_source = (
                "free_text_normalized"
                if str(selection.get("manualValue") or "").strip()
                else "structured_option"
            )
            semantic_value = json_object(answer.get("semanticValue"))
            expected_field = CLARIFICATION_FIELD_BY_DIMENSION.get(dimension, "")
            expected_fields = {expected_field}
            if (
                dimension == "route_decision.detour_tolerance"
                and expected_source == "free_text_normalized"
            ):
                expected_fields.add("adjacentLegConstraint")
            check(
                answer.get("source") == expected_source
                and set(semantic_value) == expected_fields
                and (
                    dimension != "route_decision.detour_tolerance"
                    or expected_source != "free_text_normalized"
                    or semantic_value.get("detourTolerance")
                    == {"maxGeneralizedCostDelta": 10, "maxDetourRatio": 0.2}
                )
                and (
                    dimension != "route_decision.detour_tolerance"
                    or expected_source != "free_text_normalized"
                    or semantic_value.get("adjacentLegConstraint")
                    == {
                        "candidateSearchRadiusMeters": 5000,
                        "maxProviderTravelMinutes": 45,
                    }
                ),
                f"clarification_resolved_answer_invalid:{row['id']}:{dimension}",
                failures,
            )
            if dimension == "route_decision.detour_tolerance":
                detour_evidence = detour_option_identity_from_sqlite(
                    connection=connection,
                    choice_execution=row,
                    journey_batch=journey_batch,
                )
                for error in detour_evidence["errors"]:
                    check(
                        False,
                        f"clarification_detour_option_identity:{row['id']}:{error}",
                        failures,
                    )
                detour_option_identity_rows.append(
                    {
                        "choiceExecutionId": str(row["id"]),
                        **detour_evidence,
                    }
                )
        normalization_audit = json_object(
            request_context.get("clarificationManualNormalization")
        )
        if manual_dimensions:
            transport = json_object(normalization_audit.get("transport"))
            check(
                normalization_audit.get("schemaVersion")
                == "clarification-batch-normalization-audit-v1"
                and normalization_audit.get("providerName") == "deepseek"
                and normalization_audit.get("attemptCount") == 1
                and normalization_audit.get("retryCount") == 0
                and int(normalization_audit.get("outboundQuestionCount") or 0)
                == len(manual_dimensions)
                and [
                    str(item)
                    for item in json_list(
                        normalization_audit.get("outboundDimensionIds")
                    )
                ]
                == manual_dimensions
                and json_list(normalization_audit.get("outboundQuestionFields"))
                == [
                    "dimensionId",
                    "question",
                    "manualValue",
                    "allowedSemanticFields",
                ]
                and transport.get("callKind") == "clarification_batch_normalization"
                and transport.get("captureState") == "completed"
                and transport.get("providerInvoked") is True
                and transport.get("responseHeadersReceived") is True
                and int(transport.get("httpStatus") or 0) == 200,
                f"clarification_manual_normalization_audit_invalid:{row['id']}",
                failures,
            )
            manual_normalization_evidence.append(
                {
                    "choiceExecutionId": str(row["id"]),
                    "dimensionIds": manual_dimensions,
                    "audit": normalization_audit,
                }
            )
        else:
            check(
                not normalization_audit,
                f"clarification_unexpected_normalization_audit:{row['id']}",
                failures,
            )
        clarification_choice_evidence.append(
            {
                "id": row["id"],
                "status": row["status"],
                "dimensions": selection_dimensions,
                "manualDimensions": manual_dimensions,
                **deltas,
            }
        )
        check(
            str(row["status"] or "") == "succeeded",
            f"clarification_choice_not_succeeded:{row['id']}",
            failures,
        )
        check(
            all(value == 0 for value in deltas.values()),
            f"clarification_choice_write_delta:{row['id']}:{deltas}",
            failures,
        )
        if index < len(clarification_turns):
            check(
                str(row["source_turn_id"] or "") == clarification_turns[index]["id"],
                f"clarification_choice_source_mismatch:{row['id']}",
                failures,
            )
    preflight_boundary = (
        clarification_choices[-1]["created_at"] if clarification_choices else None
    )

    version_rows = connection.execute(
        "SELECT * FROM itinerary_versions WHERE session_id = ? ORDER BY version_number",
        (session_id,),
    ).fetchall()
    patch_rows = connection.execute(
        "SELECT * FROM itinerary_patches WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    route_rows = connection.execute(
        "SELECT * FROM route_options WHERE plan_id = ? ORDER BY queried_at", (plan_id,)
    ).fetchall()
    boundary = parse_timestamp(preflight_boundary)
    if boundary:
        check(
            not any(
                parse_timestamp(row["created_at"])
                and parse_timestamp(row["created_at"]) <= boundary
                for row in version_rows
            ),
            "clarification_preflight_version_write_detected",
            failures,
        )
        check(
            not any(
                parse_timestamp(row["created_at"])
                and parse_timestamp(row["created_at"]) <= boundary
                for row in patch_rows
            ),
            "clarification_preflight_patch_write_detected",
            failures,
        )
        check(
            not any(
                parse_timestamp(row["queried_at"])
                and parse_timestamp(row["queried_at"]) <= boundary
                for row in route_rows
            ),
            "clarification_preflight_route_write_detected",
            failures,
        )

    portfolio_rows = connection.execute(
        "SELECT * FROM agent_plan_portfolios WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    simple_roots = [
        row
        for row in portfolio_rows
        if json_object(row["summary_json"]).get("workflowMode") == "simple_direction_v1"
    ]
    check(
        len(simple_roots) == 1,
        f"simple_direction_root_count:{len(simple_roots)}",
        failures,
    )
    root = simple_roots[0] if len(simple_roots) == 1 else None
    root_id = str(root["id"] or "") if root is not None else ""
    summary: dict[str, Any] = {}
    if root is not None:
        summary = json_object(root["summary_json"])
        check(
            str(root["status"] or "") == "awaiting_selection",
            "root_not_reusable_after_restore",
            failures,
        )
        check(
            str(root["selected_proposal_id"] or "") == proposal_a,
            "root_selected_proposal_not_a",
            failures,
        )
        check(
            str(root["expected_base_version_id"] or "") == version_a_restored,
            "root_base_not_final_active",
            failures,
        )
        check(
            set(str(item) for item in summary.get("visibleProposalIds") or [])
            == {proposal_a, proposal_b},
            "root_visible_proposals_mismatch",
            failures,
        )

    continuation_rows = connection.execute(
        "SELECT * FROM agent_choice_executions WHERE session_id = ? "
        "AND action = 'continue_plan_expansion' ORDER BY created_at",
        (session_id,),
    ).fetchall()
    continuation_evidence = continuation_execution_evidence(
        browser_evidence=journey.get("continuationEvidence"),
        execution_rows=continuation_rows,
        turns_by_id=turns_by_id,
        portfolio_summary=summary,
    )
    for error in continuation_evidence["errors"]:
        check(False, error, failures)
    continuation_actual_writes: list[dict[str, Any]] = []
    for row in continuation_rows:
        execution_id = str(row["id"] or "")
        request_turn = turns_by_id.get(str(row["request_turn_id"] or ""))
        result_turn = turns_by_id.get(str(row["execution_turn_id"] or ""))
        interval_versions: list[str] = []
        interval_patches: list[str] = []
        interval_routes: list[str] = []
        if isinstance(request_turn, dict) and isinstance(result_turn, dict):
            interval_versions = [
                str(item["id"])
                for item in version_rows
                if in_time_window(
                    item["created_at"],
                    request_turn["createdAt"],
                    result_turn["createdAt"],
                )
            ]
            interval_patches = [
                str(item["id"])
                for item in patch_rows
                if in_time_window(
                    item["created_at"],
                    request_turn["createdAt"],
                    result_turn["createdAt"],
                )
            ]
            interval_routes = [
                str(item["id"])
                for item in route_rows
                if in_time_window(
                    item["queried_at"],
                    request_turn["createdAt"],
                    result_turn["createdAt"],
                )
            ]
        check(
            not interval_versions and not interval_patches and not interval_routes,
            f"continuation_actual_write_detected:{execution_id}",
            failures,
        )
        continuation_actual_writes.append(
            {
                "executionId": execution_id,
                "versionIds": interval_versions,
                "patchIds": interval_patches,
                "routeIds": interval_routes,
            }
        )
    continuation_evidence["actualWrites"] = continuation_actual_writes

    proposal_rows = (
        connection.execute(
            "SELECT * FROM agent_plan_proposals WHERE portfolio_id = ? "
            "ORDER BY rank_index",
            (root_id,),
        ).fetchall()
        if root_id
        else []
    )
    proposal_by_id = {str(row["id"]): row for row in proposal_rows}
    check(
        set(proposal_by_id) == {proposal_a, proposal_b},
        "persisted_proposal_ids_mismatch",
        failures,
    )

    proposal_evidence: list[dict[str, Any]] = []
    direction_provider_evidence: list[dict[str, Any]] = []
    offer_zero_write_evidence: list[dict[str, Any]] = []
    offer_turns: dict[str, dict[str, Any]] = {}
    initial_projections: dict[str, dict[str, Any]] = {}
    request_contracts: dict[str, dict[str, Any]] = {}
    initial_hard_night_evidence: dict[str, dict[str, Any]] = {}
    persisted_hard_night_evidence: dict[str, dict[str, Any]] = {}
    for proposal_id, row in proposal_by_id.items():
        snapshot = json_object(row["snapshot_json"])
        quality = snapshot_quality(snapshot)
        persisted_daily_completion = standard_two_day_daily_completion_evidence(
            snapshot
        )
        initial_daily_completion: dict[str, Any] = {}
        initial_adjacent_search: dict[str, Any] = {}
        persisted_adjacent_search: dict[str, Any] = {}
        persisted_readiness = json_object(
            json_object(row["evidence_json"]).get("readiness")
        )
        commit_readiness_evidence = {
            "persisted": proposal_commit_readiness_evidence(
                snapshot,
                adoption_ready=persisted_readiness.get("adoptionReady") is True,
            )
        }
        record_proposal_commit_readiness_failures(
            proposal_id=proposal_id,
            stage="persisted",
            evidence=commit_readiness_evidence["persisted"],
            failures=failures,
        )
        check(
            snapshot.get("workflowMode") == "simple_direction_v1",
            f"proposal_workflow_missing:{proposal_id}",
            failures,
        )
        check(
            route_contract_ready(snapshot),
            f"proposal_route_contract_not_ready:{proposal_id}",
            failures,
        )
        for error in compact_route_evidence_errors(snapshot):
            check(False, f"proposal_compact_route:{proposal_id}:{error}", failures)
        if proposal_id == proposal_b:
            novelty = json_object(snapshot.get("simpleDirectionNoveltyEvidence"))
            comparisons = [
                json_object(item) for item in json_list(novelty.get("comparisons"))
            ]
            comparison_a = next(
                (
                    item
                    for item in comparisons
                    if str(item.get("priorProposalId") or "") == proposal_a
                ),
                {},
            )
            day_evidence = [
                json_object(item) for item in json_list(comparison_a.get("dayEvidence"))
            ]
            check(
                novelty.get("schemaVersion") == "simple-direction-novelty-v2"
                and novelty.get("priorDirectionExclusionApplied") is True
                and novelty.get("passed") is True
                and comparison_a.get("passed") is True
                and int(comparison_a.get("totalChangedCount") or 0) >= 2
                and bool(day_evidence)
                and all(
                    not json_list(item.get("candidateCanonicalIdentities"))
                    or int(item.get("changedCount") or 0) >= 1
                    for item in day_evidence
                ),
                "proposal_b_novelty_v2_invalid",
                failures,
            )
        for error in quality["errors"]:
            check(False, f"proposal_quality:{proposal_id}:{error}", failures)
        for error in persisted_daily_completion["errors"]:
            check(
                False,
                f"proposal_daily_completion:{proposal_id}:persisted:{error}",
                failures,
            )

        offer_turn = find_offer_turn(proposal_id, row, turns)
        check(
            offer_turn is not None,
            f"proposal_offer_turn_missing:{proposal_id}",
            failures,
        )
        if offer_turn is not None:
            offer_turns[proposal_id] = offer_turn
            response = offer_turn["response"]
            initial_projection = comparison_projection(response, proposal_id)
            contract = request_intent_contract(response)
            initial_daily_completion = standard_two_day_daily_completion_evidence(
                initial_projection
            )
            initial_adjacent_search = current_direction_adjacent_search_evidence(
                response,
                initial_projection,
            )
            persisted_adjacent_search = current_direction_adjacent_search_evidence(
                response,
                snapshot,
            )
            initial_projections[proposal_id] = initial_projection
            request_contracts[proposal_id] = contract
            commit_readiness_evidence["initialProjection"] = (
                proposal_commit_readiness_evidence(
                    initial_projection,
                    adoption_ready=(initial_projection.get("adoptionReady") is True),
                )
            )
            record_proposal_commit_readiness_failures(
                proposal_id=proposal_id,
                stage="initial_projection",
                evidence=commit_readiness_evidence["initialProjection"],
                failures=failures,
            )
            check(
                bool(initial_projection),
                f"proposal_initial_projection_missing:{proposal_id}",
                failures,
            )
            check(
                bool(contract),
                f"proposal_request_intent_contract_missing:{proposal_id}",
                failures,
            )
            text_count = external_call_counts(response)["amapPlaceText"]
            deepseek = deepseek_decision_evidence(
                response,
                endpoint_host=normalized_deepseek_host,
            )
            check(
                deepseek["verified"],
                f"deepseek_decision_evidence:{proposal_id}:{deepseek['reasonCode']}",
                failures,
            )
            amap = real_amap_text_evidence(response, initial_projection)
            for error in amap["errors"]:
                check(False, f"real_amap_text:{proposal_id}:{error}", failures)
            check(
                amap["verified"],
                f"real_amap_text_call_missing:{proposal_id}",
                failures,
            )
            initial_hard = hard_night_occurrence_evidence(
                initial_projection,
                contract,
            )
            persisted_hard = hard_night_occurrence_evidence(snapshot, contract)
            initial_hard_night_evidence[proposal_id] = initial_hard
            persisted_hard_night_evidence[proposal_id] = persisted_hard
            for stage, evidence in (
                ("initial", initial_hard),
                ("persisted", persisted_hard),
            ):
                check(
                    evidence["requiredOccurrenceCount"] > 0,
                    f"hard_night_required_occurrence_missing:{proposal_id}:{stage}",
                    failures,
                )
                for error in evidence["errors"]:
                    check(
                        False,
                        f"hard_night:{proposal_id}:{stage}:{error}",
                        failures,
                    )
            for stage, stage_snapshot in (
                ("initial", initial_projection),
                ("persisted", snapshot),
            ):
                for error in explicit_every_day_meal_errors(stage_snapshot, contract):
                    check(
                        False,
                        f"explicit_every_day_meal:{proposal_id}:{stage}:{error}",
                        failures,
                    )
            for error in initial_daily_completion["errors"]:
                check(
                    False,
                    f"proposal_daily_completion:{proposal_id}:initial:{error}",
                    failures,
                )
            for stage, evidence in (
                ("initial", initial_adjacent_search),
                ("persisted", persisted_adjacent_search),
            ):
                for error in evidence["errors"]:
                    check(
                        False,
                        f"proposal_adjacent_search:{proposal_id}:{stage}:{error}",
                        failures,
                    )
            check(
                text_count <= 6,
                f"proposal_text_budget_exceeded:{proposal_id}:{text_count}",
                failures,
            )
            direction_provider_evidence.append(
                {
                    "proposalId": proposal_id,
                    "offerTurnId": offer_turn["id"],
                    "deepSeekDecision": deepseek,
                    "amapPlaceText": text_count,
                    "realAmapPlaceText": len(amap["boundAmapIds"]),
                    "realAmapEvidence": amap,
                    "hardNightInitial": initial_hard,
                    "hardNightPersisted": persisted_hard,
                    "dailyCompletionInitial": initial_daily_completion,
                    "dailyCompletionPersisted": persisted_daily_completion,
                    "adjacentSearchInitial": initial_adjacent_search,
                    "adjacentSearchPersisted": persisted_adjacent_search,
                }
            )

            persist_steps = [
                step
                for step in response_steps(response)
                if step.get("type") == "simple_direction_proposal_persisted"
            ]
            check(
                len(persist_steps) == 1,
                f"proposal_persist_trace_count:{proposal_id}:{len(persist_steps)}",
                failures,
            )
            trace_deltas: dict[str, int | None] = {}
            if persist_steps:
                metadata = json_object(persist_steps[0].get("metadata"))
                preview = json_object(metadata.get("resultPreview"))
                for key in ("versionDelta", "patchDelta", "routeWriteDelta"):
                    raw = metadata.get(key, preview.get(key))
                    trace_deltas[key] = int(raw) if raw is not None else None
                    check(
                        trace_deltas[key] == 0,
                        f"proposal_offer_{key}:{proposal_id}:{trace_deltas[key]}",
                        failures,
                    )
            source_user = prior_user_turn(offer_turn, row, turns)
            check(
                source_user is not None,
                f"proposal_source_user_turn_missing:{proposal_id}",
                failures,
            )
            interval_versions: list[str] = []
            interval_patches: list[str] = []
            interval_routes: list[str] = []
            if source_user is not None:
                interval_versions = [
                    str(item["id"])
                    for item in version_rows
                    if in_time_window(
                        item["created_at"],
                        source_user["createdAt"],
                        offer_turn["createdAt"],
                    )
                ]
                interval_patches = [
                    str(item["id"])
                    for item in patch_rows
                    if in_time_window(
                        item["created_at"],
                        source_user["createdAt"],
                        offer_turn["createdAt"],
                    )
                ]
                interval_routes = [
                    str(item["id"])
                    for item in route_rows
                    if in_time_window(
                        item["queried_at"],
                        source_user["createdAt"],
                        offer_turn["createdAt"],
                    )
                ]
                check(
                    not interval_versions,
                    f"proposal_offer_version_rows:{proposal_id}:{interval_versions}",
                    failures,
                )
                check(
                    not interval_patches,
                    f"proposal_offer_patch_rows:{proposal_id}:{interval_patches}",
                    failures,
                )
                check(
                    not interval_routes,
                    f"proposal_offer_route_rows:{proposal_id}:{interval_routes}",
                    failures,
                )
            check(
                not connection.execute(
                    "SELECT 1 FROM itinerary_versions WHERE session_id = ? "
                    "AND source_turn_id = ? LIMIT 1",
                    (session_id, offer_turn["id"]),
                ).fetchone(),
                f"proposal_offer_version_source_turn:{proposal_id}",
                failures,
            )
            check(
                not connection.execute(
                    "SELECT 1 FROM itinerary_patches WHERE session_id = ? "
                    "AND source_turn_id = ? LIMIT 1",
                    (session_id, offer_turn["id"]),
                ).fetchone(),
                f"proposal_offer_patch_source_turn:{proposal_id}",
                failures,
            )
            offer_zero_write_evidence.append(
                {
                    "proposalId": proposal_id,
                    "traceDeltas": trace_deltas,
                    "intervalVersionIds": interval_versions,
                    "intervalPatchIds": interval_patches,
                    "intervalRouteIds": interval_routes,
                }
            )
        proposal_evidence.append(
            {
                "id": proposal_id,
                "status": row["status"],
                "canonicalSignature": row["canonical_signature"],
                "dayAnchorCounts": quality["dayAnchorCounts"],
                "materializedPoiCount": quality["materializedPoiCount"],
                "qualityErrors": quality["errors"],
                "dailyCompletionInitial": initial_daily_completion,
                "dailyCompletionPersisted": persisted_daily_completion,
                "adjacentSearchInitial": initial_adjacent_search,
                "adjacentSearchPersisted": persisted_adjacent_search,
                "commitReadiness": commit_readiness_evidence,
            }
        )

    if proposal_a in proposal_by_id and proposal_b in proposal_by_id:
        snapshot_a = json_object(proposal_by_id[proposal_a]["snapshot_json"])
        snapshot_b = json_object(proposal_by_id[proposal_b]["snapshot_json"])
        check(
            business_signature(snapshot_a) != business_signature(snapshot_b),
            "direction_b_not_materially_different_from_a",
            failures,
        )
        check(
            str(proposal_by_id[proposal_a]["canonical_signature"] or "")
            != str(proposal_by_id[proposal_b]["canonical_signature"] or ""),
            "direction_canonical_signatures_equal",
            failures,
        )

    initial_projection_a = initial_projections.get(proposal_a, {})
    initial_hard_a = initial_hard_night_evidence.get(
        proposal_a,
        hard_night_occurrence_evidence(
            initial_projection_a,
            request_contracts.get(proposal_a, {}),
        ),
    )
    check(initial_projection_a != {}, "initial_a_projection_missing", failures)
    check(
        initial_projection_a.get("adoptionReady") is True,
        "initial_a_direction_not_adoption_ready",
        failures,
    )

    edit_user = next(
        (
            turn
            for turn in turns
            if turn["role"] == "user"
            and turn["content"].strip() == str(journey.get("editRequest") or "").strip()
        ),
        None,
    )
    edit_turn_index = edit_user["turnIndex"] if edit_user is not None else 0
    save_projection: dict[str, Any] = {}
    for turn in turns:
        if turn["turnIndex"] <= edit_turn_index:
            continue
        response = turn["response"]
        if response.get("mode") != "simple_direction_save_active":
            continue
        candidate = comparison_projection(response, proposal_a)
        if candidate:
            save_projection = candidate
            break
    check(save_projection != {}, "saved_a_projection_missing", failures)
    saved_hard_a = hard_night_occurrence_evidence(
        save_projection,
        request_contracts.get(proposal_a, {}),
    )
    if save_projection:
        check(
            save_projection.get("adoptionReady") is True,
            "saved_a_projection_not_adoption_ready",
            failures,
        )
        for error in saved_hard_a["errors"]:
            check(False, f"hard_night:{proposal_a}:saved:{error}", failures)
        check(
            hard_night_evidence_core(saved_hard_a)
            == hard_night_evidence_core(initial_hard_a),
            "saved_a_hard_night_branch_or_core_changed",
            failures,
        )

    final_a_snapshot: dict[str, Any] = {}
    final_hard_a = persisted_hard_night_evidence.get(
        proposal_a,
        hard_night_occurrence_evidence({}, request_contracts.get(proposal_a, {})),
    )
    if proposal_a in proposal_by_id:
        final_a_snapshot = json_object(proposal_by_id[proposal_a]["snapshot_json"])
        check(
            hard_night_evidence_core(final_hard_a)
            == hard_night_evidence_core(initial_hard_a),
            "saved_a_snapshot_hard_night_branch_or_core_changed",
            failures,
        )
        check(
            not save_projection
            or business_signature(save_projection)
            == business_signature(final_a_snapshot),
            "saved_a_projection_snapshot_core_mismatch",
            failures,
        )
        readiness = json_object(
            json_object(proposal_by_id[proposal_a]["evidence_json"]).get("readiness")
        )
        check(
            readiness.get("adoptionReady") is True,
            "saved_a_persisted_readiness_not_adoption_ready",
            failures,
        )
        verifier = json_object(proposal_by_id[proposal_a]["verifier_json"])
        expected_provider_exhausted = len(
            final_hard_a.get("providerExhaustedPending") or []
        )
        check(
            int(verifier.get("providerExhaustedRequiredSlotCount") or 0)
            == expected_provider_exhausted,
            "saved_a_verifier_provider_exhausted_count_changed",
            failures,
        )

    initial_projection_b = initial_projections.get(proposal_b, {})
    initial_hard_b = initial_hard_night_evidence.get(
        proposal_b,
        hard_night_occurrence_evidence(
            initial_projection_b,
            request_contracts.get(proposal_b, {}),
        ),
    )
    final_b_snapshot = (
        json_object(proposal_by_id[proposal_b]["snapshot_json"])
        if proposal_b in proposal_by_id
        else {}
    )
    final_hard_b = persisted_hard_night_evidence.get(
        proposal_b,
        hard_night_occurrence_evidence({}, request_contracts.get(proposal_b, {})),
    )
    check(initial_projection_b != {}, "initial_b_projection_missing", failures)
    check(
        hard_night_evidence_core(initial_hard_b)
        == hard_night_evidence_core(final_hard_b),
        "saved_b_hard_night_branch_or_core_changed",
        failures,
    )
    check(
        not initial_projection_b
        or not final_b_snapshot
        or business_signature(initial_projection_b)
        == business_signature(final_b_snapshot),
        "saved_b_projection_snapshot_core_mismatch",
        failures,
    )

    browser_hard_night = json_object(journey.get("hardNightBranchEvidence"))
    browser_hard_a = json_object(browser_hard_night.get("a"))
    browser_hard_b = json_object(browser_hard_night.get("b"))
    browser_hard_pairs = (
        (
            "a_initial",
            json_object(browser_hard_a.get("initial")),
            initial_hard_a,
        ),
        (
            "a_saved",
            json_object(browser_hard_a.get("saved")),
            saved_hard_a,
        ),
        (
            "a_restored",
            json_object(browser_hard_a.get("restored")),
            final_hard_a,
        ),
        (
            "b_initial",
            json_object(browser_hard_b.get("initial")),
            initial_hard_b,
        ),
        (
            "b_saved",
            json_object(browser_hard_b.get("saved")),
            final_hard_b,
        ),
    )
    for label, browser_evidence, database_evidence in browser_hard_pairs:
        check(
            bool(browser_evidence),
            f"browser_hard_night_evidence_missing:{label}",
            failures,
        )
        check(
            hard_night_identity_core(browser_evidence)
            == hard_night_identity_core(database_evidence),
            f"browser_database_hard_night_core_mismatch:{label}",
            failures,
        )
    browser_b_core = json_object(journey.get("proposalBCoreEvidence"))
    check(
        json_object(browser_b_core.get("initial"))
        == proposal_projection_identity_core(initial_projection_b),
        "browser_database_b_initial_proposal_core_mismatch",
        failures,
    )
    check(
        json_object(browser_b_core.get("saved"))
        == proposal_projection_identity_core(final_b_snapshot),
        "browser_database_b_saved_proposal_core_mismatch",
        failures,
    )

    actual_version_ids = {str(row["id"]) for row in version_rows}
    check(
        actual_version_ids == expected_version_ids,
        f"formal_version_ids:{sorted(actual_version_ids)}",
        failures,
    )
    version_by_id = {str(row["id"]): row for row in version_rows}
    version_contracts = {
        version_a_confirmed: request_contracts.get(proposal_a, {}),
        version_a_edited: request_contracts.get(proposal_a, {}),
        version_b_confirmed: request_contracts.get(proposal_b, {}),
        version_a_restored: request_contracts.get(proposal_a, {}),
    }
    version_evidence: list[dict[str, Any]] = []
    for row in version_rows:
        snapshot = json_object(row["snapshot_json"])
        quality = snapshot_quality(snapshot)
        daily_completion = standard_two_day_daily_completion_evidence(snapshot)
        check(
            route_contract_ready(snapshot),
            f"version_route_contract_not_ready:{row['id']}",
            failures,
        )
        for error in compact_route_evidence_errors(snapshot):
            check(False, f"version_compact_route:{row['id']}:{error}", failures)
        for error in quality["errors"]:
            check(False, f"version_quality:{row['id']}:{error}", failures)
        for error in daily_completion["errors"]:
            check(
                False,
                f"version_daily_completion:{row['id']}:{error}",
                failures,
            )
        for error in explicit_every_day_meal_errors(
            snapshot,
            version_contracts.get(str(row["id"]), {}),
        ):
            check(
                False, f"version_explicit_every_day_meal:{row['id']}:{error}", failures
            )
        version_evidence.append(
            {
                "id": row["id"],
                "versionNumber": row["version_number"],
                "dayAnchorCounts": quality["dayAnchorCounts"],
                "materializedPoiCount": quality["materializedPoiCount"],
                "pendingLineage": lineage_fingerprint(pending_slots(snapshot)),
                "dailyCompletion": daily_completion,
            }
        )

    if version_a_confirmed in version_by_id and initial_projection_a:
        confirmed_a_snapshot = json_object(
            version_by_id[version_a_confirmed]["snapshot_json"]
        )
        confirmed_hard_a = hard_night_occurrence_evidence(
            confirmed_a_snapshot,
            request_contracts.get(proposal_a, {}),
        )
        check(
            business_signature(confirmed_a_snapshot)
            == business_signature(initial_projection_a),
            "confirmed_a_version_not_initial_proposal_core",
            failures,
        )
        check(
            not confirmed_hard_a["errors"]
            and hard_night_evidence_core(confirmed_hard_a)
            == hard_night_evidence_core(initial_hard_a),
            "confirmed_a_version_hard_night_core_mismatch",
            failures,
        )
    if version_b_confirmed in version_by_id and initial_projection_b:
        confirmed_b_snapshot = json_object(
            version_by_id[version_b_confirmed]["snapshot_json"]
        )
        confirmed_hard_b = hard_night_occurrence_evidence(
            confirmed_b_snapshot,
            request_contracts.get(proposal_b, {}),
        )
        check(
            business_signature(confirmed_b_snapshot)
            == business_signature(initial_projection_b),
            "confirmed_b_version_not_clicked_proposal_core",
            failures,
        )
        check(
            not final_b_snapshot
            or business_signature(confirmed_b_snapshot)
            == business_signature(final_b_snapshot),
            "confirmed_b_version_not_saved_proposal_core",
            failures,
        )
        check(
            not confirmed_hard_b["errors"]
            and hard_night_evidence_core(confirmed_hard_b)
            == hard_night_evidence_core(initial_hard_b),
            "confirmed_b_version_hard_night_core_mismatch",
            failures,
        )
    if version_a_edited in version_by_id and final_a_snapshot:
        edited_a_snapshot = json_object(
            version_by_id[version_a_edited]["snapshot_json"]
        )
        check(
            business_signature(edited_a_snapshot)
            == business_signature(final_a_snapshot),
            "edited_a_version_not_saved_proposal_core",
            failures,
        )

    if version_a_confirmed in version_by_id and version_a_edited in version_by_id:
        confirmed_snapshot = json_object(
            version_by_id[version_a_confirmed]["snapshot_json"]
        )
        edited_snapshot = json_object(version_by_id[version_a_edited]["snapshot_json"])
        confirmed_segments = snapshot_segments(confirmed_snapshot)
        edited_segments = snapshot_segments(edited_snapshot)
        check(
            len(confirmed_segments) == len(edited_segments)
            and bool(confirmed_segments),
            "a_edit_segment_cardinality_changed",
            failures,
        )
        if len(confirmed_segments) == len(edited_segments) and confirmed_segments:
            target_before = confirmed_segments[0]
            target_after = edited_segments[0]
            before_without_range = {
                key: value
                for key, value in target_before.items()
                if key not in {"startTime", "endTime"}
            }
            after_without_range = {
                key: value
                for key, value in target_after.items()
                if key not in {"startTime", "endTime"}
            }
            check(
                target_after["startTime"] == "08:00",
                "a_edit_target_start_not_0800",
                failures,
            )
            check(
                target_before["startTime"] != target_after["startTime"],
                "a_edit_target_start_unchanged",
                failures,
            )
            check(
                target_before["durationMinutes"] == target_after["durationMinutes"],
                "a_edit_target_duration_changed",
                failures,
            )
            check(
                before_without_range == after_without_range,
                "a_edit_target_identity_or_poi_changed",
                failures,
            )
            check(
                confirmed_segments[1:] == edited_segments[1:],
                "a_edit_non_target_segment_changed",
                failures,
            )

    if proposal_a in proposal_by_id and version_a_restored in version_by_id:
        saved_a_core = snapshot_core(
            json_object(proposal_by_id[proposal_a]["snapshot_json"])
        )
        restored_a_core = snapshot_core(
            json_object(version_by_id[version_a_restored]["snapshot_json"])
        )
        check(
            saved_a_core == restored_a_core,
            "restored_a_core_snapshot_mismatch",
            failures,
        )
        restored_hard_a = hard_night_occurrence_evidence(
            json_object(version_by_id[version_a_restored]["snapshot_json"]),
            request_contracts.get(proposal_a, {}),
        )
        check(
            not restored_hard_a["errors"]
            and hard_night_evidence_core(restored_hard_a)
            == hard_night_evidence_core(final_hard_a),
            "restored_a_hard_night_core_mismatch",
            failures,
        )

    patch_result_ids = {str(row["result_version_id"] or "") for row in patch_rows}
    check(
        len(patch_rows) == 4,
        f"formal_patch_count:{len(patch_rows)}",
        failures,
    )
    check(
        patch_result_ids == expected_version_ids,
        "patch_version_identity_mismatch",
        failures,
    )
    for row in patch_rows:
        check(
            str(row["validation_status"] or "") in {"accepted", "passed"},
            f"patch_not_accepted:{row['id']}",
            failures,
        )

    adoption_rows = connection.execute(
        "SELECT * FROM agent_choice_executions WHERE session_id = ? "
        "AND action = 'select_plan_proposal' AND status = 'succeeded' "
        "ORDER BY created_at",
        (session_id,),
    ).fetchall()
    adoption_versions = [str(row["result_version_id"] or "") for row in adoption_rows]
    check(
        len(adoption_rows) == 3,
        f"successful_activation_count:{len(adoption_rows)}",
        failures,
    )
    check(
        adoption_versions
        == [version_a_confirmed, version_b_confirmed, version_a_restored],
        f"activation_version_sequence:{adoption_versions}",
        failures,
    )
    turns_by_id = {turn["id"]: turn for turn in turns}
    expected_adoptions = [
        (proposal_a, version_a_confirmed),
        (proposal_b, version_b_confirmed),
        (proposal_a, version_a_restored),
    ]
    adoption_binding_evidence: list[dict[str, Any]] = []
    for index, row in enumerate(adoption_rows):
        proposal_id, expected_version_id = (
            expected_adoptions[index] if index < len(expected_adoptions) else ("", "")
        )
        proposal_row = proposal_by_id.get(proposal_id)
        binding_errors = (
            adoption_execution_binding_errors(
                row,
                proposal_row,
                turns_by_id,
                session_id=session_id,
                expected_version_id=expected_version_id,
            )
            if proposal_row is not None
            else [f"adoption_expected_proposal_missing:{row['id']}"]
        )
        if root is not None:
            check(
                str(row["source_user_turn_id"] or "")
                == str(root["source_user_turn_id"] or ""),
                f"adoption_root_source_user_mismatch:{row['id']}",
                failures,
            )
        source_user_turn = turns_by_id.get(str(row["source_user_turn_id"] or ""))
        check(
            isinstance(source_user_turn, dict)
            and source_user_turn.get("role") == "user",
            f"adoption_source_user_turn_invalid:{row['id']}",
            failures,
        )
        for error in binding_errors:
            check(False, error, failures)
        adoption_binding_evidence.append(
            {
                "id": str(row["id"]),
                "proposalId": proposal_id,
                "choiceId": str(row["choice_id"] or ""),
                "sourceTurnId": str(row["source_turn_id"] or ""),
                "sourceUserTurnId": str(row["source_user_turn_id"] or ""),
                "requestTurnId": str(row["request_turn_id"] or ""),
                "executionTurnId": str(row["execution_turn_id"] or ""),
                "resultVersionId": str(row["result_version_id"] or ""),
                "bindingErrors": binding_errors,
            }
        )

    edit_assistants: list[dict[str, Any]] = []
    if edit_user is None:
        check(False, "edit_user_turn_missing", failures)
    else:
        for turn in turns:
            if turn["turnIndex"] <= edit_user["turnIndex"]:
                continue
            if turn["role"] == "user":
                break
            if turn["role"] == "assistant" and turn["status"] == "active":
                edit_assistants.append(turn)
        check(
            len(edit_assistants) == 1,
            f"edit_active_assistant_count:{len(edit_assistants)}",
            failures,
        )
        for turn in edit_assistants:
            response = turn["response"]
            check(
                not CONTRADICTORY_EDIT_PATTERN.search(turn["content"]),
                f"edit_contradictory_assistant:{turn['id']}",
                failures,
            )
            check(
                response.get("mode") == "local_timeline_mutation"
                and response.get("terminalStatus") == "completed"
                and int(response.get("versionDelta") or 0) == 1
                and int(response.get("patchDelta") or 0) == 1
                and str(turn["itineraryVersionId"] or "") == version_a_edited,
                f"edit_assistant_not_single_success:{turn['id']}",
                failures,
            )
    browser_edit = json_object(journey.get("assistantEditEvidence"))
    browser_before = int(browser_edit.get("countBefore") or 0)
    browser_after = int(browser_edit.get("countAfter") or 0)
    browser_added = [str(item) for item in json_list(browser_edit.get("addedTexts"))]
    check(
        browser_after - browser_before == 1,
        f"browser_edit_assistant_delta:{browser_before}:{browser_after}",
        failures,
    )
    check(
        len(browser_added) == 1,
        f"browser_edit_added_text_count:{len(browser_added)}",
        failures,
    )
    check(
        not any(CONTRADICTORY_EDIT_PATTERN.search(text) for text in browser_added),
        "browser_edit_contradictory_reply",
        failures,
    )

    operation_budgets: list[dict[str, Any]] = []
    route_alternative_budgets: list[dict[str, Any]] = []
    for turn in turns:
        if turn["role"] != "assistant":
            continue
        counts = external_call_counts(turn["response"])
        if not any(counts.values()):
            continue
        operation_budgets.append({"turnId": turn["id"], **counts})
        check(
            counts["amapPlaceText"] <= 6,
            f"text_budget_exceeded:{turn['id']}:{counts['amapPlaceText']}",
            failures,
        )
        check(
            counts["amapRoute"] <= 8,
            f"route_budget_exceeded:{turn['id']}:{counts['amapRoute']}",
            failures,
        )
        check(
            counts["amapExternal"] <= 14,
            f"external_budget_exceeded:{turn['id']}:{counts['amapExternal']}",
            failures,
        )

    for proposal_id, row in proposal_by_id.items():
        evidence = route_alternative_budget_evidence(json_object(row["snapshot_json"]))
        route_alternative_budgets.append({"proposalId": proposal_id, **evidence})
        for error in json_list(evidence.get("errors")):
            check(False, f"proposal_route_alternative_budget:{proposal_id}:{error}", failures)

    social_link_evidence = social_link_database_evidence(connection, journey)
    for error in json_list(social_link_evidence.get("errors")):
        check(False, f"social_link_database:{error}", failures)

    view_resolutions: list[dict[str, Any]] = []
    internal_nonempty = 0
    for turn in turns:
        if turn["status"] == "internal_capability" and turn["content"]:
            internal_nonempty += 1
        for carrier in (turn["request"], turn["response"]):
            resolution = carrier.get("viewResolution")
            if isinstance(resolution, dict):
                view_resolutions.append(resolution)
    check(internal_nonempty == 0, "internal_capability_visible_content", failures)
    check(
        any(
            item.get("resolvedAction") == "edit_active_direction"
            and json_object(item.get("inputViewContext")).get("activeView")
            == "overview"
            and item.get("resolutionSource") == "server_validated_view_context"
            for item in view_resolutions
        ),
        "overview_edit_resolution_missing",
        failures,
    )
    check(
        any(
            item.get("resolvedAction") == "generate_new_direction"
            and item.get("resolutionSource") == "server_validated_opaque_choice"
            and str(item.get("planningSelectionRootTurnId") or "")
            and str(item.get("rootPortfolioId") or "")
            for item in view_resolutions
        ),
        "comparison_opaque_continuation_resolution_missing",
        failures,
    )

    report = {
        "passed": not failures,
        "failures": failures,
        "session": {
            "id": session_id,
            "activePlanId": plan_id,
            "activeVersionId": session["active_version_id"],
        },
        "deepSeekEndpointHost": normalized_deepseek_host,
        "allowedDeepSeekEndpointHost": TRUSTED_DEEPSEEK_ENDPOINT_HOST,
        "preflight": {
            "clarificationDimensions": actual_dimensions,
            "routeSafeDefaults": {
                "routeDecisionContract": route_default_contract,
                "editableDefaults": editable_defaults,
            },
            "batchCount": len(clarification_turns),
            "externalEvidenceBeforeCompletion": clarification_external_evidence,
            "choiceExecutions": clarification_choice_evidence,
            "manualNormalization": manual_normalization_evidence,
            "detourOptionIdentity": detour_option_identity_rows,
        },
        "directionRoot": {
            "id": root_id,
            "status": root["status"] if root is not None else None,
            "selectedProposalId": (
                root["selected_proposal_id"] if root is not None else None
            ),
            "expectedBaseVersionId": (
                root["expected_base_version_id"] if root is not None else None
            ),
        },
        "proposals": proposal_evidence,
        "directionProviderEvidence": direction_provider_evidence,
        "proposalOfferZeroWriteEvidence": offer_zero_write_evidence,
        "continuationEvidence": continuation_evidence,
        "hardNightDirections": {
            "a": {
                "initial": initial_hard_a,
                "savedProjection": saved_hard_a,
                "savedSnapshot": final_hard_a,
            },
            "b": {
                "initial": initial_hard_b,
                "savedSnapshot": final_hard_b,
            },
        },
        "versions": version_evidence,
        "writes": {
            "versionCount": len(version_rows),
            "patchCount": len(patch_rows),
            "versionIds": [str(row["id"]) for row in version_rows],
            "successfulActivationCount": len(adoption_rows),
            "activationVersionSequence": adoption_versions,
            "adoptionBindings": adoption_binding_evidence,
        },
        "editAssistantEvidence": {
            "serverTurnIds": [turn["id"] for turn in edit_assistants],
            "serverContents": [turn["content"] for turn in edit_assistants],
            "browser": browser_edit,
        },
        "operationBudgets": operation_budgets,
        "routeAlternativeBudgets": route_alternative_budgets,
        "socialLinkEvidence": social_link_evidence,
        "viewResolutionCount": len(view_resolutions),
        "browserRequests": {
            "stream": journey.get("streamRequestCount"),
            "save": journey.get("saveRequestCount"),
        },
    }
    connection.close()
    return report


def main() -> int:
    args = parse_args()
    normalized_deepseek_host, endpoint_failure = deepseek_endpoint_preflight(
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
        json.loads(args.run_summary.read_text(encoding="utf-8"))
        if args.run_summary is not None
        else {}
    )
    commit_errors = artifact_commit_errors(
        journey=journey,
        run_summary=run_summary,
        expected_git_commit=args.expected_git_commit,
    )
    if commit_errors:
        report = {
            "passed": False,
            "failures": commit_errors,
            "gitCommit": str(args.expected_git_commit or "").strip().lower(),
            "artifactCommitBinding": {
                "journeyGitCommit": str(journey.get("gitCommit") or ""),
                "runSummaryGitCommit": str(run_summary.get("gitCommit") or ""),
            },
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 1
    report = verify_database(
        args.database,
        journey,
        deepseek_endpoint_host=normalized_deepseek_host,
    )
    report["gitCommit"] = str(args.expected_git_commit).strip().lower()
    report["artifactCommitBinding"] = {
        "journeyGitCommit": str(journey.get("gitCommit") or ""),
        "runSummaryGitCommit": str(run_summary.get("gitCommit") or ""),
    }
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
