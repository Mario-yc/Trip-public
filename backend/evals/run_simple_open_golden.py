from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
for path in (BACKEND_ROOT, PROJECT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.tests.unit.test_agent_service import (  # noqa: E402
    FakePoiResolver,
    IntentContractProviderMixin,
    StagedInitialProvider,
    clear_database,
    fake_amap_search_nearby_route_compatible,
    fake_amap_search_with_keyword_candidate,
    open_db,
    two_day_initial_day_slot_output,
)
from backend.tests.unit.test_simple_open_execution_profile import (  # noqa: E402
    GoldenFollowupProvider,
)
from src.api.schemas.agent import AgentMessageRequest  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.models.itinerary_segment import ItinerarySegment  # noqa: E402
from src.models.poi import POI  # noqa: E402
from src.models.route_option import RouteOption  # noqa: E402
from src.services.agent_service import AgentService  # noqa: E402
from src.services.conversation_service import ConversationService  # noqa: E402
from src.services.creative_planning_models import proposal_structural_signature_material  # noqa: E402
from src.services.itinerary_service import ItineraryService  # noqa: E402
from src.services.map_poi_service import MapPoiService  # noqa: E402
from src.services.route_service import AMAP_ROUTE_SOURCE, RouteService  # noqa: E402
from src.services.route_insertion_scorer import RouteInsertionScorer  # noqa: E402
from src.services.simple_open_direction_service import SimpleOpenDirectionService  # noqa: E402
from src.services.ticket_service import TicketService  # noqa: E402


SCHEMA_VERSION = "simple-open-direction-golden-artifact-v3"
HANDOFF_BASELINE_SHA = "561832735f20920c570ce2b788769e73efeb7698"
OBSERVED_STARTING_SHA = "7886c23f546f2e920de09f79412d4455fa6eb7d5"
SIMPLE_OPEN_IMPLEMENTATION_PATHS = (
    "backend/evals/run_simple_open_golden.py",
    "backend/src/api/routes/agent.py",
    "backend/src/api/schemas/agent.py",
    "backend/src/api/schemas/itineraries.py",
    "backend/src/core/config.py",
    "backend/src/core/schema.py",
    "backend/src/models/itinerary_segment.py",
    "backend/src/models/poi.py",
    "backend/src/models/poi_intent.py",
    "backend/src/models/route_option.py",
    "backend/src/runtime/agent_runtime.py",
    "backend/src/services/agent_action_directive.py",
    "backend/src/services/agent_decision_contract_service.py",
    "backend/src/services/agent_turn_coordinator.py",
    "backend/src/services/agent_autonomy_service.py",
    "backend/src/services/agent_choice_trace_service.py",
    "backend/src/services/agent_context_builder_service.py",
    "backend/src/services/agent_harness_trace_service.py",
    "backend/src/services/agent_model_registry.py",
    "backend/src/services/agent_run_control.py",
    "backend/src/services/agent_reasoning_status_service.py",
    "backend/src/services/agent_service.py",
    "backend/src/services/agent_verifier_service.py",
    "backend/src/services/amap_call_budget.py",
    "backend/src/services/clarification_checkpoint_service.py",
    "backend/src/services/constraint_ledger_compiler.py",
    "backend/src/services/conversation_service.py",
    "backend/src/services/creative_planning_models.py",
    "backend/src/services/creative_proposal_title_service.py",
    "backend/src/services/creative_portfolio_staging_service.py",
    "backend/src/services/deepseek_agent_provider.py",
    "backend/src/services/feasibility_service.py",
    "backend/src/services/goal_occurrence_compiler.py",
    "backend/src/services/intent_candidate_semantic_policy.py",
    "backend/src/services/itinerary_patch_service.py",
    "backend/src/services/itinerary_service.py",
    "backend/src/services/itinerary_snapshot_service.py",
    "backend/src/services/map_poi_service.py",
    "backend/src/services/night_view_candidate_policy.py",
    "backend/src/services/plan_comparison_preview_service.py",
    "backend/src/services/plan_portfolio_store.py",
    "backend/src/services/plan_proposal_commit_service.py",
    "backend/src/services/planning_run_service.py",
    "backend/src/services/poi_physical_identity_service.py",
    "backend/src/services/portfolio_partial_projection_service.py",
    "backend/src/services/portfolio_route_feasibility_service.py",
    "backend/src/services/proposal_readiness_service.py",
    "backend/src/services/proposal_route_evidence_normalizer.py",
    "backend/src/services/provider_route_insertion_service.py",
    "backend/src/services/route_insertion_scorer.py",
    "backend/src/services/route_service.py",
    "backend/src/services/simple_open_direction_service.py",
    "backend/src/services/simple_open_dynamic_schedule_service.py",
    "backend/src/services/simple_open_itinerary_executor.py",
    "backend/src/services/simple_open_route_assignment_service.py",
    "backend/src/services/ticket_service.py",
    "backend/src/services/timeline_mutation_transaction_service.py",
    "backend/src/services/versioned_write_guard_service.py",
    "backend/tests/contract/test_agent_direction_save_api.py",
    "backend/tests/contract/test_agent_sessions_api.py",
    "backend/tests/contract/test_providers_api.py",
    "backend/tests/integration/test_simple_direction_runtime_golden.py",
    "backend/tests/unit/test_agent_service.py",
    "backend/tests/unit/test_agent_autonomy_service.py",
    "backend/tests/unit/test_agent_clarification_batch_contract.py",
    "backend/tests/unit/test_agent_reasoning_status_service.py",
    "backend/tests/unit/test_agent_run_control.py",
    "backend/tests/unit/test_clarification_checkpoint_service.py",
    "backend/tests/unit/test_comparison_projection_update_mode.py",
    "backend/tests/unit/test_creative_proposal_title_service.py",
    "backend/tests/unit/test_itinerary_snapshot_service.py",
    "backend/tests/unit/test_plan_comparison_preview_service.py",
    "backend/tests/unit/test_proposal_readiness_service.py",
    "backend/tests/unit/test_react_agent_adversarial.py",
    "backend/tests/unit/test_simple_direction_activation_integrity.py",
    "backend/tests/unit/test_simple_open_dynamic_schedule.py",
    "backend/tests/unit/test_simple_open_execution_profile.py",
    "backend/tests/unit/test_simple_open_route_assignment_service.py",
    "backend/tests/unit/test_simple_open_route_gap_supplement.py",
    "backend/tests/unit/test_simple_open_direction_workflow.py",
    "e2e/agent-reasoning-visual.spec.ts",
    "frontend/src/components/AppShell.tsx",
    "frontend/src/components/agent/AgentReasoningProgress.tsx",
    "frontend/src/components/comparison/PlanComparison.tsx",
    "frontend/src/modelRegistry.ts",
    "frontend/src/services/apiClient.ts",
    "frontend/src/state/agentContext.ts",
    "frontend/src/state/planComparisonPreview.ts",
    "frontend/src/styles.css",
    "frontend/tests/integration/agentContext.test.ts",
    "frontend/tests/integration/agentReasoningProgress.test.tsx",
    "frontend/tests/integration/agentStreamingInteraction.test.tsx",
    "frontend/tests/integration/itineraryWorkspace.test.tsx",
    "frontend/tests/integration/errorStates.test.tsx",
    "frontend/tests/integration/planComparisonPreviewState.test.tsx",
    "frontend/tests/integration/planComparisonReadiness.test.tsx",
    "frontend/tests/integration/simpleDirectionEditingFlow.test.tsx",
)
FIXED_QUERIED_AT = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
GOLDEN_INPUT = (
    "请规划一份今年国庆北京高校夜景美食两日游行程，明确参观两所不同高校，每天一所，"
    "每天午餐体验当地特色美食，晚上看夜景。10月1日到2日，中等预算，1人"
)
AMBIGUOUS_DIRECTION_INPUT = "再轻松一点"
EXPECTED_STAGE_LABELS = (
    "route_mobility_clarification",
    "direction_a_offered",
    "direction_a_confirmed",
    "direction_a_edited",
    "direction_a_saved",
    "direction_b_offered",
    "direction_b_confirmed",
    "direction_b_saved",
    "direction_a_restored",
    "duplicate_direction_a_choice",
)
ABSOLUTE_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:Users|home|tmp)/)")
SECRET_KEY_PATTERN = re.compile(r"(?:api.?key|authorization|password|secret|access.?token)", re.IGNORECASE)
COUNTER_KEYS = {"secretFieldCount", "secretValueOccurrenceCount"}
PROVIDER_COUNTER_KEYS = ("intent", "controller", "planner", "poiSearch", "route")
COUNT_KEYS = ("portfolio", "proposal", "version", "patch", "route", "choiceExecution")


