from __future__ import annotations

import copy
import json

import pytest
from fastapi import HTTPException

from backend.tests.unit.test_agent_service import clear_database, open_db
from src.api.schemas.agent import AgentInitialPlanOutput, AgentMessageRequest
from src.api.schemas.itinerary_patches import ItineraryPatchOperation
from src.services.agent_service import AgentService
from src.services.agent_autonomy_service import (
    request_context_requires_clarification,
    simple_direction_generation_authorized,
)
from src.services.conversation_service import ConversationService
from src.services.clarification_checkpoint_service import ClarificationCheckpointService
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.services.plan_proposal_commit_service import PlanProposalCommitService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor
from src.services.travel_guide_advice_service import TravelGuideAdviceService


def _route_contract(*, ready: bool = True, compact: bool = False) -> dict:
    if ready:
        contract = RouteInsertionScorer.build_route_decision_contract(
            source="request_intent_contract",
            provenance={"transportMode": "transit", "requestSemanticsPresent": True},
            detour_tolerance={"maxGeneralizedCostDelta": 30.0, "maxDetourRatio": 0.25},
            mobility_profile={
                "source": "explicit_request_mobility_semantics",
                "walkingPenaltyMinutesPerKm": 1.8,
                "transferPenaltyMinutes": 6.0,
                "waitTimeMultiplier": 1.0,
                "riskPenaltyMultiplier": 1.0,
            },
            adjacent_leg_constraint=(
                {
                    "candidateSearchRadiusMeters": 5000,
                    "maxProviderTravelMinutes": 45,
                }
                if compact
                else None
            ),
            topology_constraint={"maxBacktrackRatio": 0.15} if compact else None,
        )
        assert contract is not None
        return {
            "schemaVersion": "route-decision-contract-v2" if compact else "route-decision-contract-v1",
            "status": "ready",
            "missingFields": [],
            **contract,
        }
    return {
        "schemaVersion": "route-decision-contract-v1",
        "source": "request_intent_contract",
        "status": "awaiting_clarification",
        "missingFields": ["detourTolerance"],
        "detourTolerance": None,
        "mobilityProfile": None,
        "provenance": {"transportMode": "transit", "requestSemanticsPresent": True},
        "fingerprint": "route-missing",
    }


def _snapshot(plan_id: str, title: str, poi_id: str, *, poi_name: str | None = None) -> dict:
    explicit_poi_name = poi_name
    poi_name = poi_name or {"B000A": "清华大学", "B000B": "北京大学"}.get(poi_id, "中国人民大学")
    canonical_amap_id = {
        "B000A": "B000A6EA36",
        "B000B": "B000A7O5PK",
    }.get(poi_id, poi_id)
    title_candidates = (
        [f"{poi_name}人文漫游", f"{poi_name}校园慢游", f"{poi_name}学府一日"]
        if explicit_poi_name
        else ["京城清华人文漫游", "清华园从容漫步", "清华学府轻松一日"]
        if poi_id == "B000A"
        else ["京城北大人文漫游", "燕园从容漫步", "北大学府轻松一日"]
    )
    snapshot = {
        "id": plan_id,
        "title": title,
        "city": "北京",
        "templateType": "agent_mvp",
        "budgetEstimate": 0,
        "decisionRationale": "服务端生成的可编辑方向快照，确认前不写 active itinerary。",
        "status": "draft",
        "simpleOpenExecutionProfile": "simple_open_v1",
        "simpleOpenExecutionRoute": "simple_open_initial_pipeline",
        "requiredPlanningDayNumbers": [1],
        "explicitRestDayNumbers": [],
        "desiredDensityAnchorTargets": {"1": 1},
        "dailyPlanningCoverageSource": "authoritative_goal_occurrences",
        "routeDecisionContract": _route_contract(),
        "days": [
            {
                "id": f"day_{poi_id}",
                "dayNumber": 1,
                "date": "2026-10-01",
                "title": title,
                "totalEstimatedCost": 0,
                "segments": [
                    {
                        "id": f"seg_{poi_id}",
                        "startTime": "09:00",
                        "endTime": "11:00",
                        "durationMinutes": 120,
                        "kind": "campus",
                        "poi": {
                            "id": f"poi_{poi_id}",
                            "amapId": canonical_amap_id,
                            "name": poi_name,
                            "city": "北京",
                            "category": "campus",
                            "type": "科教文化服务;学校;高等院校",
                            "providerType": "科教文化服务;学校;高等院校",
                            "providerTypeCode": "141201",
                            "latitude": 39.99,
                            "longitude": 116.31,
                            "source": "amap-place-search",
                            "confidence": 0.9,
                        },
                        "transportMode": "public_transit",
                        "estimatedCost": 0,
                        "semanticMetadata": {
                            "intentType": "campus_visit",
                            "routeAnchor": True,
                            "groundingStatus": "verified_amap",
                            "required": True,
                            "requirementLevel": "required",
                            "goalId": "goal_campus",
                            "sourceGoalId": "goal_campus",
                            "occurrenceId": "occ:goal_campus:day:1",
                            "planningSlotId": f"slot_{poi_id}",
                            "poolId": "pool_campus_visit",
                            "dayNumber": 1,
                            "lineageAuthority": "goal_occurrence_compiler",
                        },
                        "notes": "",
                    }
                ],
            }
        ],
        "routeOptions": [],
        "weatherSignals": [],
        "trafficCrowdingSignals": [],
        "ticketLookupResults": [],
    }
    return CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=lambda _context: {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": candidate, "evidenceAmapIds": [canonical_amap_id]} for candidate in title_candidates
            ],
        },
        context={},
    )


def _live_shaped_simple_partial_snapshot(plan_id: str) -> dict:
    def segment(
        *,
        segment_id: str,
        amap_id: str,
        name: str,
        provider_type: str,
        intent_type: str,
        grounding_status: str,
        start_time: str,
        end_time: str,
    ) -> dict:
        day_number = int(segment_id.rsplit("_", 1)[-1])
        goal_id = f"goal_{intent_type}_{day_number}"
        kind = {
            "campus_visit": "campus",
            "meal": "meal",
            "night_view": "night_view",
        }[intent_type]
        return {
            "id": segment_id,
            "startTime": start_time,
            "endTime": end_time,
            "durationMinutes": 90,
            "kind": kind,
            "poi": {
                "id": f"poi_{segment_id}",
                "amapId": amap_id,
                "name": name,
                "city": "北京",
                "category": "all",
                "type": provider_type,
                "providerType": provider_type,
                "latitude": 39.90 + len(segment_id) / 10000,
                "longitude": 116.30 + len(segment_id) / 10000,
                "source": "amap-place-search",
                "confidence": 0.86,
            },
            "transportMode": "public_transit",
            "estimatedCost": 0,
            "semanticMetadata": {
                "intentType": intent_type,
                "rawNeed": {
                    "campus_visit": "高校参观",
                    "meal": "午餐",
                    "night_view": "夜景观景点",
                }[intent_type],
                "routeAnchor": True,
                "groundingStatus": grounding_status,
                "required": intent_type != "night_view",
                "requirementLevel": "required" if intent_type != "night_view" else "optional",
                "planningSlotId": f"slot_{segment_id}",
                "poolId": f"pool_{intent_type}",
                "goalId": goal_id,
                "sourceGoalId": goal_id,
                "occurrenceId": f"occ:{goal_id}:day:{day_number}",
                "dayNumber": day_number,
                "lineageAuthority": "goal_occurrence_compiler",
            },
            "notes": "夜景适配性或夜间开放状态待核验" if grounding_status == "provisional" else "",
        }

    snapshot = _snapshot(plan_id, "真实 Provider 两日方向", "B000A6EA36")
    snapshot["status"] = "partial"
    snapshot["days"] = [
        {
            "id": "day_live_1",
            "dayNumber": 1,
            "date": "2026-10-01",
            "title": "高校与夜景",
            "totalEstimatedCost": 0,
            "segments": [
                segment(
                    segment_id="seg_campus_1",
                    amap_id="B000A6EA36",
                    name="中央财经大学",
                    provider_type="科教文化服务;学校;高等院校",
                    intent_type="campus_visit",
                    grounding_status="verified_amap",
                    start_time="09:00",
                    end_time="10:30",
                ),
                segment(
                    segment_id="seg_meal_1",
                    amap_id="B0J1CRCYMW",
                    name="再疆胡·新疆特色餐厅",
                    provider_type="餐饮服务;中餐厅;清真菜馆",
                    intent_type="meal",
                    grounding_status="verified_amap",
                    start_time="12:00",
                    end_time="13:30",
                ),
                segment(
                    segment_id="seg_night_1",
                    amap_id="B0LDMS6RID",
                    name="奥林匹克塔",
                    provider_type="风景名胜;风景名胜;风景名胜",
                    intent_type="night_view",
                    grounding_status="provisional",
                    start_time="19:00",
                    end_time="20:30",
                ),
            ],
        },
        {
            "id": "day_live_2",
            "dayNumber": 2,
            "date": "2026-10-02",
            "title": "高校与城市夜景",
            "totalEstimatedCost": 0,
            "segments": [
                segment(
                    segment_id="seg_campus_2",
                    amap_id="B0G2J5IJ9U",
                    name="北京外国语大学",
                    provider_type="科教文化服务;学校;高等院校",
                    intent_type="campus_visit",
                    grounding_status="verified_amap",
                    start_time="09:00",
                    end_time="10:30",
                ),
                segment(
                    segment_id="seg_meal_2",
                    amap_id="B0FFG967HI",
                    name="揽月斋新疆风味餐厅",
                    provider_type="餐饮服务;中餐厅;清真菜馆",
                    intent_type="meal",
                    grounding_status="verified_amap",
                    start_time="12:00",
                    end_time="13:30",
                ),
                segment(
                    segment_id="seg_night_2",
                    amap_id="B000A7O5PK",
                    name="什刹海",
                    provider_type="风景名胜;风景名胜;国家级景点",
                    intent_type="night_view",
                    grounding_status="provisional",
                    start_time="19:00",
                    end_time="20:30",
                ),
            ],
        },
    ]
    snapshot["routeOptions"] = []
    snapshot["portfolioPendingSlots"] = []
    snapshot["requiredPlanningDayNumbers"] = [1, 2]
    snapshot["explicitRestDayNumbers"] = []
    snapshot["desiredDensityAnchorTargets"] = {"1": 2, "2": 2}
    return snapshot


def _request_context(*, root_turn_id: str) -> dict:
    return {
        "sessionId": "",
        "currentUserTurnId": root_turn_id,
        "sourceUserRequest": "北京高校一日游，公共交通，适度绕行",
        "latestUserMessage": "北京高校一日游，公共交通，适度绕行",
        "effectiveUserMessage": "北京高校一日游，公共交通，适度绕行",
        "selectedCity": "北京",
        "city": "北京",
        "resolvedTripDates": {
            "status": "resolved",
            "dates": ["2026-10-01"],
            "dayCount": 1,
        },
        "requestIntentContract": {
            "city": "北京",
            "dayCount": 1,
            "requiredPlanningDayNumbers": [1],
            "explicitRestDayNumbers": [],
            "clarificationRequired": False,
            "routeDecisionContract": _route_contract(),
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                }
            ],
        },
        "serverExecutionProfile": "simple_open_v1",
        "actualExecutionRoute": "simple_open_initial_pipeline",
        "simpleOpenNonBlockingRoutes": True,
        "stagedPlanningPipeline": {"enabled": True},
        "pipelineContext": {
            "serverExecutionProfile": "simple_open_v1",
            "actualExecutionRoute": "simple_open_initial_pipeline",
            "simpleOpenNonBlockingRoutes": True,
        },
    }


def _persist_direction_turn(
    *,
    connection,
    service: AgentService,
    direction_service,
    session_id: str,
    root_turn_id: str,
    source_user_turn_id: str,
    title: str,
    poi_id: str,
    snapshot_override: dict | None = None,
    request_contract_override: dict | None = None,
    request_context_override: dict | None = None,
) -> tuple[str, str]:
    assistant_turn_id = service._insert_turn(
        session_id,
        "assistant",
        "已生成一个待确认方向。",
        "active",
    )
    context = (
        copy.deepcopy(request_context_override)
        if request_context_override is not None
        else _request_context(root_turn_id=root_turn_id)
    )
    context["sessionId"] = session_id
    material = direction_service.offer_direction(
        session_id=session_id,
        planning_root_id=root_turn_id,
        source_user_turn_id=source_user_turn_id,
        source_assistant_turn_id=assistant_turn_id,
        expected_base_version_id=(str(service._session(session_id)["active_version_id"] or "") or None),
        source_observation_fingerprint=("o" if poi_id.endswith("A") else "p") * 64,
        request_contract_fingerprint="r" * 64,
        snapshot=(
            copy.deepcopy(snapshot_override)
            if snapshot_override is not None
            else _snapshot(service._session(session_id)["active_plan_id"], title, poi_id)
        ),
        request_contract=(
            copy.deepcopy(request_contract_override)
            if request_contract_override is not None
            else context["requestIntentContract"]
        ),
    )
    response_payload = {
        "mode": "simple_open_direction_proposal",
        "workflowMode": "simple_direction_v1",
        "reply": "已生成一个待确认方向。",
        **material,
        "initialPlan": {},
        "pipelineContext": context["pipelineContext"],
        "grounding": {},
        "visibleProposalCount": len(material["comparisonProjections"]),
        "terminalStatus": "needs_confirmation",
    }
    connection.execute(
        "UPDATE conversation_turns SET agent_request_json = ?, agent_response_json = ? WHERE id = ?",
        (
            json.dumps(context, ensure_ascii=False),
            json.dumps(response_payload, ensure_ascii=False),
            assistant_turn_id,
        ),
    )
    connection.commit()
    return assistant_turn_id, str(material["comparisonProjections"][0]["proposalId"])


def test_route_contract_missing_detour_is_a_server_authoritative_clarification_gate() -> None:
    context = {
        "requestIntentContract": {
            "clarificationRequired": False,
            "routeDecisionContract": _route_contract(ready=False),
        },
        "understoodRequirements": {"highImpactAmbiguityDetected": False},
    }

    assert request_context_requires_clarification(context) is True


def test_simple_open_initial_context_preserves_material_night_view_clarification() -> None:
    context = {
        "serverExecutionProfile": "simple_open_v1",
        "activeVersionId": None,
        "selectedCity": "北京",
        "resolvedTripDates": {"status": "resolved", "dayCount": 2},
        "requestIntentContract": {
            "clarificationRequired": True,
            "clarificationReason": "night_view_cardinality_ambiguous",
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.cardinality",
                    "status": "unresolved",
                    "impactCode": "required_occurrence_and_route_evidence_count",
                },
                {
                    "dimensionId": "night_view.experience_mode",
                    "status": "unresolved",
                    "impactCode": "candidate_admission_route_and_access_policy",
                },
            ],
        },
        "understoodRequirements": {"highImpactAmbiguityDetected": True},
    }

    SimpleOpenItineraryExecutor.prepare_initial_context(context)

    assert context["requestIntentContract"]["clarificationRequired"] is True
    assert context["requestIntentContract"]["clarificationReason"] == "night_view_cardinality_ambiguous"
    assert context["understoodRequirements"]["highImpactAmbiguityDetected"] is True
    assert "simpleOpenInitialRequestSufficient" not in context


def test_simple_open_initial_context_repairs_inconsistent_material_clarification_flags() -> None:
    context = {
        "serverExecutionProfile": "simple_open_v1",
        "activeVersionId": None,
        "selectedCity": "北京",
        "resolvedTripDates": {"status": "resolved", "dayCount": 2},
        "simpleOpenInitialRequestSufficient": True,
        "requestIntentContract": {
            "clarificationRequired": False,
            "clarificationReason": None,
            "clarificationDimensions": [
                {
                    "dimensionId": "night_view.cardinality",
                    "status": "unresolved",
                    "impactCode": "required_occurrence_and_route_evidence_count",
                }
            ],
        },
        "understoodRequirements": {"highImpactAmbiguityDetected": False},
    }

    SimpleOpenItineraryExecutor.prepare_initial_context(context)

    assert context["requestIntentContract"]["clarificationRequired"] is True
    assert (
        context["requestIntentContract"]["clarificationReason"]
        == "material_clarification_unresolved"
    )
    assert context["understoodRequirements"]["highImpactAmbiguityDetected"] is True
    assert "simpleOpenInitialRequestSufficient" not in context


def test_vague_overview_edit_preserves_the_exact_confirmed_route_contract() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "route contract carry")
        service = AgentService(connection)
        persisted = AgentService._compile_route_decision_contract(
            request_text="公共交通，绕路最多 20 分钟，绕路比例 15%",
            experience_specs=[],
        )
        assert persisted["status"] == "ready"
        snapshot = _snapshot(session.active_plan_id, "高校经典线", "B000A")
        snapshot["routeDecisionContract"] = persisted
        connection.execute(
            "INSERT INTO itinerary_versions "
            "(id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_route_carry', ?, ?, 1, 'agent', ?, '2026-08-18T00:00:00+00:00')",
            (session.session_id, session.active_plan_id, json.dumps(snapshot, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_route_carry' WHERE id = ?",
            (session.session_id,),
        )
        connection.commit()
        request_context = {
            "sessionId": session.session_id,
            "activePlanId": session.active_plan_id,
            "activeVersionId": "ver_route_carry",
            "latestUserMessage": "再轻松一点",
            "effectiveUserMessage": "再轻松一点",
            "requestIntentContract": {
                "clarificationRequired": False,
                "experienceSpecs": [],
                "routeDecisionContract": {
                    **persisted,
                    "detourToleranceSource": "controller_semantic_choice",
                },
            },
        }

        service._restore_active_route_decision_contract(request_context)

        assert request_context["routeDecisionContract"] == persisted
        assert request_context["requestIntentContract"]["routeDecisionContract"] == persisted
        assert request_context["routeDecisionContract"]["fingerprint"] == persisted["fingerprint"]


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("把第一天下午改成北京地铁博物馆", False),
        ("再轻松一点", False),
        ("公共交通优先", True),
        ("路线改成步行", True),
        ("改成骑行路线", True),
    ],
)
def test_explicit_route_override_gate_requires_preference_grammar(
    request_text: str,
    expected: bool,
) -> None:
    assert AgentService._explicit_route_preference_override_requested(request_text) is expected


def test_explicit_bicycling_override_compiles_a_bicycling_mobility_profile() -> None:
    existing = AgentService._compile_route_decision_contract(
        request_text="公共交通，绕路最多 20 分钟，绕路比例 15%",
        experience_specs=[],
    )

    updated = AgentService._compile_route_decision_contract(
        request_text="改成骑行路线",
        experience_specs=[],
        existing_contract=existing,
    )

    assert updated["status"] == "ready"
    assert updated["provenance"]["transportMode"] == "bicycling"
    assert updated["mobilityProfile"] == {
        "source": "explicit_request_mobility_semantics",
        "walkingPenaltyMinutesPerKm": 1.0,
        "transferPenaltyMinutes": 0.0,
        "waitTimeMultiplier": 0.0,
        "riskPenaltyMultiplier": 1.0,
    }
    assert updated["fingerprint"] != existing["fingerprint"]


def test_accessibility_route_request_becomes_typed_clarification_instead_of_reusing_contract() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "accessibility clarification")
        service = AgentService(connection)
        persisted = AgentService._compile_route_decision_contract(
            request_text="公共交通，绕路最多 20 分钟，绕路比例 15%",
            experience_specs=[],
        )
        assert persisted["status"] == "ready"
        snapshot = _snapshot(session.active_plan_id, "高校经典线", "B000A")
        snapshot["routeDecisionContract"] = persisted
        connection.execute(
            "INSERT INTO itinerary_versions "
            "(id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_accessibility', ?, ?, 1, 'agent', ?, '2026-08-18T00:00:00+00:00')",
            (session.session_id, session.active_plan_id, json.dumps(snapshot, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_accessibility' WHERE id = ?",
            (session.session_id,),
        )
        connection.commit()
        request_context = {
            "sessionId": session.session_id,
            "activePlanId": session.active_plan_id,
            "activeVersionId": "ver_accessibility",
            "latestUserMessage": "带轮椅用户，尽量无障碍",
            "effectiveUserMessage": "带轮椅用户，尽量无障碍",
            "requestIntentContract": {
                "clarificationRequired": False,
                "clarificationDimensions": [],
                "experienceSpecs": [],
                "routeDecisionContract": copy.deepcopy(persisted),
            },
        }

        service._restore_active_route_decision_contract(request_context)

        request_contract = request_context["requestIntentContract"]
        unresolved = request_contract["routeDecisionContract"]
        assert request_contract["clarificationRequired"] is True
        assert request_contract["clarificationReason"] == "route_accessibility_fallback_required"
        assert unresolved["status"] == "awaiting_clarification"
        assert unresolved["missingFields"] == ["mobilityProfile"]
        assert unresolved["accessibilityFallbackRequired"] is True
        assert not hasattr(service, "_server_route_clarification_question")


def test_public_context_flags_cannot_authorize_saved_direction_schedule_restore() -> None:
    clear_database()
    with open_db() as connection:
        snapshot = _snapshot("plan_public_flags", "高校经典线", "B000A")
        segment = snapshot["days"][0]["segments"][0]
        segment["estimateMetadata"] = {
            "duration": {
                "preferredMinutes": 120,
                "minMinutes": 120,
                "maxMinutes": 120,
                "source": "user_locked",
                "confidence": 1.0,
                "factors": ["user_locked"],
                "userLocked": True,
            }
        }
        operation = ItineraryPatchOperation(op="replace_itinerary", fullItinerary=snapshot)

        normalized = ItineraryPatchService(connection)._normalize_visit_durations(
            [operation],
            {
                # These names may arrive in a public planningContext.  Only the
                # private server authorization produced by AgentService may
                # preserve a saved proposal schedule.
                "simpleDirectionCommit": True,
                "simpleDirectionSavedSnapshotRestore": True,
            },
        )

        normalized_segment = normalized[0].full_itinerary["days"][0]["segments"][0]
        assert normalized_segment["estimateMetadata"]["duration"]["userLocked"] is False


def test_first_turn_holiday_duration_without_dates_stops_before_model_provider_or_business_write() -> None:
    class ProviderMustNotRunBeforeDateClarification:
        model = "must-not-run-before-date-clarification"

        def __init__(self) -> None:
            self.calls = 0

        def decide_autonomy(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "ask_user",
                "actionDirective": {
                    "type": "ask_user",
                    "questions": [
                        {
                            "dimensionId": "route_decision.mobility_profile",
                            "question": "希望这两天采用怎样的主要交通节奏？",
                            "whyItMatters": "交通节奏会改变候选地点和路线比较方式。",
                            "allowFreeText": True,
                            "options": [
                                {
                                    "id": "transit",
                                    "label": "公交地铁为主",
                                    "semanticValue": {
                                        "mobilityProfile": {
                                            "transportMode": "transit",
                                            "paceClass": "standard",
                                        }
                                    },
                                },
                                {
                                    "id": "walk",
                                    "label": "步行串联为主",
                                    "semanticValue": {
                                        "mobilityProfile": {
                                            "transportMode": "walking",
                                            "paceClass": "relaxed",
                                        }
                                    },
                                },
                            ],
                        },
                        {
                            "dimensionId": "route_decision.detour_tolerance",
                            "question": "本次更看重少绕路还是体验差异？",
                            "whyItMatters": "该选择只约束真实路线证据下的候选组合。",
                            "allowFreeText": True,
                            "options": [
                                {
                                    "id": "less_detour",
                                    "label": "尽量减少绕行",
                                    "semanticValue": {
                                        "detourTolerance": {
                                            "maxGeneralizedCostDelta": 15,
                                            "maxDetourRatio": 0.15,
                                        }
                                    },
                                },
                                {
                                    "id": "more_variety",
                                    "label": "可为体验适度绕行",
                                    "semanticValue": {
                                        "detourTolerance": {
                                            "maxGeneralizedCostDelta": 30,
                                            "maxDetourRatio": 0.3,
                                        }
                                    },
                                },
                            ],
                        },
                    ],
                },
            }

        def generate(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("generation provider must not run before route preference clarification")

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "route preflight")
        provider = ProviderMustNotRunBeforeDateClarification()
        service = AgentService(connection, provider=provider)
        service.initial_planning_mode = "simple_open_v1"

        response = service.send_message(
            session.session_id,
            AgentMessageRequest(content="今年国庆参观北京高校两日游，晚上看北京夜景。"),
        )

        assert provider.calls == 0
        assert response.terminal_status == "needs_confirmation"
        request_row = connection.execute(
            "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
            (response.user_turn.id,),
        ).fetchone()
        request_payload = json.loads(request_row["agent_request_json"])
        decision_state = request_payload["agentDecisionState"]
        assert decision_state["source"] == "safe_fallback"
        assert decision_state["controlOwner"] == "safe_fallback"
        assert decision_state["reasonCodes"] == [
            "holiday_dates_unspecified",
            "date_contract_clarification_required",
        ]
        assert request_payload["resolvedTripDates"]["status"] == "unresolved"
        assert request_payload["resolvedTripDates"]["reason"] == "holiday_dates_unspecified"
        assert "没有说明具体日期" in response.assistant_turn.content
        assert [item["action"] for item in response.assistant_turn.choice_options] == ["manual_continuation"]
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_choice_executions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND role = 'user'",
                (session.session_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0


def test_missing_detour_is_not_projected_as_a_fixed_server_question() -> None:
    service = object.__new__(AgentService)
    request_context = {
        "serverExecutionProfile": "simple_open_v1",
        "requestIntentContract": {
            "routeDecisionContract": {
                "schemaVersion": "route-decision-contract-v1",
                "status": "awaiting_clarification",
                "missingFields": ["detourTolerance"],
                "mobilityProfile": {
                    "source": "explicit_request_mobility_semantics",
                    "walkingPenaltyMinutesPerKm": 1.8,
                    "transferPenaltyMinutes": 6.0,
                    "waitTimeMultiplier": 1.0,
                    "riskPenaltyMultiplier": 1.0,
                },
                "detourTolerance": None,
                "provenance": {"transportMode": "transit", "requestSemanticsPresent": True},
                "fingerprint": "trace-sess-c0ac95df77c5",
            },
            "clarificationDimensions": [{"dimensionId": "route_decision.detour_tolerance", "status": "unresolved"}],
        },
    }

    assert not hasattr(service, "_server_route_clarification_question")


def test_missing_route_preferences_use_editable_defaults_without_controller_clarification() -> None:
    class CountingAskProvider:
        model = "counting-ask"

        def __init__(self) -> None:
            self.lite_calls = 0
            self.controller_calls = 0
            self.normalization_contexts = []
            self.repair_feedbacks = []
            self.controller_contexts = []

        def normalize_clarification_batch(self, context, *, timeout_seconds):
            assert timeout_seconds == 8.0
            self.normalization_contexts.append(copy.deepcopy(context))
            return {
                "schemaVersion": "clarification-batch-normalization-v1",
                "answers": [
                    {
                        "dimensionId": "route_decision.mobility_profile",
                        "semanticValue": {
                            "mobilityProfile": {
                                "transportMode": "transit",
                                "paceClass": "relaxed",
                            }
                        },
                    }
                ],
            }

        def consume_clarification_batch_normalization_audit(self):
            return {
                "callKind": "clarification_batch_normalization",
                "captureState": "completed",
                "providerInvoked": True,
                "responseHeadersReceived": True,
                "httpStatus": 200,
                "payloadBytes": 512,
                "responseBytes": 128,
                "attemptCount": 1,
                "retryCount": 0,
            }

        def decide_autonomy_lite(self, *_args, **_kwargs):
            self.lite_calls += 1
            raise AssertionError("opaque clarification answers bypass intent classification")

        def decide_autonomy(self, _context, *, timeout_seconds, repair_feedback=""):
            self.controller_calls += 1
            self.controller_contexts.append(copy.deepcopy(_context.get("clarificationDimensions") or []))
            self.repair_feedbacks.append(
                (json.loads(repair_feedback).get("invalidPaths") if repair_feedback else [])
            )
            if not _context.get("clarificationDimensions"):
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "finish",
                    "actionDirective": {
                        "type": "finish",
                        "assistantReply": "已按可编辑默认继续。",
                    },
                }
            checkpoint = _context.get("clarificationCheckpoint")
            answers = checkpoint.get("resolvedAnswers") or [] if isinstance(checkpoint, dict) else []
            if not answers:
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "ask_user",
                    "actionDirective": {
                        "type": "ask_user",
                        "questions": [
                            {
                                "dimensionId": "route_decision.mobility_profile",
                                "question": "这次希望采用怎样的主要交通节奏？",
                                "whyItMatters": "交通节奏会改变候选组合和路线比较。",
                                "allowFreeText": True,
                                "options": [
                                    {
                                        "id": "mobility_transit_standard",
                                        "label": "公交地铁为主",
                                    },
                                    {
                                        "id": "mobility_transit_relaxed",
                                        "label": "公共交通、少走慢行",
                                    },
                                    {
                                        "id": "mobility_driving_relaxed",
                                        "label": "驾车为主、少走慢行",
                                    },
                                ],
                            },
                            {
                                "dimensionId": "route_decision.detour_tolerance",
                                "question": "本次更看重少绕路还是体验变化？",
                                "whyItMatters": "该选择只约束真实 Provider 路线矩阵。",
                                "allowFreeText": False,
                                "options": [
                                    {
                                        "id": "detour_minimal",
                                        "label": "尽量少绕路",
                                    },
                                    {
                                        "id": "detour_balanced",
                                        "label": "路线与体验均衡",
                                    },
                                    {
                                        "id": "detour_flexible",
                                        "label": "可接受较多绕路",
                                    },
                                ],
                            },
                        ],
                    },
                }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "finish",
                "actionDirective": {
                    "type": "finish",
                    "assistantReply": "路线偏好已确认。",
                },
            }

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "route answers")
        provider = CountingAskProvider()
        service = AgentService(connection, provider=provider)
        service.initial_planning_mode = "simple_open_v1"
        first = service.send_message(
            session.session_id,
            AgentMessageRequest(content="今年10月1日北京一日游。"),
        )
        assert provider.lite_calls == 0
        assert provider.controller_calls == 1, {
            "repairs": provider.repair_feedbacks,
            "dimensions": provider.controller_contexts,
        }
        assert first.assistant_turn.choice_options == []
        first_request = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                (first.user_turn.id,),
            ).fetchone()["agent_request_json"]
        )
        route_contract = first_request["requestIntentContract"]["routeDecisionContract"]
        assert route_contract["status"] == "ready", route_contract
        assert route_contract["mobilityProfileSource"] == "server_safe_default_v1"
        assert route_contract["detourToleranceSource"] == "server_safe_default_v1"
        assert first_request["requestIntentContract"]["editableDefaults"] == [
            {
                "dimensionId": "route_decision.mobility_profile",
                "label": "公共交通、标准节奏",
                "source": "server_safe_default_v1",
                "editable": True,
                "userConfirmed": False,
            },
            {
                "dimensionId": "route_decision.detour_tolerance",
                "label": "均衡路线",
                "source": "server_safe_default_v1",
                "editable": True,
                "userConfirmed": False,
            },
        ]
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0


