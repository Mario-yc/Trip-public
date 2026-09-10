from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


USER_REQUEST = (
    "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，"
    "每天参观一所不同的985高校，每天中午品尝不同的当地网红美食，"
    "每天晚上去当日附近公共开放的公园或滨水夜景散步"
)
AMAP_ID_RE = re.compile(r"^B[0-9A-Z]{8,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
MAX_ADJACENT_POI_METERS = 8_000
MAX_TRANSIT_ROUTE_SECONDS = 60 * 60
CONTROLLED_NIGHT_RE = re.compile(
    r"欢乐谷|环球影城|主题乐园|摩天轮|中央电视塔|奥林匹克塔|中国尊|中信大厦"
)
CAMPUS_PROVIDER_RE = re.compile(
    r"科教文化|学校|高等院校|campus|education|university", re.IGNORECASE
)
CAMPUS_NON_ENTITY_RE = re.compile(
    r"附近|周边|商场|购物|餐厅|酒店|公寓|地铁|公交|医院|科技园|产业园"
)
CAMPUS_SUBENTITY_RE = re.compile(
    r"校本部|本部|校区|校园|校门|东门|西门|南门|北门|工字厅|主楼|教学楼|图书馆|礼堂|医学部|学院"
)
NIGHT_PLACEHOLDER_RE = re.compile(
    r"待补|待定|附近范围|周边范围|候选|placeholder|pending", re.IGNORECASE
)
NON_PUBLIC_NIGHT_FACILITY_RE = re.compile(r"观景台|观景平台|瞭望台|观景塔|展望塔")
PUBLIC_NIGHT_RE = re.compile(
    r"公园|河|湖|滨水|水岸|河畔|湖畔|步道|湿地|园林|什刹海|后海",
    re.IGNORECASE,
)
QUALIFICATION_ASSET = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "src"
    / "services"
    / "qualification_evidence_moe_985_v1.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def check(condition: bool, code: str, failures: list[str]) -> None:
    if not condition:
        failures.append(code)


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


def minutes(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if match is None:
        return -1
    return int(match.group(1)) * 60 + int(match.group(2))


def fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_entity(value: Any) -> str:
    return re.sub(r"[\s·•・()（）\[\]【】]", "", str(value or "")).casefold()


def normalize_locality(value: Any) -> str:
    return re.sub(
        r"(?:特别行政区|自治区|自治州|地区|盟|市)$",
        "",
        str(value or "").strip(),
    ).casefold()


def campus_985_binding_valid(segment: dict[str, Any]) -> bool:
    binding = json_object(segment.get("qualificationBinding"))
    if not QUALIFICATION_ASSET.is_file():
        return False
    raw = QUALIFICATION_ASSET.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        binding.get("schemaVersion") != "entity-qualification-binding-v1"
        or binding.get("qualificationScheme") != "moe_project_classification"
        or binding.get("qualificationValue") != "985"
        or str(binding.get("qualificationEvidenceFingerprint") or "")
        != hashlib.sha256(raw).hexdigest()
        or normalize_locality(binding.get("locality")) != normalize_locality("北京")
    ):
        return False
    canonical_name = str(binding.get("canonicalName") or "").strip()
    entity = next(
        (
            item
            for item in evidence.get("entities") or []
            if isinstance(item, dict)
            and str(item.get("canonicalName") or "").strip() == canonical_name
            and normalize_locality(item.get("locality")) == normalize_locality("北京")
        ),
        None,
    )
    if entity is None:
        return False
    evidence_fingerprint = str(binding.get("qualificationEvidenceFingerprint") or "")
    expected_entity_fingerprint = fingerprint(
        {
            "qualificationEvidenceFingerprint": evidence_fingerprint,
            "canonicalName": canonical_name,
            "locality": str(entity.get("locality") or "").strip(),
            "aliases": sorted(
                str(value) for value in entity.get("aliases") or [] if str(value)
            ),
        }
    )
    if (
        str(binding.get("evidenceEntityFingerprint") or "")
        != expected_entity_fingerprint
    ):
        return False
    material = {
        key: value for key, value in binding.items() if key != "bindingFingerprint"
    }
    if SHA256_RE.fullmatch(str(binding.get("bindingFingerprint") or "")) is None or str(
        binding.get("bindingFingerprint") or ""
    ) != fingerprint(material):
        return False
    candidate_name = normalize_entity(segment.get("poiName"))
    provider_classification = " ".join(
        str(segment.get(key) or "") for key in ("poiType", "poiCategory")
    )
    if (
        not candidate_name
        or CAMPUS_PROVIDER_RE.search(provider_classification) is None
        or CAMPUS_NON_ENTITY_RE.search(candidate_name) is not None
    ):
        return False
    accepted_names = {
        normalize_entity(value)
        for value in [entity.get("canonicalName"), *(entity.get("aliases") or [])]
        if value
    }
    if candidate_name in accepted_names:
        return True
    for accepted in accepted_names:
        if not accepted or not candidate_name.startswith(accepted):
            continue
        suffix = candidate_name[len(accepted) :]
        if suffix and CAMPUS_SUBENTITY_RE.search(suffix) is not None:
            return True
    return False


def haversine_meters(left: dict[str, Any], right: dict[str, Any]) -> float | None:
    try:
        lat1 = float(left.get("latitude"))
        lon1 = float(left.get("longitude"))
        lat2 = float(right.get("latitude"))
        lon2 = float(right.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (lat1, lon1, lat2, lon2)):
        return None
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    h = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def normalized_intent(segment: dict[str, Any]) -> str:
    semantic = json_object(segment.get("semanticMetadata"))
    poi = json_object(segment.get("poi"))
    explicit = str(semantic.get("intentType") or poi.get("intentType") or "")
    category = str(poi.get("category") or "")
    name = str(poi.get("name") or "")
    if explicit == "meal" or category == "food":
        return "meal"
    if explicit == "park" or "公园" in name:
        return "park"
    if (
        explicit == "campus"
        or re.search(r"campus|education", category)
        or re.search(r"大学|学院|校区", name)
    ):
        return "campus"
    return explicit or str(segment.get("kind") or "")


def itinerary_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for day_index, raw_day in enumerate(json_list(snapshot.get("days"))):
        day = json_object(raw_day)
        day_number = int(day.get("dayNumber") or day_index + 1)
        days.append({"dayNumber": day_number, "date": str(day.get("date") or "")})
        for segment_index, raw_segment in enumerate(json_list(day.get("segments"))):
            segment = json_object(raw_segment)
            semantic = json_object(segment.get("semanticMetadata"))
            schedule_constraints = json_object(semantic.get("scheduleConstraints"))
            poi = json_object(segment.get("poi"))
            segments.append(
                {
                    "dayNumber": day_number,
                    "segmentOrder": segment_index + 1,
                    "segmentId": str(segment.get("id") or ""),
                    "intentType": normalized_intent(segment),
                    "startTime": str(segment.get("startTime") or ""),
                    "endTime": str(segment.get("endTime") or ""),
                    "poiName": str(poi.get("name") or ""),
                    "amapId": str(poi.get("amapId") or "").upper(),
                    "poiSource": str(poi.get("source") or ""),
                    "latitude": poi.get("latitude"),
                    "longitude": poi.get("longitude"),
                    "planningSlotId": str(semantic.get("planningSlotId") or ""),
                    "poiType": str(poi.get("type") or ""),
                    "poiCategory": str(poi.get("category") or ""),
                    "qualificationBinding": json_object(
                        semantic.get("qualificationBinding")
                        or schedule_constraints.get("qualificationBinding")
                    ),
                    "groundingStatus": str(
                        semantic.get("groundingStatus")
                        or poi.get("groundingStatus")
                        or ""
                    ),
                }
            )
    days.sort(key=lambda item: item["dayNumber"])
    return {"days": days, "segments": segments}


def journey_semantic_errors(evidence: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    days = [json_object(item) for item in json_list(evidence.get("days"))]
    segments = [json_object(item) for item in json_list(evidence.get("segments"))]
    if [str(day.get("date") or "") for day in days] != ["2026-10-01", "2026-10-02"]:
        errors.append(f"{label}:resolved_dates_not_two_day_request")
    campuses = [
        segment for segment in segments if segment.get("intentType") == "campus"
    ]
    campus_days = [int(segment.get("dayNumber") or 0) for segment in campuses]
    if sorted(campus_days) != [1, 2]:
        errors.append(f"{label}:campus_not_distributed_across_both_days")
    if len({str(segment.get("amapId") or "") for segment in campuses}) != len(campuses):
        errors.append(f"{label}:campus_identity_reused_across_days")
    if any(not campus_985_binding_valid(segment) for segment in campuses):
        errors.append(f"{label}:campus_985_qualification_invalid")
    meals = [segment for segment in segments if segment.get("intentType") == "meal"]
    meal_days = [int(segment.get("dayNumber") or 0) for segment in meals]
    if sorted(meal_days) != [1, 2]:
        errors.append(f"{label}:explicit_daily_meal_not_distributed_across_both_days")
    if len({str(segment.get("amapId") or "") for segment in meals}) != len(meals):
        errors.append(f"{label}:meal_identity_reused_across_days")
    for meal in meals:
        start = minutes(str(meal.get("startTime") or ""))
        if not 11 * 60 <= start < 14 * 60:
            errors.append(f"{label}:explicit_meal_not_at_noon")
    nights = [
        segment
        for segment in segments
        if segment.get("intentType") in {"park", "night_view"}
    ]
    night_days = [int(segment.get("dayNumber") or 0) for segment in nights]
    if sorted(night_days) != [1, 2]:
        errors.append(f"{label}:explicit_daily_night_not_distributed_across_both_days")
    if len({str(segment.get("amapId") or "") for segment in nights}) != len(nights):
        errors.append(f"{label}:night_identity_reused_across_days")
    for night in nights:
        if minutes(str(night.get("startTime") or "")) < 17 * 60:
            errors.append(f"{label}:explicit_night_not_in_evening")
        if CONTROLLED_NIGHT_RE.search(str(night.get("poiName") or "")):
            errors.append(f"{label}:night_not_public_open_experience")
        night_name = str(night.get("poiName") or "").strip()
        public_evidence = " ".join(
            str(night.get(key) or "") for key in ("poiName", "poiType", "poiCategory")
        )
        if (
            not night_name
            or NIGHT_PLACEHOLDER_RE.search(night_name) is not None
            or NON_PUBLIC_NIGHT_FACILITY_RE.search(public_evidence) is not None
            or str(night.get("groundingStatus") or "") != "verified_amap"
            or PUBLIC_NIGHT_RE.search(public_evidence) is None
        ):
            errors.append(f"{label}:night_public_experience_invalid")
    for meal in meals:
        for night in nights:
            if int(meal.get("dayNumber") or 0) == int(
                night.get("dayNumber") or 0
            ) and minutes(str(meal.get("startTime") or "")) >= minutes(
                str(night.get("startTime") or "")
            ):
                errors.append(f"{label}:noon_meal_after_evening_experience")
    for day_number in (1, 2):
        day_segments = sorted(
            (
                segment
                for segment in segments
                if int(segment.get("dayNumber") or 0) == day_number
            ),
            key=lambda segment: int(segment.get("segmentOrder") or 0),
        )
        for left, right in zip(day_segments, day_segments[1:]):
            distance = haversine_meters(left, right)
            if distance is None:
                errors.append(f"{label}:adjacent_poi_coordinates_missing")
            elif distance > MAX_ADJACENT_POI_METERS:
                errors.append(f"{label}:adjacent_poi_distance_exceeds_quality_floor")
    for segment in segments:
        if AMAP_ID_RE.fullmatch(str(segment.get("amapId") or "")) is None:
            errors.append(f"{label}:segment_amap_identity_missing")
        if segment.get("poiSource") != "amap-place-search":
            errors.append(f"{label}:segment_not_grounded_by_amap")
    return sorted(set(errors))


def route_contract_errors(snapshot: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    contract = json_object(snapshot.get("routeDecisionContract"))
    if contract.get("status") != "ready" or json_list(contract.get("missingFields")):
        errors.append(f"{label}:route_contract_not_ready")
    mobility = json_object(contract.get("mobilityProfile"))
    if mobility.get("source") != "controller_semantic_choice":
        errors.append(f"{label}:mobility_clarification_not_attributed")
    if contract.get("detourToleranceSource") != "controller_semantic_choice":
        errors.append(f"{label}:detour_clarification_not_attributed")
    expected_mobility_fields = {
        "source",
        "walkingPenaltyMinutesPerKm",
        "transferPenaltyMinutes",
        "waitTimeMultiplier",
        "riskPenaltyMultiplier",
    }
    try:
        normalized_weights = [
            float(mobility[field]) for field in expected_mobility_fields - {"source"}
        ]
    except (KeyError, TypeError, ValueError):
        normalized_weights = []
    provenance = json_object(contract.get("provenance"))
    if (
        set(mobility) != expected_mobility_fields
        or len(normalized_weights) != 4
        or any(not math.isfinite(value) or value < 0 for value in normalized_weights)
        or provenance.get("transportMode")
        not in {"transit", "driving", "walking", "bicycling"}
    ):
        errors.append(f"{label}:mobility_clarification_not_typed")
    detour = json_object(contract.get("detourTolerance"))
    try:
        detour_delta = float(detour.get("maxGeneralizedCostDelta"))
        detour_ratio = float(detour.get("maxDetourRatio"))
    except (TypeError, ValueError):
        detour_delta = -1
        detour_ratio = -1
    if detour_delta <= 0 or not 0 <= detour_ratio <= 1:
        errors.append(f"{label}:detour_clarification_not_typed")
    return errors


def route_quality_errors(snapshot: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    contract = json_object(snapshot.get("routeDecisionContract"))
    audit = json_object(snapshot.get("simpleOpenRouteAssignment"))
    if audit.get("schemaVersion") != "simple-open-route-evidence-v2":
        errors.append(f"{label}:route_evidence_schema_invalid")
    contract_fingerprint = str(contract.get("fingerprint") or "")
    audit_fingerprint = str(audit.get("routeContractFingerprint") or "")
    if (
        SHA256_RE.fullmatch(contract_fingerprint) is None
        or audit_fingerprint != contract_fingerprint
    ):
        errors.append(f"{label}:route_contract_fingerprint_mismatch")
    if audit.get("routeCoverageComplete") is not True:
        errors.append(f"{label}:route_coverage_incomplete")
    if audit.get("adjacentLegCompliance") != "verified":
        errors.append(f"{label}:route_adjacent_leg_not_verified")
    if audit.get("topologyCompliance") != "verified":
        errors.append(f"{label}:route_topology_not_verified")
    expected = [json_object(item) for item in json_list(audit.get("expectedPairs"))]
    verified = [json_object(item) for item in json_list(audit.get("verifiedPairs"))]
    expected_identities = [
        (str(item.get("fromAmapId") or ""), str(item.get("toAmapId") or ""))
        for item in expected
    ]
    verified_identities = [
        (str(item.get("fromAmapId") or ""), str(item.get("toAmapId") or ""))
        for item in verified
    ]
    actual_identities = adjacent_pairs(snapshot, identity="amap")
    if (
        len(actual_identities) != 4
        or len(set(actual_identities)) != 4
        or any(
            AMAP_ID_RE.fullmatch(from_id) is None or AMAP_ID_RE.fullmatch(to_id) is None
            for from_id, to_id in actual_identities
        )
        or expected_identities != actual_identities
        or verified_identities != actual_identities
    ):
        errors.append(f"{label}:route_pair_identity_mismatch")
    adjacent_contract = json_object(contract.get("adjacentLegConstraint"))
    try:
        contract_limit_seconds = (
            int(adjacent_contract.get("maxProviderTravelMinutes")) * 60
        )
    except (TypeError, ValueError):
        contract_limit_seconds = MAX_TRANSIT_ROUTE_SECONDS
    duration_limit = min(
        MAX_TRANSIT_ROUTE_SECONDS,
        contract_limit_seconds
        if contract_limit_seconds > 0
        else MAX_TRANSIT_ROUTE_SECONDS,
    )
    if any(
        not isinstance(item.get("durationSeconds"), (int, float))
        or isinstance(item.get("durationSeconds"), bool)
        or not math.isfinite(float(item.get("durationSeconds") or 0))
        or float(item.get("durationSeconds") or 0) <= 0
        or float(item.get("durationSeconds") or 0) > duration_limit
        or not isinstance(item.get("distanceMeters"), (int, float))
        or isinstance(item.get("distanceMeters"), bool)
        or not math.isfinite(float(item.get("distanceMeters") or 0))
        or float(item.get("distanceMeters") or 0) <= 0
        or str(item.get("transportMode") or "") not in {"transit", "public_transit"}
        for item in verified
    ):
        errors.append(f"{label}:selected_route_exceeds_quality_floor")
    return sorted(set(errors))


def flattened_segments(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    direct = [json_object(item) for item in json_list(snapshot.get("segments"))]
    if direct:
        return direct
    return [
        json_object(item)
        for item in json_list(itinerary_evidence(snapshot).get("segments"))
    ]


def adjacent_pairs(snapshot: dict[str, Any], *, identity: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    segments = flattened_segments(snapshot)
    identity_key = "amapId" if identity == "amap" else "segmentId"
    for day_number in sorted(
        {int(segment.get("dayNumber") or 0) for segment in segments}
    ):
        day_segments = sorted(
            (
                segment
                for segment in segments
                if int(segment.get("dayNumber") or 0) == day_number
            ),
            key=lambda segment: int(segment.get("segmentOrder") or 0),
        )
        for left, right in zip(day_segments, day_segments[1:]):
            left_id = str(left.get(identity_key) or "")
            right_id = str(right.get(identity_key) or "")
            if identity == "amap":
                left_id = left_id.upper()
                right_id = right_id.upper()
            pairs.append((left_id, right_id))
    return pairs


def amap_lineage_errors(
    snapshot: dict[str, Any], events: list[dict[str, Any]], label: str
) -> list[str]:
    materialized_pairs = {
        (
            str(segment.get("amapId") or "").upper(),
            str(segment.get("planningSlotId") or ""),
        )
        for segment in flattened_segments(snapshot)
        if str(segment.get("poiSource") or "") == "amap-place-search"
        and AMAP_ID_RE.fullmatch(str(segment.get("amapId") or "").upper())
    }
    bound_pairs: set[tuple[str, str]] = set()
    errors: list[str] = []
    for event in events:
        if (
            event.get("type") != "simple_open_tool_call"
            or event.get("providerName") != "amap-place-search"
        ):
            continue
        metadata = json_object(event.get("metadata"))
        selected_id = str(metadata.get("selectedAmapId") or "").upper()
        slot_id = str(metadata.get("slotKey") or "")
        strict_success = bool(
            event.get("status") == "completed"
            and event.get("fallbackUsed") is False
            and metadata.get("providerOutcome") == "success"
            and metadata.get("cacheHit") is False
            and isinstance(metadata.get("resultCount"), int)
            and not isinstance(metadata.get("resultCount"), bool)
            and int(metadata.get("resultCount") or 0) > 0
            and re.fullmatch(
                r"[0-9a-fA-F]{16}", str(metadata.get("queryFingerprint") or "")
            )
            and AMAP_ID_RE.fullmatch(selected_id)
            and slot_id
        )
        if not strict_success:
            continue
        selected_pair = (selected_id, slot_id)
        if selected_pair not in materialized_pairs:
            errors.append(f"{label}:live_amap_search_not_materialized")
            continue
        bound_pairs.add(selected_pair)
    if not materialized_pairs or materialized_pairs - bound_pairs:
        errors.append(f"{label}:materialized_amap_identity_without_live_search")
    return sorted(set(errors))


def verify_database(
    database: Path, journey: dict[str, Any], endpoint_host: str
) -> dict[str, Any]:
    failures: list[str] = []
    check(
        endpoint_host.strip().lower() == "api.deepseek.com",
        "deepseek_endpoint_not_official",
        failures,
    )
    check(
        journey.get("schemaVersion") == "trip-two-day-quality-live-v3",
        "journey_schema_invalid",
        failures,
    )
    check(
        journey.get("userRequest") == USER_REQUEST,
        "journey_user_request_changed",
        failures,
    )
    session_id = str(journey.get("sessionId") or "")
    proposal_id = str(journey.get("proposalId") or "")
    active_version_id = str(journey.get("activeVersionId") or "")
    check(
        bool(session_id and proposal_id and active_version_id),
        "journey_identity_missing",
        failures,
    )
    clarification = json_object(journey.get("clarification"))
    initial_clarification = json_object(clarification.get("initial"))
    resolved_clarification = json_object(clarification.get("resolved"))
    dimensions = [str(item) for item in json_list(clarification.get("dimensions"))]
    selections = [
        json_object(item) for item in json_list(clarification.get("selections"))
    ]
    check(
        len(json_list(initial_clarification.get("awaitingCheckpointIds"))) == 1
        and initial_clarification.get("offeredSubmissionChoiceCount") == 1,
        "browser_dynamic_clarification_not_observed",
        failures,
    )
    check(
        not json_list(resolved_clarification.get("awaitingCheckpointIds"))
        and resolved_clarification.get("offeredSubmissionChoiceCount") == 0,
        "browser_clarification_not_resolved",
        failures,
    )
    check(
        int(clarification.get("questionCount") or 0)
        == len(dimensions)
        == len(selections)
        and 1 <= len(dimensions) <= 3
        and len(set(dimensions)) == len(dimensions)
        and {
            "route_decision.mobility_profile",
            "route_decision.detour_tolerance",
        }.issubset(set(dimensions)),
        "browser_dynamic_clarification_dimensions_invalid",
        failures,
    )
    check(
        int(clarification.get("freeTextQuestionCount") or 0) >= 1
        and int(clarification.get("inlineInputCount") or 0)
        == int(clarification.get("freeTextQuestionCount") or 0),
        "browser_inline_clarification_input_missing",
        failures,
    )
    manual_selections = [
        item for item in selections if item.get("submissionMode") == "manual_value"
    ]
    check(
        len(manual_selections) == 1
        and manual_selections[0].get("dimensionId") == "route_decision.mobility_profile"
        and bool(str(manual_selections[0].get("manualValue") or "").strip()),
        "browser_manual_clarification_not_exercised",
        failures,
    )
    submission_evidence = json_object(clarification.get("submissionEvidence"))
    check(
        submission_evidence.get("checkpointId") == clarification.get("checkpointId")
        and submission_evidence.get("checkpointFingerprint")
        == clarification.get("checkpointFingerprint")
        and submission_evidence.get("sourceAssistantTurnId")
        == clarification.get("sourceAssistantTurnId")
        and submission_evidence.get("assistantStatus") == "active"
        and submission_evidence.get("executionStatus") == "succeeded",
        "browser_clarification_submission_identity_invalid",
        failures,
    )
    for error in journey_semantic_errors(
        json_object(journey.get("proposalEvidence")), "browser_proposal"
    ):
        check(False, error, failures)
    for error in route_quality_errors(
        json_object(journey.get("proposalEvidence")), "browser_proposal"
    ):
        check(False, error, failures)
    for error in journey_semantic_errors(
        json_object(journey.get("adoptedEvidence")), "browser_adopted"
    ):
        check(False, error, failures)
    for error in route_quality_errors(
        json_object(journey.get("adoptedEvidence")), "browser_adopted"
    ):
        check(False, error, failures)

    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        session = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return {
                "passed": False,
                "failures": sorted(set([*failures, "session_missing"])),
            }
        check(
            str(session["active_version_id"] or "") == active_version_id,
            "active_version_mismatch",
            failures,
        )
        plan_id = str(session["active_plan_id"] or "")

        turns = connection.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,),
        ).fetchall()
        user_turns = [row for row in turns if row["role"] == "user"]
        check(
            len(user_turns) == 3,
            f"unexpected_user_turn_count:{len(user_turns)}",
            failures,
        )
        check(
            bool(user_turns) and str(user_turns[0]["content"] or "") == USER_REQUEST,
            "persisted_user_request_changed",
            failures,
        )

        response_objects: list[dict[str, Any]] = []
        clarification_turns: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        for row in turns:
            response = json_object(row["agent_response_json"])
            response_objects.extend(walk_objects(response))
            checkpoint = json_object(response.get("clarificationCheckpoint"))
            if (
                row["role"] == "assistant"
                and checkpoint.get("schemaVersion") == "clarification-checkpoint-v2"
                and checkpoint.get("status") == "awaiting_answer"
            ):
                clarification_turns.append((row, checkpoint))
        check(
            len(clarification_turns) == 1,
            f"persisted_clarification_turn_count:{len(clarification_turns)}",
            failures,
        )
        source_turn = clarification_turns[0][0] if clarification_turns else None
        source_checkpoint = clarification_turns[0][1] if clarification_turns else {}
        source_questions = [
            json_object(item) for item in json_list(source_checkpoint.get("questions"))
        ]
        check(
            source_turn is not None
            and str(source_turn["id"])
            == str(clarification.get("sourceAssistantTurnId") or "")
            and str(source_checkpoint.get("checkpointId") or "")
            == str(clarification.get("checkpointId") or "")
            and str(source_checkpoint.get("fingerprint") or "")
            == str(clarification.get("checkpointFingerprint") or "")
            and [str(item.get("dimensionId") or "") for item in source_questions]
            == dimensions,
            "persisted_clarification_source_identity_invalid",
            failures,
        )

        clarification_rows = connection.execute(
            "SELECT * FROM agent_choice_executions WHERE session_id = ? AND action = 'submit_clarification_batch'",
            (session_id,),
        ).fetchall()
        check(
            len(clarification_rows) == 1,
            f"clarification_choice_execution_count:{len(clarification_rows)}",
            failures,
        )
        clarification_row = clarification_rows[0] if clarification_rows else None
        clarification_outcome = (
            json_object(clarification_row["outcome_json"])
            if clarification_row is not None
            else {}
        )
        check(
            clarification_row is not None
            and clarification_row["status"] == "succeeded"
            and str(clarification_row["source_turn_id"] or "")
            == str(clarification.get("sourceAssistantTurnId") or "")
            and str(clarification_row["checkpoint_fingerprint"] or "")
            == str(clarification.get("checkpointFingerprint") or "")
            and clarification_row["result_version_id"] is None
            and all(
                int(clarification_outcome.get(key) or 0) == 0
                for key in ("versionDelta", "patchDelta", "routeWriteDelta")
            ),
            "clarification_choice_execution_invalid",
            failures,
        )

        clarification_request: dict[str, Any] = {}
        if clarification_row is not None:
            request_turn = connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                (str(clarification_row["request_turn_id"] or ""),),
            ).fetchone()
            if request_turn is not None:
                clarification_request = json_object(request_turn["agent_request_json"])
        resolved_checkpoint = json_object(
            clarification_request.get("clarificationCheckpoint")
        )
        resolved_answers = [
            json_object(item)
            for item in json_list(resolved_checkpoint.get("resolvedAnswers"))
        ]
        answer_sources = {
            str(item.get("dimensionId") or ""): str(item.get("source") or "")
            for item in resolved_answers
        }
        mobility_answer = next(
            (
                json_object(item.get("semanticValue")).get("mobilityProfile")
                for item in resolved_answers
                if str(item.get("dimensionId") or "")
                == "route_decision.mobility_profile"
            ),
            None,
        )
        check(
            resolved_checkpoint.get("status") == "answered"
            and set(answer_sources) == set(dimensions)
            and answer_sources.get("route_decision.mobility_profile")
            == "free_text_normalized"
            and answer_sources.get("route_decision.detour_tolerance")
            == "structured_option",
            "persisted_clarification_resolution_invalid",
            failures,
        )
        check(
            isinstance(mobility_answer, dict)
            and set(mobility_answer) == {"transportMode", "paceClass"}
            and mobility_answer.get("transportMode")
            in {"transit", "public_transit", "driving", "walking", "bicycling"}
            and mobility_answer.get("paceClass")
            in {"relaxed", "standard", "intensive"},
            "persisted_manual_mobility_semantics_invalid",
            failures,
        )
        normalization_audit = json_object(
            clarification_request.get("clarificationManualNormalization")
        )
        normalization_transport = json_object(normalization_audit.get("transport"))
        check(
            normalization_audit.get("schemaVersion")
            == "clarification-batch-normalization-audit-v1"
            and normalization_audit.get("providerName") == "deepseek"
            and normalization_audit.get("attemptCount") == 1
            and normalization_audit.get("retryCount") == 0
            and normalization_audit.get("outboundDimensionIds")
            == ["route_decision.mobility_profile"]
            and normalization_transport.get("callKind")
            == "clarification_batch_normalization"
            and normalization_transport.get("captureState") == "completed"
            and normalization_transport.get("providerInvoked") is True
            and int(normalization_transport.get("httpStatus") or 0) == 200,
            "real_deepseek_manual_normalization_evidence_missing",
            failures,
        )

        deepseek_events = [
            item
            for item in response_objects
            if item.get("type") == "controller_full_succeeded"
            and item.get("providerName") == "DeepSeekAgentProvider"
            and item.get("fallbackUsed") is False
        ]
        amap_events = [
            item
            for item in response_objects
            if item.get("type") == "simple_open_tool_call"
            and item.get("providerName") == "amap-place-search"
            and item.get("status") == "completed"
            and item.get("fallbackUsed") is False
        ]
        check(
            bool(deepseek_events), "real_deepseek_controller_evidence_missing", failures
        )
        check(bool(amap_events), "real_amap_search_evidence_missing", failures)
        check(
            not any(
                re.search(
                    r"mock|fixture|fake",
                    str(item.get("providerName") or ""),
                    re.IGNORECASE,
                )
                for item in response_objects
            ),
            "mock_provider_evidence_detected",
            failures,
        )

        proposal = connection.execute(
            "SELECT * FROM agent_plan_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        check(proposal is not None, "proposal_missing", failures)
        proposal_snapshot: dict[str, Any] = {}
        if proposal is not None:
            proposal_snapshot = json_object(proposal["snapshot_json"])
            verifier = json_object(proposal["verifier_json"])
            check(
                verifier.get("passed") is True, "proposal_verifier_not_passed", failures
            )
            for error in journey_semantic_errors(
                itinerary_evidence(proposal_snapshot), "database_proposal"
            ):
                check(False, error, failures)
            for error in route_contract_errors(proposal_snapshot, "database_proposal"):
                check(False, error, failures)
            for error in route_quality_errors(proposal_snapshot, "database_proposal"):
                check(False, error, failures)
            for error in amap_lineage_errors(
                proposal_snapshot, amap_events, "database_proposal"
            ):
                check(False, error, failures)

        version_rows = connection.execute(
            "SELECT * FROM itinerary_versions WHERE session_id = ? ORDER BY version_number",
            (session_id,),
        ).fetchall()
        check(
            len(version_rows) == 1,
            f"version_count_not_exactly_one:{len(version_rows)}",
            failures,
        )
        version = next(
            (row for row in version_rows if str(row["id"]) == active_version_id), None
        )
        check(version is not None, "active_version_row_missing", failures)
        version_snapshot: dict[str, Any] = {}
        if version is not None:
            version_snapshot = json_object(version["snapshot_json"])
            for error in journey_semantic_errors(
                itinerary_evidence(version_snapshot), "database_version"
            ):
                check(False, error, failures)
            for error in route_contract_errors(version_snapshot, "database_version"):
                check(False, error, failures)
            for error in route_quality_errors(version_snapshot, "database_version"):
                check(False, error, failures)
            for error in amap_lineage_errors(
                version_snapshot, amap_events, "database_version"
            ):
                check(False, error, failures)

        patch_rows = connection.execute(
            "SELECT * FROM itinerary_patches WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        check(
            len(patch_rows) == 1,
            f"patch_count_not_exactly_one:{len(patch_rows)}",
            failures,
        )
        check(
            bool(patch_rows)
            and str(patch_rows[0]["result_version_id"] or "") == active_version_id,
            "patch_result_version_mismatch",
            failures,
        )

        adoption_rows = connection.execute(
            "SELECT * FROM agent_choice_executions WHERE session_id = ? AND action = 'select_plan_proposal' ORDER BY created_at",
            (session_id,),
        ).fetchall()
        check(
            len(adoption_rows) == 1,
            f"adoption_count_not_exactly_one:{len(adoption_rows)}",
            failures,
        )
        check(
            bool(adoption_rows)
            and adoption_rows[0]["status"] == "succeeded"
            and str(adoption_rows[0]["result_version_id"] or "") == active_version_id,
            "adoption_not_committed_to_active_version",
            failures,
        )
        continuation_request_turn_ids = {
            str(row["request_turn_id"] or "")
            for row in [clarification_row, adoption_rows[0] if adoption_rows else None]
            if row is not None
        }
        check(
            len(user_turns) == 3
            and continuation_request_turn_ids
            == {str(row["id"]) for row in user_turns[1:]},
            "continuation_user_turn_identity_mismatch",
            failures,
        )

        route_rows = connection.execute(
            "SELECT * FROM route_options WHERE plan_id = ? ORDER BY sort_order",
            (plan_id,),
        ).fetchall()
        check(bool(route_rows), "persisted_route_options_missing", failures)
        selected_route_rows = [
            row for row in route_rows if int(row["is_selected"] or 0) == 1
        ]
        actual_segment_pairs = adjacent_pairs(version_snapshot, identity="segment")
        persisted_selected_pairs = [
            (
                str(row["from_segment_id"] or ""),
                str(row["to_segment_id"] or ""),
            )
            for row in selected_route_rows
        ]
        check(
            bool(route_rows)
            and all(
                "amap" in str(row["provider"] or "").lower()
                and int(row["distance_meters"] or 0) > 0
                and int(row["duration_seconds"] or 0) > 0
                for row in route_rows
            ),
            "persisted_route_provider_evidence_invalid",
            failures,
        )
        check(
            len(selected_route_rows) == 4
            and all(
                str(row["transport_mode"] or "") in {"transit", "public_transit"}
                and 0 < int(row["duration_seconds"] or 0) <= MAX_TRANSIT_ROUTE_SECONDS
                for row in selected_route_rows
            ),
            "persisted_selected_routes_exceed_quality_floor",
            failures,
        )
        check(
            len(actual_segment_pairs) == 4
            and len(set(actual_segment_pairs)) == 4
            and len(persisted_selected_pairs) == len(actual_segment_pairs)
            and len(set(persisted_selected_pairs)) == len(persisted_selected_pairs)
            and set(persisted_selected_pairs) == set(actual_segment_pairs),
            "persisted_selected_route_pairs_mismatch",
            failures,
        )

        return {
            "passed": not failures,
            "failures": sorted(set(failures)),
            "sessionId": session_id,
            "proposalId": proposal_id,
            "activeVersionId": active_version_id,
            "deepSeekControllerEventCount": len(deepseek_events),
            "amapSearchEventCount": len(amap_events),
            "routeOptionCount": len(route_rows),
            "selectedRouteOptionCount": len(selected_route_rows),
            "versionCount": len(version_rows),
            "patchCount": len(patch_rows),
            "adoptionCount": len(adoption_rows),
            "clarificationChoiceCount": len(clarification_rows),
            "clarificationQuestionCount": len(source_questions),
            "manualNormalizationProvider": normalization_audit.get("providerName"),
            "proposalEvidence": itinerary_evidence(proposal_snapshot),
            "versionEvidence": itinerary_evidence(version_snapshot),
        }
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    journey = json_object(json.loads(args.journey_result.read_text(encoding="utf-8")))
    try:
        run_summary = json_object(
            json.loads(args.run_summary.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError):
        run_summary = {}
    expected_git_commit = str(args.expected_git_commit or "").strip().lower()
    commit_failures = artifact_commit_errors(
        journey=journey,
        run_summary=run_summary,
        expected_git_commit=expected_git_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if commit_failures:
        report = {
            "passed": False,
            "failures": commit_failures,
            "gitCommit": expected_git_commit,
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False))
        return 1
    report = verify_database(args.database, journey, args.deepseek_endpoint_host)
    report["gitCommit"] = expected_git_commit
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if report.get("passed") is True:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    print(json.dumps(report, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