def _canonical_sha256(payload: Any) -> str:
    comparable = copy.deepcopy(payload)
    if isinstance(comparable, dict):
        comparable.pop("artifactSha256", None)
    encoded = json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _implementation_sha() -> str:
    completed = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", *SIMPLE_OPEN_IMPLEMENTATION_PATHS],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _implementation_file_hashes(implementation_sha: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in SIMPLE_OPEN_IMPLEMENTATION_PATHS:
        completed = subprocess.run(
            ["git", "cat-file", "blob", f"{implementation_sha}:{relative}"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            continue
        result[relative] = hashlib.sha256(completed.stdout).hexdigest()
    return result


def _git_object_id(*args: str) -> Optional[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    object_id = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", object_id) is None:
        return None
    return object_id


def _implementation_paths_clean(implementation_sha: str) -> bool:
    for relative in SIMPLE_OPEN_IMPLEMENTATION_PATHS:
        if not (PROJECT_ROOT / relative).is_file():
            return False
        frozen_blob = _git_object_id("rev-parse", f"{implementation_sha}:{relative}")
        index_blob = _git_object_id("rev-parse", f":{relative}")
        worktree_blob = _git_object_id("hash-object", f"--path={relative}", relative)
        if not frozen_blob or frozen_blob != index_blob or frozen_blob != worktree_blob:
            return False
    return True


class _FixtureDateTime(datetime):
    tick = 0

    @classmethod
    def now(cls, tz=None):
        value = FIXED_QUERIED_AT + timedelta(milliseconds=cls.tick)
        cls.tick += 1
        value = cls(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            tzinfo=value.tzinfo,
            fold=value.fold,
        )
        if tz is None:
            return value.replace(tzinfo=None)
        return value.astimezone(tz)


@contextmanager
def _temporary_directory():
    path = tempfile.mkdtemp(prefix="trip-simple-direction-")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _network_sentinel():
    attempts: list[dict[str, str]] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_connect(_socket, address):
        attempts.append({"operation": "socket.connect", "destinationType": type(address).__name__})
        raise AssertionError("golden_fixture_external_network_blocked")

    def blocked_create_connection(address, *args, **kwargs):
        del args, kwargs
        attempts.append({"operation": "socket.create_connection", "destinationType": type(address).__name__})
        raise AssertionError("golden_fixture_external_network_blocked")

    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection


@contextmanager
def _fixed_runtime_clock():
    module_names = (
        "src.services.agent_service",
        "src.services.conversation_service",
        "src.services.feasibility_service",
        "src.services.itinerary_patch_service",
        "src.services.itinerary_service",
        "src.services.itinerary_snapshot_service",
        "src.services.plan_portfolio_store",
        "src.services.planning_run_service",
        "src.services.provider_route_insertion_service",
        "src.services.simple_open_direction_service",
        "src.services.simple_open_itinerary_executor",
        "src.services.ticket_service",
        "src.services.timeline_mutation_transaction_service",
    )
    originals: list[tuple[Any, Any]] = []
    original_perf_counter = time.perf_counter
    perf_tick = 0

    def fixture_perf_counter() -> float:
        nonlocal perf_tick
        perf_tick += 1
        return perf_tick / 1000

    for module_name in module_names:
        module = importlib.import_module(module_name)
        if hasattr(module, "datetime"):
            originals.append((module, module.datetime))
            module.datetime = _FixtureDateTime
    _FixtureDateTime.tick = 0
    time.perf_counter = fixture_perf_counter
    try:
        yield
    finally:
        for module, original in originals:
            module.datetime = original
        time.perf_counter = original_perf_counter


class _GoldenInitialProvider(StagedInitialProvider):
    def __init__(self, payload: dict[str, Any], ledger: dict[str, int], *, force_direction: bool = False) -> None:
        super().__init__(payload)
        self.ledger = ledger
        self.force_direction = force_direction

    def decide_autonomy_lite(self, context, *, timeout_seconds):
        self.ledger["intent"] += 1
        return IntentContractProviderMixin.decide_autonomy_lite(self, context, timeout_seconds=timeout_seconds)

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        self.ledger["controller"] += 1
        if not self.force_direction:
            return super().decide_autonomy(
                context,
                timeout_seconds=timeout_seconds,
                repair_feedback=repair_feedback,
            )
        forced = copy.deepcopy(context)
        observation = forced.get("observation") if isinstance(forced.get("observation"), dict) else {}
        itinerary = observation.get("itinerary") if isinstance(observation.get("itinerary"), dict) else {}
        itinerary["lifecycleState"] = "no_active_version"
        observation["itinerary"] = itinerary
        forced["observation"] = observation
        return super().decide_autonomy(
            forced,
            timeout_seconds=timeout_seconds,
            repair_feedback=repair_feedback,
        )

    def generate_initial_plan(self, context: dict) -> str:
        self.ledger["planner"] += 1
        return super().generate_initial_plan(context)


class _GoldenEditProvider(GoldenFollowupProvider):
    def __init__(self, base_version_id: str, segment_id: str, ledger: dict[str, int]) -> None:
        super().__init__(base_version_id, segment_id)
        self.ledger = ledger

    def decide_autonomy_lite(self, context, *, timeout_seconds):
        self.ledger["intent"] += 1
        return IntentContractProviderMixin.decide_autonomy_lite(self, context, timeout_seconds=timeout_seconds)

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        self.ledger["controller"] += 1
        return super().decide_autonomy(
            context,
            timeout_seconds=timeout_seconds,
            repair_feedback=repair_feedback,
        )


def _direction_a_output() -> dict[str, Any]:
    payload = copy.deepcopy(two_day_initial_day_slot_output())
    payload["daySlots"] = [
        slot
        for slot in payload.get("daySlots") or []
        if not (
            isinstance(slot, dict) and slot.get("kind") == "campus" and "afternoon" in str(slot.get("slotId") or "")
        )
    ]
    retained_slot_ids = {str(slot.get("slotId") or "") for slot in payload["daySlots"] if isinstance(slot, dict)}
    for pool in payload.get("intentPools") or []:
        if not isinstance(pool, dict):
            continue
        pool["assignToSlots"] = [
            slot_id for slot_id in pool.get("assignToSlots") or [] if str(slot_id) in retained_slot_ids
        ]
        pool["targetCount"] = len(pool["assignToSlots"])
    return payload


def _direction_b_output() -> dict[str, Any]:
    payload = _direction_a_output()
    for slot in payload.get("daySlots") or []:
        if not isinstance(slot, dict):
            continue
        if slot.get("kind") == "meal":
            slot["rawNeed"] = "胡同风味午餐" if int(slot.get("dayNumber") or 0) == 1 else "创意京菜午餐"
            slot["notes"] = "第二方向使用不同餐饮检索语义。"
        elif slot.get("kind") == "night_view":
            slot["rawNeed"] = "历史城区夜景" if int(slot.get("dayNumber") or 0) == 1 else "城市高点夜景"
            slot["notes"] = "第二方向使用不同夜景检索语义。"
    for pool in payload.get("intentPools") or []:
        if not isinstance(pool, dict):
            continue
        if pool.get("intentType") == "campus_visit":
            pool["candidateHints"] = ["北京师范大学", "北京林业大学", "北京外国语大学", "中央财经大学"]
        elif pool.get("intentType") == "night_view":
            pool["candidateHints"] = ["景山公园观景台", "中央广播电视塔"]
    payload["reply"] = "已拆解第二个轻松方向，待地图候选检索。"
    return payload


def _replace_aliases(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_aliases(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_aliases(item, aliases) for item in value]
    if isinstance(value, str):
        normalized = value
        for source, alias in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
            normalized = normalized.replace(source, alias)
        return normalized
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _register_snapshot_aliases(snapshot: dict[str, Any], prefix: str, aliases: dict[str, str]) -> None:
    plan_id = str(snapshot.get("id") or "")
    if plan_id:
        aliases.setdefault(plan_id, "plan-1")
    for day_index, day in enumerate(snapshot.get("days") or [], start=1):
        if not isinstance(day, dict):
            continue
        day_id = str(day.get("id") or "")
        if day_id:
            aliases.setdefault(day_id, f"day-{prefix}-{day_index}")
        for segment_index, segment in enumerate(day.get("segments") or [], start=1):
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("id") or "")
            if segment_id:
                aliases.setdefault(segment_id, f"segment-{prefix}-{day_index}-{segment_index}")
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            poi_id = str(poi.get("id") or "")
            if poi_id:
                aliases.setdefault(poi_id, f"poi-{prefix}-{day_index}-{segment_index}")
    for index, route in enumerate(snapshot.get("routeOptions") or [], start=1):
        if isinstance(route, dict) and str(route.get("id") or ""):
            aliases.setdefault(str(route["id"]), f"route-{prefix}-{index}")


def _snapshot_business_material(snapshot: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    material = proposal_structural_signature_material(copy.deepcopy(snapshot))
    for key in ("workflowMode", "comparisonRole", "originProjectionMode", "creativeBrief", "portfolioVerifier"):
        material.pop(key, None)
    normalized = _replace_aliases(material, aliases)
    if not isinstance(normalized, dict):
        return {}
    feasibility = normalized.get("feasibilityReport")
    if isinstance(feasibility, dict) and feasibility.get("checkedAt"):
        feasibility["checkedAt"] = "<normalized-runtime-clock>"
    novelty = normalized.get("simpleDirectionNoveltyEvidence")
    if isinstance(novelty, dict) and novelty.get("fingerprint"):
        # The production fingerprint intentionally binds the persisted prior
        # proposal UUID.  Golden business comparison first replaces that UUID
        # with its stable proposal alias, so recompute the redundant digest
        # from the same comparisons-only material used by production.
        novelty["fingerprint"] = _canonical_sha256(novelty.get("comparisons") or [])
    route_contract = normalized.get("routeDecisionContract")
    if isinstance(route_contract, dict):
        provenance = route_contract.get("provenance")
        compactness = (
            provenance.get("compactnessPolicy")
            if isinstance(provenance, dict) and isinstance(provenance.get("compactnessPolicy"), dict)
            else None
        )
        if isinstance(compactness, dict):
            if compactness.get("checkpointId"):
                compactness["checkpointId"] = "<normalized-clarification-checkpoint>"
            if compactness.get("checkpointFingerprint"):
                compactness["checkpointFingerprint"] = "<normalized-checkpoint-fingerprint>"
        rebuilt_route_contract = RouteInsertionScorer.build_route_decision_contract(
            source=str(route_contract.get("source") or ""),
            provenance=provenance if isinstance(provenance, dict) else {},
            detour_tolerance=(
                route_contract.get("detourTolerance")
                if isinstance(route_contract.get("detourTolerance"), dict)
                else {}
            ),
            mobility_profile=(
                route_contract.get("mobilityProfile")
                if isinstance(route_contract.get("mobilityProfile"), dict)
                else {}
            ),
            adjacent_leg_constraint=(
                route_contract.get("adjacentLegConstraint") if "adjacentLegConstraint" in route_contract else None
            ),
            topology_constraint=(
                route_contract.get("topologyConstraint") if "topologyConstraint" in route_contract else None
            ),
        )
        if rebuilt_route_contract is None:
            raise AssertionError("golden_route_decision_contract_cannot_be_normalized")
        normalized_route_fingerprint = str(rebuilt_route_contract["fingerprint"])
        route_contract["fingerprint"] = normalized_route_fingerprint
        route_assignment = normalized.get("simpleOpenRouteAssignment")
        if isinstance(route_assignment, dict) and route_assignment.get("routeContractFingerprint"):
            route_assignment["routeContractFingerprint"] = normalized_route_fingerprint
        for day in normalized.get("days") or []:
            for segment in day.get("segments") or [] if isinstance(day, dict) else []:
                metadata = segment.get("semanticMetadata") if isinstance(segment, dict) else None
                constraints = metadata.get("scheduleConstraints") if isinstance(metadata, dict) else None
                assignment = constraints.get("routeAssignment") if isinstance(constraints, dict) else None
                if isinstance(assignment, dict) and assignment.get("routeContractFingerprint"):
                    assignment["routeContractFingerprint"] = normalized_route_fingerprint
    route_rows = normalized.get("routeOptions")
    if isinstance(route_rows, list):
        for row in route_rows:
            if isinstance(row, dict) and row.get("queriedAt"):
                # Adoption refreshes official route rows at a later instant.
                # The Golden business identity compares the selected physical
                # path and Provider facts, not the refresh wall-clock value.
                row["queriedAt"] = "<normalized-runtime-clock>"
    traffic_rows = normalized.get("trafficCrowdingSignals")
    if isinstance(traffic_rows, list):
        traffic_rows.sort(key=lambda row: str(row.get("routeOptionId") or "") if isinstance(row, dict) else "")
        for index, row in enumerate(traffic_rows, start=1):
            if not isinstance(row, dict):
                continue
            if row.get("id"):
                row["id"] = f"traffic-{index}"
            if row.get("queriedAt"):
                row["queriedAt"] = "<normalized-runtime-clock>"
    for day in normalized.get("days") or []:
        for segment in day.get("segments") or [] if isinstance(day, dict) else []:
            metadata = segment.get("semanticMetadata") if isinstance(segment, dict) else None
            constraints = metadata.get("scheduleConstraints") if isinstance(metadata, dict) else None
            if not isinstance(constraints, dict):
                continue
            meal_brief = constraints.get("mealExperienceBrief")
            if isinstance(meal_brief, dict) and meal_brief.get("sourceFingerprint"):
                # The production value intentionally binds the frozen planning
                # root.  Golden replay compares portable business content across
                # independent roots, while source consistency remains verified
                # before this business-only material is assembled.
                meal_brief["sourceFingerprint"] = "<normalized-request-contract-fingerprint>"
            meal_evidence = constraints.get("mealSemanticEvidence")
            if isinstance(meal_evidence, dict):
                if meal_evidence.get("sourceFingerprint"):
                    meal_evidence["sourceFingerprint"] = "<normalized-request-contract-fingerprint>"
                if meal_evidence.get("evidenceFingerprint"):
                    meal_evidence["evidenceFingerprint"] = "<normalized-meal-evidence-fingerprint>"
    return normalized


def _snapshot_business_sha(snapshot: dict[str, Any], aliases: dict[str, str]) -> str:
    return _canonical_sha256(_snapshot_business_material(snapshot, aliases))


def _snapshot_items(snapshot: dict[str, Any], aliases: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for day in snapshot.get("days") or []:
        if not isinstance(day, dict):
            continue
        for segment in day.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            metadata = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            items.append(
                {
                    "dayNumber": int(day.get("dayNumber") or 0),
                    "segmentAlias": aliases.get(str(segment.get("id") or ""), "unknown-segment"),
                    "kind": str(segment.get("kind") or ""),
                    "groundingStatus": str(
                        segment.get("groundingStatus") or metadata.get("groundingStatus") or "unresolved"
                    ),
                    "routeAnchor": metadata.get("routeAnchor") is True,
                    "amapId": poi.get("amapId"),
                    "name": poi.get("name"),
                    "address": poi.get("address"),
                    "source": poi.get("source"),
                    "longitude": poi.get("longitude"),
                    "latitude": poi.get("latitude"),
                }
            )
    return items


def _route_assignment_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    assignment = (
        snapshot.get("simpleOpenRouteAssignment") if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict) else {}
    )
    topology = assignment.get("topologyEvidence") if isinstance(assignment.get("topologyEvidence"), dict) else {}
    overlap = (
        assignment.get("dailyRouteOverlapEvidence")
        if isinstance(assignment.get("dailyRouteOverlapEvidence"), dict)
        else {}
    )
    per_day_overlap = []
    for value in overlap.get("perDay") or []:
        if not isinstance(value, dict):
            continue
        per_day_overlap.append(
            {
                key: copy.deepcopy(value.get(key))
                for key in (
                    "schemaVersion",
                    "geometryPolicyVersion",
                    "dayNumber",
                    "status",
                    "selectionStatus",
                    "geometryComplete",
                    "totalTraversedMeters",
                    "repeatedMeters",
                    "exemptRepeatedMeters",
                    "nonExemptRepeatedMeters",
                    "overlapRatio",
                    "sameDirectionRepeatedMeters",
                    "reverseDirectionRepeatedMeters",
                    "alternativesEvaluated",
                    "boundedRouteOptionCombinationCount",
                    "availableRouteOptionCombinationCount",
                    "routeOptionCombinationTruncated",
                    "routePairFingerprints",
                    "selectedAlternativeIds",
                    "geometryFingerprint",
                    "evidenceFingerprint",
                    "failureReason",
                    "geometryMaterialLimits",
                )
            }
        )
    return {
        "schemaVersion": str(assignment.get("schemaVersion") or ""),
        "routeContractFingerprintPresent": bool(assignment.get("routeContractFingerprint")),
        "adjacentLegConstraint": copy.deepcopy(assignment.get("adjacentLegConstraint") or {}),
        "topologyCompliance": str(assignment.get("topologyCompliance") or ""),
        "adjacentLegCompliance": str(assignment.get("adjacentLegCompliance") or ""),
        "detourCompliance": str(assignment.get("detourCompliance") or ""),
        "providerBaselineCompared": bool(assignment.get("providerBaselineCompared")),
        "routeCoverageComplete": bool(assignment.get("routeCoverageComplete")),
        "routeProviderAttemptCount": int(assignment.get("routeProviderAttemptCount") or 0),
        "expectedPairs": copy.deepcopy(assignment.get("expectedPairs") or []),
        "verifiedPairs": copy.deepcopy(assignment.get("verifiedPairs") or []),
        "perDayTopology": copy.deepcopy(topology.get("perDay") or []),
        "legacyBacktrackMetric": str(assignment.get("legacyBacktrackMetric") or ""),
        "legacyBacktrackMetricUsedForActualRoadOverlap": (
            assignment.get("legacyBacktrackMetricUsedForActualRoadOverlap") is True
        ),
        "dailyRouteOverlapPolicy": str(assignment.get("dailyRouteOverlapPolicy") or ""),
        "dailyRouteOverlapStatus": str(assignment.get("dailyRouteOverlapStatus") or ""),
        "perDayRouteOverlap": per_day_overlap,
    }


def _route_projection(rows: list[dict[str, Any]], aliases: dict[str, str]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append(
            _replace_aliases(
                {
                    "routeId": str(row.get("id") or ""),
                    "fromSegmentId": str(row.get("from_segment_id") or ""),
                    "toSegmentId": str(row.get("to_segment_id") or ""),
                    "distanceMeters": row.get("distance_meters"),
                    "durationSeconds": row.get("duration_seconds"),
                    "mode": row.get("mode"),
                    "provider": row.get("provider"),
                    "queriedAt": row.get("queried_at"),
                },
                aliases,
            )
        )
    return sorted(result, key=lambda item: (str(item["fromSegmentId"]), str(item["toSegmentId"])))


def _snapshot_route_lineage(
    snapshot: dict[str, Any],
    aliases: dict[str, str],
    *,
    allow_semantic_route_anchors: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    route_service = RouteService(map_provider_key="")
    domain_segments: list[ItinerarySegment] = []
    domain_pois_by_id: dict[str, POI] = {}
    day_numbers_by_id: dict[str, int] = {}
    days = snapshot.get("days")
    if not isinstance(days, list) or any(not isinstance(day, dict) for day in days):
        raise AssertionError("golden_route_lineage_days_invalid")
    for day in days:
        day_id = str(day.get("id") or "")
        day_number = int(day.get("dayNumber") or 0)
        if not day_id or day_number <= 0 or day_id in day_numbers_by_id:
            raise AssertionError("golden_route_lineage_day_invalid")
        day_numbers_by_id[day_id] = day_number
        raw_segments = day.get("segments")
        if not isinstance(raw_segments, list) or any(not isinstance(segment, dict) for segment in raw_segments):
            raise AssertionError("golden_route_lineage_segments_invalid")
        for segment_order, segment in enumerate(raw_segments, start=1):
            segment_id = str(segment.get("id") or "")
            poi_data = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            poi_id = str(poi_data.get("id") or "")
            if not segment_id or segment_id not in aliases or not poi_id:
                raise AssertionError("golden_route_anchor_alias_missing")
            domain_pois_by_id[poi_id] = POI(
                id=poi_id,
                name=str(poi_data.get("name") or ""),
                city=str(poi_data.get("city") or ""),
                category=str(poi_data.get("category") or ""),
                latitude=poi_data.get("latitude"),
                longitude=poi_data.get("longitude"),
                photo_url=poi_data.get("photoUrl"),
                source=str(poi_data.get("source") or ""),
                confidence=poi_data.get("confidence") or 0,
                amap_id=poi_data.get("amapId"),
                type=str(poi_data.get("type") or ""),
                district=str(poi_data.get("district") or ""),
                address=str(poi_data.get("address") or ""),
                source_note=str(poi_data.get("sourceNote") or ""),
                source_url=poi_data.get("sourceUrl"),
                photos=list(poi_data.get("photos") or []),
            )
            domain_segments.append(
                ItinerarySegment(
                    id=segment_id,
                    day_id=day_id,
                    segment_order=segment_order,
                    kind=str(segment.get("kind") or "activity"),
                    start_time=str(segment.get("startTime") or ""),
                    end_time=str(segment.get("endTime") or ""),
                    poi_id=poi_id,
                    transport_mode=str(segment.get("transportMode") or ""),
                    estimated_cost=segment.get("estimatedCost") or 0,
                    notes=str(segment.get("notes") or ""),
                    estimate_metadata=(
                        dict(segment.get("estimateMetadata"))
                        if isinstance(segment.get("estimateMetadata"), dict)
                        else {}
                    ),
                    semantic_metadata=(
                        dict(segment.get("semanticMetadata"))
                        if isinstance(segment.get("semanticMetadata"), dict)
                        else {}
                    ),
                )
            )

    route_anchor_sequence: list[dict[str, Any]] = []
    for day_id, day_number in day_numbers_by_id.items():
        anchors = [
            segment
            for segment in sorted(
                (segment for segment in domain_segments if segment.day_id == day_id),
                key=lambda segment: segment.segment_order,
            )
            if route_service._is_route_anchor_segment(
                segment,
                domain_pois_by_id.get(segment.poi_id),
                allow_semantic_route_anchor=allow_semantic_route_anchors,
            )
        ]
        route_anchor_sequence.extend(
            {
                "dayNumber": day_number,
                "chronologicalOrder": order,
                "segmentId": aliases[segment.id],
            }
            for order, segment in enumerate(anchors, start=1)
        )

    expected_pairs: list[dict[str, Any]] = []
    pair_order_by_day: dict[int, int] = {}
    for from_segment, to_segment, _from_poi, _to_poi in route_service._route_groups(
        list(domain_pois_by_id.values()),
        domain_segments,
        allow_semantic_route_anchors=allow_semantic_route_anchors,
    ):
        if from_segment is None or to_segment is None:
            raise AssertionError("golden_route_lineage_segment_missing")
        day_number = day_numbers_by_id[from_segment.day_id]
        pair_order = pair_order_by_day.get(day_number, 0) + 1
        pair_order_by_day[day_number] = pair_order
        expected_pairs.append(
            {
                "dayNumber": day_number,
                "pairOrder": pair_order,
                "fromSegmentId": aliases[from_segment.id],
                "toSegmentId": aliases[to_segment.id],
            }
        )
    return route_anchor_sequence, expected_pairs


def _route_sha(rows: list[dict[str, Any]], aliases: dict[str, str]) -> str:
    return _canonical_sha256(_route_projection(rows, aliases))


def _capture_state(connection, session_id: str, ledger: dict[str, int]) -> dict[str, Any]:
    session = connection.execute(
        "SELECT active_plan_id, active_version_id FROM conversation_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    plan_id = str(session["active_plan_id"] or "")
    routes = [
        dict(row)
        for row in connection.execute(
            "SELECT id, from_segment_id, to_segment_id, distance_meters, duration_seconds, "
            "mode, provider, queried_at FROM route_options WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
    ]
    counts = {
        "portfolio": int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_plan_portfolios WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        ),
        "proposal": int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_plan_proposals p JOIN agent_plan_portfolios f ON f.id = p.portfolio_id "
                "WHERE f.session_id = ?",
                (session_id,),
            ).fetchone()[0]
        ),
        "version": int(
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        ),
        "patch": int(
            connection.execute("SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session_id,)).fetchone()[
                0
            ]
        ),
        "route": len(routes),
        "choiceExecution": int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_choice_executions WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        ),
    }
    return {
        "counts": counts,
        "activeVersionId": str(session["active_version_id"] or "") or None,
        "routes": routes,
        "providerCalls": {key: int(ledger[key]) for key in PROVIDER_COUNTER_KEYS},
    }


def _count_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {key: int(after["counts"][key]) - int(before["counts"][key]) for key in COUNT_KEYS}


def _provider_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {key: int(after["providerCalls"][key]) - int(before["providerCalls"][key]) for key in PROVIDER_COUNTER_KEYS}


def _turn_payloads(
    connection, assistant_turn_id: Optional[str]
) -> tuple[dict[str, Any], dict[str, Any], Optional[str]]:
    if not assistant_turn_id:
        return {}, {}, None
    row = connection.execute(
        "SELECT agent_request_json, agent_response_json, itinerary_version_id FROM conversation_turns WHERE id = ?",
        (assistant_turn_id,),
    ).fetchone()
    if row is None:
        return {}, {}, None
    return _json_dict(row["agent_request_json"]), _json_dict(row["agent_response_json"]), row["itinerary_version_id"]


def _extract_view_resolution(
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    view_context = request_payload.get("viewContext") if isinstance(request_payload.get("viewContext"), dict) else {}
    request_resolution = (
        request_payload.get("viewResolution") if isinstance(request_payload.get("viewResolution"), dict) else {}
    )
    response_resolution = (
        response_payload.get("viewResolution") if isinstance(response_payload.get("viewResolution"), dict) else {}
    )
    if request_resolution and response_resolution:
        for key in ("resolvedAction", "resolutionSource"):
            if str(request_resolution.get(key) or "") != str(response_resolution.get(key) or ""):
                raise AssertionError(f"golden_persisted_view_resolution_conflict:{key}")
    candidates = [
        response_resolution,
        request_resolution,
        response_payload.get("viewActionResolution"),
        request_payload.get("viewActionResolution"),
        response_payload.get("directionActionResolution"),
        request_payload.get("directionActionResolution"),
        response_payload,
        request_payload,
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        action = str(candidate.get("resolvedAction") or "")
        source = str(candidate.get("resolutionSource") or "")
        if action or source:
            return copy.deepcopy(view_context), action, source
    return copy.deepcopy(view_context), "", ""


def _sanitize_events(events: list[Any], aliases: dict[str, str]) -> list[dict[str, Any]]:
    metadata_keys = {
        "executionProfile",
        "workflowMode",
        "stepIndex",
        "slotKey",
        "selectedAmapId",
        "groundingStatus",
        "resultStatus",
        "planningSelectionRootTurnId",
        "rootPortfolioId",
        "proposalId",
        "newProposalDelta",
        "visibleProposalCount",
        "versionDelta",
        "patchDelta",
        "routeWriteDelta",
        "baseVersionId",
        "resultVersionId",
        "activeVersionId",
        "resolvedAction",
        "resolutionSource",
    }
    result: list[dict[str, Any]] = []
    for sequence, event in enumerate(events, start=1):
        row = {
            "sequence": sequence,
            "type": event.type,
            "status": event.status,
            "providerName": event.provider_name,
            "timestamp": (FIXED_QUERIED_AT + timedelta(milliseconds=sequence)).isoformat(),
            "durationMs": 1 if event.duration_ms else 0,
            "metadata": {key: value for key, value in event.metadata.items() if key in metadata_keys},
        }
        result.append(_replace_aliases(row, aliases))
    return result


def _sanitize_persisted_events(events: list[Any], aliases: dict[str, str]) -> list[dict[str, Any]]:
    metadata_keys = {
        "executionProfile",
        "workflowMode",
        "stepIndex",
        "slotKey",
        "selectedAmapId",
        "groundingStatus",
        "resultStatus",
        "planningSelectionRootTurnId",
        "rootPortfolioId",
        "proposalId",
        "newProposalDelta",
        "visibleProposalCount",
        "versionDelta",
        "patchDelta",
        "routeWriteDelta",
        "baseVersionId",
        "resultVersionId",
        "activeVersionId",
        "resolvedAction",
        "resolutionSource",
    }
    result: list[dict[str, Any]] = []
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        result.append(
            _replace_aliases(
                {
                    "sequence": sequence,
                    "type": str(event.get("type") or ""),
                    "status": str(event.get("status") or ""),
                    "providerName": event.get("providerName") or event.get("provider_name"),
                    "timestamp": (FIXED_QUERIED_AT + timedelta(milliseconds=sequence)).isoformat(),
                    "durationMs": 1 if (event.get("durationMs") or event.get("duration_ms")) else 0,
                    "metadata": {key: value for key, value in metadata.items() if key in metadata_keys},
                },
                aliases,
            )
        )
    return result


def _response_summary(response: Any, turn_version_id: Optional[str], aliases: dict[str, str]) -> dict[str, Any]:
    assistant = response.assistant_turn
    return {
        "terminalStatus": response.terminal_status,
        "comparisonProjectionUpdateMode": assistant.comparison_projection_update_mode,
        "itineraryPresent": response.itinerary is not None,
        "versionPresent": response.version is not None,
        "turnVersionAlias": aliases.get(str(turn_version_id or "")) if turn_version_id else None,
        "reply": assistant.content,
    }


def _stage_record(
    *,
    label: str,
    before: dict[str, Any],
    after: dict[str, Any],
    aliases: dict[str, str],
    connection,
    response: Any = None,
    content: str = "",
    input_capability: Optional[dict[str, Any]] = None,
    explicit_view_context: Optional[dict[str, Any]] = None,
    save_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    assistant_turn_id = str(response.assistant_turn.id) if response is not None else None
    request_payload, response_payload, turn_version_id = _turn_payloads(connection, assistant_turn_id)
    view_context, resolved_action, resolution_source = _extract_view_resolution(request_payload, response_payload)
    if explicit_view_context is not None and not view_context:
        view_context = copy.deepcopy(explicit_view_context)
    events = _sanitize_events(list(response.planning_steps), aliases) if response is not None else []
    persisted_events = _sanitize_persisted_events(
        list(response_payload.get("planningSteps") or []),
        aliases,
    )
    response_summary = (
        _response_summary(response, turn_version_id, aliases)
        if response is not None
        else {
            "terminalStatus": "saved",
            "comparisonProjectionUpdateMode": "replace",
            "itineraryPresent": True,
            "versionPresent": True,
            "turnVersionAlias": aliases.get(str((save_result or {}).get("activeVersionId") or "")),
            "reply": "",
            "saved": bool((save_result or {}).get("saved")),
            "unchanged": bool((save_result or {}).get("unchanged")),
        }
    )
    route_write_delta = sum(
        int((event.get("metadata") or {}).get("routeWriteDelta") or 0) for event in events if isinstance(event, dict)
    )
    route_contract = (request_payload.get("requestIntentContract") or {}).get("routeDecisionContract") or {}
    return {
        "label": label,
        "operation": "save_active_direction" if save_result is not None else "agent_message",
        "input": content,
        "inputCapability": _replace_aliases(input_capability or {}, aliases),
        "viewContext": _replace_aliases(view_context, aliases),
        "resolvedAction": resolved_action,
        "resolutionSource": resolution_source,
        "routeContract": {
            "schemaVersion": str(route_contract.get("schemaVersion") or ""),
            "status": str(route_contract.get("status") or ""),
            "missingFields": list(route_contract.get("missingFields") or []),
            "fingerprintPresent": bool(route_contract.get("fingerprint")),
            "adjacentLegConstraint": copy.deepcopy(route_contract.get("adjacentLegConstraint") or {}),
            "topologyConstraint": copy.deepcopy(route_contract.get("topologyConstraint") or {}),
        },
        "activeVersionBefore": aliases.get(str(before.get("activeVersionId") or ""))
        if before.get("activeVersionId")
        else None,
        "activeVersionAfter": aliases.get(str(after.get("activeVersionId") or ""))
        if after.get("activeVersionId")
        else None,
        "rowCountsBefore": copy.deepcopy(before["counts"]),
        "rowCountsAfter": copy.deepcopy(after["counts"]),
        "deltas": _count_deltas(before, after),
        "providerCallDeltas": _provider_deltas(before, after),
        "routeCanonicalSha256Before": _route_sha(before["routes"], aliases),
        "routeCanonicalSha256After": _route_sha(after["routes"], aliases),
        "routeWriteDelta": route_write_delta,
        "eventTypes": list(
            dict.fromkeys([event["type"] for event in events] + [event["type"] for event in persisted_events])
        ),
        "events": events,
        "persistedEvents": persisted_events,
        "response": response_summary,
    }


def _choice_by_dimension(response: Any, dimension_id: str, option_suffix: str) -> dict[str, Any]:
    legacy = next(
        (
            item
            for item in response.assistant_turn.choice_options
            if item.get("dimensionId") == dimension_id and str(item.get("id") or "").endswith(option_suffix)
        ),
        None,
    )
    if legacy is not None:
        return legacy
    checkpoint = response.assistant_turn.clarification_checkpoint or {}
    questions = list(checkpoint.get("questions") or [])
    target_question = next(item for item in questions if item.get("dimensionId") == dimension_id)
    target_option = next(
        item for item in target_question.get("options") or [] if str(item.get("id") or "").endswith(option_suffix)
    )
    submit_choice = next(
        item for item in response.assistant_turn.choice_options if item.get("action") == "submit_clarification_batch"
    )
    selections = []
    for question in questions:
        option = (
            target_option if question.get("dimensionId") == dimension_id else next(iter(question.get("options") or []))
        )
        selections.append({"dimensionId": question["dimensionId"], "optionId": option["id"]})
    return {**target_option, "id": submit_choice["id"], "_batchSelections": selections}


def _batch_choice_by_dimensions(response: Any, option_suffixes: dict[str, str]) -> dict[str, Any]:
    checkpoint = response.assistant_turn.clarification_checkpoint or {}
    questions = list(checkpoint.get("questions") or [])
    submit_choice = next(
        (
            item
            for item in response.assistant_turn.choice_options
            if item.get("action") == "submit_clarification_batch"
        ),
        None,
    )
    if submit_choice is None:
        if len(questions) != 1:
            raise ValueError(
                "golden_clarification_capability_missing:"
                + json.dumps(
                    {
                        "questionCount": len(questions),
                        "choiceOptions": response.assistant_turn.choice_options,
                        "failureReason": response.assistant_turn.failure_reason,
                        "reply": response.assistant_turn.content,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
        question = questions[0]
        dimension_id = str(question.get("dimensionId") or "")
        suffix = option_suffixes.get(dimension_id)
        options = list(question.get("options") or [])
        option = next(
            (item for item in options if suffix is not None and str(item.get("id") or "").endswith(suffix)),
            options[0],
        )
        direct_choice = next(
            item
            for item in response.assistant_turn.choice_options
            if item.get("action") == "continue_clarification"
            and str(item.get("dimensionId") or "") == dimension_id
            and (
                str(item.get("id") or "").endswith(str(option.get("id") or ""))
                or item.get("semanticValue") == option.get("semanticValue")
            )
        )
        return copy.deepcopy(direct_choice)
    selections = []
    for question in questions:
        dimension_id = str(question.get("dimensionId") or "")
        suffix = option_suffixes.get(dimension_id)
        options = list(question.get("options") or [])
        option = next(
            (item for item in options if suffix is not None and str(item.get("id") or "").endswith(suffix)),
            options[0],
        )
        selections.append({"dimensionId": dimension_id, "optionId": option["id"]})
    return {**submit_choice, "_batchSelections": selections}


def _choice_by_proposal(response_or_turn: Any, proposal_id: str) -> dict[str, Any]:
    return next(item for item in response_or_turn.choice_options if str(item.get("proposalId") or "") == proposal_id)


def _selected_choice_context(
    source_assistant_turn_id: str,
    choice_id: str,
    batch_selections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_choice = {
        "sourceAssistantTurnId": source_assistant_turn_id,
        "choiceId": choice_id,
    }
    if batch_selections is not None:
        selected_choice["batchSelections"] = batch_selections
    return {"selectedAgentChoice": selected_choice}


def _view_context(
    *,
    active_view: str,
    projection: dict[str, Any],
    active_version_id: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": "agent-view-context-v1",
        "activeView": active_view,
        "editingProposal": {
            "planningSelectionRootTurnId": str(projection["planningSelectionRootTurnId"]),
            "rootPortfolioId": str(projection["rootPortfolioId"]),
            "proposalId": str(projection["proposalId"]),
            "sourceAssistantTurnId": str(projection["sourceAssistantTurnId"]),
            "activeVersionId": active_version_id,
        },
    }


def _proposal_snapshot(connection, proposal_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise AssertionError("golden_proposal_snapshot_missing")
    return _json_dict(row["snapshot_json"])


def _active_snapshot(connection, session_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT v.snapshot_json FROM itinerary_versions v JOIN conversation_sessions s ON s.active_version_id = v.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise AssertionError("golden_active_snapshot_missing")
    return _json_dict(row["snapshot_json"])


def _alias_response_write(
    connection, response: Any, aliases: dict[str, str], version_alias: str, patch_alias: str
) -> None:
    if response.version is None:
        raise AssertionError(f"golden_expected_version_missing:{version_alias}")
    aliases[str(response.version.id)] = version_alias
    row = connection.execute(
        "SELECT id FROM itinerary_patches WHERE result_version_id = ? ORDER BY created_at DESC LIMIT 1",
        (response.version.id,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"golden_expected_patch_missing:{patch_alias}")
    aliases[str(row["id"])] = patch_alias


def _register_response_turns(response: Any, aliases: dict[str, str], ordinal: int) -> None:
    aliases.setdefault(str(response.user_turn.id), f"user-turn-{ordinal}")
    aliases.setdefault(str(response.assistant_turn.id), f"assistant-turn-{ordinal}")


def _capability(source_turn_id: str, option: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    value = {
        "sourceAssistantTurnId": source_turn_id,
        "choiceId": str(option.get("id") or option.get("choiceId") or ""),
        "planningSelectionRootTurnId": option.get("planningSelectionRootTurnId"),
        "rootPortfolioId": option.get("rootPortfolioId"),
        "proposalId": option.get("proposalId"),
        "expectedBaseVersionId": option.get("expectedBaseVersionId"),
        "action": option.get("action"),
    }
    return _replace_aliases(value, aliases)


def _activation_record(
    *,
    stage: dict[str, Any],
    proposal_alias: str,
    input_capability: dict[str, Any],
    snapshot: dict[str, Any],
    routes: list[dict[str, Any]],
    aliases: dict[str, str],
    allow_semantic_route_anchors: bool,
) -> dict[str, Any]:
    route_anchor_sequence, expected_pairs = _snapshot_route_lineage(
        snapshot,
        aliases,
        allow_semantic_route_anchors=allow_semantic_route_anchors,
    )
    return {
        "stage": stage["label"],
        "proposalAlias": proposal_alias,
        "inputCapability": _replace_aliases(input_capability, aliases),
        "resultVersionAlias": stage["activeVersionAfter"],
        "versionDelta": stage["deltas"]["version"],
        "patchDelta": stage["deltas"]["patch"],
        "routeWriteDelta": stage["routeWriteDelta"],
        "activeBusinessSha256After": _snapshot_business_sha(snapshot, aliases),
        "semanticRouteAnchorsEnabled": allow_semantic_route_anchors,
        "routeAnchorSequence": route_anchor_sequence,
        "expectedAdjacentRoutePairs": expected_pairs,
        "routesAfter": _route_projection(routes, aliases),
        "activationVerifiedEventPresent": "simple_direction_activation_verified" in stage["eventTypes"],
        "commitEventPresent": "proposal_adoption_committed" in stage["eventTypes"],
    }


def _count_forbidden_fields(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            (1 if key not in COUNTER_KEYS and SECRET_KEY_PATTERN.search(str(key)) else 0)
            + _count_forbidden_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_count_forbidden_fields(item) for item in value)
    return 0


def _configured_secret_values() -> list[str]:
    settings = get_settings()
    values: list[str] = []
    for key, value in settings.model_dump().items():
        if not SECRET_KEY_PATTERN.search(str(key)) or value in (None, ""):
            continue
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        if len(raw) >= 8:
            values.append(raw)
    return values


def _scan_artifact(payload: dict[str, Any]) -> dict[str, int]:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return {
        "absolutePathCount": len(ABSOLUTE_PATH_PATTERN.findall(rendered)),
        "secretFieldCount": _count_forbidden_fields(payload),
        "secretValueOccurrenceCount": sum(rendered.count(value) for value in _configured_secret_values()),
    }


def _derive_artifact_truth(artifact: dict[str, Any]) -> dict[str, bool]:
    stages = [item for item in artifact.get("stages") or [] if isinstance(item, dict)]
    by_label = {str(item.get("label") or ""): item for item in stages}
    clarification = [by_label.get("route_mobility_clarification", {})]
    offers = [item for item in artifact.get("directionOffers") or [] if isinstance(item, dict)]
    activations = [item for item in artifact.get("activations") or [] if isinstance(item, dict)]
    saves = [item for item in artifact.get("saves") or [] if isinstance(item, dict)]
    duplicate = artifact.get("duplicateReplay") if isinstance(artifact.get("duplicateReplay"), dict) else {}

    controller_owned_clarification = sum(
        int((stage.get("providerCallDeltas") or {}).get("controller") or 0) for stage in clarification
    ) == 1 and all(
        all(
            int((stage.get("providerCallDeltas") or {}).get(key) or 0) == 0
            for key in ("intent", "planner", "poiSearch", "route")
        )
        for stage in clarification
    )
    zero_formal = all(
        all(
            int((stage.get("deltas") or {}).get(key) or 0) == 0
            for key in ("portfolio", "proposal", "version", "patch", "route")
        )
        and stage.get("activeVersionAfter") is None
        for stage in clarification
    )
    direction_items = [[item for item in offer.get("items") or [] if isinstance(item, dict)] for offer in offers]

    def valid_item(item: dict[str, Any]) -> bool:
        longitude = item.get("longitude")
        latitude = item.get("latitude")
        return bool(
            str(item.get("groundingStatus") or "") in {"verified_amap", "provisional"}
            and re.fullmatch(r"B[0-9A-Z]{8,31}", str(item.get("amapId") or "").upper())
            and item.get("source") == "amap-place-search"
            and isinstance(longitude, (int, float))
            and not isinstance(longitude, bool)
            and isinstance(latitude, (int, float))
            and not isinstance(latitude, bool)
            and -180 <= float(longitude) <= 180
            and -90 <= float(latitude) <= 90
            and float(longitude) != 0
            and float(latitude) != 0
        )

    def physical_keys(items: list[dict[str, Any]]) -> list[tuple[str, str, float, float]]:
        return [
            (
                re.sub(r"\W+", "", str(item.get("name") or "")).casefold(),
                re.sub(r"\W+", "", str(item.get("address") or "")).casefold(),
                round(float(item.get("longitude") or 0), 6),
                round(float(item.get("latitude") or 0), 6),
            )
            for item in items
            if str(item.get("groundingStatus") or "") in {"verified_amap", "provisional"}
        ]

    keys_by_direction = [physical_keys(items) for items in direction_items]
    provider_evidence = bool(direction_items) and all(
        items and all(valid_item(item) for item in items) for items in direction_items
    )
    no_internal_duplicates = all(len(keys) == len(set(keys)) for keys in keys_by_direction)
    directions_distinct = len(keys_by_direction) == 2 and set(keys_by_direction[0]).isdisjoint(
        set(keys_by_direction[1])
    )
    offer_stages = [by_label.get("direction_a_offered", {}), by_label.get("direction_b_offered", {})]
    offers_zero_write = all(
        int((stage.get("deltas") or {}).get("version") or 0) == 0
        and int((stage.get("deltas") or {}).get("patch") or 0) == 0
        and int((stage.get("deltas") or {}).get("route") or 0) == 0
        and int((stage.get("deltas") or {}).get("proposal") or 0) == 1
        and "simple_direction_proposal_persisted" in (stage.get("eventTypes") or [])
        and not any(str(event).startswith("simple_open_persist_") for event in stage.get("eventTypes") or [])
        for stage in offer_stages
    )
    activation_writes = len(activations) == 3 and all(
        item.get("versionDelta") == 1
        and item.get("patchDelta") == 1
        and item.get("resultVersionAlias")
        and item.get("activationVerifiedEventPresent") is True
        and item.get("commitEventPresent") is True
        for item in activations
    )

    def positive_number(value: Any) -> bool:
        try:
            return not isinstance(value, bool) and float(value) > 0
        except (TypeError, ValueError):
            return False

    def activation_route_evidence_complete(item: dict[str, Any]) -> bool:
        raw_anchor_rows = item.get("routeAnchorSequence")
        raw_expected_rows = item.get("expectedAdjacentRoutePairs")
        raw_actual_routes = item.get("routesAfter")
        if not all(isinstance(rows, list) for rows in (raw_anchor_rows, raw_expected_rows, raw_actual_routes)):
            return False
        anchor_rows = [row for row in raw_anchor_rows if isinstance(row, dict)]
        expected_rows = [row for row in raw_expected_rows if isinstance(row, dict)]
        actual_routes = [row for row in raw_actual_routes if isinstance(row, dict)]
        capability = item.get("inputCapability") if isinstance(item.get("inputCapability"), dict) else {}
        proposal_alias = str(item.get("proposalAlias") or "").strip()
        route_write_delta = item.get("routeWriteDelta")
        if (
            len(anchor_rows) != len(raw_anchor_rows)
            or len(expected_rows) != len(raw_expected_rows)
            or len(actual_routes) != len(raw_actual_routes)
            or not proposal_alias
            or str(capability.get("proposalId") or "").strip() != proposal_alias
            or not str(item.get("resultVersionAlias") or "").strip()
            or item.get("semanticRouteAnchorsEnabled") is not True
            or not isinstance(route_write_delta, int)
            or isinstance(route_write_delta, bool)
            or route_write_delta < 0
        ):
            return False

        anchors_by_day: dict[int, list[tuple[int, str]]] = {}
        anchor_ids: list[str] = []
        for row in anchor_rows:
            day_number = row.get("dayNumber")
            order = row.get("chronologicalOrder")
            segment_id = str(row.get("segmentId") or "").strip()
            if (
                not isinstance(day_number, int)
                or isinstance(day_number, bool)
                or day_number <= 0
                or not isinstance(order, int)
                or isinstance(order, bool)
                or order <= 0
                or not segment_id
            ):
                return False
            anchors_by_day.setdefault(day_number, []).append((order, segment_id))
            anchor_ids.append(segment_id)
        if len(anchor_ids) != len(set(anchor_ids)):
            return False

        derived_expected: list[dict[str, Any]] = []
        for day_number in sorted(anchors_by_day):
            ordered = sorted(anchors_by_day[day_number])
            if [order for order, _ in ordered] != list(range(1, len(ordered) + 1)):
                return False
            ordered_ids = [segment_id for _, segment_id in ordered]
            derived_expected.extend(
                {
                    "dayNumber": day_number,
                    "pairOrder": pair_order,
                    "fromSegmentId": from_segment_id,
                    "toSegmentId": to_segment_id,
                }
                for pair_order, (from_segment_id, to_segment_id) in enumerate(
                    zip(ordered_ids, ordered_ids[1:]),
                    start=1,
                )
            )
        if expected_rows != derived_expected:
            return False

        expected_pairs = [
            (str(row.get("fromSegmentId") or ""), str(row.get("toSegmentId") or "")) for row in expected_rows
        ]
        if len(expected_pairs) != len(set(expected_pairs)):
            return False

        route_ids: list[str] = []
        actual_pairs: list[tuple[str, str]] = []
        anchor_id_set = set(anchor_ids)
        for route in actual_routes:
            route_id = str(route.get("routeId") or "")
            pair = (str(route.get("fromSegmentId") or ""), str(route.get("toSegmentId") or ""))
            if (
                not route_id
                or not pair[0]
                or not pair[1]
                or pair[0] == pair[1]
                or pair[0] not in anchor_id_set
                or pair[1] not in anchor_id_set
                or not positive_number(route.get("distanceMeters"))
                or not positive_number(route.get("durationSeconds"))
                or not str(route.get("provider") or "").strip()
            ):
                return False
            route_ids.append(route_id)
            actual_pairs.append(pair)
        exact_pairs = (
            len(route_ids) == len(set(route_ids))
            and len(actual_pairs) == len(set(actual_pairs))
            and set(actual_pairs) == set(expected_pairs)
        )
        if not exact_pairs:
            return False
        if expected_pairs:
            return route_write_delta > 0
        return route_write_delta == 0

    route_evidence = bool(activations) and all(activation_route_evidence_complete(item) for item in activations)
    saves_zero_write = len(saves) == 2 and all(
        all(int((item.get("deltas") or {}).get(key) or 0) == 0 for key in ("version", "patch", "route"))
        and item.get("routeCanonicalSha256Before") == item.get("routeCanonicalSha256After")
        and item.get("activeVersionBefore") == item.get("activeVersionAfter")
        for item in saves
    )
    view_routing = artifact.get("viewRouting") or []
    capability_routing_valid = (
        len(view_routing) == 2
        and len({str(item.get("input") or "") for item in view_routing}) == 1
        and str(view_routing[0].get("activeView") or "") == "overview"
        and not str(view_routing[0].get("resolvedAction") or "")
        and not str(view_routing[0].get("resolutionSource") or "")
        and str(view_routing[1].get("activeView") or "") == "comparison"
        and str(view_routing[1].get("resolvedAction") or "") == "generate_new_direction"
        and str(view_routing[1].get("resolutionSource") or "") == "server_validated_opaque_choice"
        and bool(str((by_label.get("direction_b_offered", {}).get("inputCapability") or {}).get("choiceId") or ""))
    )
    saved_a_restored = bool(saves and activations) and (
        saves[0].get("proposalAlias") == activations[-1].get("proposalAlias") == "proposal-A"
        and saves[0].get("proposalBusinessSha256After") == activations[-1].get("activeBusinessSha256After")
        and saves[0].get("proposalBusinessSha256Before") != saves[0].get("proposalBusinessSha256After")
    )
    duplicate_zero_write = (
        bool(duplicate)
        and all(int((duplicate.get("deltas") or {}).get(key) or 0) == 0 for key in COUNT_KEYS)
        and duplicate.get("resultVersionAlias") == "version-A-restored"
        and (duplicate.get("routeCanonicalSha256Before") == duplicate.get("routeCanonicalSha256After"))
    )
    capabilities = [item.get("capability") for item in offers if isinstance(item.get("capability"), dict)] + [
        item.get("inputCapability") for item in activations if isinstance(item.get("inputCapability"), dict)
    ]
    capabilities_valid = bool(capabilities) and all(
        all(
            capability.get(key) not in (None, "")
            for key in ("sourceAssistantTurnId", "choiceId", "proposalId", "rootPortfolioId")
        )
        and capability.get("action") == "select_plan_proposal"
        for capability in capabilities
    )
    return {
        "stageSequenceComplete": [stage.get("label") for stage in stages] == list(EXPECTED_STAGE_LABELS),
        "controllerOwnedPreflightClarificationUsesOneDecisionAndZeroPlanningTools": controller_owned_clarification,
        "deterministicPreflightClarificationZeroFormalWrites": zero_formal,
        "routeContractReadyBeforeFirstPlannerCall": (
            (by_label.get("direction_a_offered", {}).get("routeContract") or {}).get("status") == "ready"
            and not (by_label.get("direction_a_offered", {}).get("routeContract") or {}).get("missingFields")
        ),
        "oneDirectionPerGenerationTurn": len(offers) == 2 and all(offer.get("proposalAlias") for offer in offers),
        "proposalTurnsHaveZeroFormalWrites": offers_zero_write,
        "confirmationsUseExactlyOneVersionAndPatch": activation_writes,
        "saveCreatesNoFormalOrRouteMutation": saves_zero_write,
        "viewContextCannotMintGenerationAndOpaqueChoiceCan": capability_routing_valid,
        "savedDirectionARestoredAfterDirectionB": saved_a_restored,
        "duplicateOpaqueChoiceExactlyOnce": duplicate_zero_write,
        "opaqueCapabilitiesServerIssued": capabilities_valid,
        "providerIdentityEvidenceComplete": provider_evidence,
        "noPhysicalDuplicatesWithinDirection": no_internal_duplicates,
        "directionBPhysicallyDistinctFromDirectionA": directions_distinct,
        "activationRouteEvidenceComplete": route_evidence,
    }


def build_artifact(*, route_overlap_policy: str = "observe") -> dict[str, Any]:
    normalized_route_overlap_policy = str(route_overlap_policy or "").strip().casefold()
    if normalized_route_overlap_policy not in {"observe", "rank"}:
        raise ValueError("route_overlap_policy must be 'observe' or 'rank'")
    original_database_url = os.environ.get("DATABASE_URL")
    original_route_overlap_policy = os.environ.get("DAILY_ROUTE_OVERLAP_POLICY")
    original_intent_routing_mode = os.environ.get("AGENT_INTENT_ROUTING_MODE")
    original_search = MapPoiService.search
    original_search_nearby = MapPoiService.search_nearby
    original_build_routes = RouteService.build_routes
    original_build_tickets = TicketService.build_for_segments
    original_refresh_planning_tools = ItineraryService.refresh_planning_tools
    ledger = {key: 0 for key in PROVIDER_COUNTER_KEYS}
    aliases: dict[str, str] = {}
    stages: list[dict[str, Any]] = []
    direction_offers: list[dict[str, Any]] = []
    activations: list[dict[str, Any]] = []
    saves: list[dict[str, Any]] = []
    provider_search_calls: list[dict[str, Any]] = []

    try:
        with _temporary_directory() as temp_dir, _network_sentinel() as network_attempts, _fixed_runtime_clock():
            database_path = Path(temp_dir) / "golden.sqlite3"
            os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
            os.environ["DAILY_ROUTE_OVERLAP_POLICY"] = normalized_route_overlap_policy
            # This frozen golden validates Simple Open writes, not the router
            # rollout experiment. Keep its pre-existing semantic authority.
            os.environ["AGENT_INTENT_ROUTING_MODE"] = "legacy-only"
            get_settings.cache_clear()
            clear_database()

            def recorded_amap_search(*args, **kwargs):
                ledger["poiSearch"] += 1
                fixture_kwargs = {
                    key: value for key, value in kwargs.items() if key not in {"page", "offset"}
                }
                result = fake_amap_search_with_keyword_candidate(*args, **fixture_kwargs)
                keyword = str(kwargs.get("keyword") or (args[2] if len(args) > 2 else result.keyword))
                category = str(kwargs.get("category") or (args[3] if len(args) > 3 else result.category))
                provider_search_calls.append(
                    {
                        "operation": "text",
                        "keyword": keyword,
                        "category": category,
                        "page": int(kwargs.get("page") or 1),
                        "offset": int(kwargs.get("offset") or 0),
                    }
                )
                pois = []
                for index, poi in enumerate(result.pois, start=1):
                    digest = hashlib.sha256(f"{category}|{keyword}|{index}".encode("utf-8")).hexdigest().upper()
                    display_name = str(poi.name or "")
                    is_campus_candidate = "高等院校" in str(poi.type or "") or any(
                        marker in keyword for marker in ("大学", "学院", "高校", "校区", "高等院校")
                    )
                    if is_campus_candidate and not any(
                        marker in display_name for marker in ("大学", "学院", "校园", "校区")
                    ):
                        display_name = re.sub(r"^(北京|北京市)\s*", "", keyword).strip() or display_name
                    elif not is_campus_candidate and ("测试" in display_name or "样例" in display_name):
                        display_name = re.sub(r"^(北京|北京市)\s*", "", keyword).strip() or display_name
                    longitude = 116.15 + (int(digest[:4], 16) % 5000) / 100000
                    latitude = 39.80 + (int(digest[4:8], 16) % 5000) / 100000
                    pois.append(
                        poi.model_copy(
                            update={
                                "id": f"B{digest[:15]}",
                                "name": display_name,
                                "address": f"{display_name} 高德记录地址",
                                "longitude": longitude,
                                "latitude": latitude,
                                "source": "amap-place-search",
                            }
                        )
                    )
                return result.model_copy(
                    update={
                        "providerName": "amap-place-search",
                        "queriedAt": FIXED_QUERIED_AT,
                        "pois": pois,
                    }
                )

            def recorded_amap_search_nearby(*args, **kwargs):
                ledger["poiSearch"] += 1
                fixture_kwargs = {
                    key: value for key, value in kwargs.items() if key not in {"page", "offset"}
                }
                result = fake_amap_search_nearby_route_compatible(*args, **fixture_kwargs)
                longitude = float(kwargs.get("longitude") if "longitude" in kwargs else args[2])
                latitude = float(kwargs.get("latitude") if "latitude" in kwargs else args[3])
                keyword = str(kwargs.get("keyword") if "keyword" in kwargs else args[4])
                category = str(kwargs.get("category") or (args[5] if len(args) > 5 else result.category))
                radius = int(kwargs.get("radius") or (args[6] if len(args) > 6 else 0))
                query_scope_fingerprint = str(kwargs.get("query_scope_fingerprint") or "")
                # Mirror the real MapPoiService receipt boundary: a nearby
                # response and every candidate it carries must share one
                # query-bound Provider receipt and timestamp.  The strict
                # experience-independence gate consumes the POI evidence, so
                # leaving it on the response alone would make the fixture
                # unrealistically bypass (or fail) production admission.
                provider_query_receipt_fingerprint = hashlib.sha256(
                    (
                        "place/around|"
                        f"{longitude:.6f}|{latitude:.6f}|{keyword}|{category}|{radius}|"
                        f"{int(kwargs.get('page') or 1)}|{int(kwargs.get('offset') or 0)}"
                    ).encode("utf-8")
                ).hexdigest()
                provider_search_calls.append(
                    {
                        "operation": "nearby",
                        "keyword": keyword,
                        "category": category,
                        "origin": [round(longitude, 6), round(latitude, 6)],
                        "radiusMeters": radius,
                        "queryScopeFingerprintPresent": bool(query_scope_fingerprint),
                        "page": int(kwargs.get("page") or 1),
                        "offset": int(kwargs.get("offset") or 0),
                    }
                )
                is_meal_result = str(result.category or category) == "food" or any(
                    "餐饮服务" in str(poi.type or "") for poi in result.pois
                )
                is_night_query = bool(
                    re.search(r"(?:夜景|观景|灯光|奥林匹克塔|中信大厦|景山公园观景台|中央广播电视塔)", keyword)
                )
                meal_theme_key = hashlib.sha256(
                    f"{longitude:.6f}|{latitude:.6f}|{keyword}".encode("utf-8")
                ).hexdigest()[:8]
                offset = 0.0015 if str(result.category or category) == "food" else 0.003
                pois = [
                        poi.model_copy(
                            update={
                                "longitude": longitude + offset,
                                "latitude": latitude,
                                "name": (
                                    f"高德实证风味馆·{meal_theme_key}"
                                    if is_meal_result
                                    else str(keyword or poi.name)
                                    if is_night_query
                                    else poi.name
                                ),
                                "type": (
                                    "餐饮服务;中餐厅;北京菜"
                                    if is_meal_result
                                    else "风景名胜;观景点"
                                    if is_night_query
                                    else poi.type
                                ),
                                "provider_type_code": (
                                    "050111"
                                    if is_meal_result
                                    else poi.provider_type_code
                                    or (
                                        "110101"
                                        if "公园广场;公园" in str(poi.type or "")
                                        else None
                                    )
                                ),
                                "tags": ["北京菜", f"高德主题{meal_theme_key}"]
                                if is_meal_result
                                else poi.tags,
                                "source_claims": [
                                    {
                                        "claimKey": "local_food",
                                        "stance": "support",
                                        "locality": "北京",
                                        "evidenceSource": "provider_city_specific_fact",
                                    }
                                ]
                                if is_meal_result
                                else poi.source_claims,
                                "open_time_today": "10:00-22:00"
                                if is_meal_result
                                else "00:00-23:59"
                                if is_night_query
                                else poi.open_time_today,
                                "business_status": "营业中"
                                if is_meal_result or is_night_query
                                else poi.business_status,
                                "provider_queried_at": FIXED_QUERIED_AT,
                                "provider_query_receipt_fingerprint": provider_query_receipt_fingerprint,
                            }
                        )
                    for poi in result.pois
                ]
                return result.model_copy(
                    update={
                        "providerName": "amap-place-search",
                        "queriedAt": FIXED_QUERIED_AT,
                        "provider_query_receipt_fingerprint": provider_query_receipt_fingerprint,
                        "pois": pois,
                    }
                )

            def recorded_build_routes(
                route_service,
                plan_id,
                pois,
                transport_mode="transit",
                segments=None,
                route_pairs=None,
                **kwargs,
            ):
                ledger["route"] += 1
                route_service.warnings = []
                segment_by_id = {segment.id: segment for segment in segments or []}
                poi_by_id = {poi.id: poi for poi in pois or []}
                pairs = list(route_pairs or [])
                if not pairs:
                    pairs = [
                        (left.id, right.id)
                        for left, right, _left_poi, _right_poi in route_service._route_groups(
                            list(pois or []),
                            list(segments or []),
                            allow_semantic_route_anchors=bool(kwargs.get("allow_semantic_route_anchors")),
                        )
                    ]
                include_alternatives = bool(kwargs.get("include_provider_alternatives"))
                routes: list[RouteOption] = []
                for index, (left_id, right_id) in enumerate(pairs, start=1):
                    left_poi = poi_by_id[segment_by_id[left_id].poi_id]
                    right_poi = poi_by_id[segment_by_id[right_id].poi_id]
                    route_scope = hashlib.sha256(
                        (
                            f"{str(left_poi.amap_id or left_poi.id).strip().upper()}|"
                            f"{str(right_poi.amap_id or right_poi.id).strip().upper()}|"
                            f"{transport_mode}"
                        ).encode("utf-8")
                    ).hexdigest()[:16]
                    base_route_id = f"route_fixture_{route_scope}"
                    variants = [
                        (
                            1,
                            0,
                            0,
                            [
                                [left_poi.longitude, left_poi.latitude],
                                [right_poi.longitude, right_poi.latitude],
                            ],
                        )
                    ]
                    if include_alternatives:
                        variants.append(
                            (
                                2,
                                # Golden alternatives stay inside even a zero
                                # detour envelope so this fixture proves the
                                # geometry rank path, not cost-envelope policy.
                                0,
                                0,
                                [
                                    [left_poi.longitude, left_poi.latitude],
                                    [
                                        (left_poi.longitude + right_poi.longitude) / 2 + 0.0002,
                                        (left_poi.latitude + right_poi.latitude) / 2 + 0.0002,
                                    ],
                                    [right_poi.longitude, right_poi.latitude],
                                ],
                            )
                        )
                    for alternative_index, distance_delta, duration_delta, polyline in variants:
                        route_id = (
                            base_route_id
                            if not include_alternatives
                            else f"{base_route_id}_alternative_{alternative_index}"
                        )
                        routes.append(
                            RouteOption(
                                id=route_id,
                                plan_id=plan_id,
                                from_segment_id=left_id,
                                to_segment_id=right_id,
                                from_poi_id=segment_by_id[left_id].poi_id,
                                to_poi_id=segment_by_id[right_id].poi_id,
                                distance_meters=1200 + index * 100 + distance_delta,
                                duration_seconds=900 + index * 60 + duration_delta,
                                mode=transport_mode,
                                provider=AMAP_ROUTE_SOURCE,
                                is_selected=alternative_index == 1,
                                polyline=polyline,
                                steps=[],
                                provider_payload={
                                    "fixture": "simple_direction_golden_v3",
                                    "providerAlternativeIndex": alternative_index,
                                },
                                # ProviderRouteInsertionService's frozen clock replaces
                                # its ``datetime`` class with ``_FixtureDateTime``.  Use
                                # the same class here so its strict freshness/type gate
                                # sees deterministic, current Provider evidence.
                                queried_at=_FixtureDateTime(
                                    FIXED_QUERIED_AT.year,
                                    FIXED_QUERIED_AT.month,
                                    FIXED_QUERIED_AT.day,
                                    tzinfo=timezone.utc,
                                ),
                            )
                        )
                return routes

            MapPoiService.search = recorded_amap_search
            MapPoiService.search_nearby = recorded_amap_search_nearby
            RouteService.build_routes = recorded_build_routes
            TicketService.build_for_segments = lambda ticket_service, segments, pois: (
                ticket_service.build_pending_for_segments(segments, pois)
            )

            with open_db() as connection:
                session = ConversationService(connection).create_session("北京", "simple direction golden artifact")
                session_id = str(session.session_id)
                aliases[session_id] = "session-1"
                aliases[str(session.active_plan_id)] = "plan-1"
                provider_a = _GoldenInitialProvider(_direction_a_output(), ledger)

                def service_for(provider) -> AgentService:
                    service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
                    service.initial_planning_mode = "simple_open_v1"
                    return service

                service = service_for(provider_a)
                before = _capture_state(connection, session_id, ledger)
                mobility = service.send_message(session_id, AgentMessageRequest(content=GOLDEN_INPUT))
                _register_response_turns(mobility, aliases, 1)
                after = _capture_state(connection, session_id, ledger)
                mobility_stage = _stage_record(
                    label="route_mobility_clarification",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=mobility,
                    content=GOLDEN_INPUT,
                )
                stages.append(mobility_stage)
                mobility_option = _batch_choice_by_dimensions(
                    mobility,
                    {
                        "route_decision.mobility_profile": "transit_relaxed",
                        "route_decision.detour_tolerance": "strict_detour",
                        "night_view.cardinality": "one_evening",
                        "night_view.experience_mode": "public_city_view",
                    },
                )
                aliases[str(mobility_option["id"])] = "choice-clarification-batch"

                mobility_capability_raw = {
                    "sourceAssistantTurnId": str(mobility.assistant_turn.id),
                    "choiceId": str(mobility_option["id"]),
                }
                before = _capture_state(connection, session_id, ledger)
                offered_a = service.send_message(
                    session_id,
                    AgentMessageRequest(
                        content="确认交通与夜间体验并开始规划",
                        context=_selected_choice_context(
                            str(mobility.assistant_turn.id),
                            str(mobility_option["id"]),
                            mobility_option.get("_batchSelections"),
                        ),
                    ),
                )
                _register_response_turns(offered_a, aliases, 2)
                for clarification_ordinal in range(3, 6):
                    if any(
                        str(item.get("proposalId") or "")
                        for item in offered_a.assistant_turn.choice_options
                    ):
                        break
                    try:
                        followup_option = _batch_choice_by_dimensions(
                            offered_a,
                            {
                                "route_decision.mobility_profile": "transit_relaxed",
                                "route_decision.detour_tolerance": "strict_detour",
                                "night_view.cardinality": "one_evening",
                                "night_view.experience_mode": "public_city_view",
                            },
                        )
                    except ValueError as error:
                        request_debug, response_debug, _ = _turn_payloads(
                            connection, str(offered_a.assistant_turn.id)
                        )
                        raise AssertionError(
                            "golden_followup_clarification_invalid:"
                            + json.dumps(
                                {
                                    "error": str(error),
                                    "request": request_debug,
                                    "response": response_debug,
                                },
                                ensure_ascii=False,
                                default=str,
                            )
                        ) from error
                    aliases[str(followup_option["id"])] = (
                        f"choice-clarification-followup-{clarification_ordinal - 2}"
                    )
                    offered_a = service.send_message(
                        session_id,
                        AgentMessageRequest(
                            content="确认剩余关键约束并继续规划",
                            context=_selected_choice_context(
                                str(offered_a.assistant_turn.id),
                                str(followup_option["id"]),
                                followup_option.get("_batchSelections"),
                            ),
                        ),
                    )
                    _register_response_turns(offered_a, aliases, clarification_ordinal)
                after = _capture_state(connection, session_id, ledger)
                if not offered_a.assistant_turn.choice_options:
                    request_debug, response_debug, _ = _turn_payloads(connection, str(offered_a.assistant_turn.id))
                    raise AssertionError(
                        "direction_a_proposal_choice_missing:"
                        + json.dumps(
                            {
                                "terminalStatus": offered_a.terminal_status,
                                "failureReason": offered_a.assistant_turn.failure_reason,
                                "reply": offered_a.assistant_turn.content,
                                "request": request_debug,
                                "response": response_debug,
                                "events": [
                                    {
                                        "type": event.type,
                                        "status": event.status,
                                        "detail": event.detail,
                                        "metadata": event.metadata,
                                    }
                                    for event in offered_a.planning_steps
                                ],
                                "providerCalls": copy.deepcopy(ledger),
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                choice_a = offered_a.assistant_turn.choice_options[0]
                if not str(choice_a.get("proposalId") or ""):
                    request_debug, response_debug, _ = _turn_payloads(connection, str(offered_a.assistant_turn.id))
                    raise AssertionError(
                        "direction_a_proposal_identity_missing:"
                        + json.dumps(
                            {
                                "terminalStatus": offered_a.terminal_status,
                                "failureReason": offered_a.assistant_turn.failure_reason,
                                "reply": offered_a.assistant_turn.content,
                                "choiceOptions": offered_a.assistant_turn.choice_options,
                                "clarificationCheckpoint": offered_a.assistant_turn.clarification_checkpoint,
                                "request": request_debug,
                                "response": response_debug,
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                proposal_a = str(choice_a["proposalId"])
                portfolio_id = str(choice_a["rootPortfolioId"])
                planning_root_id = str(choice_a["planningSelectionRootTurnId"])
                aliases[proposal_a] = "proposal-A"
                aliases[portfolio_id] = "portfolio-1"
                aliases[planning_root_id] = "planning-root-1"
                aliases[str(choice_a["id"])] = "choice-confirm-A"
                snapshot_a_initial = _proposal_snapshot(connection, proposal_a)
                _register_snapshot_aliases(snapshot_a_initial, "A", aliases)
                after = _capture_state(connection, session_id, ledger)
                offered_a_stage = _stage_record(
                    label="direction_a_offered",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=offered_a,
                    content="确认交通与夜间体验并开始规划",
                    input_capability=mobility_capability_raw,
                )
                stages.append(offered_a_stage)
                direction_offers.append(
                    {
                        "stage": "direction_a_offered",
                        "proposalAlias": "proposal-A",
                        "portfolioAlias": "portfolio-1",
                        "updateMode": offered_a.assistant_turn.comparison_projection_update_mode,
                        "proposalBusinessSha256": _snapshot_business_sha(snapshot_a_initial, aliases),
                        "items": _snapshot_items(snapshot_a_initial, aliases),
                        "routeAssignment": _route_assignment_summary(snapshot_a_initial),
                        "capability": _capability(str(offered_a.assistant_turn.id), choice_a, aliases),
                    }
                )

                capability_a_raw = {
                    "sourceAssistantTurnId": str(offered_a.assistant_turn.id),
                    "choiceId": str(choice_a["id"]),
                }
                before = _capture_state(connection, session_id, ledger)
                confirmed_a = service.send_message(
                    session_id,
                    AgentMessageRequest(
                        content="确认编辑",
                        context=_selected_choice_context(str(offered_a.assistant_turn.id), str(choice_a["id"])),
                    ),
                )
                _register_response_turns(confirmed_a, aliases, 6)
                _alias_response_write(connection, confirmed_a, aliases, "version-A-1", "patch-A-confirm")
                snapshot_a_active = _active_snapshot(connection, session_id)
                _register_snapshot_aliases(snapshot_a_active, "A", aliases)
                after = _capture_state(connection, session_id, ledger)
                confirmed_a_stage = _stage_record(
                    label="direction_a_confirmed",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=confirmed_a,
                    content="确认编辑",
                    input_capability=capability_a_raw,
                )
                stages.append(confirmed_a_stage)
                activations.append(
                    _activation_record(
                        stage=confirmed_a_stage,
                        proposal_alias="proposal-A",
                        input_capability=_capability(str(offered_a.assistant_turn.id), choice_a, aliases),
                        snapshot=snapshot_a_active,
                        routes=after["routes"],
                        aliases=aliases,
                        allow_semantic_route_anchors=True,
                    )
                )

                first_segment = snapshot_a_active["days"][0]["segments"][0]
                edit_provider = _GoldenEditProvider(str(confirmed_a.version.id), str(first_segment["id"]), ledger)
                edit_service = service_for(edit_provider)
                confirmed_a_projection = next(
                    item
                    for item in confirmed_a.assistant_turn.comparison_projections
                    if str(item.get("proposalId") or "") == proposal_a
                )
                overview_context = _view_context(
                    active_view="overview",
                    projection=confirmed_a_projection,
                    active_version_id=str(confirmed_a.version.id),
                )
                ItineraryService.refresh_planning_tools = lambda *_args, **_kwargs: []
                before = _capture_state(connection, session_id, ledger)
                edited_a = edit_service.send_message(
                    session_id,
                    AgentMessageRequest(content=AMBIGUOUS_DIRECTION_INPUT, context={"viewContext": overview_context}),
                )
                _register_response_turns(edited_a, aliases, 7)
                _alias_response_write(connection, edited_a, aliases, "version-A-2", "patch-A-edit")
                snapshot_a_edited = _active_snapshot(connection, session_id)
                _register_snapshot_aliases(snapshot_a_edited, "A", aliases)
                after = _capture_state(connection, session_id, ledger)
                edited_a_stage = _stage_record(
                    label="direction_a_edited",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=edited_a,
                    content=AMBIGUOUS_DIRECTION_INPUT,
                    explicit_view_context=overview_context,
                )
                stages.append(edited_a_stage)

                direction_service = SimpleOpenDirectionService(connection)
                before = _capture_state(connection, session_id, ledger)
                proposal_a_before_save = _proposal_snapshot(connection, proposal_a)
                save_a = direction_service.save_active_direction(
                    session_id=session_id,
                    proposal_id=proposal_a,
                    planning_root_id=planning_root_id,
                    portfolio_id=portfolio_id,
                    base_version_id=str(edited_a.version.id),
                )
                carrier_a = str((save_a.get("comparisonProjection") or {}).get("sourceAssistantTurnId") or "")
                aliases[carrier_a] = "capability-turn-A-saved"
                proposal_a_saved = _proposal_snapshot(connection, proposal_a)
                _register_snapshot_aliases(proposal_a_saved, "A", aliases)
                after = _capture_state(connection, session_id, ledger)
                save_a_stage = _stage_record(
                    label="direction_a_saved",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    content="切换到行程对比",
                    explicit_view_context=_view_context(
                        active_view="comparison",
                        projection=save_a["comparisonProjection"],
                        active_version_id=str(edited_a.version.id),
                    ),
                    save_result=save_a,
                )
                stages.append(save_a_stage)
                saves.append(
                    {
                        "stage": "direction_a_saved",
                        "proposalAlias": "proposal-A",
                        "activeVersionAlias": "version-A-2",
                        "saved": bool(save_a.get("saved")),
                        "unchanged": bool(save_a.get("unchanged")),
                        "proposalBusinessSha256Before": _snapshot_business_sha(proposal_a_before_save, aliases),
                        "proposalBusinessSha256After": _snapshot_business_sha(proposal_a_saved, aliases),
                        "activeBusinessSha256": _snapshot_business_sha(snapshot_a_edited, aliases),
                        "activeVersionBefore": save_a_stage["activeVersionBefore"],
                        "activeVersionAfter": save_a_stage["activeVersionAfter"],
                        "deltas": copy.deepcopy(save_a_stage["deltas"]),
                        "routeCanonicalSha256Before": save_a_stage["routeCanonicalSha256Before"],
                        "routeCanonicalSha256After": save_a_stage["routeCanonicalSha256After"],
                        "capabilityCarrierTurnAlias": aliases.get(carrier_a),
                    }
                )

                provider_b = _GoldenInitialProvider(_direction_b_output(), ledger, force_direction=True)
                service_b = service_for(provider_b)
                comparison_context = _view_context(
                    active_view="comparison",
                    projection=save_a["comparisonProjection"],
                    active_version_id=str(edited_a.version.id),
                )
                carrier_a_turn = service._turn_response(carrier_a)
                continue_b_choice = next(
                    item
                    for item in carrier_a_turn.choice_options
                    if str(item.get("action") or "") == "continue_plan_expansion"
                    and str(item.get("kind") or "") == "simple_direction_more_plans"
                )
                aliases[str(continue_b_choice["id"])] = "choice-continue-B"
                continue_b_capability_raw = {
                    "sourceAssistantTurnId": carrier_a,
                    "choiceId": str(continue_b_choice["id"]),
                }
                continue_b_context = _selected_choice_context(
                    carrier_a,
                    str(continue_b_choice["id"]),
                )
                # The comparison view is presentation context only.  The
                # persisted opaque choice above is the sole generation
                # authority; the ambiguous label cannot mint a new direction.
                continue_b_context["viewContext"] = comparison_context
                before = _capture_state(connection, session_id, ledger)
                offered_b = service_b.send_message(
                    session_id,
                    AgentMessageRequest(content=AMBIGUOUS_DIRECTION_INPUT, context=continue_b_context),
                )
                _register_response_turns(offered_b, aliases, 8)
                choice_b = next(
                    (item for item in offered_b.assistant_turn.choice_options if str(item.get("proposalId") or "")),
                    None,
                )
                if choice_b is None:
                    request_debug, response_debug, _ = _turn_payloads(connection, str(offered_b.assistant_turn.id))
                    raise AssertionError(
                        "direction_b_proposal_choice_missing:"
                        + json.dumps(
                            {
                                "terminalStatus": offered_b.terminal_status,
                                "failureReason": offered_b.assistant_turn.failure_reason,
                                "reply": offered_b.assistant_turn.content,
                                "choices": offered_b.assistant_turn.choice_options,
                                "request": request_debug,
                                "response": response_debug,
                                "events": [
                                    {
                                        "type": event.type,
                                        "status": event.status,
                                        "detail": event.detail,
                                        "metadata": event.metadata,
                                    }
                                    for event in offered_b.planning_steps
                                ],
                                "providerCalls": copy.deepcopy(ledger),
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                proposal_b = str(choice_b["proposalId"])
                aliases[proposal_b] = "proposal-B"
                aliases[str(choice_b["id"])] = "choice-confirm-B"
                snapshot_b_initial = _proposal_snapshot(connection, proposal_b)
                _register_snapshot_aliases(snapshot_b_initial, "B", aliases)
                after = _capture_state(connection, session_id, ledger)
                offered_b_stage = _stage_record(
                    label="direction_b_offered",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=offered_b,
                    content=AMBIGUOUS_DIRECTION_INPUT,
                    input_capability=continue_b_capability_raw,
                    explicit_view_context=comparison_context,
                )
                stages.append(offered_b_stage)
                direction_offers.append(
                    {
                        "stage": "direction_b_offered",
                        "proposalAlias": "proposal-B",
                        "portfolioAlias": "portfolio-1",
                        "updateMode": offered_b.assistant_turn.comparison_projection_update_mode,
                        "proposalBusinessSha256": _snapshot_business_sha(snapshot_b_initial, aliases),
                        "items": _snapshot_items(snapshot_b_initial, aliases),
                        "routeAssignment": _route_assignment_summary(snapshot_b_initial),
                        "capability": _capability(str(offered_b.assistant_turn.id), choice_b, aliases),
                    }
                )

                capability_b_raw = {
                    "sourceAssistantTurnId": str(offered_b.assistant_turn.id),
                    "choiceId": str(choice_b["id"]),
                }
                before = _capture_state(connection, session_id, ledger)
                confirmed_b = service_b.send_message(
                    session_id,
                    AgentMessageRequest(
                        content="确认编辑",
                        context=_selected_choice_context(str(offered_b.assistant_turn.id), str(choice_b["id"])),
                    ),
                )
                _register_response_turns(confirmed_b, aliases, 9)
                _alias_response_write(connection, confirmed_b, aliases, "version-B-1", "patch-B-confirm")
                snapshot_b_active = _active_snapshot(connection, session_id)
                _register_snapshot_aliases(snapshot_b_active, "B", aliases)
                after = _capture_state(connection, session_id, ledger)
                confirmed_b_stage = _stage_record(
                    label="direction_b_confirmed",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=confirmed_b,
                    content="确认编辑",
                    input_capability=capability_b_raw,
                )
                stages.append(confirmed_b_stage)
                activations.append(
                    _activation_record(
                        stage=confirmed_b_stage,
                        proposal_alias="proposal-B",
                        input_capability=_capability(str(offered_b.assistant_turn.id), choice_b, aliases),
                        snapshot=snapshot_b_active,
                        routes=after["routes"],
                        aliases=aliases,
                        allow_semantic_route_anchors=True,
                    )
                )

                before = _capture_state(connection, session_id, ledger)
                proposal_b_before_save = _proposal_snapshot(connection, proposal_b)
                save_b = direction_service.save_active_direction(
                    session_id=session_id,
                    proposal_id=proposal_b,
                    planning_root_id=planning_root_id,
                    portfolio_id=portfolio_id,
                    base_version_id=str(confirmed_b.version.id),
                )
                carrier_b = str((save_b.get("comparisonProjection") or {}).get("sourceAssistantTurnId") or "")
                aliases[carrier_b] = "capability-turn-B-saved"
                proposal_b_saved = _proposal_snapshot(connection, proposal_b)
                _register_snapshot_aliases(proposal_b_saved, "B", aliases)
                after = _capture_state(connection, session_id, ledger)
                save_b_stage = _stage_record(
                    label="direction_b_saved",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    content="切换到行程对比",
                    explicit_view_context=_view_context(
                        active_view="comparison",
                        projection=save_b["comparisonProjection"],
                        active_version_id=str(confirmed_b.version.id),
                    ),
                    save_result=save_b,
                )
                stages.append(save_b_stage)
                saves.append(
                    {
                        "stage": "direction_b_saved",
                        "proposalAlias": "proposal-B",
                        "activeVersionAlias": "version-B-1",
                        "saved": bool(save_b.get("saved")),
                        "unchanged": bool(save_b.get("unchanged")),
                        "proposalBusinessSha256Before": _snapshot_business_sha(proposal_b_before_save, aliases),
                        "proposalBusinessSha256After": _snapshot_business_sha(proposal_b_saved, aliases),
                        "activeBusinessSha256": _snapshot_business_sha(snapshot_b_active, aliases),
                        "activeVersionBefore": save_b_stage["activeVersionBefore"],
                        "activeVersionAfter": save_b_stage["activeVersionAfter"],
                        "deltas": copy.deepcopy(save_b_stage["deltas"]),
                        "routeCanonicalSha256Before": save_b_stage["routeCanonicalSha256Before"],
                        "routeCanonicalSha256After": save_b_stage["routeCanonicalSha256After"],
                        "capabilityCarrierTurnAlias": aliases.get(carrier_b),
                    }
                )

                carrier_turn = service_b._turn_response(carrier_b)
                choice_a_again = _choice_by_proposal(carrier_turn, proposal_a)
                capability_a_again_raw = {
                    "sourceAssistantTurnId": carrier_b,
                    "choiceId": str(choice_a_again["id"]),
                }
                before = _capture_state(connection, session_id, ledger)
                restored_a = service_b.send_message(
                    session_id,
                    AgentMessageRequest(
                        content="确认编辑",
                        context=_selected_choice_context(carrier_b, str(choice_a_again["id"])),
                    ),
                )
                _register_response_turns(restored_a, aliases, 10)
                _alias_response_write(connection, restored_a, aliases, "version-A-restored", "patch-A-restore")
                snapshot_a_restored = _active_snapshot(connection, session_id)
                _register_snapshot_aliases(snapshot_a_restored, "A", aliases)
                after = _capture_state(connection, session_id, ledger)
                restored_a_stage = _stage_record(
                    label="direction_a_restored",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=restored_a,
                    content="确认编辑",
                    input_capability=capability_a_again_raw,
                )
                stages.append(restored_a_stage)
                activations.append(
                    _activation_record(
                        stage=restored_a_stage,
                        proposal_alias="proposal-A",
                        input_capability=_capability(carrier_b, choice_a_again, aliases),
                        snapshot=snapshot_a_restored,
                        routes=after["routes"],
                        aliases=aliases,
                        allow_semantic_route_anchors=True,
                    )
                )

                before = _capture_state(connection, session_id, ledger)
                duplicate_a = service_b.send_message(
                    session_id,
                    AgentMessageRequest(
                        content="确认编辑",
                        context=_selected_choice_context(carrier_b, str(choice_a_again["id"])),
                    ),
                )
                _register_response_turns(duplicate_a, aliases, 11)
                after = _capture_state(connection, session_id, ledger)
                duplicate_stage = _stage_record(
                    label="duplicate_direction_a_choice",
                    before=before,
                    after=after,
                    aliases=aliases,
                    connection=connection,
                    response=duplicate_a,
                    content="确认编辑",
                    input_capability=capability_a_again_raw,
                )
                stages.append(duplicate_stage)

                duplicate_replay = {
                    "inputCapability": _capability(carrier_b, choice_a_again, aliases),
                    "resultVersionAlias": aliases.get(str(duplicate_a.version.id)) if duplicate_a.version else None,
                    "activeVersionBefore": duplicate_stage["activeVersionBefore"],
                    "activeVersionAfter": duplicate_stage["activeVersionAfter"],
                    "deltas": copy.deepcopy(duplicate_stage["deltas"]),
                    "routeCanonicalSha256Before": duplicate_stage["routeCanonicalSha256Before"],
                    "routeCanonicalSha256After": duplicate_stage["routeCanonicalSha256After"],
                }

            implementation_sha = _implementation_sha()
            source_clean = _implementation_paths_clean(implementation_sha)
            artifact: dict[str, Any] = {
                "schemaVersion": SCHEMA_VERSION,
                "gitCommit": implementation_sha,
                "handoffBaselineSha": HANDOFF_BASELINE_SHA,
                "observedStartingSha": OBSERVED_STARTING_SHA,
                "sourceAttribution": {
                    "commitBoundary": "frozen-implementation-source"
                    if source_clean
                    else "working-tree-source-fingerprint",
                    "implementationCommitSha": implementation_sha,
                    "implementationPaths": list(SIMPLE_OPEN_IMPLEMENTATION_PATHS),
                    "implementationPathsClean": source_clean,
                    "implementationFilesSha256": _implementation_file_hashes(implementation_sha),
                },
                "fixture": {
                    "type": "recorded_provider_shaped_deterministic",
                    "liveProviderClaimed": False,
                    "networkAttempts": copy.deepcopy(network_attempts),
                    "providerCallTotals": copy.deepcopy(ledger),
                    "providerSearchCalls": copy.deepcopy(provider_search_calls),
                    "fixedQueriedAt": FIXED_QUERIED_AT.isoformat(),
                    "eventClock": "logical_stage_sequence",
                },
                "workflow": {"executionProfile": "simple_open_v1", "workflowMode": "simple_direction_v1"},
                "input": {
                    "initialRequest": GOLDEN_INPUT,
                    "sameAmbiguousDirectionRequest": AMBIGUOUS_DIRECTION_INPUT,
                },
                "stages": _replace_aliases(stages, aliases),
                "directionOffers": _replace_aliases(direction_offers, aliases),
                "activations": _replace_aliases(activations, aliases),
                "saves": _replace_aliases(saves, aliases),
                "viewRouting": [
                    {
                        "stage": "direction_a_edited",
                        "input": AMBIGUOUS_DIRECTION_INPUT,
                        "activeView": edited_a_stage["viewContext"].get("activeView"),
                        "editingProposal": edited_a_stage["viewContext"].get("editingProposal"),
                        "resolvedAction": edited_a_stage["resolvedAction"],
                        "resolutionSource": edited_a_stage["resolutionSource"],
                        "deltas": copy.deepcopy(edited_a_stage["deltas"]),
                    },
                    {
                        "stage": "direction_b_offered",
                        "input": AMBIGUOUS_DIRECTION_INPUT,
                        "activeView": offered_b_stage["viewContext"].get("activeView"),
                        "editingProposal": offered_b_stage["viewContext"].get("editingProposal"),
                        "resolvedAction": offered_b_stage["resolvedAction"],
                        "resolutionSource": offered_b_stage["resolutionSource"],
                        "deltas": copy.deepcopy(offered_b_stage["deltas"]),
                    },
                ],
                "duplicateReplay": _replace_aliases(duplicate_replay, aliases),
                "result": {
                    "terminalStatus": "direction_a_restored",
                    "activeVersionAlias": "version-A-restored",
                    "visibleDirectionAliases": ["proposal-A", "proposal-B"],
                    "formalVersionCount": stages[-2]["rowCountsAfter"]["version"],
                    "formalPatchCount": stages[-2]["rowCountsAfter"]["patch"],
                },
            }
            artifact["invariants"] = _derive_artifact_truth(artifact)
            artifact["safetyScan"] = {
                **_scan_artifact(copy.deepcopy(artifact)),
                "externalNetworkCallCount": len(network_attempts),
            }
            artifact["artifactSha256"] = _canonical_sha256(artifact)
            return artifact
    finally:
        MapPoiService.search = original_search
        MapPoiService.search_nearby = original_search_nearby
        RouteService.build_routes = original_build_routes
        TicketService.build_for_segments = original_build_tickets
        ItineraryService.refresh_planning_tools = original_refresh_planning_tools
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url
        if original_route_overlap_policy is None:
            os.environ.pop("DAILY_ROUTE_OVERLAP_POLICY", None)
        else:
            os.environ["DAILY_ROUTE_OVERLAP_POLICY"] = original_route_overlap_policy
        if original_intent_routing_mode is None:
            os.environ.pop("AGENT_INTENT_ROUTING_MODE", None)
        else:
            os.environ["AGENT_INTENT_ROUTING_MODE"] = original_intent_routing_mode
        get_settings.cache_clear()


def verify_artifact(artifact: dict[str, Any], *, require_clean_implementation: bool = True) -> None:
    if artifact.get("schemaVersion") != SCHEMA_VERSION:
        raise SystemExit("artifact_schema_version_mismatch")
    if artifact.get("artifactSha256") != _canonical_sha256(artifact):
        raise SystemExit("artifact_sha256_mismatch")
    source = artifact.get("sourceAttribution") or {}
    if artifact.get("gitCommit") != source.get("implementationCommitSha"):
        raise SystemExit("artifact_frozen_commit_mismatch")
    implementation_sha = _implementation_sha()
    artifact_implementation_sha = source.get("implementationCommitSha")
    if artifact_implementation_sha != implementation_sha:
        raise SystemExit("artifact_implementation_commit_mismatch")
    if source.get("implementationPaths") != list(SIMPLE_OPEN_IMPLEMENTATION_PATHS):
        raise SystemExit("artifact_implementation_paths_mismatch")
    if source.get("implementationFilesSha256") != _implementation_file_hashes(artifact_implementation_sha):
        raise SystemExit("artifact_implementation_file_hash_mismatch")
    if require_clean_implementation:
        if source.get("commitBoundary") != "frozen-implementation-source":
            raise SystemExit("artifact_commit_boundary_invalid")
        if source.get("implementationPathsClean") is not True or not _implementation_paths_clean(
            artifact_implementation_sha
        ):
            raise SystemExit("artifact_implementation_paths_dirty")
    elif source.get("commitBoundary") not in {"frozen-implementation-source", "working-tree-source-fingerprint"}:
        raise SystemExit("artifact_commit_boundary_invalid")
    derived_truth = _derive_artifact_truth(artifact)
    invariants = artifact.get("invariants") or {}
    mismatched_truth = [key for key, value in derived_truth.items() if invariants.get(key) != value]
    if mismatched_truth:
        raise SystemExit(f"artifact_derived_truth_mismatch:{','.join(mismatched_truth)}")
    failed = [key for key, value in invariants.items() if value is not True]
    if failed:
        raise SystemExit(f"artifact_invariant_failed:{','.join(failed)}")
    scan_target = copy.deepcopy(artifact)
    scan_target.pop("artifactSha256", None)
    scan_target.pop("safetyScan", None)
    expected_scan = {
        **_scan_artifact(scan_target),
        "externalNetworkCallCount": len((artifact.get("fixture") or {}).get("networkAttempts") or []),
    }
    if artifact.get("safetyScan") != expected_scan:
        raise SystemExit("artifact_safety_scan_mismatch")
    if any(int(value or 0) != 0 for value in expected_scan.values()):
        raise SystemExit("artifact_safety_scan_failed")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or verify the deterministic Simple direction Golden vertical slice."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify_artifact(json.loads(args.verify.read_text(encoding="utf-8")))
        print(json.dumps({"verified": True, "artifact": args.verify.name}, ensure_ascii=False))
        return 0
    artifact = build_artifact()
    replay = build_artifact()
    if _canonical_sha256(artifact) != _canonical_sha256(replay):
        raise SystemExit("artifact_deterministic_replay_mismatch")
    artifact["invariants"]["deterministicReplayVerified"] = True
    artifact["artifactSha256"] = _canonical_sha256(artifact)
    verify_artifact(artifact)
    rendered = json.dumps(artifact, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