def test_venue_family_alias_merges_nearby_phase_names_but_not_distant_homonyms() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    base = {
        "name": "星河公园",
        "address": "海淀区清河路10号",
        "latitude": 39.9991,
        "longitude": 116.3001,
        "amapId": "B000000001",
        "source": "amap-place-search",
    }
    nearby_phase = {
        **base,
        "name": "星河公园一期",
        "address": "海淀区清河路12号",
        "latitude": 39.9992,
        "longitude": 116.3002,
        "amapId": "B000000002",
    }
    distant_phase = {
        **nearby_phase,
        "address": "通州区运河路88号",
        "latitude": 39.91,
        "longitude": 116.69,
        "amapId": "B000000003",
    }

    base_aliases = SimpleOpenDirectionService._physical_poi_aliases(base)
    nearby_aliases = SimpleOpenDirectionService._physical_poi_aliases(nearby_phase)
    distant_aliases = SimpleOpenDirectionService._physical_poi_aliases(distant_phase)

    assert any(alias.startswith("venue-family:") for alias in base_aliases & nearby_aliases)
    assert not any(alias.startswith("venue-family:") for alias in base_aliases & distant_aliases)


def test_coordinator_reload_preserves_manual_normalization_audit() -> None:
    service = AgentService.__new__(AgentService)
    audit = {
        "schemaVersion": "clarification-batch-normalization-audit-v1",
        "providerName": "deepseek",
        "attemptCount": 1,
        "retryCount": 0,
        "outboundDimensionIds": ["route_decision.mobility_profile"],
        "transport": {
            "callKind": "clarification_batch_normalization",
            "captureState": "completed",
            "providerInvoked": True,
            "httpStatus": 200,
        },
    }
    previous = {"clarificationManualNormalization": audit}
    reloaded: dict = {}

    service._preserve_coordinator_request_lineage(previous, reloaded)

    assert reloaded["clarificationManualNormalization"] == audit
    assert reloaded["clarificationManualNormalization"] is not audit


def test_explicit_detour_request_continues_into_simple_direction_generation_without_clarification(monkeypatch) -> None:
    from backend.tests.unit.test_agent_service import (
        ControllerStagedInitialProvider,
        fake_amap_search_with_keyword_candidate,
        two_day_initial_day_slot_output,
    )
    from src.services.map_poi_service import MapPoiService

    class TraceRouteClarificationThenDirectionProvider(ControllerStagedInitialProvider):
        def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
            serialized = json.loads(json.dumps(dict(context), ensure_ascii=False))
            checkpoint = serialized.get("clarificationCheckpoint")
            answers = checkpoint.get("resolvedAnswers") or [] if isinstance(checkpoint, dict) else []
            detour_answered = any(
                isinstance(item, dict) and item.get("dimensionId") == "route_decision.detour_tolerance"
                for item in answers
            )
            unresolved_dimensions = {
                str(item.get("dimensionId") or "")
                for item in serialized.get("clarificationDimensions") or []
                if isinstance(item, dict) and str(item.get("status") or "") != "resolved"
            }
            if "route_decision.detour_tolerance" in unresolved_dimensions and not detour_answered:
                checkpoint_identity = (
                    {
                        "checkpointId": str(checkpoint.get("checkpointId") or ""),
                        "planningRootId": str(checkpoint.get("planningRootId") or ""),
                        "requestFingerprint": str(checkpoint.get("requestFingerprint") or ""),
                        "checkpointFingerprint": str(checkpoint.get("fingerprint") or ""),
                    }
                    if isinstance(checkpoint, dict) and checkpoint
                    else {}
                )
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "ask_user",
                    "actionDirective": {
                        "type": "ask_user",
                        "question": "为了在真实公交路线中筛选地点，你愿意接受怎样的额外绕行？",
                        "dimensionId": "route_decision.detour_tolerance",
                        "whyItMatters": "这个范围会约束候选组合的真实路线成本。",
                        "allowFreeText": True,
                        **checkpoint_identity,
                        "options": [
                            {
                                "id": "low_detour",
                                "label": "优先少绕行",
                                "semanticValue": {
                                    "detourTolerance": {
                                        "maxGeneralizedCostDelta": 15,
                                        "maxDetourRatio": 0.15,
                                    }
                                },
                                "allowsManualInput": False,
                            },
                            {
                                "id": "balanced_detour",
                                "label": "可为更合适地点适度绕行",
                                "semanticValue": {
                                    "detourTolerance": {
                                        "maxGeneralizedCostDelta": 30,
                                        "maxDetourRatio": 0.3,
                                    }
                                },
                                "allowsManualInput": False,
                            },
                        ],
                    },
                }
            draft_context = context
            if detour_answered:
                draft_context = {
                    **dict(context),
                    "clarificationDimensions": [],
                }
            decision = super().decide_autonomy(
                draft_context,
                timeout_seconds=timeout_seconds,
                repair_feedback=repair_feedback,
            )
            if decision.get("primaryAction") == "draft_itinerary":
                route_contract = (
                    serialized.get("routeDecisionContract")
                    if isinstance(serialized.get("routeDecisionContract"), dict)
                    else {}
                )
                detour = (
                    route_contract.get("detourTolerance")
                    if isinstance(route_contract.get("detourTolerance"), dict)
                    else {
                        "maxGeneralizedCostDelta": 15,
                        "maxDetourRatio": 0.15,
                    }
                )
                decision["actionDirective"]["routePlanningPolicy"].update(
                    {
                        "mobilityProfile": {
                            "transportMode": "transit",
                            "paceClass": "standard",
                        },
                        "detourEnvelope": dict(detour),
                    }
                )
            return decision

    clear_database()
    monkeypatch.setattr(MapPoiService, "search", fake_amap_search_with_keyword_candidate)
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "trace shaped route clarification")
        provider = TraceRouteClarificationThenDirectionProvider(two_day_initial_day_slot_output())
        service = AgentService(connection, provider=provider)
        service.initial_planning_mode = "simple_open_v1"
        first = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content=(
                    "今年国庆参观北京高校两日游，每天晚上逛公园。"
                    "10月1日到2日，中等预算，1人，公交地铁优先。"
                    "每天午餐想体验北京当地特色美食，并且尽量少绕路。"
                ),
                context={
                    "viewContext": {
                        "schemaVersion": "agent-view-context-v1",
                        "activeView": "overview",
                        "editingProposal": None,
                    }
                },
            ),
        )
        assert not any(
            option.get("action") == "submit_clarification_batch"
            for option in first.assistant_turn.choice_options
        )
        first_request = json.loads(
            connection.execute(
                "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
                (first.user_turn.id,),
            ).fetchone()["agent_request_json"]
        )
        route_contract = first_request["requestIntentContract"]["routeDecisionContract"]
        assert route_contract["status"] == "ready", route_contract
        assert route_contract["detourTolerance"]["maxDetourRatio"] == 0.15


def test_simple_direction_offer_appends_one_server_persisted_proposal_without_timeline_write() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction workflow")
        service = SimpleOpenDirectionService(connection)

        first = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        second = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_direction_b",
            source_assistant_turn_id="turn_assistant_b",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校公园线", "B000B"),
            request_contract={"routeDecisionContract": _route_contract()},
        )

        assert first["workflowMode"] == "simple_direction_v1"
        assert first["comparisonProjectionUpdateMode"] == "replace"
        assert len(first["comparisonProjections"]) == 1
        assert first["visibleProposalCount"] == 1
        assert [option["action"] for option in first["choiceOptions"]] == [
            "select_plan_proposal",
            "continue_plan_expansion",
            "search_travel_guide_advice",
        ]
        assert all(option["scopeKind"] == "comparison" for option in first["choiceOptions"])
        continuation = next(
            option for option in first["choiceOptions"] if option["action"] == "continue_plan_expansion"
        )
        assert continuation["kind"] == "simple_direction_more_plans"
        assert continuation["sourceAssistantTurnId"] == "turn_assistant_a"
        assert continuation["planningSelectionRootTurnId"] == "turn_root"
        assert continuation["rootPortfolioId"] == first["rootPortfolioId"]
        assert continuation["requestContractFingerprint"] == "r" * 64
        assert first["payloadProposalCount"] == 1
        assert second["comparisonProjectionUpdateMode"] == "append"
        assert len(second["comparisonProjections"]) == 1
        assert second["visibleProposalCount"] == 2
        assert second["payloadProposalCount"] == 1
        assert second["comparisonProjections"][0]["nextAction"] == "confirm_edit"
        projection = second["comparisonProjections"][0]
        assert projection["nextActionLabel"] == f"确认编辑「{projection['displayTitle']}」"
        confirmation_labels = {
            option["label"] for option in second["choiceOptions"] if option["action"] == "select_plan_proposal"
        }
        assert len(confirmation_labels) == 2
        assert all(label.startswith("确认编辑「") and label.endswith("」") for label in confirmation_labels)
        assert {option["action"] for option in second["choiceOptions"]} == {
            "select_plan_proposal",
            "continue_plan_expansion",
            "search_travel_guide_advice",
        }


def test_guide_execution_reissues_fresh_opaque_capability_for_explicit_retry(monkeypatch) -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "guide retry capability")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"

        def forbidden_controller(*_args, **_kwargs):
            raise AssertionError("guide retry must not call Lite or Full Controller")

        monkeypatch.setattr(service.provider, "decide_autonomy_lite", forbidden_controller)
        monkeypatch.setattr(service.provider, "decide_autonomy", forbidden_controller)
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京高校一日游，公共交通，适度绕行",
            "active",
        )
        source_turn_id, _proposal_id = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="高校经典线",
            poi_id="B000A",
        )
        source_turn = service._turn_response(source_turn_id)
        original_choice = next(
            option
            for option in source_turn.choice_options
            if option.get("action") == "search_travel_guide_advice"
        )
        provider_calls: list[tuple[str, dict]] = []

        def fake_search(_self, *, city: str, request_contract: dict) -> dict:
            provider_calls.append((city, copy.deepcopy(request_contract)))
            return {
                "status": "completed",
                "failureReason": None,
                "recommendations": [
                    {
                        "text": "高校访客规则可能随日期变化，出发前应再次核对。",
                        "sourceUrl": "https://travel.example/guide",
                        "poiVerificationStatus": "unverified_advice",
                    }
                ],
                "cautions": [],
                "sourceRefs": [
                    {
                        "title": "北京高校参观攻略",
                        "url": "https://travel.example/guide",
                        "sourceName": "测试攻略",
                    }
                ],
                "queryFingerprint": "q" * 64,
                "queriedAt": "2026-09-01T00:00:00+00:00",
                "caveat": "只作经验性建议，不会自动写入行程。",
                "queryCount": 1,
                "attemptedProviders": ["test-search"],
                "successfulProviders": ["test-search"],
                "failedProviders": [],
                "skippedProviders": [],
                "providerDiagnostics": [],
            }

        monkeypatch.setattr(TravelGuideAdviceService, "search", fake_search)
        before = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options", "agent_plan_proposals")
        }
        response = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="搜索普通攻略并给我建议",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_turn_id,
                        "choiceId": original_choice["id"],
                    }
                },
            ),
        )
        after = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options", "agent_plan_proposals")
        }

        assert len(provider_calls) == 1
        assert after == before
        assert response.assistant_turn.guide_advice is not None
        assert "已找到 1 条与当前目的地和体验主题匹配的普通攻略摘要" in response.assistant_turn.content
        assert "高校访客规则可能随日期变化" not in response.assistant_turn.content
        assert response.terminal_status == "success"
        retry_choice = next(
            option
            for option in response.assistant_turn.choice_options
            if option.get("action") == "search_travel_guide_advice"
        )
        assert retry_choice["label"] == "重新搜索普通攻略"
        assert retry_choice["sourceAssistantTurnId"] == response.assistant_turn.id
        assert retry_choice["id"] != original_choice["id"]

        routed_payload, routed, resolved = service._route_conversation_turn(
            session=service._session(session.session_id),
            content="重新搜索一遍攻略",
            payload=AgentMessageRequest(content="重新搜索一遍攻略", context={}),
        )

        assert routed.classification is not None
        assert routed.classification.intent == "search_travel_guide_advice"
        assert routed.model_called is False
        assert resolved.status == "unique"
        selected = routed_payload.context.selected_agent_choice
        assert selected is not None
        assert selected.source_assistant_turn_id == response.assistant_turn.id
        assert selected.choice_id == retry_choice["id"]

        retried = service.send_message(
            session.session_id,
            AgentMessageRequest(content="重新搜索一遍攻略", context={}),
        )
        final_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options", "agent_plan_proposals")
        }

        assert len(provider_calls) == 2
        assert final_counts == before
        assert retried.terminal_status == "success"
        second_retry_choice = next(
            option
            for option in retried.assistant_turn.choice_options
            if option.get("action") == "search_travel_guide_advice"
        )
        assert second_retry_choice["sourceAssistantTurnId"] == retried.assistant_turn.id
        assert second_retry_choice["id"] not in {original_choice["id"], retry_choice["id"]}
        executions = connection.execute(
            "SELECT source_turn_id, choice_id, status FROM agent_choice_executions "
            "WHERE session_id = ? AND action = 'search_travel_guide_advice' ORDER BY created_at",
            (session.session_id,),
        ).fetchall()
        assert [(row["status"]) for row in executions] == ["succeeded", "succeeded"]
        assert [(row["source_turn_id"], row["choice_id"]) for row in executions] == [
            (source_turn_id, original_choice["id"]),
            (response.assistant_turn.id, retry_choice["id"]),
        ]

        continuation_choice = next(
            option
            for option in retried.assistant_turn.choice_options
            if option.get("action") == "continue_plan_expansion"
        )
        continuation_user_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "继续生成其他方案",
            "active",
        )
        continuation_payload, continuation_route, continuation_capability = service._route_conversation_turn(
            session=service._session(session.session_id),
            content="继续生成其他方案",
            payload=AgentMessageRequest(content="继续生成其他方案", context={}),
        )

        stale_version_id = "ver_stale_guide_continuation"
        connection.execute(
            "INSERT INTO itinerary_versions "
            "(id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES (?, ?, ?, 1, 'user_timeline_mutation', ?, '2026-09-03T00:00:00+00:00')",
            (
                stale_version_id,
                session.session_id,
                session.active_plan_id,
                json.dumps(_snapshot(session.active_plan_id, "其他标签页已修改", "B000Z"), ensure_ascii=False),
            ),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            (stale_version_id, session.session_id),
        )
        connection.commit()

        with pytest.raises(HTTPException) as stale_error:
            service._build_request_context(
                service._session(session.session_id),
                "继续生成其他方案",
                continuation_payload,
                current_user_turn_id=continuation_user_turn_id,
                defer_agent_decision=True,
                allow_plan_expansion_rebind=False,
                conversation_intent_route=continuation_route.to_context(),
                conversation_capability_resolution=continuation_capability.to_context(),
            )
        assert stale_error.value.status_code == 409
        assert stale_error.value.detail["code"] == "agent_choice_version_stale"
        stale_execution_count = connection.execute(
            "SELECT COUNT(*) FROM agent_choice_executions "
            "WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?",
            (session.session_id, retried.assistant_turn.id, continuation_choice["id"]),
        ).fetchone()[0]
        assert int(stale_execution_count) == 0

        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = NULL WHERE id = ?",
            (session.session_id,),
        )
        connection.execute("DELETE FROM itinerary_versions WHERE id = ?", (stale_version_id,))
        connection.commit()
        continuation_context = service._build_request_context(
            service._session(session.session_id),
            "继续生成其他方案",
            continuation_payload,
            current_user_turn_id=continuation_user_turn_id,
            defer_agent_decision=True,
            allow_plan_expansion_rebind=False,
            conversation_intent_route=continuation_route.to_context(),
            conversation_capability_resolution=continuation_capability.to_context(),
        )

        assert continuation_capability.status == "unique"
        assert continuation_context["conversationCapability"] == {
            **continuation_capability.to_context(),
            "capability": "create_itinerary",
            "reasonCode": "server_validated_opaque_choice",
        }
        assert continuation_context["conversationCapability"]["selectedChoiceRequest"] == {
            "sourceAssistantTurnId": retried.assistant_turn.id,
            "choiceId": continuation_choice["id"],
        }
        assert continuation_context["conversationCapability"]["requestContractFingerprint"] == "r" * 64
        assert continuation_context["_simpleDirectionGenerationAuthorized"] is True
        assert simple_direction_generation_authorized(continuation_context) is True
        assert continuation_context["retryExecutionPlan"]["kind"] == "ask_retry_scope"
        assert service._conversation_intent_decision(continuation_context, cycle_index=0) is None
        assert service._retry_recovery_preempts_controller(continuation_context, cycle_index=0) is False


def test_single_page_compatibility_frontier_does_not_offer_dead_continuation() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "single page compatibility frontier")
        service = SimpleOpenDirectionService(connection)
        contract = {"routeDecisionContract": _route_contract()}
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_single_page_root",
            source_assistant_turn_id="turn_single_page_source",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=1,
        )

        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_single_page_root",
            source_user_turn_id="turn_single_page_root",
            source_assistant_turn_id="turn_single_page_result",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "单页高校方向", "B000A"),
            request_contract=contract,
        )
        frozen_summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (root["id"],),
            ).fetchone()["summary_json"]
        )

    assert frozen_summary["simpleDirectionCompatibilityFrontier"]["maxPagesPerQuery"] == 1
    assert offered["adoptionReadyProposalCount"] == 1
    assert offered["frontierStatus"] == "poi_exhausted"
    assert offered["comparisonSummary"]["remainingPoiPageCount"] == 0
    assert not any(option["action"] == "continue_plan_expansion" for option in offered["choiceOptions"])


def test_simple_direction_novelty_v2_compares_every_replaceable_day_with_all_prior_directions() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    def two_day_snapshot(plan_id: str, first_id: str, second_id: str) -> dict:
        snapshot = _snapshot(plan_id, "高校双日方向", first_id)
        first = snapshot["days"][0]["segments"][0]
        first["poi"]["name"] = f"地点{first_id}"
        first["semanticMetadata"]["scheduleConstraints"] = {"replaceablePoi": True}
        second = copy.deepcopy(first)
        second["id"] = f"seg_{second_id}"
        second["poi"]["id"] = f"poi_{second_id}"
        second["poi"]["amapId"] = second_id
        second["poi"]["name"] = f"地点{second_id}"
        second["semanticMetadata"]["dayNumber"] = 2
        second["semanticMetadata"]["planningSlotId"] = f"slot_{second_id}"
        second["semanticMetadata"]["occurrenceId"] = "occ:goal_campus:day:2"
        snapshot["days"].append(
            {
                "id": f"day_{second_id}",
                "dayNumber": 2,
                "date": "2026-10-02",
                "title": "第二天",
                "totalEstimatedCost": 0,
                "segments": [second],
            }
        )
        return snapshot

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "novelty v2")
        service = SimpleOpenDirectionService(connection)
        first = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_novelty_root",
            source_user_turn_id="turn_novelty_root",
            source_assistant_turn_id="turn_novelty_a",
            expected_base_version_id=None,
            source_observation_fingerprint="a" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=two_day_snapshot(session.active_plan_id, "B000A6EA36", "B000SECOND1"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        second = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_novelty_root",
            source_user_turn_id="turn_novelty_b",
            source_assistant_turn_id="turn_novelty_b",
            expected_base_version_id=None,
            source_observation_fingerprint="b" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=two_day_snapshot(session.active_plan_id, "B000A7O5PK", "B000SECOND2"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        blocked = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_novelty_root",
            source_user_turn_id="turn_novelty_c",
            source_assistant_turn_id="turn_novelty_c",
            expected_base_version_id=None,
            source_observation_fingerprint="c" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=two_day_snapshot(session.active_plan_id, "B000A6EA36", "B000SECOND3"),
            request_contract={"routeDecisionContract": _route_contract()},
        )

    assert first["proposalDelta"] == 1
    assert second["proposalDelta"] == 1, second.get("simpleDirectionNoveltyEvidence")
    assert blocked["proposalDelta"] == 0
    assert blocked["reasonCode"] == "no_material_novelty"
    evidence = blocked["simpleDirectionNoveltyEvidence"]
    assert evidence["schemaVersion"] == "simple-direction-novelty-v2"
    assert evidence["priorDirectionExclusionApplied"] is True
    assert evidence["passed"] is False
    assert evidence["comparisons"][0]["dayEvidence"][0]["changedCount"] == 0


def test_compact_route_pending_direction_is_read_only_and_has_no_confirm_capability() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    contract = AgentService._compile_route_decision_contract(
        request_text=("公交地铁优先，景点间距离不要太远；绕路最多10分钟，绕路比例最多20%"),
        experience_specs=[],
    )
    snapshot = _snapshot("plan_compact_blocked", "紧凑路线待核验", "B000A")
    snapshot["routeDecisionContract"] = copy.deepcopy(contract)
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": [],
        "verifiedPairs": [],
        "routeCoverageComplete": False,
        "adjacentLegCompliance": "pending",
        "detourCompliance": "pending",
        "failureReason": "provider_route_matrix_incomplete",
    }

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "compact blocked")
        response = SimpleOpenDirectionService(connection).offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_compact_root",
            source_user_turn_id="turn_compact_root",
            source_assistant_turn_id="turn_compact_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract={"routeDecisionContract": contract},
        )

    projection = response["comparisonProjections"][0]
    assert projection["adoptionReady"] is False
    assert "route_evidence_incomplete" in projection["blockingReasons"]
    assert not any(option["action"] == "select_plan_proposal" for option in response["choiceOptions"])
    assert not any(option["action"] == "continue_plan_expansion" for option in response["choiceOptions"])


def test_route_blocked_soft_partial_does_not_project_an_unexecutable_frontier() -> None:
    """A -06-shaped partial must not claim `has_more` without a signed capability."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    contract = _route_contract(compact=True)
    snapshot = _live_shaped_simple_partial_snapshot("plan_route_blocked_soft_partial")
    snapshot["days"] = [copy.deepcopy(snapshot["days"][0])]
    for segment in snapshot["days"][0]["segments"]:
        segment["semanticMetadata"]["requiresRouteEdge"] = True
    snapshot["routeDecisionContract"] = copy.deepcopy(contract)
    snapshot["portfolioPendingSlots"] = [
        {
            "id": "pending:day2_goal_meal_2",
            "state": "pending",
            "groundingStatus": "unresolved",
            "required": False,
            "requirementLevel": "explicit_soft",
            "intentType": "meal",
            "kind": "meal",
            "goalId": "goal_meal",
            "sourceGoalId": "goal_meal",
            "occurrenceId": "occ:goal_meal:day:2",
            "planningSlotId": "day2_goal_meal_2",
            "poolId": "goal_meal_pool",
            "dayNumber": 2,
            "lineageAuthority": "goal_occurrence_compiler",
            "routeAnchorExpected": False,
            "simpleDirectionProviderExhausted": True,
            "simpleDirectionRequirementLineageConflict": False,
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
        }
    ]
    snapshot["simpleOpenRouteAssignment"] = {
        "schemaVersion": "simple-open-route-evidence-v2",
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": [],
        "verifiedPairs": [],
        "providerRoutePairs": [],
        "routeCoverageComplete": False,
        "routeFeasibilityExhausted": False,
        "routeProviderAttemptCount": 0,
        "adjacentLegCompliance": "pending",
        "detourCompliance": "not_evaluated",
        "topologyCompliance": "failed",
        "failureReason": "topology_constraint_exceeded",
    }

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "route blocked soft partial")
        response = SimpleOpenDirectionService(connection).offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_route_blocked_soft_root",
            source_user_turn_id="turn_route_blocked_soft_root",
            source_assistant_turn_id="turn_route_blocked_soft_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract={"routeDecisionContract": contract},
        )
        formal_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        }

    continuations = [
        option for option in response["choiceOptions"] if option.get("action") == "continue_plan_expansion"
    ]
    assert continuations == []
    assert response["frontierStatus"] == "route_feasible_exhausted"
    assert response["comparisonSummary"]["frontierStatus"] == response["frontierStatus"]
    assert response["comparisonSummary"]["blockingLayer"] == "route"
    assert response["comparisonSummary"]["lastOutcomeReason"] == "topology_constraint_exceeded"
    projection = response["comparisonProjections"][0]
    assert projection["routeExpectedLegCount"] == 2
    assert projection["routeVerifiedLegCount"] == 0
    assert projection["routeSummary"] == "相邻路线证据不足 0/2 段"
    assert "topology_constraint_exceeded" in projection["blockingReasons"]
    assert "日内停靠顺序回折超过当前偏好" in projection["blockingReasonLabels"]
    assert not any(option["action"] == "select_plan_proposal" for option in response["choiceOptions"])
    assert formal_counts == {
        "itinerary_versions": 0,
        "itinerary_patches": 0,
        "route_options": 0,
    }


def test_provider_exhausted_hard_poi_partial_without_qualification_frontier_can_expand_once() -> None:
    """A new POI-blocked direction must not advertise an unusable frontier."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    contract = {"routeDecisionContract": _route_contract(compact=True)}
    snapshot = _snapshot("plan_provider_exhausted_partial", "高校夜景待补方向", "B000A")
    snapshot["status"] = "partial"
    snapshot["routeDecisionContract"] = copy.deepcopy(contract["routeDecisionContract"])
    snapshot["portfolioPendingSlots"] = [
        {
            "id": "pending:day1_goal_night_view_2",
            "state": "pending",
            "groundingStatus": "unresolved",
            "required": True,
            "requirementLevel": "hard",
            "intentType": "night_view",
            "kind": "night_view",
            "goalId": "goal_night_view",
            "sourceGoalId": "goal_night_view",
            "occurrenceId": "occ:goal_night_view:day:1",
            "planningSlotId": "day1_goal_night_view_2",
            "poolId": "goal_night_view_pool",
            "dayNumber": 1,
            "lineageAuthority": "goal_occurrence_compiler",
            "routeAnchorExpected": True,
            "simpleDirectionProviderExhausted": True,
            "simpleDirectionRequirementLineageConflict": False,
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
        }
    ]
    snapshot["simpleOpenRouteAssignment"] = {
        "schemaVersion": "simple-open-route-evidence-v2",
        "routeContractFingerprint": contract["routeDecisionContract"]["fingerprint"],
        "expectedPairs": [],
        "verifiedPairs": [],
        "providerRoutePairs": [],
        "routeCoverageComplete": False,
        "routeFeasibilityExhausted": False,
        "adjacentLegCompliance": "pending",
        "detourCompliance": "not_evaluated",
        "topologyCompliance": "pending",
        "failureReason": "candidate_assignment_incomplete",
    }

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "provider exhausted partial")
        response = SimpleOpenDirectionService(connection).offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_provider_exhausted_root",
            source_user_turn_id="turn_provider_exhausted_root",
            source_assistant_turn_id="turn_provider_exhausted_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract=contract,
        )
        formal_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        }

    assert response["proposalDelta"] == 1
    assert response["adoptionReadyProposalCount"] == 0
    assert response["partialComparisonProposalCount"] == 1
    assert response["frontierStatus"] == "has_more"
    assert [option["action"] for option in response["choiceOptions"]] == [
        "continue_plan_expansion",
        "search_travel_guide_advice",
    ]
    continuation = response["choiceOptions"][0]
    assert continuation["sourceAssistantTurnId"] == "turn_provider_exhausted_assistant"
    assert continuation["planningSelectionRootTurnId"] == "turn_provider_exhausted_root"
    assert continuation["requestContractFingerprint"] == "r" * 64
    assert formal_counts == {
        "itinerary_versions": 0,
        "itinerary_patches": 0,
        "route_options": 0,
    }


@pytest.mark.parametrize(
    ("seed_number", "expected_name"),
    [(0, "清华大学"), (1, "北京航空航天大学"), (4, "北京大学"), (8, "中国人民大学")],
)
def test_semantically_rejected_required_poi_keeps_partial_read_only_but_can_expand_frontier(
    monkeypatch,
    seed_number: int,
    expected_name: str,
) -> None:
    """A successful Provider call with rejected POIs is not provider_pending."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    monkeypatch.setattr(
        SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(lambda: format(seed_number, "032x"))
    )
    qualification_evidence = {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "moe_project_classification",
        "qualificationValue": "985",
        "entities": [
            {"canonicalName": name, "locality": "北京"}
            for name in ["清华大学", "北京大学", "中国人民大学", "北京航空航天大学"]
        ],
    }
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: copy.deepcopy(qualification_evidence)),
    )
    contract = {
        "routeDecisionContract": _route_contract(compact=True),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    request_fingerprint = "r" * 64

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "partial frontier")
        service = SimpleOpenDirectionService(connection)
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_partial_root",
            source_assistant_turn_id="turn_partial_initial",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=3,
        )
        attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_partial",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_B000A"}],
            request_contract_fingerprint=request_fingerprint,
        )
        assignment = attempt["campusAssignments"][0]
        evidence_entity = next(
            entity
            for entity in qualification_evidence["entities"]
            if EntityQualificationEvidenceService.entity_fingerprint(
                evidence_fingerprint=qualification_evidence["contentSha256"], entity=entity
            )
            == assignment["evidenceEntityFingerprint"]
        )
        assert evidence_entity["canonicalName"] == assignment["canonicalName"] == expected_name
        snapshot = _snapshot(
            session.active_plan_id,
            "高校公园待补方向",
            "B" + assignment["evidenceEntityFingerprint"][:10].upper(),
            poi_name=evidence_entity["canonicalName"],
        )
        campus_segment = snapshot["days"][0]["segments"][0]
        campus_segment["semanticMetadata"]["planningSlotId"] = assignment["slotId"]
        assert campus_segment["poi"]["name"] == evidence_entity["canonicalName"]
        snapshot["status"] = "partial"
        snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending:day1_evening_park",
                "state": "pending",
                "groundingStatus": "unresolved",
                "required": True,
                "requirementLevel": "hard",
                "intentType": "park",
                "kind": "park",
                "goalId": "goal_park",
                "sourceGoalId": "goal_park",
                "occurrenceId": "occ:goal_park:day:1",
                "planningSlotId": "day1_evening_park",
                "poolId": "park_pool",
                "dayNumber": 1,
                "lineageAuthority": "goal_occurrence_compiler",
                "routeAnchorExpected": True,
                "simpleDirectionProviderExhausted": True,
                "simpleDirectionRequirementLineageConflict": False,
                "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
            }
        ]
        snapshot["simpleOpenRouteAssignment"] = {
            "schemaVersion": "simple-open-route-evidence-v2",
            "routeContractFingerprint": contract["routeDecisionContract"]["fingerprint"],
            "expectedPairs": [],
            "verifiedPairs": [],
            "providerRoutePairs": [],
            "routeCoverageComplete": False,
            "routeFeasibilityExhausted": False,
            "adjacentLegCompliance": "pending",
            "detourCompliance": "not_evaluated",
            "topologyCompliance": "pending",
            "failureReason": "candidate_assignment_incomplete",
        }
        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_partial_root",
            source_user_turn_id="turn_partial_root",
            source_assistant_turn_id="turn_partial_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=request_fingerprint,
            snapshot=snapshot,
            request_contract=contract,
            frontier_execution_id="choice_exec_partial",
            frontier_outcomes=[
                {
                    "slotId": assignment["slotId"],
                    "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
                    "providerOutcome": "success",
                    "selectedAmapId": campus_segment["poi"]["amapId"],
                    "queryFingerprint": assignment["queryFingerprint"],
                    "page": assignment["page"],
                    "reasonCode": None,
                }
            ],
            slot_frontier_outcomes=[],
        )

    assert response["proposalDelta"] == 1
    assert response["adoptionReadyProposalCount"] == 0
    assert response["partialComparisonProposalCount"] == 1
    assert response["frontierStatus"] == "has_more"
    assert response["comparisonSummary"]["blockingLayer"] == "poi"
    assert response["comparisonSummary"]["lastOutcomeReason"] == "candidate_assignment_incomplete"
    assert [option["action"] for option in response["choiceOptions"]] == [
        "continue_plan_expansion",
        "search_travel_guide_advice",
    ]
    assert response["choiceOptions"][0]["kind"] == "simple_direction_more_plans"


@pytest.mark.parametrize(
    ("max_pages", "expected_frontier_status", "expect_continuation"),
    [(2, "has_more", True), (1, "poi_exhausted", False)],
)
@pytest.mark.parametrize(("seed_number", "expected_success_name"), [(0, "北京大学"), (1, "清华大学")])
def test_mixed_campus_frontier_batch_is_settled_as_incomplete_without_proposal(
    monkeypatch,
    max_pages: int,
    expected_frontier_status: str,
    expect_continuation: bool,
    seed_number: int,
    expected_success_name: str,
) -> None:
    """One admitted campus cannot turn a partly rejected batch into a novelty collision."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    monkeypatch.setattr(
        SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(lambda: format(seed_number, "032x"))
    )
    qualification_evidence = {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "moe_project_classification",
        "qualificationValue": "985",
        "entities": [
            {"canonicalName": "清华大学", "locality": "北京"},
            {"canonicalName": "北京大学", "locality": "北京"},
        ],
    }
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: copy.deepcopy(qualification_evidence)),
    )
    contract = {
        "routeDecisionContract": _route_contract(compact=True),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    request_fingerprint = "r" * 64

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "mixed campus frontier")
        service = SimpleOpenDirectionService(connection)
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_mixed_root",
            source_assistant_turn_id="turn_mixed_initial",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=max_pages,
        )
        attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_mixed",
            campus_slots=[
                {"dayNumber": 1, "slotId": "slot_day_1_campus"},
                {"dayNumber": 2, "slotId": "slot_day_2_campus"},
            ],
            request_contract_fingerprint=request_fingerprint,
        )
        rejected_assignment, successful_assignment = attempt["campusAssignments"]
        evidence_entity = next(
            entity
            for entity in qualification_evidence["entities"]
            if EntityQualificationEvidenceService.entity_fingerprint(
                evidence_fingerprint=qualification_evidence["contentSha256"], entity=entity
            )
            == successful_assignment["evidenceEntityFingerprint"]
        )
        assert evidence_entity["canonicalName"] == successful_assignment["canonicalName"] == expected_success_name
        selected_amap_id = "B" + successful_assignment["evidenceEntityFingerprint"][:10].upper()
        snapshot = _snapshot(
            session.active_plan_id,
            "部分高校已核验方向",
            selected_amap_id,
            poi_name=evidence_entity["canonicalName"],
        )
        assert snapshot["days"][0]["segments"][0]["poi"]["name"] == evidence_entity["canonicalName"]
        snapshot["requiredPlanningDayNumbers"] = [1, 2]
        snapshot["desiredDensityAnchorTargets"] = {"1": 1, "2": 1}
        day = snapshot["days"][0]
        day["id"] = "day_mixed_2"
        day["dayNumber"] = 2
        day["date"] = "2026-10-02"
        segment = day["segments"][0]
        segment["semanticMetadata"].update(
            {
                "planningSlotId": successful_assignment["slotId"],
                "dayNumber": 2,
                "occurrenceId": "occ:goal_campus:day:2",
            }
        )
        segment["poi"]["amapId"] = selected_amap_id
        frontier_outcomes = [
            {
                "slotId": rejected_assignment["slotId"],
                "evidenceEntityFingerprint": rejected_assignment["evidenceEntityFingerprint"],
                "providerOutcome": "rejected",
                "selectedAmapId": None,
                "queryFingerprint": rejected_assignment["queryFingerprint"],
                "page": rejected_assignment["page"],
                "reasonCode": "campus_assignment_candidate_rejected",
                "rejectionReasonCodes": ["exact_entity_mismatch"],
            },
            {
                "slotId": successful_assignment["slotId"],
                "evidenceEntityFingerprint": successful_assignment["evidenceEntityFingerprint"],
                "providerOutcome": "success",
                "selectedAmapId": selected_amap_id,
                "queryFingerprint": successful_assignment["queryFingerprint"],
                "page": successful_assignment["page"],
                "reasonCode": None,
            },
        ]

        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_mixed_root",
            source_user_turn_id="turn_mixed_user",
            source_assistant_turn_id="turn_mixed_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=request_fingerprint,
            snapshot=snapshot,
            request_contract=contract,
            frontier_execution_id="choice_exec_mixed",
            frontier_outcomes=frontier_outcomes,
            slot_frontier_outcomes=[],
        )
        summary = service.store.summary(portfolio_id=root["id"])
        formal_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "agent_plan_proposals",
                "itinerary_versions",
                "itinerary_patches",
                "route_options",
                "timeline_mutation_transactions",
            )
        }

    assert response["status"] == "frontier_advanced"
    assert response["reasonCode"] == (
        "simple_direction_candidate_batch_incomplete_frontier_remaining"
        if expected_frontier_status == "has_more"
        else "simple_direction_candidate_batch_incomplete_frontier_exhausted"
    )
    assert response["frontierStatus"] == expected_frontier_status
    assert response["proposalDelta"] == 0
    assert response["frontierAttemptConsumed"] is True
    assert response["checkedCampusCount"] == 2
    assert response["rejectedCampusCount"] == 1
    assert response["campusRejectionReasonCounts"] == {"exact_entity_mismatch": 1}
    assert response["simpleDirectionNoveltyEvidence"]["campusAssignmentsGrounded"] is False
    assert (
        any(option["action"] == "continue_plan_expansion" for option in response["choiceOptions"])
        is expect_continuation
    )
    settled = summary["simpleDirectionFrontierAttempts"]["choice_exec_mixed"]
    assert settled["status"] == "reconciled"
    assert settled["disposition"] == "assigned_partial"
    assert settled["proposalId"] is None
    frontier_entities = {
        item["evidenceEntityFingerprint"]: item
        for item in summary["simpleDirectionFrontier"]["qualifiedEntityFrontier"]
    }
    rejected_entity = frontier_entities[rejected_assignment["evidenceEntityFingerprint"]]
    successful_entity = frontier_entities[successful_assignment["evidenceEntityFingerprint"]]
    assert rejected_entity["state"] == ("untried" if max_pages > 1 else "rejected")
    assert rejected_entity["attemptedPages"] == [1]
    assert rejected_entity["reasonCode"] == (
        "poi_page_remaining" if max_pages > 1 else "campus_assignment_candidate_rejected"
    )
    assert successful_entity["state"] == "assigned_partial"
    assert successful_entity["canonicalAmapId"] == selected_amap_id
    assert successful_entity["assignedProposalId"] is None
    assert formal_counts == {
        "agent_plan_proposals": 0,
        "itinerary_versions": 0,
        "itinerary_patches": 0,
        "route_options": 0,
        "timeline_mutation_transactions": 0,
    }


@pytest.mark.parametrize(
    ("exploration_seed", "expected_first_name"),
    [("0" * 32, "清华大学"), ("3" * 32, "中国人民大学")],
)
def test_rejected_campus_page_is_settled_and_reissues_next_frontier_capability(
    monkeypatch,
    exploration_seed: str,
    expected_first_name: str,
) -> None:
    """A successful AMap page with no admissible campus must not be retried forever."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    monkeypatch.setattr(SimpleOpenDirectionService, "_new_exploration_seed", staticmethod(lambda: exploration_seed))
    qualification_evidence = {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "moe_project_classification",
        "qualificationValue": "985",
        "entities": [
            {"canonicalName": "清华大学", "locality": "北京"},
            {"canonicalName": "中国人民大学", "locality": "北京"},
        ],
    }
    evidence_by_fingerprint = {
        EntityQualificationEvidenceService.entity_fingerprint(
            evidence_fingerprint=qualification_evidence["contentSha256"], entity=entity
        ): entity
        for entity in qualification_evidence["entities"]
    }
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: copy.deepcopy(qualification_evidence)),
    )
    contract = {
        "routeDecisionContract": _route_contract(),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    request_fingerprint = "r" * 64

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "rejected campus page")
        service = SimpleOpenDirectionService(connection)
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_frontier_root",
            source_assistant_turn_id="turn_initial_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=3,
        )
        first_attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_ready",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_B000A"}],
            request_contract_fingerprint=request_fingerprint,
        )
        first_assignment = first_attempt["campusAssignments"][0]
        first_name = evidence_by_fingerprint[first_assignment["evidenceEntityFingerprint"]]["canonicalName"]
        assert first_name == first_assignment["canonicalName"] == expected_first_name
        first_snapshot = _snapshot(
            session.active_plan_id,
            f"{first_name}校园方向",
            "B" + first_assignment["evidenceEntityFingerprint"][:10].upper(),
            poi_name=first_name,
        )
        first_segment = first_snapshot["days"][0]["segments"][0]
        first_segment["semanticMetadata"]["planningSlotId"] = first_assignment["slotId"]
        assert first_segment["poi"]["name"] == first_name
        first = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_frontier_root",
            source_user_turn_id="turn_frontier_root",
            source_assistant_turn_id="turn_ready_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint=request_fingerprint,
            snapshot=first_snapshot,
            request_contract=contract,
            frontier_execution_id="choice_exec_ready",
            frontier_outcomes=[
                {
                    "slotId": first_assignment["slotId"],
                    "evidenceEntityFingerprint": first_assignment["evidenceEntityFingerprint"],
                    "providerOutcome": "success",
                    "selectedAmapId": first_segment["poi"]["amapId"],
                    "queryFingerprint": first_assignment["queryFingerprint"],
                    "page": first_assignment["page"],
                    "reasonCode": None,
                }
            ],
            slot_frontier_outcomes=[],
        )
        rejected_attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_rejected_page",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_next_campus"}],
            request_contract_fingerprint=request_fingerprint,
        )
        rejected_assignment = rejected_attempt["campusAssignments"][0]
        rejected_fingerprint = rejected_assignment["evidenceEntityFingerprint"]
        rejected_name = evidence_by_fingerprint[rejected_fingerprint]["canonicalName"]
        assert rejected_name == rejected_assignment["canonicalName"]
        assert rejected_name != first_name
        assert {first_name, rejected_name} == {entity["canonicalName"] for entity in qualification_evidence["entities"]}
        assert (
            EntityQualificationEvidenceService.validate_binding(
                rejected_assignment["qualificationBinding"],
                expected_planning_root_id="turn_frontier_root",
                expected_request_contract_fingerprint=request_fingerprint,
                expected_entity_fingerprint=rejected_fingerprint,
                expected_canonical_name=rejected_name,
            )
            == ""
        )
        rejected_outcomes = [
            {
                "slotId": rejected_assignment["slotId"],
                "evidenceEntityFingerprint": rejected_assignment["evidenceEntityFingerprint"],
                "providerOutcome": "rejected",
                "selectedAmapId": None,
                "queryFingerprint": rejected_assignment["queryFingerprint"],
                "page": rejected_assignment["page"],
                "reasonCode": "campus_assignment_candidate_rejected",
                "rejectionReasonCodes": ["exact_entity_mismatch"],
            }
        ]
        agent = AgentService(connection)
        rejected_user_turn_id = agent._insert_turn(
            session.session_id,
            "user",
            "继续探索其他方向",
            "active",
        )
        rejected_assistant_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "处理中",
            "streaming",
        )
        request_context = {
            "requestIntentContract": copy.deepcopy(contract),
            "planningSelectionRootTurnId": "turn_frontier_root",
            "_simpleDirectionRequestContractFingerprint": request_fingerprint,
        }
        pipeline_context = {
            "city": "北京",
            "requestIntentContract": copy.deepcopy(contract),
            "simpleDirectionFrontierExecutionId": "choice_exec_rejected_page",
            "simpleDirectionFrontierOutcomes": rejected_outcomes,
            "simpleDirectionSlotFrontierOutcomes": [],
            "simpleDirectionFrontierPreparation": {
                "planningSelectionRootTurnId": "turn_frontier_root",
                "rootPortfolioId": root["id"],
                "requestContractFingerprint": request_fingerprint,
            },
        }
        response = agent._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=rejected_user_turn_id,
            assistant_turn_id=rejected_assistant_turn_id,
            content="继续探索其他方向",
            request_context=request_context,
            pipeline_context=pipeline_context,
            session_before=agent._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates={"status": "resolved", "dates": ["2026-10-01"]},
            grounding_report={"resultState": "simple_open_failed_no_real_poi"},
            snapshot={},
            tool_events=[],
            settle_unresolved_frontier=True,
        )
        response_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (rejected_assistant_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        summary = service.store.summary(portfolio_id=root["id"])
        next_attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="choice_exec_next_page",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_next_campus"}],
            request_contract_fingerprint=request_fingerprint,
        )

    assert first["proposalDelta"] == 1
    assert response.terminal_status == "needs_confirmation"
    assert "下一次会从后续候选页继续，不会重复本页" in response.assistant_turn.content
    assert response_payload["proposalDelta"] == 0
    assert response_payload["reasonCode"] == "simple_direction_campus_candidate_page_rejected_frontier_remaining"
    assert response_payload["frontierAttemptConsumed"] is True
    assert response_payload["campusRejectionReasonCounts"] == {"exact_entity_mismatch": 1}
    assert "主要拒绝原因：不是指定高校 1 个" in response.assistant_turn.content
    assert {option["action"] for option in response.assistant_turn.choice_options} == {
        "select_plan_proposal",
        "continue_plan_expansion",
        "search_travel_guide_advice",
    }
    continuation = next(
        option for option in response.assistant_turn.choice_options if option["action"] == "continue_plan_expansion"
    )
    assert continuation["sourceAssistantTurnId"] == rejected_assistant_turn_id
    settled = summary["simpleDirectionFrontierAttempts"]["choice_exec_rejected_page"]
    assert settled["status"] == "reconciled"
    rejected_entity = next(
        item
        for item in summary["simpleDirectionFrontier"]["qualifiedEntityFrontier"]
        if item["evidenceEntityFingerprint"] == rejected_fingerprint
    )
    assert rejected_entity["canonicalName"] == rejected_name
    assert rejected_entity["state"] == "untried"
    assert rejected_entity["attemptedPages"] == [1]
    assert rejected_entity["reasonCode"] == "poi_page_remaining"
    assert next_attempt["campusAssignments"][0]["evidenceEntityFingerprint"] == rejected_fingerprint
    assert next_attempt["campusAssignments"][0]["canonicalName"] == rejected_name
    assert next_attempt["campusAssignments"][0]["page"] == 2
    assert (
        next_attempt["campusAssignments"][0]["qualificationBindingFingerprint"]
        == rejected_assignment["qualificationBindingFingerprint"]
    )


def test_rejected_first_campus_page_offers_next_page_without_placeholder(
    monkeypatch,
) -> None:
    """The first rejected page remains zero-write but still has an explicit continuation."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    qualification_evidence = {
        "schemaVersion": "entity-qualification-evidence-v1",
        "contentSha256": "e" * 64,
        "qualificationScheme": "moe_project_classification",
        "qualificationValue": "985",
        "entities": [{"canonicalName": "中国人民大学", "locality": "北京"}],
    }
    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "qualified_entities",
        classmethod(lambda _cls, **_kwargs: copy.deepcopy(qualification_evidence)),
    )
    contract = {
        "routeDecisionContract": _route_contract(),
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification",
            "qualificationValue": "985",
        },
    }
    request_fingerprint = "r" * 64

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "first rejected campus page")
        service = SimpleOpenDirectionService(connection)
        root = service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_first_root",
            source_assistant_turn_id="turn_first_source",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint=request_fingerprint,
            request_contract=contract,
            locality="北京",
            max_pages_per_query=2,
        )
        attempt = service.claim_frontier_assignment(
            portfolio_id=root["id"],
            execution_id="initial:turn_first_assistant",
            campus_slots=[{"dayNumber": 1, "slotId": "slot_RUC"}],
            request_contract_fingerprint=request_fingerprint,
        )
        assignment = attempt["campusAssignments"][0]
        response = service.settle_unresolved_frontier_attempt(
            session_id=session.session_id,
            planning_root_id="turn_first_root",
            source_assistant_turn_id="turn_first_assistant",
            expected_base_version_id=None,
            request_contract_fingerprint=request_fingerprint,
            frontier_execution_id="initial:turn_first_assistant",
            frontier_outcomes=[
                {
                    "slotId": assignment["slotId"],
                    "evidenceEntityFingerprint": assignment["evidenceEntityFingerprint"],
                    "providerOutcome": "rejected",
                    "selectedAmapId": None,
                    "queryFingerprint": assignment["queryFingerprint"],
                    "page": assignment["page"],
                    "reasonCode": "campus_assignment_candidate_rejected",
                    "rejectionReasonCodes": ["exact_entity_mismatch"],
                }
            ],
            slot_frontier_outcomes=[],
        )
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]
        patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0]

    assert response["proposalDelta"] == 0
    assert response["adoptionReadyProposalCount"] == 0
    assert response["frontierStatus"] == "has_more"
    assert [option["action"] for option in response["choiceOptions"]] == [
        "continue_plan_expansion",
        "search_travel_guide_advice",
    ]
    assert response["choiceOptions"][0]["sourceAssistantTurnId"] == "turn_first_assistant"
    assert version_count == 0
    assert patch_count == 0


def _compact_two_anchor_snapshot() -> dict:
    snapshot = _snapshot("plan_compact_pairs", "清华燕园紧凑线", "B000A")
    first = snapshot["days"][0]["segments"][0]
    second = copy.deepcopy(first)
    second["id"] = "seg_B000B"
    second["startTime"] = "12:00"
    second["endTime"] = "14:00"
    second["poi"].update(
        {
            "id": "poi_B000B",
            "amapId": "B000A7O5PK",
            "name": "北京大学",
            "latitude": 39.9869,
            "longitude": 116.3059,
        }
    )
    second["semanticMetadata"].update(
        {
            "goalId": "goal_campus_second",
            "sourceGoalId": "goal_campus_second",
            "planningSlotId": "slot_B000B",
            "occurrenceId": "occ:goal_campus_second:day:1",
        }
    )
    snapshot["days"][0]["segments"].append(second)
    snapshot["desiredDensityAnchorTargets"] = {"1": 2}
    contract = RouteInsertionScorer.build_route_decision_contract(
        source="request_intent_contract",
        provenance={"transportMode": "transit", "requestSemanticsPresent": True},
        detour_tolerance={"maxGeneralizedCostDelta": 10.0, "maxDetourRatio": 0.2},
        mobility_profile={
            "source": "explicit_request_mobility_semantics",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
        adjacent_leg_constraint={
            "candidateSearchRadiusMeters": 5000,
            "maxProviderTravelMinutes": 45,
        },
        topology_constraint={"maxBacktrackRatio": 0.15},
    )
    assert contract is not None
    snapshot["routeDecisionContract"] = {
        "schemaVersion": "route-decision-contract-v1",
        "status": "ready",
        "missingFields": [],
        **contract,
    }
    return snapshot


def test_compact_route_aggregate_flags_cannot_replace_frozen_pair_evidence() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    contract = snapshot["routeDecisionContract"]
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": [],
        "verifiedPairs": [],
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }

    verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert verifier["confirmationPassed"] is False
    assert verifier["frozenRoutePairEvidencePassed"] is False
    assert verifier["frozenRoutePairEvidenceReason"] == "route_expected_pairs_mismatch"
    assert "route_evidence_incomplete" in verifier["hardFailures"]


def test_compact_route_requires_exact_positive_in_envelope_provider_pairs() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    contract = snapshot["routeDecisionContract"]
    expected = [{"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"}]
    verified_pair = {
        **expected[0],
        "transportMode": "transit",
        "durationSeconds": 1800,
        "distanceMeters": 4000,
        "provider": "amap-webservice",
        "queriedAt": "2026-08-23T00:00:00+00:00",
    }
    verified_pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(
        verified_pair
    )
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": expected,
        "verifiedPairs": [verified_pair],
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }

    passing = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert passing["frozenRoutePairEvidencePassed"] is True, passing["frozenRoutePairEvidenceReason"]

    legacy_pair = {**verified_pair, "transportMode": "public_transit"}
    legacy_pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(legacy_pair)
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [legacy_pair]
    legacy = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert legacy["confirmationPassed"] is True, legacy

    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [verified_pair]
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["durationSeconds"] = 2701
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["providerEvidenceFingerprint"] = (
        SimpleOpenDirectionService._provider_evidence_fingerprint(
            snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]
        )
    )
    exceeded = SimpleOpenDirectionService._proposal_verifier(snapshot)

    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"] = [
        {
            **verified_pair,
            "durationSeconds": 1800,
            "distanceMeters": 5001,
        }
    ]
    snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]["providerEvidenceFingerprint"] = (
        SimpleOpenDirectionService._provider_evidence_fingerprint(
            snapshot["simpleOpenRouteAssignment"]["verifiedPairs"][0]
        )
    )
    distance_exceeded = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert passing["confirmationPassed"] is True, passing
    assert passing["frozenRoutePairEvidencePassed"] is True
    assert exceeded["confirmationPassed"] is False
    assert exceeded["frozenRoutePairEvidenceReason"] == "adjacent_leg_limit_exceeded"
    assert distance_exceeded["confirmationPassed"] is False
    assert distance_exceeded["frozenRoutePairEvidenceReason"] == "adjacent_leg_limit_exceeded"


def test_compact_route_pair_evidence_distinguishes_same_amap_edge_on_two_days() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    day_two = copy.deepcopy(snapshot["days"][0])
    day_two.update({"id": "day_repeat_2", "dayNumber": 2, "date": "2026-10-02"})
    for index, segment in enumerate(day_two["segments"], start=1):
        segment["id"] = f"seg_repeat_day2_{index}"
        semantic = segment["semanticMetadata"]
        semantic["dayNumber"] = 2
        semantic["planningSlotId"] = f"slot_repeat_day2_{index}"
        semantic["occurrenceId"] = f"occ:repeat:{index}:day:2"
    snapshot["days"].append(day_two)

    expected = [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "slot_B000A",
            "toSegmentId": "slot_B000B",
            "fromAmapId": "B000A6EA36",
            "toAmapId": "B000A7O5PK",
        },
        {
            "dayNumber": 2,
            "pairOrdinal": 1,
            "fromSegmentId": "slot_repeat_day2_1",
            "toSegmentId": "slot_repeat_day2_2",
            "fromAmapId": "B000A6EA36",
            "toAmapId": "B000A7O5PK",
        },
    ]
    verified = []
    for index, pair in enumerate(expected, start=1):
        item = {
            **pair,
            "transportMode": "transit",
            "durationSeconds": 900 + index,
            "distanceMeters": 1800 + index,
            "provider": "amap-webservice",
            "queriedAt": "2026-08-30T00:00:00+00:00",
        }
        item["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(item)
        verified.append(item)

    passing = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        snapshot,
        route_audit={"expectedPairs": expected, "verifiedPairs": verified},
        route_contract=snapshot["routeDecisionContract"],
    )
    assert passing["passed"] is True, passing
    assert passing["expectedPairs"] == expected
    original_fingerprint = verified[0]["providerEvidenceFingerprint"]
    for key, replacement in (
        ("dayNumber", 9),
        ("pairOrdinal", 7),
        ("fromSegmentId", "slot_tampered_from"),
        ("toSegmentId", "slot_tampered_to"),
    ):
        tampered_material = {**verified[0], key: replacement}
        assert SimpleOpenDirectionService._provider_evidence_fingerprint(tampered_material) != original_fingerprint

    rebound_snapshot = copy.deepcopy(snapshot)
    rebound_expected = copy.deepcopy(expected)
    rebound_verified = copy.deepcopy(verified)
    rebound_snapshot["days"][1]["segments"][1]["semanticMetadata"]["planningSlotId"] = "slot_tampered_to"
    rebound_expected[1]["toSegmentId"] = "slot_tampered_to"
    rebound_verified[1]["toSegmentId"] = "slot_tampered_to"
    identity_tampered_without_rehash = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        rebound_snapshot,
        route_audit={"expectedPairs": rebound_expected, "verifiedPairs": rebound_verified},
        route_contract=rebound_snapshot["routeDecisionContract"],
    )
    assert identity_tampered_without_rehash["passed"] is False
    assert identity_tampered_without_rehash["reason"] == "route_verified_pairs_invalid"

    missing_day = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        snapshot,
        route_audit={"expectedPairs": expected, "verifiedPairs": verified[:1]},
        route_contract=snapshot["routeDecisionContract"],
    )
    assert missing_day["passed"] is False
    assert missing_day["reason"] == "route_verified_pairs_mismatch"

    missing_segment_identity = copy.deepcopy(verified)
    missing_segment_identity[1].pop("toSegmentId")
    missing_segment_identity[1]["providerEvidenceFingerprint"] = (
        SimpleOpenDirectionService._provider_evidence_fingerprint(missing_segment_identity[1])
    )
    invalid_identity = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        snapshot,
        route_audit={"expectedPairs": expected, "verifiedPairs": missing_segment_identity},
        route_contract=snapshot["routeDecisionContract"],
    )
    assert invalid_identity["passed"] is False
    assert invalid_identity["reason"] == "route_verified_pairs_invalid"


def test_legacy_amap_only_pair_evidence_fails_when_snapshot_pair_is_ambiguous_across_days() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    day_two = copy.deepcopy(snapshot["days"][0])
    day_two.update({"id": "day_legacy_repeat_2", "dayNumber": 2, "date": "2026-10-02"})
    for index, segment in enumerate(day_two["segments"], start=1):
        segment["id"] = f"seg_legacy_repeat_day2_{index}"
        semantic = segment["semanticMetadata"]
        semantic["dayNumber"] = 2
        semantic["planningSlotId"] = f"slot_legacy_repeat_day2_{index}"
        semantic["occurrenceId"] = f"occ:legacy-repeat:{index}:day:2"
    snapshot["days"].append(day_two)

    legacy_expected = [
        {"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"},
        {"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"},
    ]
    legacy_verified = []
    for index, pair in enumerate(legacy_expected, start=1):
        item = {
            **pair,
            "transportMode": "transit",
            "durationSeconds": 900 + index,
            "distanceMeters": 1800 + index,
            "provider": "amap-webservice",
            "queriedAt": "2026-08-30T00:00:00+00:00",
        }
        item["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(item)
        legacy_verified.append(item)

    result = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        snapshot,
        route_audit={"expectedPairs": legacy_expected, "verifiedPairs": legacy_verified},
        route_contract=snapshot["routeDecisionContract"],
    )
    assert result["passed"] is False
    assert result["reason"] == "route_expected_pairs_mismatch"


def _attach_daily_overlap_evidence(snapshot: dict, *, policy: str) -> None:
    from src.services.daily_route_overlap_service import DailyRouteOverlapService

    route_audit = snapshot["simpleOpenRouteAssignment"]
    verified_pair = route_audit["verifiedPairs"][0]
    leg = {
        "routeOptionId": "route-daily-overlap-1",
        "fromAmapId": verified_pair["fromAmapId"],
        "toAmapId": verified_pair["toAmapId"],
        "mode": verified_pair["transportMode"],
        "durationSeconds": verified_pair["durationSeconds"],
        "distanceMeters": verified_pair["distanceMeters"],
        "provider": verified_pair["provider"],
        "queriedAt": verified_pair["queriedAt"],
        "polyline": [[116.31, 39.91], [116.41, 39.91]],
        "steps": [],
    }
    alternatives_evaluated = 2 if policy == "rank" else 1
    evidence = DailyRouteOverlapService().evaluate(
        day_number=1,
        route_legs=[leg],
        alternatives_evaluated=alternatives_evaluated,
        selected_alternative_ids=[leg["routeOptionId"]],
    )
    evidence.update(
        {
            "selectionStatus": "ranked_bounded_options" if policy == "rank" else "observed_first_option",
            "boundedRouteOptionCombinationCount": 2,
            "availableRouteOptionCombinationCount": 2,
            "routeOptionCombinationTruncated": False,
        }
    )
    evidence["evidenceFingerprint"] = DailyRouteOverlapService.evidence_fingerprint(evidence)
    route_audit.update(
        {
            "dailyRouteOverlapPolicy": policy,
            "dailyRouteOverlapStatus": evidence["selectionStatus"],
            "dailyRouteOverlapEvidence": {
                "schemaVersion": "daily-route-continuity-audit-v1",
                "perDay": [evidence],
            },
        }
    )


def test_daily_route_overlap_observe_evidence_is_replayed_but_does_not_change_adoption_gate() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    contract = snapshot["routeDecisionContract"]
    expected = [{"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"}]
    verified_pair = {
        **expected[0],
        "transportMode": "transit",
        "durationSeconds": 1800,
        "distanceMeters": 4000,
        "provider": "amap-webservice",
        "queriedAt": "2026-08-23T00:00:00+00:00",
    }
    verified_pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(
        verified_pair
    )
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": expected,
        "verifiedPairs": [verified_pair],
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }
    _attach_daily_overlap_evidence(snapshot, policy="observe")

    passing = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert passing["confirmationPassed"] is True
    assert passing["dailyRouteOverlapEvidencePassed"] is True
    assert passing["dailyRouteOverlapEvidenceRequired"] is False

    snapshot["simpleOpenRouteAssignment"]["dailyRouteOverlapEvidence"]["perDay"][0]["nonExemptRepeatedMeters"] = 999
    tampered = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert tampered["confirmationPassed"] is True
    assert tampered["dailyRouteOverlapEvidencePassed"] is False
    assert tampered["dailyRouteOverlapEvidenceReason"] == "daily_route_overlap_fingerprint_mismatch"


def test_daily_route_overlap_rank_claim_fails_closed_when_frozen_evidence_is_tampered() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _compact_two_anchor_snapshot()
    contract = snapshot["routeDecisionContract"]
    expected = [{"fromAmapId": "B000A6EA36", "toAmapId": "B000A7O5PK"}]
    verified_pair = {
        **expected[0],
        "transportMode": "transit",
        "durationSeconds": 1800,
        "distanceMeters": 4000,
        "provider": "amap-webservice",
        "queriedAt": "2026-08-23T00:00:00+00:00",
    }
    verified_pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(
        verified_pair
    )
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": contract["fingerprint"],
        "expectedPairs": expected,
        "verifiedPairs": [verified_pair],
        "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified",
        "topologyCompliance": "verified",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
    }
    _attach_daily_overlap_evidence(snapshot, policy="rank")

    passing = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert passing["confirmationPassed"] is True
    assert passing["dailyRouteOverlapEvidencePassed"] is True
    assert passing["dailyRouteOverlapEvidenceRequired"] is True

    snapshot["simpleOpenRouteAssignment"]["dailyRouteOverlapEvidence"]["perDay"][0]["selectedAlternativeIds"] = [
        "route-tampered"
    ]
    tampered = SimpleOpenDirectionService._proposal_verifier(snapshot)
    assert tampered["confirmationPassed"] is False
    assert tampered["dailyRouteOverlapEvidenceReason"] == "daily_route_overlap_fingerprint_mismatch"
    assert "daily_route_overlap_fingerprint_mismatch" in tampered["hardFailures"]


def test_server_verifier_snapshot_update_keeps_fingerprint_retryable_after_persist_failure() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "snapshot fingerprint retry")
        direction_service = SimpleOpenDirectionService(connection)
        response = direction_service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_fingerprint_root",
            source_user_turn_id="turn_fingerprint_root",
            source_assistant_turn_id="turn_fingerprint_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        choice = next(item for item in response["choiceOptions"] if item["action"] == "select_plan_proposal")
        store = PlanPortfolioStore(connection)
        commit_service = PlanProposalCommitService(store)

        def verify(snapshot: dict) -> bool:
            verifier = direction_service._proposal_verifier(snapshot)
            snapshot["portfolioVerifier"] = copy.deepcopy(verifier)
            direction_service.update_server_verified_proposal_material(
                portfolio_id=response["rootPortfolioId"],
                proposal_id=choice["proposalId"],
                snapshot=snapshot,
                verifier=verifier,
                status="adoption_ready",
                evidence={"serverVerificationPersisted": True},
            )
            return verifier["confirmationPassed"] is True

        with pytest.raises(RuntimeError, match="persist failed"):
            commit_service.commit(
                session_id=session.session_id,
                source_user_turn_id="turn_fingerprint_root",
                choice_id=choice["id"],
                active_version_id=None,
                verify=verify,
                persist=lambda _snapshot: (_ for _ in ()).throw(RuntimeError("persist failed")),
            )

        stored = connection.execute(
            "SELECT snapshot_json, generation_lineage_json FROM agent_plan_proposals WHERE id = ?",
            (choice["proposalId"],),
        ).fetchone()
        stored_snapshot = json.loads(stored["snapshot_json"])
        stored_lineage = json.loads(stored["generation_lineage_json"])
        assert stored_lineage["proposalSnapshotFingerprint"] == direction_service._fingerprint(stored_snapshot)

        result = commit_service.commit(
            session_id=session.session_id,
            source_user_turn_id="turn_fingerprint_root",
            choice_id=choice["id"],
            active_version_id=None,
            verify=verify,
            persist=lambda _snapshot: "version_retry_succeeded",
        )

    assert result == "version_retry_succeeded"


def test_truthful_status_title_keeps_sealed_fallback_reserved_for_the_next_direction() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    def without_agent_title(poi_id: str) -> dict:
        snapshot = _snapshot("plan_title_fallback", "方向待命名", poi_id)
        snapshot.pop("portfolioTitleEvidence", None)
        snapshot.pop("portfolioTitleGeneration", None)
        return snapshot

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "fallback title reservation")
        direction_service = SimpleOpenDirectionService(connection)
        first = direction_service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_fallback_root",
            source_user_turn_id="turn_fallback_root",
            source_assistant_turn_id="turn_fallback_a",
            expected_base_version_id=None,
            source_observation_fingerprint="t" * 64,
            request_contract_fingerprint="u" * 64,
            snapshot=without_agent_title("B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )

        visible = direction_service.store.visible_comparison_projections(portfolio_id=first["rootPortfolioId"])
        first_snapshot = direction_service.store.load_visible_proposal_snapshot(
            portfolio_id=first["rootPortfolioId"],
            proposal_id=visible[0]["proposalId"],
        )
        assert first_snapshot is not None
        first_title = first_snapshot["portfolioTitleGeneration"]["fallbackTitle"]
        assert visible[0]["title"] == first_title

        second = direction_service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_fallback_root",
            source_user_turn_id="turn_fallback_b",
            source_assistant_turn_id="turn_fallback_b",
            expected_base_version_id=None,
            source_observation_fingerprint="v" * 64,
            request_contract_fingerprint="u" * 64,
            snapshot=without_agent_title("B000B"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        second_snapshot = direction_service.store.load_visible_proposal_snapshot(
            portfolio_id=first["rootPortfolioId"],
            proposal_id=next(
                option["proposalId"]
                for option in second["choiceOptions"]
                if option["action"] == "select_plan_proposal" and option["label"] != f"确认编辑「{first_title}」"
            ),
        )
        assert second_snapshot is not None
        second_title = second_snapshot["portfolioTitleGeneration"]["fallbackTitle"]
        assert second_title != first_title
        labels = {option["label"] for option in second["choiceOptions"] if option["action"] == "select_plan_proposal"}
        assert labels == {f"确认编辑「{first_title}」", f"确认编辑「{second_title}」"}
        assert sum(option["action"] == "continue_plan_expansion" for option in second["choiceOptions"]) == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_portfolios").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_focused_direction_repair_fills_only_matching_pending_occurrence_without_new_proposal_or_write() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "focused direction repair")
        service = SimpleOpenDirectionService(connection)
        current = _snapshot(session.active_plan_id, "高校方向", "B000A")
        current["days"].append(
            {
                "id": "day_2",
                "dayNumber": 2,
                "date": "2026-10-02",
                "title": "第二天",
                "segments": [],
            }
        )
        current["portfolioPendingSlots"] = [
            {
                "id": "pending:day2_campus",
                "planningSlotId": "day2_campus",
                "poolId": "campus_pool",
                "dayNumber": 2,
                "intentType": "campus_visit",
                "requirementLevel": "hard",
                "required": True,
                "goalId": "goal_campus",
                "sourceGoalId": "goal_campus",
                "occurrenceId": "occ:goal_campus:day:2",
                "lineageAuthority": "goal_occurrence_compiler",
                "futureRouteAnchor": True,
                "routeAnchorExpected": True,
            }
        ]
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=current,
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = offered["comparisonProjections"][0]["proposalId"]
        portfolio_id = offered["rootPortfolioId"]
        candidate = copy.deepcopy(current)
        repaired_segment = copy.deepcopy(
            _snapshot(session.active_plan_id, "补全候选", "B000B")["days"][0]["segments"][0]
        )
        repaired_segment["id"] = "seg_day2_campus"
        repaired_segment["semanticMetadata"].update(
            {
                "goalId": "goal_campus",
                "sourceGoalId": "goal_campus",
                "occurrenceId": "occ:goal_campus:day:2",
                "planningSlotId": "day2_campus",
                "poolId": "campus_pool",
                "dayNumber": 2,
                "lineageAuthority": "goal_occurrence_compiler",
            }
        )
        candidate["days"][1]["segments"] = [repaired_segment]
        candidate["portfolioPendingSlots"] = []

        repaired = service.repair_direction(
            session_id=session.session_id,
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            source_assistant_turn_id="assistant_repair",
            expected_base_version_id=None,
            candidate_snapshot=candidate,
            request_contract={"routeDecisionContract": _route_contract()},
        )

        assert repaired["proposalDelta"] == 0
        assert repaired["comparisonProjectionUpdateMode"] == "replace"
        projection = next(item for item in repaired["comparisonProjections"] if item["proposalId"] == proposal_id)
        assert [segment["poi"]["amapId"] for segment in projection["days"][1]["segments"]] == ["B000A7O5PK"]
        assert projection["pendingSlots"] == []
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0


@pytest.mark.parametrize("identity_mode", ["exact_amap", "shared_parent", "physical_key"])
def test_simple_direction_offer_rejects_title_only_duplicate_physical_direction(
    identity_mode: str,
) -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", f"direction novelty {identity_mode}")
        service = SimpleOpenDirectionService(connection)
        first_snapshot = _snapshot(session.active_plan_id, "高校经典线", "B000A")
        duplicate_snapshot = copy.deepcopy(first_snapshot)
        duplicate_snapshot.update(
            {
                "title": "仅修改标题的另一个方向",
                "decisionRationale": "仅修改展示文案，不代表新的物理行程。",
            }
        )
        duplicate_snapshot["days"][0].update(
            {
                "id": "day_display_only",
                "title": "展示字段不同",
            }
        )
        duplicate_snapshot["days"][0]["segments"][0]["id"] = "seg_display_only"
        first_poi = first_snapshot["days"][0]["segments"][0]["poi"]
        duplicate_poi = duplicate_snapshot["days"][0]["segments"][0]["poi"]
        if identity_mode == "shared_parent":
            first_poi["parentPoiId"] = "B000PARENT1"
            duplicate_poi.update(
                {
                    "amapId": "B000A7O5PK",
                    "parentPoiId": "B000PARENT1",
                    "name": "清华大学东门子地点",
                    "latitude": 39.991,
                    "longitude": 116.311,
                }
            )
        elif identity_mode == "physical_key":
            for poi in (first_poi, duplicate_poi):
                poi.update(
                    {
                        "name": "同一物理校区",
                        "address": "北京市海淀区同一地址",
                        "latitude": 39.990001,
                        "longitude": 116.310001,
                    }
                )
            duplicate_poi["amapId"] = "B000A7O5PK"

        first = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=first_snapshot,
            request_contract={"routeDecisionContract": _route_contract()},
        )
        duplicate = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_duplicate",
            source_assistant_turn_id="turn_assistant_duplicate",
            expected_base_version_id=None,
            source_observation_fingerprint="p" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=duplicate_snapshot,
            request_contract={"routeDecisionContract": _route_contract()},
        )

        assert first["visibleProposalCount"] == 1
        assert duplicate["reasonCode"] == "no_material_novelty"
        assert duplicate["proposalDelta"] == 0
        assert duplicate["comparisonProjections"] == []
        assert duplicate["choiceOptions"] == []
        assert duplicate["visibleProposalCount"] == 1
        assert duplicate["payloadProposalCount"] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_simple_direction_physical_novelty_checks_all_prior_directions_a_to_b_to_a() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction novelty A B A")
        service = SimpleOpenDirectionService(connection)

        def offer(*, source: str, snapshot: dict) -> dict:
            return service.offer_direction(
                session_id=session.session_id,
                planning_root_id="turn_root",
                source_user_turn_id=source,
                source_assistant_turn_id=f"assistant_{source}",
                expected_base_version_id=None,
                source_observation_fingerprint=(source[-1] * 64),
                request_contract_fingerprint="r" * 64,
                snapshot=snapshot,
                request_contract={"routeDecisionContract": _route_contract()},
            )

        direction_a = _snapshot(session.active_plan_id, "高校经典线 A", "B000A")
        direction_b = _snapshot(session.active_plan_id, "高校经典线 B", "B000B")
        repeated_a = copy.deepcopy(direction_a)
        repeated_a["title"] = "A 的新展示标题"
        repeated_a["days"][0]["title"] = "A 的新展示日标题"

        first = offer(source="turn_a", snapshot=direction_a)
        second = offer(source="turn_b", snapshot=direction_b)
        third = offer(source="turn_repeat_a", snapshot=repeated_a)

        assert first["visibleProposalCount"] == 1
        assert second["comparisonProjectionUpdateMode"] == "append"
        assert second["visibleProposalCount"] == 2
        assert second["proposalDelta"] == 1
        assert third["reasonCode"] == "no_material_novelty"
        assert third["proposalDelta"] == 0
        assert third["comparisonProjections"] == []
        assert third["choiceOptions"] == []
        assert third["visibleProposalCount"] == 2
        assert connection.execute("SELECT COUNT(*) FROM agent_plan_proposals").fetchone()[0] == 2


def test_simple_direction_exposes_server_owned_prior_physical_aliases_for_candidate_admission() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "prior direction exclusions")
        service = SimpleOpenDirectionService(connection)
        first_snapshot = _snapshot(session.active_plan_id, "高校经典线 A", "B000A")
        first_snapshot["days"][0]["segments"][0]["poi"]["parentPoiId"] = "B000PARENT1"
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=first_snapshot,
            request_contract={"routeDecisionContract": _route_contract()},
        )

        aliases = service.prior_direction_physical_aliases(
            session_id=session.session_id,
            portfolio_id=offered["rootPortfolioId"],
        )

        assert "amap:B000A6EA36" in aliases
        assert "amap:B000PARENT1" in aliases
        assert any(alias.startswith("physical:") for alias in aliases)
        with pytest.raises(ValueError, match="simple_direction_physical_exclusion_scope_invalid"):
            service.prior_direction_physical_aliases(
                session_id="other-session",
                portfolio_id=offered["rootPortfolioId"],
            )


def test_server_validated_new_direction_passes_prior_aliases_to_simple_consumer_admission() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "new direction exclusions")
        service = AgentService(connection)
        direction_service = SimpleOpenDirectionService(connection)
        offered = direction_service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线 A", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        root_id = str(offered["rootPortfolioId"])
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "更轻松的高校方向",
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                }
            ],
            "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        resolution = {
            "schemaVersion": "agent-view-resolution-v1",
            "resolutionSource": "server_validated_opaque_choice",
            "resolvedAction": "generate_new_direction",
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": root_id,
        }
        capability = {
            "status": "unique",
            "reasonCode": "server_validated_opaque_choice",
            "capability": "create_itinerary",
            "planningSelectionRootTurnId": "turn_root",
            "rootPortfolioId": root_id,
        }
        pipeline_context = {
            "sessionId": session.session_id,
            "serverExecutionProfile": "simple_open_v1",
            "_simpleDirectionGenerationAuthorized": True,
            "viewResolution": resolution,
            "conversationCapability": capability,
            "planningDirective": copy.deepcopy(directive),
            "agentDecisionState": {
                "accepted": True,
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "primaryAction": "draft_itinerary",
                "actionDirective": copy.deepcopy(directive),
                "reasonCodes": [],
            },
        }
        captured: dict = {}

        class CapturingExecutor:
            @staticmethod
            def build_segment_plans(initial_plan, **kwargs):
                del initial_plan
                captured.update(kwargs)
                return [], []

        service.simple_open_itinerary_executor = CapturingExecutor()
        service._simple_open_slot_occurrence_lineage = lambda _plan, _context: {}

        service._simple_open_persistable_segment_plans(
            AgentInitialPlanOutput.model_validate(
                {"reply": "", "mode": "day_slots", "daySlots": [], "intentPools": []}
            ),
            city="北京",
            transport_mode="public_transit",
            tool_events=[],
            pipeline_context=pipeline_context,
            session_id=session.session_id,
        )

        assert "amap:B000A6EA36" in captured["excluded_physical_aliases"]
        assert any(alias.startswith("physical:") for alias in captured["excluded_physical_aliases"])


def test_simple_direction_route_missing_is_read_only_and_keeps_all_real_segments_visible() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "live shaped partial")
        service = SimpleOpenDirectionService(connection)

        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_live_root",
            source_user_turn_id="turn_live_root",
            source_assistant_turn_id="turn_live_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_live_shaped_simple_partial_snapshot(session.active_plan_id),
            request_contract={"routeDecisionContract": _route_contract()},
        )

        projection = response["comparisonProjections"][0]
        visible_segments = [segment for day in projection["days"] for segment in day["segments"]]
        provisional_night = next(
            item for item in visible_segments if (item.get("poi") or {}).get("name") == "奥林匹克塔"
        )
        assert len(visible_segments) == 6
        assert projection["status"] == "partial"
        assert projection["isPartial"] is True
        assert projection["adoptionReady"] is False
        assert projection["draftAdoptionReady"] is False
        assert projection["adoptionMode"] == "blocked"
        assert projection["routeStatus"] == "route_pending"
        assert "portfolio_route_quality:route_evidence_missing" in projection["softWarnings"]
        assert "route_evidence_incomplete" in projection["blockingReasons"]
        assert "仍缺与当前停靠顺序一致的路线核验" in projection["blockingReasonLabels"]
        assert "具体地点位置未通过校验" not in projection["blockingReasonLabels"]
        assert provisional_night["semanticMetadata"]["groundingStatus"] == "provisional"
        assert response["choiceOptions"] == []

        proposal = connection.execute("SELECT status, verifier_json FROM agent_plan_proposals").fetchone()
        verifier = json.loads(proposal["verifier_json"])
        assert proposal["status"] == "blocked"
        assert verifier["draftPassed"] is True
        assert verifier["confirmationPassed"] is False
        assert verifier["pendingHardSlotCount"] == 0
        assert verifier["verifiedAmapRouteAnchorCount"] == 4
        assert "route_evidence_incomplete" in verifier["hardFailures"]
        assert "portfolio_route_quality:route_evidence_missing" in verifier["softWarnings"]
        portfolio_summary = json.loads(
            connection.execute("SELECT summary_json FROM agent_plan_portfolios").fetchone()["summary_json"]
        )
        assert portfolio_summary["adoptionReadyProposalCount"] == 0


def test_required_provider_exhausted_slot_is_metadata_only_read_only_partial() -> None:
    """A missing required POI stays truthful without becoming adoptable.

    This mirrors the live failure where four valid AMap anchors were discarded
    from the comparison card because one unresolved night slot had been
    materialized as a fake timeline POI.  Simple Direction may expose that
    provider-exhausted required slot as explicit pending metadata, but it may
    never persist or commit the skeleton entity itself and must not issue a
    proposal-adoption capability.
    """

    from src.services.proposal_readiness_service import ProposalReadinessService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "provider exhausted required slot")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "两天高校、美食和一晚城市夜景，公共交通，适度绕行",
            "active",
        )
        assistant_turn_id = service._insert_turn(session.session_id, "assistant", "", "streaming")
        request_context = _request_context(root_turn_id=root_turn_id)
        request_context["sessionId"] = session.session_id
        request_context["resolvedTripDates"] = {
            "status": "resolved",
            "dates": ["2026-10-01", "2026-10-02"],
            "dayCount": 2,
        }
        request_context["requestIntentContract"].update(
            {
                "dayCount": 2,
                "requiredIntents": [
                    {
                        "goalId": "goal_campus",
                        "intentType": "campus_visit",
                        "requiredMin": 2,
                        "requirementLevel": "required",
                    },
                    {
                        "goalId": "goal_local_food",
                        "intentType": "meal",
                        "requiredMin": 2,
                        "requirementLevel": "required",
                    },
                    {
                        "goalId": "goal_night_view",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    },
                ],
            }
        )
        snapshot = _live_shaped_simple_partial_snapshot(session.active_plan_id)
        unresolved = snapshot["days"][0]["segments"][2]
        unresolved["poi"] = {
            "id": "poi_skeleton_night",
            "amapId": None,
            "name": "夜景观景点待补充",
            "city": "北京",
            "category": "night_view",
            "type": "地图候选待补全",
            "providerType": "地图候选待补全",
            "latitude": None,
            "longitude": None,
            "source": "agent-text-timeline",
            "confidence": 0,
        }
        unresolved["semanticMetadata"].update(
            {
                "groundingStatus": "unresolved",
                # The deterministic slot fallback lost the requirement in the
                # live run.  The server must rebind it from the request contract.
                "required": False,
                "requirementLevel": "optional",
                "goalId": None,
                "sourceGoalId": None,
                "occurrenceId": "occ:goal_night_view:day:1",
                "lineageAuthority": "goal_occurrence_compiler",
                "reasonCode": "simple_open_slot_unresolved",
                "reason": "高德候选已穷尽，且返回实体均未通过夜景用途校验",
            }
        )
        unresolved["notes"] = ""
        snapshot["days"][1]["segments"] = snapshot["days"][1]["segments"][:2]

        response = service._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=root_turn_id,
            assistant_turn_id=assistant_turn_id,
            content="两天高校、美食和一晚城市夜景，公共交通，适度绕行",
            request_context=request_context,
            pipeline_context=request_context["pipelineContext"],
            session_before=service._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates=request_context["resolvedTripDates"],
            grounding_report={"resultState": "partial"},
            snapshot=snapshot,
            tool_events=[],
        )

        proposal = connection.execute(
            "SELECT status, snapshot_json, verifier_json FROM agent_plan_proposals"
        ).fetchone()
        persisted = json.loads(proposal["snapshot_json"])
        verifier = json.loads(proposal["verifier_json"])
        persisted_segments = [segment for day in persisted["days"] for segment in day["segments"]]
        assert len(persisted_segments) == 4
        assert all((segment.get("poi") or {}).get("source") == "amap-place-search" for segment in persisted_segments)
        assert all((segment.get("poi") or {}).get("amapId") for segment in persisted_segments)
        assert all((segment.get("poi") or {}).get("name") != "夜景观景点待补充" for segment in persisted_segments)

        pending = persisted["portfolioPendingSlots"]
        assert len(pending) == 1
        assert pending[0]["intentType"] == "night_view"
        assert pending[0]["requirementLevel"] == "required"
        assert pending[0]["goalId"] == "goal_night_view"
        assert pending[0]["sourceGoalId"] == "goal_night_view"
        assert pending[0]["dayNumber"] == 1
        assert pending[0]["startTime"] == "19:00"
        assert pending[0]["endTime"] == "20:30"
        assert pending[0]["groundingStatus"] == "unresolved"
        assert pending[0]["reasonCode"] == "provider_candidates_exhausted_or_semantically_rejected"
        assert pending[0]["sourceReasonCode"] == "simple_open_slot_unresolved"
        assert pending[0]["reason"] == "高德候选已穷尽，且返回实体均未通过夜景用途校验"
        assert pending[0]["simpleDirectionProviderExhausted"] is True
        assert "夜景" in pending[0]["displayNeed"]
        fallback_generation = persisted["portfolioTitleGeneration"]
        assert any(
            item.get("state") == "pending" and item.get("occurrenceId") == pending[0]["occurrenceId"]
            for item in fallback_generation["fallbackEvidence"]
        )

        projection = response.assistant_turn.comparison_projections[0]
        visible_segments = [segment for day in projection["days"] for segment in day["segments"]]
        assert len(visible_segments) == 4
        assert projection["status"] == "partial"
        assert projection["adoptionReady"] is False
        assert projection["draftAdoptionReady"] is False
        assert projection["pendingHardSlotCount"] == 1
        assert projection["pendingSlots"][0]["requirementLevel"] == "required"
        assert projection["pendingSlots"][0]["reasonCode"] == pending[0]["reasonCode"]
        assert projection["adoptionMode"] == "blocked"
        assert "pending_slots_remaining" in projection["blockingReasons"]
        assert projection["title"] == CreativeProposalTitleService.sealed_server_fallback_title(persisted)
        assert proposal["status"] == "blocked"
        assert verifier["passed"] is False
        assert verifier["draftPassed"] is False
        assert verifier["confirmationPassed"] is False
        assert verifier["pendingHardSlotCount"] == 1
        assert verifier["blockingPendingHardSlotCount"] == 1
        assert verifier["providerExhaustedRequiredSlotCount"] == 1
        assert verifier["semanticFailureCount"] == 0
        assert "simple_direction_pending_hard_slot" in verifier["hardFailures"]
        strict_readiness = ProposalReadinessService.compute(persisted, verifier=verifier)
        assert strict_readiness["adoptionReady"] is False
        assert strict_readiness["blockingPendingHardSlotCount"] == 1
        assert "pending_slots_remaining" in strict_readiness["blockingReasons"]
        assert response.terminal_status == "candidate_refresh_required"
        assert len(response.assistant_turn.choice_options) == 2
        continuation = response.assistant_turn.choice_options[0]
        assert continuation["action"] == "continue_plan_expansion"
        assert continuation["sourceAssistantTurnId"] == assistant_turn_id
        assert continuation["planningSelectionRootTurnId"] == root_turn_id
        assert continuation["rootPortfolioId"]
        assert continuation["requestContractFingerprint"]
        assert all(choice["action"] != "select_plan_proposal" for choice in response.assistant_turn.choice_options)
        assert "仍有 1 个必选地点" in response.assistant_turn.content
        assert "夜景" in response.assistant_turn.content
        assert "待补充" in response.assistant_turn.content
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0


def test_simple_timeout_fallback_preserves_controller_hard_goal_occurrence_lineage() -> None:
    """A server fallback may ground a Controller slot, never relocate it.

    The live Simple Direction run accepted a V3 directive that placed the one
    required night-view occurrence on Day 1.  When the initial DaySlot provider
    timed out, the contract fallback chose the last allowed day instead.  This
    regression follows that same Controller -> fallback -> executor -> proposal
    shape and requires the formal server-compiled occurrence to survive whether
    AMap materializes the slot or reports its bounded frontier exhausted.
    """

    class FailingMapProvider:
        def search(self, city, *, keyword, category, limit):
            del city, keyword, category, limit
            raise TimeoutError("bounded AMap frontier exhausted")

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple occurrence lineage")
        service = AgentService(connection)
        service.simple_open_itinerary_executor.map_poi_service = FailingMapProvider()
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "高校与夜景",
                    "requiredGoalIds": ["goal_night_view"],
                    "requiredGoalCounts": {"goal_night_view": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "高校漫游",
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_night_view",
                    "intentType": "night_view",
                    "target": 1,
                    "requiredMin": 1,
                    "minCount": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "one_of_allowed_days",
                    "cardinalitySource": "explicit_singular",
                }
            ],
        }
        pipeline_context = {
            "sessionId": session.session_id,
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": (
                "2026年10月1日至2日北京两日游，1人，中等预算，公交地铁优先，绕行最多30分钟，只安排一晚城市夜景。"
            ),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "understoodRequirements": {
                "fields": {
                    "travelDate": "2026-10-01 至 2026-10-02",
                    "travelDays": "2天",
                    "partySize": "1人",
                    "budget": "中等",
                    "transportPreference": "公共交通",
                }
            },
            "requestIntentContract": request_contract,
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        service._simple_open_goal_occurrence_plan(pipeline_context)
        rebuilt_slots, _rebuilt_pools = service._simple_open_authoritative_occurrence_inventory(
            pipeline_context=pipeline_context,
            day_slots=[],
            intent_pools=[],
            dates=["2026-10-01", "2026-10-02"],
            city="北京",
        )
        assert rebuilt_slots
        assert all(slot["startTime"] == "" for slot in rebuilt_slots)
        assert all(slot["durationMinutes"] == 0 for slot in rebuilt_slots)

        initial_plan = service._server_generic_day_slots_fallback(
            pipeline_context,
            ["TimeoutError: initial DaySlot provider timed out"],
            fallback_reason="simple_open_initial_provider_timeout",
        )
        assert initial_plan is not None
        night_slots = [slot for slot in initial_plan.day_slots if slot.kind == "night_view"]
        assert [(slot.slot_id, slot.day_number) for slot in night_slots] == [("day1_evening_night", 1)]
        assert night_slots[0].start_time == ""
        assert night_slots[0].time_window == ""
        assert night_slots[0].duration_minutes == 0

        tool_events: list[dict] = []
        plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=tool_events,
            pipeline_context=pipeline_context,
        )
        night_plan = next(plan for plan in plans if plan.intent_type == "night_view")
        assert night_plan.day_number == 1
        assert night_plan.goal_id == "goal_night_view"
        assert night_plan.source_goal_id == "goal_night_view"
        assert night_plan.occurrence_id == "occ:goal_night_view:day:1"
        assert night_plan.pool_id == "night_view_pool"
        assert night_plan.planning_slot_id == "day1_evening_night"
        assert night_plan.occurrence_id != night_plan.planning_slot_id
        assert night_plan.selected_poi is None

        snapshot = service._snapshot_from_persistable_segment_plans(
            service._session(session.session_id),
            initial_plan,
            plans,
            pipeline_context,
        )
        night_segment = next(
            segment
            for day in snapshot["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("intentType") == "night_view"
        )
        semantic = night_segment["semanticMetadata"]
        assert semantic["goalId"] == "goal_night_view"
        assert semantic["sourceGoalId"] == "goal_night_view"
        assert semantic["occurrenceId"] == "occ:goal_night_view:day:1"
        assert semantic["poolId"] == "night_view_pool"
        assert semantic["planningSlotId"] == "day1_evening_night"

        proposal = SimpleOpenDirectionService._materialize_unresolved_slots_as_pending_metadata(
            snapshot,
            request_contract=request_contract,
        )
        pending = next(
            item
            for item in proposal["portfolioPendingSlots"]
            if item.get("occurrenceId") == "occ:goal_night_view:day:1"
        )
        assert pending["dayNumber"] == 1
        assert pending["goalId"] == "goal_night_view"
        assert pending["sourceGoalId"] == "goal_night_view"
        assert pending["occurrenceId"] == "occ:goal_night_view:day:1"
        assert pending["poolId"] == "night_view_pool"
        assert pending["planningSlotId"] == "day1_evening_night"

        # The exact same sealed lineage must survive when the Provider does
        # materialize the occurrence instead of exhausting it.
        from datetime import datetime, timezone

        from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse

        class MaterializedNightProvider:
            def search(self, city, *, keyword, category, limit):
                del category, limit
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category="all",
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[
                        MapPoiResponse(
                            id="B000A7O5PK",
                            name="什刹海",
                            city="北京市",
                            district="朝阳区",
                            category="风景名胜",
                            type="风景名胜;风景名胜;国家级景点",
                            address="前海西街",
                            longitude=116.388,
                            latitude=39.941,
                            source="amap-place-search",
                            sourceNote="real-provider-shape",
                            confidence=0.9,
                        )
                    ]
                    if "夜景" in keyword
                    else [],
                )

        class AcceptingSemanticPolicy:
            @staticmethod
            def evaluate(*_args, **_kwargs):
                class Decision:
                    passed = True

                return Decision()

        service.simple_open_itinerary_executor.map_poi_service = MaterializedNightProvider()
        service.simple_open_itinerary_executor.intent_candidate_semantic_policy = AcceptingSemanticPolicy()
        materialized_plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=[],
            pipeline_context=pipeline_context,
        )
        materialized_night = next(plan for plan in materialized_plans if plan.intent_type == "night_view")
        assert materialized_night.selected_poi is not None
        assert materialized_night.day_number == 1
        assert materialized_night.goal_id == "goal_night_view"
        assert materialized_night.source_goal_id == "goal_night_view"
        assert materialized_night.occurrence_id == "occ:goal_night_view:day:1"
        assert materialized_night.pool_id == "night_view_pool"
        assert materialized_night.planning_slot_id == "day1_evening_night"
        materialized_snapshot = service._snapshot_from_persistable_segment_plans(
            service._session(session.session_id),
            initial_plan,
            materialized_plans,
            pipeline_context,
        )
        materialized_day_number, materialized_semantic = next(
            (day["dayNumber"], segment["semanticMetadata"])
            for day in materialized_snapshot["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("occurrenceId") == "occ:goal_night_view:day:1"
        )
        assert materialized_day_number == 1
        assert materialized_semantic["goalId"] == "goal_night_view"
        assert materialized_semantic["sourceGoalId"] == "goal_night_view"
        assert materialized_semantic["poolId"] == "night_view_pool"
        assert materialized_semantic["planningSlotId"] == "day1_evening_night"


def test_simple_timeout_fallback_filters_campus_inventory_to_controller_occurrence_day() -> None:
    class FailingMapProvider:
        def search(self, city, *, keyword, category, limit):
            del city, keyword, category, limit
            raise TimeoutError("bounded AMap frontier exhausted")

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "campus occurrence inventory")
        service = AgentService(connection)
        service.simple_open_itinerary_executor.map_poi_service = FailingMapProvider()
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": (
                "2026年10月1日至2日北京两日游，1人，中等预算，公交地铁优先，绕行最多30分钟，参观一所高校。"
            ),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "understoodRequirements": {
                "fields": {
                    "travelDate": "2026-10-01 至 2026-10-02",
                    "travelDays": "2天",
                    "partySize": "1人",
                    "budget": "中等",
                    "transportPreference": "公共交通",
                }
            },
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "target": 1,
                        "requiredMin": 1,
                        "preferredCount": 1,
                        "maxCount": 1,
                        "requirementLevel": "required",
                        "priorityTier": "hard",
                        "userExplicit": True,
                        "allowedDayNumbers": [1, 2],
                        "distributionPolicy": "one_of_allowed_days",
                    }
                ],
            },
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        service._simple_open_goal_occurrence_plan(pipeline_context)
        rebuilt_slots, _rebuilt_pools = service._simple_open_authoritative_occurrence_inventory(
            pipeline_context=pipeline_context,
            day_slots=[],
            intent_pools=[],
            dates=["2026-10-01", "2026-10-02"],
            city="北京",
        )
        assert rebuilt_slots
        assert all(slot["startTime"] == "" for slot in rebuilt_slots)
        assert all(slot["durationMinutes"] == 0 for slot in rebuilt_slots)

        initial_plan = service._server_generic_day_slots_fallback(
            pipeline_context,
            ["TimeoutError: initial DaySlot provider timed out"],
            fallback_reason="simple_open_initial_provider_timeout",
        )
        assert initial_plan is not None
        campus_slots = [slot for slot in initial_plan.day_slots if slot.kind == "campus"]
        assert [(slot.slot_id, slot.day_number) for slot in campus_slots] == [("day2_morning_campus", 2)]
        assert campus_slots[0].start_time == ""
        assert campus_slots[0].time_window == ""
        assert campus_slots[0].duration_minutes == 0

        plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=[],
            pipeline_context=pipeline_context,
        )
        assert len(plans) == 1
        campus = plans[0]
        assert campus.selected_poi is None
        assert campus.goal_id == "goal_campus_visit"
        assert campus.source_goal_id == "goal_campus_visit"
        assert campus.occurrence_id == "occ:goal_campus_visit:day:2"
        assert campus.day_number == 2
        assert campus.lineage_authority == "goal_occurrence_compiler"


def test_simple_timeout_fallback_uses_formal_meal_occurrences_for_materialized_and_pending_slots() -> None:
    from datetime import datetime, timezone

    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse

    class OneMealThenTimeoutProvider:
        def __init__(self):
            self.calls = 0

        def search(self, city, *, keyword, category, limit):
            del category, limit
            self.calls += 1
            if self.calls > 1:
                raise TimeoutError("second formal meal occurrence exhausted")
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category="food",
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B0J1CRCYMW",
                        name="北京地方特色餐厅",
                        city="北京市",
                        district="海淀区",
                        category="餐饮服务",
                        type="餐饮服务;中餐厅;北京菜",
                        providerTypeCode="050111",
                        tags=["北京菜", "地方风味"],
                        sourceClaims=[
                            {
                                "claimKey": "local_food",
                                "stance": "support",
                                "locality": "北京",
                                "evidenceSource": "provider_city_specific_fact",
                            }
                        ],
                        address="学院路1号",
                        longitude=116.35,
                        latitude=39.99,
                        source="amap-place-search",
                        sourceNote="real-provider-shape",
                        confidence=0.9,
                    )
                ],
            )

    class AcceptingSemanticPolicy:
        @staticmethod
        def evaluate(*_args, **_kwargs):
            class Decision:
                passed = True

            return Decision()

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "meal occurrence inventory")
        service = AgentService(connection)
        provider = OneMealThenTimeoutProvider()
        service.simple_open_itinerary_executor.map_poi_service = provider
        service.simple_open_itinerary_executor.intent_candidate_semantic_policy = AcceptingSemanticPolicy()
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": ["goal_meal"],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": ["goal_meal"],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 2,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": (
                "2026年10月1日至2日北京两日游，1人，中等预算，公交地铁优先，绕行最多30分钟，每天午餐吃当地特色美食。"
            ),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "understoodRequirements": {
                "fields": {
                    "travelDate": "2026-10-01 至 2026-10-02",
                    "travelDays": "2天",
                    "partySize": "1人",
                    "budget": "中等",
                    "transportPreference": "公共交通",
                }
            },
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "target": 2,
                        "requiredMin": 0,
                        "preferredCount": 2,
                        "maxCount": 2,
                        "requirementLevel": "soft_experience",
                        "priorityTier": "explicit_soft",
                        "userExplicit": True,
                        "allowedDayNumbers": [1, 2],
                        "distributionPolicy": "every_allowed_day",
                        "cardinalitySource": "explicit_every_day",
                    }
                ],
            },
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        initial_plan = service._server_generic_day_slots_fallback(
            pipeline_context,
            ["TimeoutError: initial DaySlot provider timed out"],
            fallback_reason="simple_open_initial_provider_timeout",
        )
        assert initial_plan is not None
        plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=[],
            pipeline_context=pipeline_context,
        )
        assert [
            (plan.day_number, plan.intent_type, plan.occurrence_id, plan.selected_poi is not None) for plan in plans
        ] == [
            (1, "meal", "occ:goal_meal:day:1", True),
            (2, "meal", "occ:goal_meal:day:2", False),
        ]
        assert all(plan.goal_id == "goal_meal" for plan in plans)
        assert all(plan.source_goal_id == "goal_meal" for plan in plans)
        assert all(plan.pool_id == "meal_pool" for plan in plans)
        assert all(plan.requirement_level == "explicit_soft" for plan in plans)
        assert all(plan.required is False for plan in plans)
        assert all(plan.lineage_authority == "goal_occurrence_compiler" for plan in plans)
        assert all(plan.start_time == "" for plan in plans)
        assert all(plan.duration_minutes == 0 for plan in plans)
        assert all(plan.schedule_decision.get("failureReason") is None for plan in plans)
        assert all(plan.schedule_decision.get("constraintPassed") is True for plan in plans)
        assert all(plan.schedule_decision.get("scheduleConfidence") == "flexible" for plan in plans)
        assert all(
            "duration_estimate_unavailable" in (plan.schedule_decision.get("provisionalReasons") or [])
            for plan in plans
        )


def test_simple_timeout_fallback_marks_explicit_every_day_meal_supplement_authority() -> None:
    from datetime import datetime, timezone

    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    class FirstMealOnlyProvider:
        def __init__(self):
            self.meal_materialized = False

        def search(self, city, *, keyword, category, limit):
            del category, limit
            if "美食" not in keyword and "餐" not in keyword:
                raise TimeoutError("campus occurrence exhausted")
            if self.meal_materialized:
                raise TimeoutError("second supplemented meal occurrence exhausted")
            self.meal_materialized = True
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category="food",
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B0J1CRCYMW",
                        name="北京地方特色餐厅",
                        city="北京市",
                        district="海淀区",
                        category="餐饮服务",
                        type="餐饮服务;中餐厅;北京菜",
                        providerTypeCode="050111",
                        tags=["北京菜", "地方风味"],
                        sourceClaims=[
                            {
                                "claimKey": "local_food",
                                "stance": "support",
                                "locality": "北京",
                                "evidenceSource": "provider_city_specific_fact",
                            }
                        ],
                        address="学院路1号",
                        longitude=116.35,
                        latitude=39.99,
                        source="amap-place-search",
                        sourceNote="real-provider-shape",
                        confidence=0.9,
                    )
                ],
            )

    class AcceptingSemanticPolicy:
        @staticmethod
        def evaluate(*_args, **_kwargs):
            class Decision:
                passed = True

            return Decision()

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "meal supplement authority")
        service = AgentService(connection)
        service.simple_open_itinerary_executor.map_poi_service = FirstMealOnlyProvider()
        service.simple_open_itinerary_executor.intent_candidate_semantic_policy = AcceptingSemanticPolicy()
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus_visit",
                    "intentType": "campus_visit",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "one_of_allowed_days",
                },
                {
                    "goalId": "goal_meal",
                    "intentType": "meal",
                    "target": 2,
                    "requiredMin": 0,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "soft_experience",
                    "priorityTier": "explicit_soft",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
            ],
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": (
                "2026年10月1日至2日北京两日游，1人，中等预算，公交地铁优先，参观一所高校，每天午餐吃当地特色美食。"
            ),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "understoodRequirements": {
                "fields": {
                    "travelDate": "2026-10-01 至 2026-10-02",
                    "travelDays": "2天",
                    "partySize": "1人",
                    "budget": "中等",
                    "transportPreference": "公共交通",
                }
            },
            "requestIntentContract": request_contract,
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        initial_plan = service._server_generic_day_slots_fallback(
            pipeline_context,
            ["TimeoutError: initial DaySlot provider timed out"],
            fallback_reason="simple_open_initial_provider_timeout",
        )
        assert initial_plan is not None
        assert [(slot.day_number, slot.kind) for slot in initial_plan.day_slots] == [
            (1, "meal"),
            (2, "campus"),
            (2, "meal"),
        ]

        plans = service._simple_open_persistable_segment_plans(
            initial_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=[],
            pipeline_context=pipeline_context,
        )
        meal_plans = [plan for plan in plans if plan.intent_type == "meal"]
        assert [plan.occurrence_id for plan in meal_plans] == [
            "occ:goal_meal:day:1",
            "occ:goal_meal:day:2",
        ]
        assert [plan.selected_poi is not None for plan in meal_plans] == [True, False]
        assert all(plan.goal_id == "goal_meal" for plan in meal_plans)
        assert all(plan.source_goal_id == "goal_meal" for plan in meal_plans)
        assert all(plan.pool_id == "meal_pool" for plan in meal_plans)
        assert all(plan.requirement_level == "explicit_soft" for plan in meal_plans)
        assert all(plan.required is False for plan in meal_plans)
        assert all(plan.lineage_authority == "simple_open_request_contract_every_day_meal" for plan in meal_plans)
        campus_plan = next(plan for plan in plans if plan.intent_type == "campus_visit")
        assert campus_plan.day_number == 2
        assert campus_plan.lineage_authority == "goal_occurrence_compiler"

        snapshot = service._snapshot_from_persistable_segment_plans(
            service._session(session.session_id),
            initial_plan,
            plans,
            pipeline_context,
        )
        meal_semantics = [
            segment["semanticMetadata"]
            for day in snapshot["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("intentType") == "meal"
        ]
        assert all(
            metadata["lineageAuthority"] == "simple_open_request_contract_every_day_meal" for metadata in meal_semantics
        )
        proposal = SimpleOpenDirectionService._materialize_unresolved_slots_as_pending_metadata(
            snapshot,
            request_contract=request_contract,
        )
        pending_meal = next(
            item for item in proposal["portfolioPendingSlots"] if item.get("occurrenceId") == "occ:goal_meal:day:2"
        )
        assert pending_meal["goalId"] == "goal_meal"
        assert pending_meal["sourceGoalId"] == "goal_meal"
        assert pending_meal["planningSlotId"] == "day2_lunch"
        assert pending_meal["dayNumber"] == 2
        assert pending_meal["requirementLevel"] == "explicit_soft"
        assert pending_meal["requirementLevel"] != "required"
        assert pending_meal["lineageAuthority"] == "simple_open_request_contract_every_day_meal"


def test_standard_two_day_direction_seals_one_route_local_completion_slot_for_sparse_day() -> None:
    """A non-rest Day 2 cannot stay at campus + meal while Day 1 has three anchors."""

    from src.models.poi_intent import PersistableSegmentPlan
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        service = AgentService(connection)
        session = ConversationService(connection).create_session("北京", "daily completion policy")
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["goal_campus", "goal_park"],
                    "requiredGoalCounts": {"goal_campus": 1, "goal_park": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 1,
            "routeGapSupplementHints": [],
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "target": 2,
                    "requiredMin": 2,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
                {
                    "goalId": "goal_park",
                    "intentType": "park",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "one_of_allowed_days",
                    "cardinalitySource": "explicit_singular",
                },
                {
                    "goalId": "goal_meal",
                    "intentType": "meal",
                    "target": 2,
                    "requiredMin": 0,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "soft_experience",
                    "priorityTier": "explicit_soft",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
            ],
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": (
                "2026年10月1日至2日北京两日高校游，晚上逛公园，每天午餐体验当地特色美食；"
                "其余空余时间补充一个顺路、轻松的真实地点。"
            ),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "requestIntentContract": request_contract,
            "routeDecisionContract": {
                "status": "ready",
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
            },
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        service._simple_open_goal_occurrence_plan(pipeline_context)
        timeout_fallback = service._validated_controller_draft_slot_fallback(
            pipeline_context,
            TimeoutError("initial day slot provider timeout"),
        )
        assert timeout_fallback is not None
        assert {
            day_number: len([slot for slot in timeout_fallback.day_slots if int(slot.day_number) == day_number])
            for day_number in (1, 2)
        } == {1: 3, 2: 3}
        fallback_completion = next(
            slot for slot in timeout_fallback.day_slots if slot.slot_id == "day2_daily_completion_1"
        )
        assert fallback_completion.kind == "park"
        assert fallback_completion.route_anchor is True

        slots, pools = service._simple_open_authoritative_occurrence_inventory(
            pipeline_context=pipeline_context,
            day_slots=[],
            intent_pools=[],
            dates=["2026-10-01", "2026-10-02"],
            city="北京",
        )

        assert {
            day_number: len([slot for slot in slots if int(slot["dayNumber"]) == day_number])
            for day_number in (1, 2)
        } == {1: 3, 2: 3}
        supplements = pipeline_context["simpleOpenDailyCompletionOccurrences"]
        assert len(supplements) == 1
        assert supplements[0] | {
            "occurrenceId": "occ:goal_daily_completion_day_2:day:2",
            "sourceGoalId": "goal_daily_completion_day_2",
            "intentType": "park",
            "dayNumber": 2,
            "requirementLevel": "inferred_preferred",
            "userExplicit": False,
            "allowedDayNumbers": [2],
            "source": "simple_open_daily_completion_policy",
            "lineageAuthority": "simple_open_daily_completion_policy",
            "dayCompletionRequired": True,
            "supplementReason": "standard_day_route_anchor_floor",
            "experienceFamily": "park_relax",
        } == supplements[0]
        supplement_slot = next(slot for slot in slots if slot["slotId"] == "day2_daily_completion_1")
        assert supplement_slot["routeAnchor"] is True
        assert supplement_slot["rawNeed"] == "顺路城市公园"
        supplement_pool = next(pool for pool in pools if pool["poolId"] == "daily_completion_day_2_pool")
        assert supplement_pool["assignToSlots"] == ["day2_daily_completion_1"]
        assert supplement_pool["candidateHints"] == ["公园"]
        initial_plan = AgentInitialPlanOutput.model_validate(
            {
                "reply": "server-sealed two-day plan",
                "mode": "day_slots",
                "daySlots": slots,
                "intentPools": pools,
            }
        )
        lineage = service._simple_open_slot_occurrence_lineage(initial_plan, pipeline_context)
        supplement_lineage = lineage["day2_daily_completion_1"]
        assert supplement_lineage["lineageAuthority"] == "simple_open_daily_completion_policy"
        assert supplement_lineage["completionRequired"] is False
        assert supplement_lineage["dayCompletionRequired"] is True
        assert supplement_lineage["offerCompletionPriority"] is True
        assert supplement_lineage["schedulePreference"]["dayPart"] == "afternoon"
        assert supplement_lineage["scheduleConstraints"]["durationEstimateSource"] == "server_policy_estimate"
        assert supplement_lineage["scheduleConstraints"]["preferredStartTime"] == "14:00"
        unresolved_supplement = PersistableSegmentPlan(
            day_number=2,
            date="2026-10-02",
            start_time="14:00",
            duration_minutes=60,
            kind="park",
            route_anchor=True,
            selected_poi=None,
            display_title="顺路城市公园待补充",
            notes="未找到通过真实地图与路线门禁的候选",
            grounding_status="unresolved",
            ticket_status="not_checked",
            raw_need="顺路城市公园",
            intent_type="park",
            goal_id=supplement_lineage["goalId"],
            planning_slot_id="day2_daily_completion_1",
            pool_id=supplement_lineage["poolId"],
            source_goal_id=supplement_lineage["sourceGoalId"],
            occurrence_id=supplement_lineage["occurrenceId"],
            lineage_authority=supplement_lineage["lineageAuthority"],
            requirement_level=supplement_lineage["requirementLevel"],
            required=False,
            schedule_preference=supplement_lineage["schedulePreference"],
            schedule_constraints=supplement_lineage["scheduleConstraints"],
        )
        snapshot = service._snapshot_from_persistable_segment_plans(
            service._session(session.session_id),
            initial_plan,
            [unresolved_supplement],
            pipeline_context,
        )
        proposal = SimpleOpenDirectionService._materialize_unresolved_slots_as_pending_metadata(
            snapshot,
            request_contract=request_contract,
        )
        pending = proposal["portfolioPendingSlots"][0]
        assert pending["planningSlotId"] == "day2_daily_completion_1"
        assert pending["lineageAuthority"] == "simple_open_daily_completion_policy"
        assert pending["completionRequired"] is False
        assert pending["dayCompletionRequired"] is True
        assert pending["simpleDirectionRequirementLineageConflict"] is False


def test_daily_completion_policy_respects_optional_budget_zero() -> None:
    """A sparse day cannot receive a server-owned filler when optional budget is zero."""

    clear_database()
    with open_db() as connection:
        service = AgentService(connection)
        ConversationService(connection).create_session("北京", "daily completion budget zero")
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["goal_campus", "goal_park"],
                    "requiredGoalCounts": {"goal_campus": 1, "goal_park": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus"],
                    "requiredGoalCounts": {"goal_campus": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 0,
            "routeGapSupplementHints": [],
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "target": 2,
                    "requiredMin": 2,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
                {
                    "goalId": "goal_park",
                    "intentType": "park",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "one_of_allowed_days",
                    "cardinalitySource": "explicit_singular",
                },
                {
                    "goalId": "goal_meal",
                    "intentType": "meal",
                    "target": 2,
                    "requiredMin": 0,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "soft_experience",
                    "priorityTier": "explicit_soft",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
            ],
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": "北京两日高校游，每天午餐体验当地特色美食。",
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "requestIntentContract": request_contract,
            "routeDecisionContract": {
                "status": "ready",
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
            },
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        service._simple_open_goal_occurrence_plan(pipeline_context)
        slots, _ = service._simple_open_authoritative_occurrence_inventory(
            pipeline_context=pipeline_context,
            day_slots=[],
            intent_pools=[],
            dates=["2026-10-01", "2026-10-02"],
            city="北京",
        )

        assert pipeline_context["simpleOpenDailyCompletionOccurrences"] == []
        assert all(slot["slotId"] != "day2_daily_completion_1" for slot in slots)
        assert {
            day_number: len([slot for slot in slots if int(slot["dayNumber"]) == day_number])
            for day_number in (1, 2)
        } == {1: 3, 2: 2}


def test_daily_completion_policy_skips_meal_only_day_without_non_meal_anchor() -> None:
    """A day containing only meal coverage must not be upgraded into a tour day."""

    clear_database()
    with open_db() as connection:
        service = AgentService(connection)
        ConversationService(connection).create_session("北京", "daily completion meal-only day")
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": ["goal_campus", "goal_park"],
                    "requiredGoalCounts": {"goal_campus": 1, "goal_park": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 1,
            "routeGapSupplementHints": [],
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus",
                    "intentType": "campus_visit",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
                {
                    "goalId": "goal_park",
                    "intentType": "park",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1],
                    "distributionPolicy": "one_of_allowed_days",
                    "cardinalitySource": "explicit_singular",
                },
                {
                    "goalId": "goal_meal",
                    "intentType": "meal",
                    "target": 2,
                    "requiredMin": 0,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "soft_experience",
                    "priorityTier": "explicit_soft",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                },
            ],
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": "北京两日出行，第二天只安排午餐，不补其他景点。",
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "requestIntentContract": request_contract,
            "routeDecisionContract": {
                "status": "ready",
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
            },
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        service._simple_open_goal_occurrence_plan(pipeline_context)
        slots, _ = service._simple_open_authoritative_occurrence_inventory(
            pipeline_context=pipeline_context,
            day_slots=[],
            intent_pools=[],
            dates=["2026-10-01", "2026-10-02"],
            city="北京",
        )

        assert pipeline_context["simpleOpenDailyCompletionOccurrences"] == []
        day_two_slots = [slot for slot in slots if int(slot["dayNumber"]) == 2]
        assert len(day_two_slots) == 1
        assert day_two_slots[0]["kind"] == "meal"
        assert day_two_slots[0]["slotId"] == "day2_lunch"


def test_simple_provider_success_projects_only_controller_authorized_occurrences() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    provider_payload = {
        "reply": "provider returned a broad two-day skeleton",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "provider_day1_campus",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
            },
            {
                "slotId": "provider_day1_area_walk",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "15:00-16:30",
                "startTime": "15:00",
                "durationMinutes": 90,
                "kind": "area_walk",
                "rawNeed": "区域漫步",
                "routeAnchor": True,
            },
            {
                "slotId": "provider_day1_meal",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "12:00-13:00",
                "startTime": "12:00",
                "durationMinutes": 60,
                "kind": "meal",
                "rawNeed": "当地特色美食",
                "routeAnchor": True,
            },
            {
                "slotId": "provider_day1_night",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "19:00-20:30",
                "startTime": "19:00",
                "durationMinutes": 90,
                "kind": "night_view",
                "rawNeed": "夜景观景点",
                "routeAnchor": True,
            },
            {
                "slotId": "provider_day2_campus",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
            },
        ],
        "intentPools": [
            {
                "poolId": "provider_campus_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 2,
                "requirementLevel": "required",
                "goalId": "provider_self_authorized_campus",
                "assignToSlots": ["provider_day1_campus", "provider_day2_campus"],
            },
            {
                "poolId": "provider_area_pool",
                "rawNeed": "区域漫步",
                "city": "北京",
                "intentType": "area_walk",
                "targetCount": 1,
                "assignToSlots": ["provider_day1_area_walk"],
            },
            {
                "poolId": "provider_meal_pool",
                "rawNeed": "当地特色美食",
                "city": "北京",
                "intentType": "meal",
                "targetCount": 1,
                "assignToSlots": ["provider_day1_meal"],
            },
            {
                "poolId": "provider_night_pool",
                "rawNeed": "夜景观景点",
                "city": "北京",
                "intentType": "night_view",
                "targetCount": 1,
                "assignToSlots": ["provider_day1_night"],
            },
        ],
        "warnings": [],
    }

    class SuccessfulInitialPlanProvider:
        @staticmethod
        def generate_initial_plan(_context):
            return json.dumps(provider_payload, ensure_ascii=False)

    class FailingMapProvider:
        def __init__(self):
            self.calls = 0

        def search(self, city, *, keyword, category, limit):
            del city, keyword, category, limit
            self.calls += 1
            raise TimeoutError("authorized campus candidate frontier exhausted")

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "provider success inventory")
        service = AgentService(connection)
        service.provider = SuccessfulInitialPlanProvider()
        map_provider = FailingMapProvider()
        service.simple_open_itinerary_executor.map_poi_service = map_provider
        directive = {
            "type": "draft_itinerary",
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": [],
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        request_contract = {
            "dayCount": 2,
            "requiredIntents": [
                {
                    "goalId": "goal_campus_visit",
                    "intentType": "campus_visit",
                    "target": 1,
                    "requiredMin": 1,
                    "preferredCount": 1,
                    "maxCount": 1,
                    "requirementLevel": "required",
                    "priorityTier": "hard",
                    "userExplicit": True,
                    "allowedDayNumbers": [1, 2],
                    "distributionPolicy": "one_of_allowed_days",
                }
            ],
        }
        pipeline_context = {
            "selectedCity": "北京",
            "city": "北京",
            "effectiveUserMessage": ("2026年10月1日至2日北京两日游，1人，中等预算，公交地铁优先，只参观一所高校。"),
            "serverExecutionProfile": "simple_open_v1",
            "resolvedTripDates": {
                "status": "resolved",
                "startDate": "2026-10-01",
                "endDate": "2026-10-02",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "understoodRequirements": {
                "fields": {
                    "travelDate": "2026-10-01 至 2026-10-02",
                    "travelDays": "2天",
                    "partySize": "1人",
                    "budget": "中等",
                    "transportPreference": "公共交通",
                }
            },
            "requestIntentContract": request_contract,
            "planningDirective": directive,
            "agentDecisionState": {
                "source": "controller",
                "controlOwner": "model_controller",
                "decisionPath": "full",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "actionDirective": directive,
            },
        }

        initial_plan = service._generate_initial_day_slot_output(pipeline_context, [])
        assert initial_plan.mode == "day_slots"
        assert [(slot.slot_id, slot.day_number, slot.kind) for slot in initial_plan.day_slots] == [
            ("provider_day2_campus", 2, "campus")
        ]
        assert len(initial_plan.intent_pools) == 1
        assert initial_plan.intent_pools[0].goal_id == "goal_campus_visit"
        assert initial_plan.intent_pools[0].assign_to_slots == ["provider_day2_campus"]

        # Defense in depth: even if an untrusted caller reintroduces a slot
        # after the provider-success projection, the executor must not spend a
        # Provider call or emit an empty-occurrence plan for it.
        tampered_payload = initial_plan.model_dump(by_alias=True)
        tampered_payload["daySlots"].append(
            {
                "slotId": "post_projection_unlineaged_area",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "15:00-16:30",
                "startTime": "15:00",
                "durationMinutes": 90,
                "kind": "area_walk",
                "rawNeed": "区域漫步",
                "routeAnchor": True,
            }
        )
        tampered_plan = AgentInitialPlanOutput.model_validate(tampered_payload)
        tool_events: list[dict] = []
        plans = service._simple_open_persistable_segment_plans(
            tampered_plan,
            city="北京",
            transport_mode="public_transit",
            tool_events=tool_events,
            pipeline_context=pipeline_context,
        )
        assert len(plans) == 1
        assert map_provider.calls == 1
        assert any(
            event.get("type") == "simple_open_slot_rejected"
            and (event.get("metadata") or {}).get("slotKey") == "post_projection_unlineaged_area"
            for event in tool_events
        )
        authorized = plans[0]
        assert authorized.selected_poi is None
        assert authorized.day_number == 2
        assert authorized.goal_id == "goal_campus_visit"
        assert authorized.source_goal_id == "goal_campus_visit"
        assert authorized.occurrence_id == "occ:goal_campus_visit:day:2"
        assert authorized.planning_slot_id == "provider_day2_campus"
        assert authorized.lineage_authority == "goal_occurrence_compiler"

        snapshot = service._snapshot_from_persistable_segment_plans(
            service._session(session.session_id),
            tampered_plan,
            plans,
            pipeline_context,
        )
        snapshot_segments = [segment for day in snapshot["days"] for segment in day["segments"]]
        business_segments = [
            segment
            for segment in snapshot_segments
            if (segment.get("semanticMetadata") or {}).get("intentType") != "rest"
        ]
        assert len(business_segments) == 1
        assert business_segments[0]["semanticMetadata"]["occurrenceId"] == "occ:goal_campus_visit:day:2"
        assert not any(
            (segment.get("semanticMetadata") or {}).get("intentType") in {"area_walk", "meal", "night_view"}
            for segment in snapshot_segments
        )
        proposal = SimpleOpenDirectionService._materialize_unresolved_slots_as_pending_metadata(
            snapshot,
            request_contract=request_contract,
        )
        assert not any(
            (segment.get("semanticMetadata") or {}).get("intentType")
            in {"area_walk", "meal", "night_view", "campus_visit"}
            for day in proposal["days"]
            for segment in day["segments"]
        )
        assert len(proposal["portfolioPendingSlots"]) == 1
        pending = proposal["portfolioPendingSlots"][0]
        assert pending["dayNumber"] == 2
        assert pending["goalId"] == "goal_campus_visit"
        assert pending["sourceGoalId"] == "goal_campus_visit"
        assert pending["occurrenceId"] == "occ:goal_campus_visit:day:2"
        assert pending["planningSlotId"] == "provider_day2_campus"
        assert pending["lineageAuthority"] == "goal_occurrence_compiler"


def test_trace_safe_fallback_draft_compiles_sealed_required_and_optional_occurrences() -> None:
    """The server cardinality fallback is authority, not untrusted model output.

    This reproduces sess_f5c7a2e7a616: Full proposed an optional budget of
    5/3, the server accepted a deterministic budget-3 fallback, and the same
    campus goal was hard on Day 1 but optional on Day 2.  Both occurrences
    must pass through the normal ledger/compiler path with independent
    requirement levels and server-sealed identities.
    """

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        service = AgentService(connection)
        directive = {
            "type": "draft_itinerary",
            "goalPriority": ["goal_campus_visit", "goal_park", "goal_meal"],
            "dayStrategies": [
                {
                    "dayNumber": 1,
                    "theme": "满足已确认约束",
                    "requiredGoalIds": ["goal_campus_visit"],
                    "requiredGoalCounts": {"goal_campus_visit": 1},
                    "optionalGoalIds": ["goal_park", "goal_meal"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
                {
                    "dayNumber": 2,
                    "theme": "保留可执行弹性",
                    "requiredGoalIds": [],
                    "requiredGoalCounts": {},
                    "optionalGoalIds": ["goal_campus_visit"],
                    "pace": "standard",
                    "maxRouteAnchors": 4,
                },
            ],
            "optionalExperienceBudget": 3,
            "searchPriority": ["goal_campus_visit"],
            "candidateSelectionPolicy": {"avoidRecentEntities": True},
        }
        context = {
            "serverExecutionProfile": "simple_open_v1",
            "selectedCity": "北京",
            "city": "北京",
            "resolvedTripDates": {
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            "requestIntentContract": {
                "dayCount": 2,
                "requiredIntents": [
                    {
                        "goalId": "goal_campus_visit",
                        "intentType": "campus_visit",
                        "target": 1,
                        "requiredMin": 1,
                        "preferredCount": 2,
                        "maxCount": 2,
                        "requirementLevel": "required",
                        "priorityTier": "hard",
                        "userExplicit": True,
                        "allowedDayNumbers": [1, 2],
                        "distributionPolicy": "spread_across_distinct_days",
                    },
                    {
                        "goalId": "goal_park",
                        "intentType": "park",
                        "target": 1,
                        "requiredMin": 0,
                        "preferredCount": 1,
                        "maxCount": 1,
                        "requirementLevel": "soft_experience",
                        "priorityTier": "explicit_soft",
                        "userExplicit": True,
                        "allowedDayNumbers": [1, 2],
                        "distributionPolicy": "one_of_allowed_days",
                    },
                    {
                        "goalId": "goal_meal",
                        "intentType": "meal",
                        "target": 2,
                        "requiredMin": 2,
                        "preferredCount": 2,
                        "maxCount": 2,
                        "requirementLevel": "soft_experience",
                        "priorityTier": "explicit_soft",
                        "userExplicit": True,
                        "allowedDayNumbers": [1, 2],
                        "distributionPolicy": "every_allowed_day",
                        "cardinalitySource": "explicit_every_day",
                    },
                ],
            },
            "planningDirective": copy.deepcopy(directive),
            "agentDecisionState": {
                "source": "safe_fallback",
                "controlOwner": "safe_fallback",
                "decisionPath": "fallback",
                "accepted": True,
                "primaryAction": "draft_itinerary",
                "reasonCodes": [
                    "controller_cardinality_invalid",
                    "deterministic_cardinality_fallback",
                ],
                "controllerError": (
                    "DecisionNormalizationError:draft_optional_experience_budget_exceeded:"
                    "actionDirective.optionalExperienceBudget:5/3"
                ),
                "actionDirective": copy.deepcopy(directive),
            },
        }

        assert service._simple_open_server_authoritative_draft_applicable(context) is True
        occurrences = service._simple_open_authoritative_occurrences(context)
        campus = [item for item in occurrences if item["sourceGoalId"] == "goal_campus_visit"]
        assert [(item["dayNumber"], item["requirementLevel"]) for item in campus] == [
            (1, "hard"),
            (2, "inferred_preferred"),
        ]
        assert [item["occurrenceId"] for item in campus] == [
            "occ:goal_campus_visit:day:1",
            "occ:goal_campus_visit:day:2",
        ]
        assert all(item["lineageAuthority"] == "goal_occurrence_compiler" for item in campus)
        assert len({item["occurrenceId"] for item in occurrences}) == len(occurrences)

        forged = copy.deepcopy(context)
        forged["planningDirective"]["dayStrategies"][0]["occurrenceId"] = "occ:model:forged"
        forged["agentDecisionState"]["actionDirective"] = copy.deepcopy(forged["planningDirective"])
        assert service._simple_open_server_authoritative_draft_applicable(forged) is False

        unresolved_snapshot = {
            "planId": "plan_trace_fallback",
            "city": "北京",
            "status": "partial",
            "routeDecisionContract": _route_contract(),
            "days": [
                {
                    "dayNumber": 2,
                    "date": "2026-10-02",
                    "segments": [
                        {
                            "id": "seg_day2_optional_campus",
                            "startTime": "09:00",
                            "endTime": "11:00",
                            "durationMinutes": 120,
                            "kind": "campus",
                            "poi": {
                                "id": "poi_unresolved_day2_campus",
                                "amapId": None,
                                "name": "高校地点待补",
                                "source": "agent-text-timeline",
                                "latitude": None,
                                "longitude": None,
                            },
                            "semanticMetadata": {
                                "goalId": "goal_campus_visit",
                                "sourceGoalId": "goal_campus_visit",
                                "occurrenceId": "occ:goal_campus_visit:day:2",
                                "poolId": "campus_pool",
                                "planningSlotId": "day2_optional_campus",
                                "dayNumber": 2,
                                "intentType": "campus_visit",
                                "requirementLevel": "inferred_preferred",
                                "required": False,
                                "lineageAuthority": "goal_occurrence_compiler",
                                "routeAnchor": True,
                                "futureRouteAnchor": True,
                                "routeAnchorExpected": True,
                                "groundingStatus": "unresolved",
                            },
                            "notes": "第 2 天高校候选全部被拒绝",
                        }
                    ],
                }
            ],
        }
        pending_snapshot = SimpleOpenDirectionService._materialize_unresolved_slots_as_pending_metadata(
            unresolved_snapshot,
            request_contract=context["requestIntentContract"],
        )
        pending = pending_snapshot["portfolioPendingSlots"][0]
        assert pending["goalId"] == "goal_campus_visit"
        assert pending["sourceGoalId"] == "goal_campus_visit"
        assert pending["occurrenceId"] == "occ:goal_campus_visit:day:2"
        assert pending["poolId"] == "campus_pool"
        assert pending["planningSlotId"] == "day2_optional_campus"
        assert pending["dayNumber"] == 2
        assert pending["requirementLevel"] == "inferred_preferred"
        assert pending["required"] is False
        assert pending["lineageAuthority"] == "goal_occurrence_compiler"
        assert pending["futureRouteAnchor"] is True
        assert pending["routeAnchorExpected"] is True


def test_append_blocked_direction_keeps_existing_fresh_capability_and_truthful_reply() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "trace shaped append")
        service = AgentService(connection)
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(session.session_id, "user", "北京高校两日游", "active")
        first_assistant_id = service._insert_turn(session.session_id, "assistant", "", "active")
        request_contract = {
            "city": "北京",
            "dayCount": 2,
            "clarificationRequired": False,
            "routeDecisionContract": _route_contract(),
            "requiredIntents": [
                {
                    "goalId": "goal_campus_visit",
                    "intentType": "campus_visit",
                    "requiredMin": 1,
                    "preferredCount": 2,
                    "maxCount": 2,
                    "requirementLevel": "required",
                    "allowedDayNumbers": [1, 2],
                }
            ],
        }
        first = direction_service.offer_direction(
            session_id=session.session_id,
            planning_root_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            source_assistant_turn_id=first_assistant_id,
            expected_base_version_id=None,
            source_observation_fingerprint="a" * 64,
            request_contract_fingerprint=ClarificationCheckpointService._fingerprint(request_contract),
            snapshot=_snapshot(session.active_plan_id, "方案 A", "B000A"),
            request_contract=request_contract,
        )
        proposal_a_id = first["comparisonProjections"][0]["proposalId"]
        assert first["adoptionReadyProposalCount"] == 1

        b_snapshot = _live_shaped_simple_partial_snapshot(session.active_plan_id)
        b_snapshot["title"] = "方案 B"
        b_snapshot["days"][1]["segments"] = []
        b_snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending:day2_optional_campus",
                "slotId": "day2_optional_campus",
                "planningSlotId": "day2_optional_campus",
                "poolId": "campus_visit_pool",
                "dayNumber": 2,
                "startTime": "09:00",
                "endTime": "11:00",
                "durationMinutes": 120,
                "timeWindow": "09:00-11:00",
                "kind": "campus",
                "intentType": "campus_visit",
                "displayNeed": "高校地点",
                "goalId": "goal_campus_visit",
                "sourceGoalId": "goal_campus_visit",
                "occurrenceId": "occ:goal_campus_visit:day:2",
                "lineageAuthority": "goal_occurrence_compiler",
                "requirementLevel": "explicit_soft",
                "required": False,
                "groundingStatus": "unresolved",
                "futureRouteAnchor": True,
                "routeAnchorExpected": True,
                "simpleDirectionProviderExhausted": True,
                "reasonCode": "provider_candidates_exhausted_after_safe_alternative_query",
                "reason": "第 2 天高校候选重复或语义冲突，安全替代查询后仍无合格地点",
            }
        ]
        user_turn_id = service._insert_turn(session.session_id, "user", "继续生成其他方向", "active")
        assistant_turn_id = service._insert_turn(session.session_id, "assistant", "", "streaming")
        request_context = _request_context(root_turn_id=root_turn_id)
        request_context.update(
            {
                "sessionId": session.session_id,
                "planningSelectionRootTurnId": root_turn_id,
                "requestIntentContract": request_contract,
                "viewResolution": {
                    "resolvedAction": "generate_new_direction",
                    "resolutionSource": "server_validated_opaque_choice",
                },
            }
        )
        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options")
        }

        response = service._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            content="继续生成其他方向",
            request_context=request_context,
            pipeline_context=request_context["pipelineContext"],
            session_before=service._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates={
                "status": "resolved",
                "dates": ["2026-10-01", "2026-10-02"],
                "dayCount": 2,
            },
            grounding_report={"resultState": "partial"},
            snapshot=b_snapshot,
            tool_events=[],
        )
        payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (assistant_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options")
        }

        assert counts_after["agent_plan_proposals"] - counts_before["agent_plan_proposals"] == 1
        assert counts_after["itinerary_versions"] - counts_before["itinerary_versions"] == 0
        assert counts_after["itinerary_patches"] - counts_before["itinerary_patches"] == 0
        assert counts_after["route_options"] - counts_before["route_options"] == 0
        assert payload["visibleProposalCount"] == 2
        assert payload["proposalDelta"] == 1
        assert payload["adoptionReadyProposalCount"] == 1
        assert payload["comparisonProjections"][0]["adoptionReady"] is False
        assert payload["comparisonProjections"][0]["pendingSlots"][0]["requirementLevel"] == "explicit_soft"
        assert payload["comparisonProjections"][0]["pendingSlots"][0]["occurrenceId"] == ("occ:goal_campus_visit:day:2")
        assert {item["action"] for item in payload["choiceOptions"]} == {
            "select_plan_proposal",
            "continue_plan_expansion",
            "search_travel_guide_advice",
        }
        confirm = next(item for item in payload["choiceOptions"] if item["action"] == "select_plan_proposal")
        assert confirm["proposalId"] == proposal_a_id
        assert confirm["sourceAssistantTurnId"] == assistant_turn_id
        assert next(item for item in payload["choiceOptions"] if item["action"] == "continue_plan_expansion")[
            "value"
        ] == ("继续生成其他方向")
        assert response.terminal_status == "needs_confirmation"
        assert "已新增第 2 个方向" in response.assistant_turn.content
        assert "第 2 天" in response.assistant_turn.content
        assert "高校" in response.assistant_turn.content
        assert "安全替代查询后仍无合格地点" in response.assistant_turn.content
        assert "共有 1 个可确认方向" in response.assistant_turn.content
        assert "原有方案仍可确认编辑" in response.assistant_turn.content
        assert "补全这个方向，或继续探索新的方向" in response.assistant_turn.content
        assert "请补充缺失要求或生成新的方向" not in response.assistant_turn.content


def test_unresolved_slot_goal_lineage_conflict_is_preserved_and_blocks_confirmation() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "pending lineage conflict")
        service = SimpleOpenDirectionService(connection)
        snapshot = _live_shaped_simple_partial_snapshot(session.active_plan_id)
        unresolved = snapshot["days"][0]["segments"][2]
        unresolved["poi"] = {
            "id": "poi_unresolved_night",
            "amapId": None,
            "name": "夜景地点待补",
            "source": "agent-text-timeline",
            "latitude": None,
            "longitude": None,
        }
        unresolved["semanticMetadata"].update(
            {
                "groundingStatus": "unresolved",
                "goalId": "goal_night_view_occurrence_2",
                "sourceGoalId": "goal_night_view_occurrence_2",
                "requirementLevel": "required",
            }
        )
        snapshot["days"][1]["segments"] = snapshot["days"][1]["segments"][:2]

        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_lineage_root",
            source_user_turn_id="turn_lineage_root",
            source_assistant_turn_id="turn_lineage_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract={
                "routeDecisionContract": _route_contract(),
                "requiredIntents": [
                    {
                        "goalId": "goal_night_view_occurrence_1",
                        "intentType": "night_view",
                        "requiredMin": 1,
                        "requirementLevel": "required",
                    }
                ],
            },
        )

        proposal = connection.execute(
            "SELECT status, snapshot_json, verifier_json FROM agent_plan_proposals"
        ).fetchone()
        material = json.loads(proposal["snapshot_json"])
        verifier = json.loads(proposal["verifier_json"])
        pending = material["portfolioPendingSlots"][0]
        assert pending["goalId"] == "goal_night_view_occurrence_2"
        assert pending["sourceGoalId"] == "goal_night_view_occurrence_2"
        assert pending["simpleDirectionRequirementLineageConflict"] is True
        assert verifier["pendingSlotLineageConflictCount"] == 1
        assert "simple_direction_pending_slot_lineage_conflict" in verifier["hardFailures"]
        assert proposal["status"] == "blocked"
        assert response["choiceOptions"] == []


def test_simple_direction_activation_does_not_relax_hard_slot_or_verified_semantic_failure() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _live_shaped_simple_partial_snapshot("plan_simple_hard_guards")
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": "hard_missing_campus",
            "planningSlotId": "hard_missing_campus",
            "poolId": "campus_pool",
            "dayNumber": 2,
            "intentType": "campus_visit",
            "requirementLevel": "required",
        }
    ]
    hard_slot = SimpleOpenDirectionService.activation_verifier(snapshot)
    assert hard_slot["passed"] is False
    assert hard_slot["pendingHardSlotCount"] == 1
    assert "simple_direction_pending_hard_slot" in hard_slot["hardFailures"]

    invalid_identity_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_identity_guard")
    invalid_identity = invalid_identity_snapshot["days"][0]["segments"][2]
    invalid_identity["poi"].update(
        {
            "amapId": None,
            "source": "agent-text-timeline",
            "latitude": None,
            "longitude": None,
        }
    )
    invalid = SimpleOpenDirectionService.activation_verifier(invalid_identity_snapshot)
    assert invalid["passed"] is False
    assert invalid["invalidMaterializedIdentityCount"] == 1
    assert "simple_direction_materialized_map_identity_invalid" in invalid["hardFailures"]


def test_provider_exhausted_required_slot_never_becomes_confirmable() -> None:
    """A partial 985-style draft may remain visible, but is never adoptable."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _snapshot("plan_required_gap_guard", "资格高校候选不足", "B000A6EA36")
    snapshot["portfolioPendingSlots"] = [
        {
            "slotId": "slot_required_campus_day_2",
            "planningSlotId": "slot_required_campus_day_2",
            "poolId": "pool_campus_visit",
            "dayNumber": 2,
            "intentType": "campus_visit",
            "requirementLevel": "required",
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": "occ:goal_campus_visit:day:2",
            "lineageAuthority": "goal_occurrence_compiler",
            "groundingStatus": "unresolved",
            "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
            "simpleDirectionProviderExhausted": True,
            "simpleDirectionRequirementLineageConflict": False,
        }
    ]
    snapshot["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": str((snapshot.get("routeDecisionContract") or {}).get("fingerprint") or ""),
        "expectedPairs": [],
        "verifiedPairs": [],
        "routeCoverageComplete": False,
        "adjacentLegCompliance": "pending",
        "topologyCompliance": "pending",
        "providerBaselineCompared": False,
        "detourCompliance": "not_evaluated",
        "failureReason": "topology_constraint_not_ready",
    }

    activation = SimpleOpenDirectionService.activation_verifier(snapshot)
    verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert activation["pendingHardSlotCount"] == 1
    assert activation["providerExhaustedRequiredSlotCount"] == 1
    assert activation["blockingPendingHardSlotCount"] == 1
    assert activation["passed"] is False
    assert verifier["confirmationPassed"] is False
    assert verifier["passed"] is False
    assert "simple_direction_pending_hard_slot" in verifier["hardFailures"]


def test_noncompact_route_pending_snapshot_is_read_only_instead_of_legacy_confirmable() -> None:
    """Missing adjacent constraints must not turn missing Provider routes into success."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _live_shaped_simple_partial_snapshot("plan_legacy_route_pending_guard")
    verifier = SimpleOpenDirectionService._proposal_verifier(snapshot)

    assert verifier["compactRouteContractRequired"] is False
    assert verifier["routeStatus"] == "route_pending"
    assert verifier["routeCoverageComplete"] is False
    assert verifier["confirmationPassed"] is False
    assert verifier["passed"] is False
    assert "route_evidence_incomplete" in verifier["hardFailures"]

    non_anchor_fake_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_non_anchor_identity_guard")
    non_anchor_fake = non_anchor_fake_snapshot["days"][0]["segments"][1]
    non_anchor_fake["semanticMetadata"]["routeAnchor"] = False
    non_anchor_fake["poi"].update(
        {
            "amapId": None,
            "source": "agent-text-timeline",
            "latitude": None,
            "longitude": None,
            "name": "待补餐厅",
            "type": "餐饮服务;中餐厅",
            "providerType": "餐饮服务;中餐厅",
        }
    )
    non_anchor_invalid = SimpleOpenDirectionService.activation_verifier(non_anchor_fake_snapshot)
    assert non_anchor_invalid["passed"] is False
    assert non_anchor_invalid["invalidMaterializedIdentityCount"] == 1
    assert "simple_direction_materialized_map_identity_invalid" in non_anchor_invalid["hardFailures"]

    semantic_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_semantic_guard")
    photographer = semantic_snapshot["days"][0]["segments"][2]
    photographer["poi"].update(
        {
            "name": "三川影像婚纱摄影(北京店)",
            "type": "生活服务;摄影冲印店;摄影冲印",
            "providerType": "生活服务;摄影冲印店;摄影冲印",
        }
    )
    semantic = SimpleOpenDirectionService.activation_verifier(semantic_snapshot)
    assert semantic["passed"] is False
    assert semantic["semanticFailureCount"] == 1
    assert "simple_direction_verified_semantic_mismatch" in semantic["hardFailures"]

    institutional_meal_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_meal_quality_guard")
    institutional_meal = institutional_meal_snapshot["days"][0]["segments"][1]
    institutional_meal["poi"].update(
        {
            "name": "中央财经大学(沙河校区)西区食堂",
            "type": "餐饮服务;中餐厅;中餐厅",
            "providerType": "餐饮服务;中餐厅;中餐厅",
            "providerTypeCode": "050100",
        }
    )
    institutional_meal["semanticMetadata"]["rawNeed"] = "当地特色美食"
    institutional = SimpleOpenDirectionService.activation_verifier(institutional_meal_snapshot)
    assert institutional["passed"] is False
    assert institutional["semanticFailureCount"] == 1
    assert "simple_direction_verified_semantic_mismatch" in institutional["hardFailures"]

    non_985_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_985_guard")
    non_985_campus = non_985_snapshot["days"][0]["segments"][0]
    qualification_evidence = EntityQualificationEvidenceService.qualified_entities(
        locality="北京",
        scheme="moe_project_classification",
        value="985",
    )
    assert qualification_evidence is not None
    peking_entity = next(
        item for item in qualification_evidence["entities"] if item["canonicalName"] == "北京大学"
    )
    qualification_binding = EntityQualificationEvidenceService.build_binding(
        evidence=qualification_evidence,
        entity=peking_entity,
        planning_root_id="plan_simple_985_guard",
        request_contract_fingerprint="f" * 64,
    )
    non_985_campus["poi"].update(
        {
            "amapId": "B000A7PRL6",
            "name": "北京科技大学管庄校区",
            "type": "科教文化服务;学校;高等院校",
            "providerType": "科教文化服务;学校;高等院校",
        }
    )
    non_985_campus["semanticMetadata"].update(
        {
            "rawNeed": "高校参观",
            "scheduleConstraints": {
                "qualificationBinding": qualification_binding,
                "qualificationBindingFingerprint": qualification_binding["bindingFingerprint"],
            },
        }
    )
    non_985 = SimpleOpenDirectionService.activation_verifier(non_985_snapshot)
    assert non_985["passed"] is False
    assert non_985["semanticFailureCount"] == 1
    assert "simple_direction_verified_semantic_mismatch" in non_985["hardFailures"]

    generic_local_meal_snapshot = _live_shaped_simple_partial_snapshot("plan_simple_local_contract_guard")
    generic_local_meal = generic_local_meal_snapshot["days"][0]["segments"][1]
    generic_local_meal["semanticMetadata"]["rawNeed"] = "午餐"
    generic_local_meal["semanticMetadata"].setdefault("scheduleConstraints", {})["localFoodRequired"] = True
    generic_local = SimpleOpenDirectionService.activation_verifier(generic_local_meal_snapshot)
    assert generic_local["passed"] is False
    assert generic_local["semanticFailureCount"] == 1
    assert "simple_direction_verified_semantic_mismatch" in generic_local["hardFailures"]


def test_campus_meal_park_route_targets_are_shared_by_readiness_and_compact_verifier() -> None:
    """Density flags do not remove a real meal from the physical route graph."""

    from src.services.proposal_readiness_service import ProposalReadinessService
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    snapshot = _live_shaped_simple_partial_snapshot("plan_explicit_non_anchor_meal_pair_guard")
    snapshot["days"] = snapshot["days"][:1]
    day = snapshot["days"][0]
    meal = next(segment for segment in day["segments"] if segment["semanticMetadata"]["intentType"] == "meal")
    meal["semanticMetadata"].update(
        {
            "routeAnchor": False,
            "routeAnchorExpected": False,
        }
    )
    park = day["segments"][2]
    park["id"] = "seg_park_1"
    park["kind"] = "park"
    park["poi"].update(
        {
            "amapId": "B000A7O5PK",
            "name": "奥林匹克森林公园",
            "type": "风景名胜;公园广场;公园",
            "providerType": "风景名胜;公园广场;公园",
        }
    )

    expected_segment_pairs = [
        {
            "fromSegmentId": "seg_campus_1",
            "toSegmentId": "seg_meal_1",
            "dayNumber": 1,
        },
        {
            "fromSegmentId": "seg_meal_1",
            "toSegmentId": "seg_park_1",
            "dayNumber": 1,
        },
    ]
    assert ProposalReadinessService.expected_route_pairs(snapshot) == expected_segment_pairs

    route_contract = _route_contract(compact=True)
    expected_amap_pairs = [
        {
            "dayNumber": 1,
            "pairOrdinal": 1,
            "fromSegmentId": "slot_seg_campus_1",
            "toSegmentId": "slot_seg_meal_1",
            "fromAmapId": "B000A6EA36",
            "toAmapId": "B0J1CRCYMW",
        },
        {
            "dayNumber": 1,
            "pairOrdinal": 2,
            "fromSegmentId": "slot_seg_meal_1",
            "toSegmentId": "slot_seg_night_1",
            "fromAmapId": "B0J1CRCYMW",
            "toAmapId": "B000A7O5PK",
        },
    ]
    verified_pairs = []
    for index, pair in enumerate(expected_amap_pairs, start=1):
        verified_pair = {
            **pair,
            "transportMode": "transit",
            "durationSeconds": 600 + index,
            "distanceMeters": 2000 + index,
            "provider": "amap-webservice",
            "queriedAt": "2026-08-23T00:00:00+00:00",
        }
        verified_pair["providerEvidenceFingerprint"] = SimpleOpenDirectionService._provider_evidence_fingerprint(
            verified_pair
        )
        verified_pairs.append(verified_pair)
    compact = SimpleOpenDirectionService._verify_compact_route_pair_evidence(
        snapshot,
        route_audit={
            "expectedPairs": expected_amap_pairs,
            "verifiedPairs": verified_pairs,
        },
        route_contract=route_contract,
    )
    assert compact["passed"] is True, compact
    assert compact["expectedPairs"] == expected_amap_pairs

    meal["semanticMetadata"]["requiresRouteEdge"] = False
    assert ProposalReadinessService.expected_route_pairs(snapshot) == [
        {
            "fromSegmentId": "seg_campus_1",
            "toSegmentId": "seg_park_1",
            "dayNumber": 1,
        }
    ]


def test_explicit_every_day_meal_pending_is_read_only_blocked_and_zero_writer() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    request_contract = {
        "dayCount": 1,
        "routeDecisionContract": _route_contract(),
        "requiredIntents": [
            {
                "goalId": "goal_meal",
                "intentType": "meal",
                "target": 1,
                "requiredMin": 0,
                "preferredCount": 1,
                "maxCount": 1,
                "requirementLevel": "soft_experience",
                "priorityTier": "explicit_soft",
                "userExplicit": True,
                "allowedDayNumbers": [1],
                "distributionPolicy": "every_allowed_day",
                "cardinalitySource": "explicit_every_day",
            }
        ],
    }
    snapshot = _snapshot("plan_daily_meal_pending", "每日用餐待补", "B000A")
    snapshot["days"][0]["segments"].append(
        {
            "id": "seg_unresolved_meal_1",
            "startTime": "12:00",
            "endTime": "13:00",
            "durationMinutes": 60,
            "kind": "meal",
            "poi": {
                "id": "poi_unresolved_meal_1",
                "amapId": None,
                "name": "当地特色餐厅待补",
                "source": "agent-text-timeline",
                "latitude": None,
                "longitude": None,
            },
            "semanticMetadata": {
                "goalId": "goal_meal",
                "sourceGoalId": "goal_meal",
                "occurrenceId": "occ:goal_meal:day:1",
                "poolId": "meal_pool",
                "planningSlotId": "day1_lunch",
                "dayNumber": 1,
                "intentType": "meal",
                "requirementLevel": "explicit_soft",
                "required": False,
                "lineageAuthority": "simple_open_request_contract_every_day_meal",
                "routeAnchor": False,
                "futureRouteAnchor": False,
                "routeAnchorExpected": False,
                "groundingStatus": "unresolved",
            },
            "notes": "该日明确午餐尚未找到合格 AMap 候选",
        }
    )
    snapshot["simpleOpenRouteAssignment"] = {
        "schemaVersion": "simple-open-route-evidence-v2",
        "routeContractFingerprint": request_contract["routeDecisionContract"]["fingerprint"],
        "expectedPairs": [],
        "verifiedPairs": [],
        "providerRoutePairs": [],
        "routeCoverageComplete": False,
        "routeFeasibilityExhausted": False,
        "routeProviderAttemptCount": 0,
        "adjacentLegCompliance": "pending",
        "detourCompliance": "not_evaluated",
        "topologyCompliance": "failed",
        "failureReason": "topology_constraint_exceeded",
    }
    remaining_query_scopes = [
        {
            "dayNumber": 1,
            "slotId": "day1_lunch",
            "daySeedAmapId": "B000000001",
            "queryScopeFingerprint": "m" * 64,
            "centerRole": "predecessor",
            "queryRole": "adjacent_candidate_center",
            "queryText": "餐厅",
            "priority": 0,
            "currentPartialCompletionSlot": True,
            "predecessorAmapId": "B000000001",
            "successorAmapId": None,
            "predecessorBeamRank": 0,
            "isActiveScope": True,
            "attemptedThisTurn": True,
            "providerOutcome": "success",
            "remainingReason": "no_candidate_selected",
        }
    ]
    prematurely_materialized = SimpleOpenDirectionService.prepare_direction_snapshot(
        snapshot,
        request_contract=request_contract,
    )
    assert prematurely_materialized["portfolioPendingSlots"][0]["simpleDirectionProviderExhausted"] is True

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "explicit daily meal pending")
        service = SimpleOpenDirectionService(connection)
        service.ensure_root(
            session_id=session.session_id,
            planning_root_id="turn_daily_meal_root",
            source_assistant_turn_id="turn_daily_meal_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            request_contract=request_contract,
            locality="北京",
            max_pages_per_query=3,
        )
        response = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_daily_meal_root",
            source_user_turn_id="turn_daily_meal_root",
            source_assistant_turn_id="turn_daily_meal_assistant",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=prematurely_materialized,
            request_contract=request_contract,
            remaining_query_scopes=remaining_query_scopes,
        )
        stored = connection.execute("SELECT status, snapshot_json, verifier_json FROM agent_plan_proposals").fetchone()
        write_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("itinerary_versions", "itinerary_patches", "route_options")
        }

    stored_snapshot = json.loads(stored["snapshot_json"])
    stored_verifier = json.loads(stored["verifier_json"])
    pending = stored_snapshot["portfolioPendingSlots"][0]
    projection = response["comparisonProjections"][0]

    assert pending["requirementLevel"] == "explicit_soft"
    assert pending["completionRequired"] is True
    assert pending["userExplicit"] is True
    assert pending["distributionPolicy"] == "every_allowed_day"
    assert pending["cardinalitySource"] == "explicit_every_day"
    assert pending["reasonCode"] == "provider_candidate_frontier_remaining"
    assert pending["simpleDirectionProviderExhausted"] is False
    assert stored_verifier["completionRequiredPendingSlotCount"] == 1
    assert "simple_direction_pending_completion_required_slot" in stored_verifier["hardFailures"]
    assert stored["status"] == "blocked"
    assert projection["adoptionReady"] is False
    assert projection["strictlyVerified"] is False
    assert projection["structureReady"] is False
    assert projection["adoptionMode"] == "blocked"
    assert projection["isPartial"] is True
    assert not any(option["action"] == "select_plan_proposal" for option in response["choiceOptions"])
    continuation_choices = [
        option for option in response["choiceOptions"] if option["action"] == "continue_plan_expansion"
    ]
    assert len(continuation_choices) == 1
    assert continuation_choices[0]["sourceAssistantTurnId"] == "turn_daily_meal_assistant"
    assert continuation_choices[0]["rootPortfolioId"] == response["rootPortfolioId"]
    assert response["frontierStatus"] == "has_more"
    assert response["comparisonSummary"]["remainingPoiPageCount"] > 0
    assert write_counts == {
        "itinerary_versions": 0,
        "itinerary_patches": 0,
        "route_options": 0,
    }


def test_compatibility_frontier_projection_uses_post_outcome_authoritative_queue() -> None:
    from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="turn_frontier_projection_root",
        request_contract_fingerprint="r" * 64,
        evidence={"schemaVersion": "compatibility-slot-frontier-v1", "entities": []},
        locality="北京",
        max_pages_per_query=1,
    )
    scope = {
        "dayNumber": 1,
        "slotId": "day1_lunch",
        "daySeedAmapId": "B000000001",
        "queryScopeFingerprint": "s" * 64,
        "centerRole": "predecessor",
        "queryRole": "adjacent_candidate_center",
        "queryText": "北京菜",
        "priority": 0,
        "currentPartialCompletionSlot": True,
        "predecessorAmapId": "B000000001",
        "successorAmapId": "",
        "predecessorBeamRank": 0,
        "initialPageAlreadyAttempted": False,
        "isActiveScope": True,
        "attemptedThisTurn": True,
        "providerOutcome": "success",
        "remainingReason": "no_candidate_selected",
    }
    frontier["remainingQueryScopes"] = SimpleDirectionFrontierService.normalize_remaining_query_scopes([scope])
    frontier["remainingQueryScopesAuthoritative"] = True
    SimpleDirectionFrontierService._refresh_status(frontier)
    query = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="day1_lunch",
        day_seed_amap_id="B000000001",
        query_scope_fingerprint="s" * 64,
    )
    attempt = {
        "slotFrontierSnapshot": frontier,
        "slotQueries": {"day1_lunch": query},
        "maxPagesPerQuery": 1,
    }
    outcome = {
        "query": query,
        "providerOutcome": "success",
        "admittedPhysicalGroups": ["amap:B0LOCALFOOD"],
        "rejectedPhysicalGroups": [],
    }

    assert (
        SimpleOpenDirectionService._compatibility_page_has_more(
            attempt,
            remaining_query_scopes=[scope],
            slot_query_outcomes=[outcome],
        )
        is False
    )
    assert (
        SimpleOpenDirectionService._compatibility_page_has_more(
            attempt,
            remaining_query_scopes=[],
            slot_query_outcomes=[outcome],
        )
        is False
    )


def test_blocked_simple_direction_response_never_claims_it_can_be_confirmed() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "blocked direction reply")
        service = AgentService(connection)
        user_turn_id = service._insert_turn(session.session_id, "user", "生成一个夜景方向", "active")
        assistant_turn_id = service._insert_turn(session.session_id, "assistant", "", "streaming")
        request_context = _request_context(root_turn_id=user_turn_id)
        request_context["sessionId"] = session.session_id
        snapshot = _live_shaped_simple_partial_snapshot(session.active_plan_id)
        photographer = snapshot["days"][0]["segments"][2]
        photographer["poi"].update(
            {
                "name": "三川影像婚纱摄影(北京店)",
                "type": "生活服务;摄影冲印店;摄影冲印",
                "providerType": "生活服务;摄影冲印店;摄影冲印",
            }
        )

        response = service._simple_open_direction_proposal_response(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            content="生成一个夜景方向",
            request_context=request_context,
            pipeline_context=request_context["pipelineContext"],
            session_before=service._session(session.session_id),
            initial_plan=AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[]),
            resolved_dates=request_context["resolvedTripDates"],
            grounding_report={"resultState": "partial"},
            snapshot=snapshot,
            tool_events=[],
        )

        payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (assistant_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        assert response.terminal_status == "candidate_refresh_required"
        assert response.assistant_turn.choice_options == []
        assert "你可以确认编辑" not in response.assistant_turn.content
        assert "暂不能确认编辑" in response.assistant_turn.content
        assert payload["adoptionReadyProposalCount"] == 0
        assert payload["terminalStatus"] == "candidate_refresh_required"
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_direction_offer_during_commit_fails_before_appending_proposal() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction offer CAS")
        service = SimpleOpenDirectionService(connection)
        first = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        portfolio_id = str(first["rootPortfolioId"])
        connection.execute(
            "UPDATE agent_plan_portfolios SET status = 'committing' WHERE id = ?",
            (portfolio_id,),
        )
        connection.commit()
        before_summary = connection.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()["summary_json"]

        with pytest.raises(ValueError, match="simple_direction_offer_in_progress"):
            service.offer_direction(
                session_id=session.session_id,
                planning_root_id="turn_root",
                source_user_turn_id="turn_direction_b",
                source_assistant_turn_id="turn_assistant_b",
                expected_base_version_id=None,
                source_observation_fingerprint="p" * 64,
                request_contract_fingerprint="r" * 64,
                snapshot=_snapshot(session.active_plan_id, "高校人文线", "B000B"),
                request_contract={"routeDecisionContract": _route_contract()},
            )

        root = connection.execute(
            "SELECT status, summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        assert root["status"] == "committing"
        assert root["summary_json"] == before_summary
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0


def test_save_active_direction_reloads_server_snapshot_and_never_creates_version_or_patch() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction save")
        service = SimpleOpenDirectionService(connection)
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = offered["comparisonProjections"][0]["proposalId"]
        portfolio_id = offered["rootPortfolioId"]
        active = _snapshot(session.active_plan_id, "高校经典线（已编辑）", "B000A")
        active["days"][0]["segments"][0]["startTime"] = "08:30"
        active["days"][0]["segments"][0]["endTime"] = "10:30"
        connection.execute(
            "INSERT INTO itinerary_versions (id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_active', ?, ?, 1, 'agent', ?, '2026-08-18T00:00:00+00:00')",
            (session.session_id, session.active_plan_id, json.dumps(active, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_active' WHERE id = ?",
            (session.session_id,),
        )
        connection.execute(
            "UPDATE agent_plan_portfolios SET selected_proposal_id = ?, expected_base_version_id = 'ver_active' WHERE id = ?",
            (proposal_id, portfolio_id),
        )
        connection.commit()

        result = service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_id,
            planning_root_id="turn_root",
            portfolio_id=portfolio_id,
            base_version_id="ver_active",
        )
        repeated = service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_id,
            planning_root_id="turn_root",
            portfolio_id=portfolio_id,
            base_version_id="ver_active",
        )

        stored = connection.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        assert json.loads(stored[0])["days"][0]["segments"][0]["startTime"] == "08:30"
        assert result["saved"] is True and result["unchanged"] is False
        assert repeated["saved"] is True and repeated["unchanged"] is True
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_save_active_committed_direction_keeps_confirmation_capability_when_only_title_is_pending() -> None:
    """Missing title prose cannot revoke an already-confirmed editable direction."""

    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "committed direction save")
        service = SimpleOpenDirectionService(connection)
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = str(offered["comparisonProjections"][0]["proposalId"])
        portfolio_id = str(offered["rootPortfolioId"])
        active = _snapshot(session.active_plan_id, "高校经典线（已编辑）", "B000A")
        active["days"][0]["segments"][0]["startTime"] = "08:30"
        active["days"][0]["segments"][0]["endTime"] = "10:30"
        active.pop("portfolioTitleGeneration", None)
        active.pop("portfolioTitleEvidence", None)
        connection.execute(
            "INSERT INTO itinerary_versions (id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_active', ?, ?, 1, 'user_timeline_mutation', ?, '2026-08-18T00:00:00+00:00')",
            (session.session_id, session.active_plan_id, json.dumps(active, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_active' WHERE id = ?",
            (session.session_id,),
        )
        connection.execute(
            "UPDATE agent_plan_portfolios SET selected_proposal_id = ?, expected_base_version_id = 'ver_active' "
            "WHERE id = ?",
            (proposal_id, portfolio_id),
        )
        connection.execute(
            "UPDATE agent_plan_proposals SET status = 'committed' WHERE id = ?",
            (proposal_id,),
        )
        proposal_row = connection.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        proposal_snapshot = json.loads(proposal_row["snapshot_json"])
        proposal_snapshot.pop("portfolioTitleGeneration", None)
        proposal_snapshot.pop("portfolioTitleEvidence", None)
        connection.execute(
            "UPDATE agent_plan_proposals SET snapshot_json = ? WHERE id = ?",
            (json.dumps(proposal_snapshot, ensure_ascii=False), proposal_id),
        )
        connection.commit()

        result = service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_id,
            planning_root_id="turn_root",
            portfolio_id=portfolio_id,
            base_version_id="ver_active",
        )
        carrier_id = str(result["comparisonProjection"]["sourceAssistantTurnId"])
        carrier = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (carrier_id,),
            ).fetchone()["agent_response_json"]
        )
        portfolio_summary = json.loads(
            connection.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()["summary_json"]
        )

    projection = result["comparisonProjection"]
    assert projection["isAdopted"] is True
    assert projection["proposalLifecycleStatus"] == "committed"
    assert projection["adoptionReady"] is True
    assert "proposal_title_generation_pending" not in projection["blockingReasons"]
    assert len(carrier["choiceOptions"]) == 1
    embedded = carrier["choiceOptions"][0]["comparisonProjection"]
    assert embedded["proposalId"] == proposal_id
    assert embedded["adoptionReady"] is True
    assert embedded["blockingReasons"] == []
    assert portfolio_summary["adoptionReadyProposalCount"] == 1


def test_unadopted_simple_direction_with_missing_title_keeps_confirmation_capability() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "untitled candidate")
        snapshot = _snapshot(session.active_plan_id, "高校经典线", "B000A")
        snapshot.pop("portfolioTitleGeneration", None)
        snapshot.pop("portfolioTitleEvidence", None)

        offered = SimpleOpenDirectionService(connection).offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=snapshot,
            request_contract={"routeDecisionContract": _route_contract()},
        )

    projection = offered["comparisonProjections"][0]
    assert projection["adoptionReady"] is True
    assert "proposal_title_generation_pending" not in projection["blockingReasons"]
    assert "simple_direction_title_generation_unavailable" not in projection["softWarnings"]
    assert [option["action"] for option in offered["choiceOptions"]] == [
        "select_plan_proposal",
        "continue_plan_expansion",
        "search_travel_guide_advice",
    ]


def test_save_active_direction_rolls_back_material_when_capability_carrier_fails(monkeypatch) -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction save atomicity")
        service = SimpleOpenDirectionService(connection)
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = str(offered["comparisonProjections"][0]["proposalId"])
        portfolio_id = str(offered["rootPortfolioId"])
        before_proposal = connection.execute(
            "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()["snapshot_json"]
        active = _snapshot(session.active_plan_id, "高校经典线（已编辑）", "B000A")
        active["days"][0]["segments"][0]["startTime"] = "08:30"
        active["days"][0]["segments"][0]["endTime"] = "10:30"
        connection.execute(
            "INSERT INTO itinerary_versions (id, session_id, plan_id, version_number, source_type, snapshot_json, created_at) "
            "VALUES ('ver_active', ?, ?, 1, 'agent', ?, '2026-08-18T00:00:00+00:00')",
            (session.session_id, session.active_plan_id, json.dumps(active, ensure_ascii=False)),
        )
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = 'ver_active' WHERE id = ?",
            (session.session_id,),
        )
        connection.execute(
            "UPDATE agent_plan_portfolios SET selected_proposal_id = ?, expected_base_version_id = 'ver_before' "
            "WHERE id = ?",
            (proposal_id, portfolio_id),
        )
        connection.commit()

        def fail_carrier(**_kwargs):
            raise RuntimeError("carrier insert failed")

        monkeypatch.setattr(service, "_persist_capability_carrier", fail_carrier)
        with pytest.raises(RuntimeError, match="carrier insert failed"):
            service.save_active_direction(
                session_id=session.session_id,
                proposal_id=proposal_id,
                planning_root_id="turn_root",
                portfolio_id=portfolio_id,
                base_version_id="ver_active",
            )

        root = connection.execute(
            "SELECT status, expected_base_version_id FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        assert root["status"] == "awaiting_selection"
        assert root["expected_base_version_id"] == "ver_before"
        assert (
            connection.execute(
                "SELECT snapshot_json FROM agent_plan_proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()["snapshot_json"]
            == before_proposal
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE status = 'internal_capability'",
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_save_active_direction_rejects_committing_root_without_mutation() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction save CAS")
        service = SimpleOpenDirectionService(connection)
        offered = service.offer_direction(
            session_id=session.session_id,
            planning_root_id="turn_root",
            source_user_turn_id="turn_root",
            source_assistant_turn_id="turn_assistant_a",
            expected_base_version_id=None,
            source_observation_fingerprint="o" * 64,
            request_contract_fingerprint="r" * 64,
            snapshot=_snapshot(session.active_plan_id, "高校经典线", "B000A"),
            request_contract={"routeDecisionContract": _route_contract()},
        )
        proposal_id = str(offered["comparisonProjections"][0]["proposalId"])
        portfolio_id = str(offered["rootPortfolioId"])
        connection.execute(
            "UPDATE agent_plan_portfolios SET status = 'committing', selected_proposal_id = ? WHERE id = ?",
            (proposal_id, portfolio_id),
        )
        connection.commit()
        before = connection.execute(
            "SELECT status, expected_base_version_id, summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()

        with pytest.raises(ValueError, match="simple_direction_save_in_progress"):
            service.save_active_direction(
                session_id=session.session_id,
                proposal_id=proposal_id,
                planning_root_id="turn_root",
                portfolio_id=portfolio_id,
                base_version_id="ver_missing",
            )

        after = connection.execute(
            "SELECT status, expected_base_version_id, summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        assert tuple(after) == tuple(before)
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


def test_confirm_save_and_switch_directions_reissues_fresh_opaque_capabilities_exactly_once() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "direction switch")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京高校一日游，公共交通，适度绕行",
            "active",
        )
        turn_a, proposal_a = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="高校经典线",
            poi_id="B000A",
        )
        direction_b_user_turn = service._insert_turn(
            session.session_id,
            "user",
            "再生成一个公园方向",
            "active",
        )
        turn_b, proposal_b = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=direction_b_user_turn,
            title="高校人文线",
            poi_id="B000B",
        )

        first_choice = next(
            item for item in service._turn_response(turn_b).choice_options if item.get("proposalId") == proposal_a
        )
        stale_choice = next(
            item for item in service._turn_response(turn_a).choice_options if item.get("proposalId") == proposal_a
        )
        with pytest.raises(HTTPException) as stale_error:
            service.send_message(
                session.session_id,
                AgentMessageRequest(
                    content="确认编辑",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": turn_a,
                            "choiceId": stale_choice["id"],
                        }
                    },
                ),
            )
        assert stale_error.value.detail["code"] == "plan_proposal_request_scope_invalid"
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        confirmed_a = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": turn_b,
                        "choiceId": first_choice["id"],
                    }
                },
            ),
        )
        assert confirmed_a.version is not None
        assert confirmed_a.assistant_turn.comparison_projection_update_mode == "replace"
        assert {item.get("proposalId") for item in confirmed_a.assistant_turn.choice_options} == {
            proposal_a,
            proposal_b,
        }
        assert all(
            item.get("expectedBaseVersionId") == confirmed_a.version.id
            for item in confirmed_a.assistant_turn.choice_options
        )
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 1
        confirmed_a_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (confirmed_a.version.id,),
            ).fetchone()["snapshot_json"]
        )
        assert confirmed_a_snapshot["routeDecisionContract"]["status"] == "ready"

        active_a = service._session(session.session_id)
        segment_a = connection.execute(
            "SELECT id FROM itinerary_segments WHERE plan_id = ? ORDER BY start_time LIMIT 1",
            (active_a["active_plan_id"],),
        ).fetchone()
        edited_a = ItineraryPatchService(connection).apply_patch(
            active_a["active_plan_id"],
            [
                ItineraryPatchOperation(
                    op="replace_segment_start_time",
                    segmentId=str(segment_a["id"]),
                    startTime="08:30",
                )
            ],
            source_type="user_timeline_mutation",
            base_version_id=confirmed_a.version.id,
            source_turn_id=None,
            preference_summary="",
            planning_context={"simpleOpenNonBlockingRoutes": True},
        )
        edited_a_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (edited_a.version.id,),
            ).fetchone()["snapshot_json"]
        )
        assert edited_a_snapshot["routeDecisionContract"]["status"] == "ready"
        saved_a = direction_service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_a,
            planning_root_id=root_turn_id,
            portfolio_id=str(first_choice["rootPortfolioId"]),
            base_version_id=edited_a.version.id,
        )
        repeated_save_a = direction_service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_a,
            planning_root_id=root_turn_id,
            portfolio_id=str(first_choice["rootPortfolioId"]),
            base_version_id=edited_a.version.id,
        )
        assert saved_a["unchanged"] is False
        assert repeated_save_a["unchanged"] is True
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 2
        carrier_a = str(saved_a["comparisonProjection"]["sourceAssistantTurnId"])
        assert carrier_a
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND status = 'internal_capability'",
                (session.session_id,),
            ).fetchone()[0]
            == 1
        )

        choice_b = next(
            item for item in service._turn_response(carrier_a).choice_options if item.get("proposalId") == proposal_b
        )
        confirmed_b = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": carrier_a,
                        "choiceId": choice_b["id"],
                    }
                },
            ),
        )
        assert confirmed_b.version is not None
        assert confirmed_b.version.id != edited_a.version.id
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 3
        assert all(
            item.get("expectedBaseVersionId") == confirmed_b.version.id
            for item in confirmed_b.assistant_turn.choice_options
        )

        choice_a_again = next(
            item for item in confirmed_b.assistant_turn.choice_options if item.get("proposalId") == proposal_a
        )
        confirmed_a_again = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": confirmed_b.assistant_turn.id,
                        "choiceId": choice_a_again["id"],
                    }
                },
            ),
        )
        assert confirmed_a_again.version is not None
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 4
        final_version_row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (confirmed_a_again.version.id,),
        ).fetchone()
        final_snapshot = json.loads(final_version_row["snapshot_json"])
        assert final_snapshot["days"][0]["segments"][0]["startTime"] == "08:30"
        assert final_snapshot["days"] == edited_a_snapshot["days"]
        assert final_snapshot["routeDecisionContract"]["status"] == "ready"

        duplicate = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": confirmed_b.assistant_turn.id,
                        "choiceId": choice_a_again["id"],
                    }
                },
            ),
        )
        assert duplicate.version is not None
        assert duplicate.version.id == confirmed_a_again.version.id
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 4


@pytest.mark.parametrize("raise_after_original", [False, True])
def test_mark_committed_fault_window_recovers_one_canonical_write_and_replays(
    monkeypatch,
    raise_after_original: bool,
) -> None:
    from src.services.plan_portfolio_store import PlanPortfolioStore
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "mark committed recovery")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京高校一日游，公共交通，适度绕行",
            "active",
        )
        source_turn_id, proposal_id = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="高校经典线",
            poi_id="B000A",
        )
        choice = next(
            item
            for item in service._turn_response(source_turn_id).choice_options
            if item.get("proposalId") == proposal_id
        )
        original = PlanPortfolioStore.mark_committed

        def fail_mark_committed(self, **kwargs):
            if raise_after_original:
                original(self, **kwargs)
            raise RuntimeError("fault after writer before proposal bookkeeping")

        monkeypatch.setattr(PlanPortfolioStore, "mark_committed", fail_mark_committed)
        request = AgentMessageRequest(
            content="确认编辑",
            context={
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": source_turn_id,
                    "choiceId": choice["id"],
                }
            },
        )

        confirmed = service.send_message(session.session_id, request)
        replayed = service.send_message(session.session_id, request)

        assert confirmed.version is not None
        assert replayed.version is not None
        assert replayed.version.id == confirmed.version.id
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 1
        root = connection.execute(
            "SELECT status, selected_proposal_id, expected_base_version_id FROM agent_plan_portfolios"
        ).fetchone()
        assert root["status"] == "awaiting_selection"
        assert root["selected_proposal_id"] == proposal_id
        assert root["expected_base_version_id"] == confirmed.version.id
        execution = connection.execute("SELECT status, result_version_id FROM agent_choice_executions").fetchone()
        assert execution["status"] == "succeeded"
        assert execution["result_version_id"] == confirmed.version.id


def test_zero_target_calendar_days_without_explicit_rest_provenance_cannot_be_confirmed() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "国庆七日高校游")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "今年国庆参观北京高校，10月1日到7日，预算和每天安排由你规划。",
            "active",
        )
        snapshot = _snapshot(session.active_plan_id, "海淀学府漫游", "B000A")
        for day_number in range(2, 8):
            snapshot["days"].append(
                {
                    "id": f"day_zero_target_{day_number}",
                    "dayNumber": day_number,
                    "date": f"2026-10-{day_number:02d}",
                    "title": f"Day {day_number} 待继续编辑",
                    "totalEstimatedCost": 0,
                    "segments": [],
                }
            )
        snapshot["desiredDensityAnchorTargets"] = {
            str(day_number): (1 if day_number == 1 else 0) for day_number in range(1, 8)
        }
        request_context = _request_context(root_turn_id=root_turn_id)
        request_context["sourceUserRequest"] = "今年国庆参观北京高校，10月1日到7日，预算和每天安排由你规划。"
        request_context["latestUserMessage"] = request_context["sourceUserRequest"]
        request_context["effectiveUserMessage"] = request_context["sourceUserRequest"]
        request_context["resolvedTripDates"] = {
            "status": "resolved",
            "dates": [f"2026-10-{day_number:02d}" for day_number in range(1, 8)],
            "dayCount": 7,
        }
        request_context["requestIntentContract"]["dayCount"] = 7
        source_turn_id, proposal_id = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="海淀学府漫游",
            poi_id="B000A",
            snapshot_override=snapshot,
            request_context_override=request_context,
        )
        turn = service._turn_response(source_turn_id)
        projection = next(
            item
            for item in turn.comparison_projections
            if item.get("proposalId") == proposal_id
        )
        assert projection["confirmationPassed"] is False
        assert projection["adoptionReady"] is False
        assert projection["uncoveredDayNumbers"] == [2, 3, 4, 5, 6, 7]
        assert not any(item.get("proposalId") == proposal_id for item in turn.choice_options)
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_choice_executions").fetchone()[0] == 0


def test_confirmed_simple_direction_repairs_malformed_time_patch_and_writes_one_new_version() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    class MalformedThenCanonicalTimePatchProvider:
        model = "malformed-then-canonical-time-patch"

        def __init__(self) -> None:
            self.full_calls: list[dict] = []
            self.repair_feedbacks: list[str] = []

        def decide_autonomy_lite(self, *_args, **_kwargs):
            raise AssertionError("server-validated overview edit must bypass lite intent classification")

        def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
            assert timeout_seconds > 0
            self.full_calls.append(copy.deepcopy(context))
            self.repair_feedbacks.append(str(repair_feedback or ""))
            observation = context.get("observation") if isinstance(context.get("observation"), dict) else {}
            lineage = observation.get("versionLineage") or {}
            segment_ids = [
                str(item.get("segmentId"))
                for item in observation.get("segmentRefs") or []
                if isinstance(item, dict) and item.get("segmentId")
            ]
            target_segment_id = segment_ids[0]
            if not repair_feedback:
                # Exact live failure shape: business intent is correct, but the
                # actionDirective fields are not canonical PatchDirective fields.
                return {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "patch_itinerary",
                    "actionDirective": {
                        "patchType": "update_segment_start_time",
                        "targetSegmentId": target_segment_id,
                        "newStartTime": "08:00",
                        "preserveOtherSegments": True,
                    },
                }
            return {
                "schemaVersion": "agent-decision-v3",
                "primaryAction": "patch_itinerary",
                "actionDirective": {
                    "type": "patch_itinerary",
                    "operationIntent": "replace_segment_start_time",
                    "baseVersionId": str(lineage.get("currentVersionId") or ""),
                    "targetSegmentIds": [target_segment_id],
                    "requestedOutcome": "把第一天上午第一站开始时间改为08:00，其他安排不变",
                    "startTime": "08:00",
                    "preserve": ["other_segments", "other_days"],
                    "maxChangedSegmentCount": 1,
                },
            }

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "malformed time patch repair")
        provider = MalformedThenCanonicalTimePatchProvider()
        service = AgentService(connection, provider=provider)
        service.initial_planning_mode = "simple_open_v1"
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京高校两日游，公共交通，适度绕行",
            "active",
        )
        live_snapshot = _live_shaped_simple_partial_snapshot(session.active_plan_id)
        for day in live_snapshot["days"]:
            meal_segment = next(segment for segment in day["segments"] if segment["kind"] == "meal")
            day_number = int(day["dayNumber"])
            family = "炸酱面" if day_number == 1 else "铜锅涮肉"
            fingerprint = ("a" if day_number == 1 else "b") * 64
            constraints = meal_segment["semanticMetadata"].setdefault("scheduleConstraints", {})
            constraints["mealExperienceBrief"] = {
                "briefId": f"meal-brief-{day_number}",
                "planningSlotId": meal_segment["semanticMetadata"]["planningSlotId"],
                "dayNumber": day_number,
                "themeId": family,
                "themeLabel": family,
                "searchTerms": [family],
                "sourceFingerprint": fingerprint,
            }
            constraints["mealSemanticEvidence"] = {
                "amapPoiId": meal_segment["poi"]["amapId"],
                "canonicalBrand": f"meal-brand-{day_number}",
                "themeId": family,
                "themeLabel": family,
                "groundedFamilyKey": family,
                "matchedTerms": [family],
                "matchedFields": ["tags"],
                "localFoodEvidenceKind": "amap_destination_cuisine_subtype",
                "themeGrounded": True,
                "localFoodPassed": True,
                "sourceFingerprint": fingerprint,
            }
        compact_route_contract = _route_contract(compact=True)
        live_snapshot["routeDecisionContract"] = copy.deepcopy(compact_route_contract)
        live_snapshot["portfolioPendingSlots"] = [
            {
                "id": "pending:slot_required_night_a",
                "slotId": "slot_required_night_a",
                "planningSlotId": "slot_required_night_a",
                "poolId": "pool_night_view_required",
                "dayNumber": 1,
                "startTime": "21:00",
                "endTime": "22:30",
                "timeWindow": "21:00-22:30",
                "durationMinutes": 90,
                "intentType": "night_view",
                "kind": "night_view",
                "displayNeed": "夜景地点",
                "rawNeed": "夜景地点",
                "label": "待补：夜景地点",
                "state": "pending",
                "requirementLevel": "optional",
                "required": False,
                "goalId": None,
                "sourceGoalId": None,
                "occurrenceId": "occ:optional_night_a:day:1",
                "lineageAuthority": "goal_occurrence_compiler",
                "groundingStatus": "unresolved",
                "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
                "sourceReasonCode": "simple_open_slot_unresolved",
                "reason": "高德候选已穷尽，且返回实体均未通过夜景用途校验",
                "timingStatus": "awaiting_route_confirmation",
                "timingBasis": "simple_direction_provider_exhausted_slot",
                "futureRouteAnchor": False,
                "routeAnchorExpected": False,
                "simpleDirectionProviderExhausted": True,
                "simpleDirectionRequirementLineageConflict": False,
                "requirementEvidenceSource": "provider_optional_slot",
            }
        ]
        expected_pairs: list[dict[str, str]] = []
        verified_pairs: list[dict] = []
        for day in live_snapshot["days"]:
            amap_ids = [str(segment["poi"]["amapId"]) for segment in day["segments"]]
            for from_amap_id, to_amap_id in zip(amap_ids, amap_ids[1:]):
                expected_pair = {"fromAmapId": from_amap_id, "toAmapId": to_amap_id}
                verified_pair = {
                    **expected_pair,
                    "transportMode": "transit",
                    "durationSeconds": 900,
                    "distanceMeters": 2000,
                    "provider": "amap-webservice",
                    "queriedAt": "2026-08-23T00:00:00+00:00",
                }
                verified_pair["providerEvidenceFingerprint"] = (
                    SimpleOpenDirectionService._provider_evidence_fingerprint(verified_pair)
                )
                expected_pairs.append(expected_pair)
                verified_pairs.append(verified_pair)
        live_snapshot["simpleOpenRouteAssignment"] = {
            "schemaVersion": "simple-open-route-evidence-v2",
            "routeContractFingerprint": live_snapshot["routeDecisionContract"]["fingerprint"],
            "expectedPairs": expected_pairs,
            "verifiedPairs": verified_pairs,
            "routeCoverageComplete": True,
            "adjacentLegCompliance": "verified",
            "topologyCompliance": "verified",
            "providerBaselineCompared": False,
            "detourCompliance": "not_evaluated",
        }
        source_turn_id, proposal_id = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="真实 Provider 两日方向",
            poi_id="B000A",
            snapshot_override=live_snapshot,
            request_context_override={
                **_request_context(root_turn_id=root_turn_id),
                "sourceUserRequest": "北京高校两日游，公共交通，适度绕行",
                "latestUserMessage": "北京高校两日游，公共交通，适度绕行",
                "effectiveUserMessage": "北京高校两日游，公共交通，适度绕行",
                "resolvedTripDates": {
                    "status": "resolved",
                    "dates": ["2026-10-01", "2026-10-02"],
                    "dayCount": 2,
                },
                "requestIntentContract": {
                    **_request_context(root_turn_id=root_turn_id)["requestIntentContract"],
                    "dayCount": 2,
                    "routeDecisionContract": copy.deepcopy(compact_route_contract),
                    "requiredIntents": [
                        {
                            "goalId": "goal_campus",
                            "intentType": "campus_visit",
                            "requiredMin": 2,
                            "requirementLevel": "required",
                        },
                    ],
                },
            },
        )
        source_turn = service._turn_response(source_turn_id)
        assert source_turn.choice_options, json.dumps(
            source_turn.comparison_projections,
            ensure_ascii=False,
            indent=2,
        )
        choice = source_turn.choice_options[0]
        confirmed = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_turn_id,
                        "choiceId": choice["id"],
                    }
                },
            ),
        )
        assert confirmed.version is not None
        projection = next(
            item for item in confirmed.assistant_turn.comparison_projections if item.get("proposalId") == proposal_id
        )
        before_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                (confirmed.version.id,),
            ).fetchone()["snapshot_json"]
        )
        before_segments = [copy.deepcopy(segment) for day in before_snapshot["days"] for segment in day["segments"]]
        counts_before = tuple(
            connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM agent_plan_portfolios), "
                "(SELECT COUNT(*) FROM agent_plan_proposals), "
                "(SELECT COUNT(*) FROM itinerary_versions), "
                "(SELECT COUNT(*) FROM itinerary_patches)"
            ).fetchone()
        )
        assistant_turn_count_before = connection.execute(
            "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND role = 'assistant' ",
            (session.session_id,),
        ).fetchone()[0]
        editing = {
            "planningSelectionRootTurnId": projection["planningSelectionRootTurnId"],
            "rootPortfolioId": projection["rootPortfolioId"],
            "proposalId": proposal_id,
            "sourceAssistantTurnId": projection["sourceAssistantTurnId"],
            "activeVersionId": confirmed.version.id,
        }

        edited = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="把第一天上午的第一站开始时间改为08:00，其他安排不变。",
                context={
                    "viewContext": {
                        "schemaVersion": "agent-view-context-v1",
                        "activeView": "overview",
                        "editingProposal": editing,
                    }
                },
            ),
        )

        counts_after = tuple(
            connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM agent_plan_portfolios), "
                "(SELECT COUNT(*) FROM agent_plan_proposals), "
                "(SELECT COUNT(*) FROM itinerary_versions), "
                "(SELECT COUNT(*) FROM itinerary_patches)"
            ).fetchone()
        )
        edited_turn_row = connection.execute(
            "SELECT agent_request_json, agent_response_json FROM conversation_turns WHERE id = ?",
            (edited.assistant_turn.id,),
        ).fetchone()
        edited_turn_payload = {
            **json.loads(edited_turn_row["agent_request_json"] or "{}"),
            **json.loads(edited_turn_row["agent_response_json"] or "{}"),
        }
        decision_state = edited_turn_payload.get("agentDecisionState") or {}
        assert edited.version is not None, {
            "reply": edited.assistant_turn.content,
            "warnings": edited.warnings,
            "terminalStatus": edited.terminal_status,
            "outcomeStatuses": edited.outcome_statuses,
            "controllerError": decision_state.get("controllerError"),
            "reasonCodes": decision_state.get("reasonCodes"),
            "providerRawDecisions": decision_state.get("providerRawDecisions"),
            "normalizedDecision": decision_state.get("normalizedDecision"),
        }
        assert edited.version.id != confirmed.version.id
        assert counts_after == (
            counts_before[0],
            counts_before[1],
            counts_before[2] + 1,
            counts_before[3] + 1,
        )
        assert any(provider.repair_feedbacks)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE session_id = ? AND role = 'assistant' ",
                (session.session_id,),
            ).fetchone()[0]
            == assistant_turn_count_before + 1
        )
        response_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (edited.assistant_turn.id,),
            ).fetchone()["agent_response_json"]
        )
        assert response_payload["agentControlLoop"]["stopReason"] == "verified_timeline_mutation"
        assert len(response_payload["agentControlLoop"]["cycles"]) == 1
        assert response_payload["agentControlLoop"]["cycles"][0]["outcome"]["status"] == "success"
        assert edited.terminal_status == "success"
        assert "并保存为新版本" in edited.assistant_turn.content
        assert edited.itinerary.days[0].segments[0].id == before_segments[0]["id"]
        assert edited.itinerary.days[0].segments[0].start_time == "08:00"
        assert edited.itinerary.days[0].segments[0].end_time == "09:30"
        edited_segments = [segment for day in edited.itinerary.days for segment in day.segments]
        assert [segment.id for segment in edited_segments[1:]] == [segment["id"] for segment in before_segments[1:]]
        assert [segment.start_time for segment in edited_segments[1:]] == [
            segment["startTime"] for segment in before_segments[1:]
        ]
        assert [segment.poi.amap_id for segment in edited_segments[1:]] == [
            (segment.get("poi") or {}).get("amapId") for segment in before_segments[1:]
        ]
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()["active_version_id"]
        assert active_version_id == edited.version.id

        # A confirmed proposal may carry a truthfully exhausted optional slot.
        # A normal timeline edit must preserve that exact lineage, and switching
        # back to comparison must not demote the editable draft. Required pending
        # slots are covered separately and can never reach this confirmation path.
        original_pending = before_snapshot["portfolioPendingSlots"]
        assert len(original_pending) == 1
        assert original_pending[0]["planningSlotId"] == "slot_required_night_a"
        lineage_keys = (
            "planningSlotId",
            "poolId",
            "dayNumber",
            "intentType",
            "requirementLevel",
            "goalId",
            "sourceGoalId",
            "occurrenceId",
            "groundingStatus",
            "reasonCode",
            "sourceReasonCode",
            "simpleDirectionProviderExhausted",
            "simpleDirectionRequirementLineageConflict",
        )
        original_lineage = {key: original_pending[0].get(key) for key in lineage_keys}
        edited_version_row = connection.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
            (edited.version.id,),
        ).fetchone()
        edited_snapshot = json.loads(edited_version_row["snapshot_json"])
        assert edited_snapshot["portfolioPendingSlots"] == original_pending
        write_counts_before_save = tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM itinerary_versions), (SELECT COUNT(*) FROM itinerary_patches)"
            ).fetchone()
        )

        saved = direction_service.save_active_direction(
            session_id=session.session_id,
            proposal_id=proposal_id,
            planning_root_id=projection["planningSelectionRootTurnId"],
            portfolio_id=projection["rootPortfolioId"],
            base_version_id=edited.version.id,
        )

        assert (
            tuple(
                connection.execute(
                    "SELECT (SELECT COUNT(*) FROM itinerary_versions), (SELECT COUNT(*) FROM itinerary_patches)"
                ).fetchone()
            )
            == write_counts_before_save
        )
        saved_projection = saved["comparisonProjection"]
        assert saved_projection["pendingHardSlotCount"] == 0
        assert saved_projection["blockingPendingHardSlotCount"] == 0
        assert saved_projection["providerExhaustedRequiredSlotCount"] == 0
        assert saved_projection["adoptionReady"] is True, json.dumps(
            saved_projection,
            ensure_ascii=False,
            indent=2,
        )
        assert saved_projection["proposalLifecycleStatus"] == "committed"
        assert len(saved_projection["pendingSlots"]) == 1
        assert {key: saved_projection["pendingSlots"][0].get(key) for key in lineage_keys} == original_lineage

        proposal_row = connection.execute(
            "SELECT snapshot_json, verifier_json FROM agent_plan_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        stored_snapshot = json.loads(proposal_row["snapshot_json"])
        stored_verifier = json.loads(proposal_row["verifier_json"])
        assert stored_snapshot["portfolioPendingSlots"] == original_pending
        assert stored_verifier == direction_service._proposal_verifier(stored_snapshot)
        assert stored_verifier["pendingHardSlotCount"] == 0
        assert stored_verifier["blockingPendingHardSlotCount"] == 0
        assert stored_verifier["providerExhaustedRequiredSlotCount"] == 0

        carrier_turn_id = str(saved_projection["sourceAssistantTurnId"])
        carrier_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (carrier_turn_id,),
            ).fetchone()["agent_response_json"]
        )
        carrier_options = carrier_payload["choiceOptions"]
        assert {str(option.get("action") or "") for option in carrier_options} == {
            "select_plan_proposal",
            "continue_plan_expansion",
            "search_travel_guide_advice",
        }
        carrier_choice = next(option for option in carrier_options if option.get("action") == "select_plan_proposal")
        continuation_choice = next(
            option for option in carrier_options if option.get("action") == "continue_plan_expansion"
        )
        assert continuation_choice["scopeKind"] == "comparison"
        assert continuation_choice["planningSelectionRootTurnId"] == projection["planningSelectionRootTurnId"]
        assert continuation_choice["rootPortfolioId"] == projection["rootPortfolioId"]
        carrier_projection = carrier_choice["comparisonProjection"]
        assert carrier_choice["expectedBaseVersionId"] == edited.version.id
        assert carrier_projection == saved_projection
        assert carrier_projection["pendingHardSlotCount"] == 0
        assert carrier_projection["providerExhaustedRequiredSlotCount"] == 0
        assert carrier_projection["adoptionReady"] is True
        assert {key: carrier_projection["pendingSlots"][0].get(key) for key in lineage_keys} == original_lineage


def test_server_validated_view_context_only_binds_explicit_edit_or_repair() -> None:
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "view routing")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        direction_service = SimpleOpenDirectionService(connection)
        root_turn_id = service._insert_turn(
            session.session_id,
            "user",
            "北京高校一日游，公共交通，适度绕行",
            "active",
        )
        source_turn_id, proposal_id = _persist_direction_turn(
            connection=connection,
            service=service,
            direction_service=direction_service,
            session_id=session.session_id,
            root_turn_id=root_turn_id,
            source_user_turn_id=root_turn_id,
            title="高校经典线",
            poi_id="B000A",
        )
        choice = service._turn_response(source_turn_id).choice_options[0]
        confirmed = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": source_turn_id,
                        "choiceId": choice["id"],
                    }
                },
            ),
        )
        assert confirmed.version is not None
        projection = next(
            item for item in confirmed.assistant_turn.comparison_projections if item.get("proposalId") == proposal_id
        )
        editing = {
            "planningSelectionRootTurnId": projection["planningSelectionRootTurnId"],
            "rootPortfolioId": projection["rootPortfolioId"],
            "proposalId": proposal_id,
            "sourceAssistantTurnId": projection["sourceAssistantTurnId"],
            "activeVersionId": confirmed.version.id,
        }
        focused = {
            "planningSelectionRootTurnId": projection["planningSelectionRootTurnId"],
            "rootPortfolioId": projection["rootPortfolioId"],
            "proposalId": proposal_id,
            "sourceAssistantTurnId": projection["sourceAssistantTurnId"],
            "materialFingerprint": projection["materialFingerprint"],
            "repairChoiceId": projection["repairChoiceId"],
        }

        repair_resolution = direction_service.resolve_view_context(
            session_id=session.session_id,
            active_version_id=confirmed.version.id,
            view_context={
                "activeView": "comparison",
                "focusedProposal": focused,
                "requestedProposalOrdinal": 1,
            },
            explicit_direction_intent="repair_comparison_direction",
            explicit_action="repair_comparison_direction",
        )
        assert repair_resolution["resolvedAction"] == "repair_comparison_direction"
        with pytest.raises(ValueError, match="simple_direction_view_identity_mismatch"):
            direction_service.resolve_view_context(
                session_id=session.session_id,
                active_version_id=confirmed.version.id,
                view_context={
                    "activeView": "comparison",
                    "focusedProposal": {**focused, "sourceAssistantTurnId": "assistant_stale"},
                    "requestedProposalOrdinal": 1,
                },
                explicit_direction_intent="repair_comparison_direction",
                explicit_action="repair_comparison_direction",
            )

        def payload(active_view: str, message: str) -> AgentMessageRequest:
            return AgentMessageRequest(
                content=message,
                context={
                    "viewContext": {
                        "schemaVersion": "agent-view-context-v1",
                        "activeView": active_view,
                        "editingProposal": editing,
                    }
                },
            )

        current_session = service._session(session.session_id)
        comparison_payload, comparison_route, comparison_capability = service._route_conversation_turn(
            session=current_session,
            content="再轻松一点",
            payload=payload("comparison", "再轻松一点"),
        )
        comparison_context = comparison_payload.context.model_dump(by_alias=True)
        assert "viewResolution" not in comparison_context
        assert comparison_capability.reason_code != "server_validated_view_context"

        overview_payload, overview_route, overview_capability = service._route_conversation_turn(
            session=current_session,
            content="再轻松一点",
            payload=payload("overview", "再轻松一点"),
        )
        overview_context = overview_payload.context.model_dump(by_alias=True)
        assert "viewResolution" not in overview_context
        assert overview_capability.reason_code != "server_validated_view_context"

        explicit_new_payload, explicit_new_route, explicit_new_capability = service._route_conversation_turn(
            session=current_session,
            content="继续生成一个其他方案",
            payload=payload("overview", "继续生成一个其他方案"),
        )
        explicit_new_context = explicit_new_payload.context.model_dump(by_alias=True)
        assert "viewResolution" not in explicit_new_context
        assert explicit_new_route.classification is not None
        assert explicit_new_route.classification.intent == "continue_plan_expansion"
        assert explicit_new_capability.status == "none"
        assert explicit_new_context.get("selectedAgentChoice") is None

        explicit_edit_payload, explicit_edit_route, _ = service._route_conversation_turn(
            session=current_session,
            content="修改当前行程",
            payload=payload("comparison", "修改当前行程"),
        )
        explicit_edit_resolution = explicit_edit_payload.context.model_dump(by_alias=True)["viewResolution"]
        assert explicit_edit_resolution["resolvedAction"] == "edit_active_direction"
        assert explicit_edit_resolution["explicitDirectionIntent"] == "modify_itinerary"
        assert explicit_edit_route.classification is not None
        assert explicit_edit_route.classification.intent == "modify_itinerary"

        counts_before = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM agent_plan_proposals), "
            "(SELECT COUNT(*) FROM itinerary_versions), "
            "(SELECT COUNT(*) FROM itinerary_patches)"
        ).fetchone()
        stale_editing = {**editing, "activeVersionId": "version_stale"}
        stale_payload = AgentMessageRequest(
            content="修改当前行程",
            context={
                "viewContext": {
                    "schemaVersion": "agent-view-context-v1",
                    "activeView": "overview",
                    "editingProposal": stale_editing,
                }
            },
        )
        with pytest.raises(HTTPException) as raised:
            service._route_conversation_turn(
                session=current_session,
                content="修改当前行程",
                payload=stale_payload,
            )
        assert raised.value.status_code == 409
        assert raised.value.detail["code"] == "simple_direction_view_base_stale"
        counts_after = connection.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM agent_plan_proposals), "
            "(SELECT COUNT(*) FROM itinerary_versions), "
            "(SELECT COUNT(*) FROM itinerary_patches)"
        ).fetchone()
        assert tuple(counts_after) == tuple(counts_before)


def test_empty_overview_without_active_direction_routes_first_request_to_new_direction() -> None:
    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "empty overview")
        service = AgentService(connection)
        service.initial_planning_mode = "simple_open_v1"
        current_session = service._session(session.session_id)
        payload = AgentMessageRequest(
            content="今年国庆参观北京高校两日游，晚上看北京夜景。",
            context={
                "viewContext": {
                    "schemaVersion": "agent-view-context-v1",
                    "activeView": "overview",
                    "editingProposal": None,
                }
            },
        )

        routed_payload, route, capability = service._route_conversation_turn(
            session=current_session,
            content=payload.content,
            payload=payload,
        )

        routed_context = routed_payload.context.model_dump(by_alias=True)
        assert "viewResolution" not in routed_context
        assert route.classification is not None
        assert route.classification.intent == "create_itinerary"
        assert route.model_called is False
        assert capability.status == "not_required"
        assert capability.capability == "create_itinerary"
