from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

import pytest

from src.core.config import Settings
from src.services.agent_executor_registry import (
    AgentActionExecutorRegistry,
    AgentActionOutcome,
    DraftItineraryExecutor,
    SimpleOpenDraftItineraryExecutor,
)
from src.services.agent_observation_service import AgentObservationBuilder
from src.services.agent_turn_coordinator import AgentTurnCoordinator
from backend.tests.unit.test_agent_service import IntentContractProviderMixin


GOLDEN_INPUT = (
    "今年国庆参观北京高校两日游，晚上看北京夜景。"
    "10月1日到2日，中等预算，1人，公交地铁优先。"
    "每天午餐想体验当地特色美食。"
)


def _outcome(request):
    return AgentActionOutcome(
        action="draft_itinerary",
        execution_route=request.decision_state["actualExecutionRoute"],
        status="partial",
        result_version_id="ver_simple",
        observed_active_version_id="ver_simple",
        patch_ids=["patch_simple"],
        candidate_summary={"terminalStatus": "simple_open_terminal"},
        verifier={"passed": True, "pendingSlotTruthValid": True},
        control_loop_disposition="terminal_success",
        safe_for_future_user_continuation=True,
        safe_to_continue=True,
    )


def _registry() -> AgentActionExecutorRegistry:
    return AgentActionExecutorRegistry(
        [
            DraftItineraryExecutor(_outcome),
            SimpleOpenDraftItineraryExecutor(_outcome),
        ]
    )


class GoldenFollowupProvider(IntentContractProviderMixin):
    def __init__(self, base_version_id: str, segment_id: str):
        self.base_version_id = base_version_id
        self.segment_id = segment_id

    def decide_autonomy(self, context, *, timeout_seconds, repair_feedback=""):
        del timeout_seconds, repair_feedback
        from src.services.agent_autonomy_service import ModelDecisionV3

        last_outcome = (context.get("observation") or {}).get("lastOutcome") or {}
        if last_outcome.get("action") == "patch_itinerary" and last_outcome.get("status") == "success":
            decision = ModelDecisionV3.model_validate(
                {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "finish",
                    "actionDirective": {"type": "finish", "assistantReply": "时间修改已保存。"},
                }
            )
        else:
            decision = ModelDecisionV3.model_validate(
                {
                    "schemaVersion": "agent-decision-v3",
                    "primaryAction": "patch_itinerary",
                    "actionDirective": {
                        "type": "patch_itinerary",
                        "operationIntent": "replace_segment_start_time",
                        "baseVersionId": self.base_version_id,
                        "targetSegmentIds": [self.segment_id],
                        "requestedOutcome": "把第一天上午行程提前到 08:00",
                        "startTime": "08:00",
                    },
                }
            )
        return decision.model_dump(mode="json", by_alias=True)


def _send_after_route_clarification(service, session_id: str, content: str):
    """Resolve the bounded server-owned clarification sequence before planning."""

    from src.api.schemas.agent import AgentMessageRequest

    response = service.send_message(session_id, AgentMessageRequest(content=content))
    for _ in range(5):
        checkpoint = response.assistant_turn.clarification_checkpoint or {}
        batch_choice = next(
            (
                item
                for item in response.assistant_turn.choice_options
                if item.get("action") == "submit_clarification_batch"
            ),
            None,
        )
        if batch_choice is not None:
            questions = list(checkpoint.get("questions") or [])
            selections = []
            for question in questions:
                options = list(question.get("options") or [])
                if not options:
                    pytest.fail("batch clarification question has no selectable option")
                selections.append(
                    {
                        "dimensionId": question["dimensionId"],
                        "optionId": options[0]["id"],
                    }
                )
            response = service.send_message(
                session_id,
                AgentMessageRequest(
                    content="确认当前批次并继续规划",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": response.assistant_turn.id,
                            "choiceId": batch_choice["id"],
                            "batchSelections": selections,
                        }
                    },
                ),
            )
            continue
        clarification_choice = next(
            (item for item in response.assistant_turn.choice_options if item.get("action") == "continue_clarification"),
            None,
        )
        if clarification_choice is None:
            return response
        response = service.send_message(
            session_id,
            AgentMessageRequest(
                content=str(clarification_choice.get("label") or "按推荐偏好"),
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": response.assistant_turn.id,
                        "choiceId": clarification_choice["id"],
                    }
                },
            ),
        )
    pytest.fail("server clarification exceeded the bounded question count")


def test_server_execution_profile_selects_simple_open_route_and_ignores_model_route() -> None:
    registry = _registry()

    route = registry.route(
        {
            "accepted": True,
            "primaryAction": "draft_itinerary",
            "serverExecutionProfile": "simple_open_v1",
            "proposedExecutionRoute": "staged_initial_pipeline",
        }
    )

    assert route == "simple_open_initial_pipeline"


def test_strict_profile_keeps_staged_route_even_when_model_requests_simple_open() -> None:
    registry = _registry()

    route = registry.route(
        {
            "accepted": True,
            "primaryAction": "draft_itinerary",
            "serverExecutionProfile": "strict_portfolio",
            "proposedExecutionRoute": "simple_open_initial_pipeline",
        }
    )

    assert route == "staged_initial_pipeline"


def test_simple_open_candidate_selection_uses_shared_semantic_policy_before_confirming_campus() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def search(self, city, *, keyword, category, limit):
            del limit
            common = {
                "city": "北京市",
                "district": "海淀区",
                "category": category,
                "source": "amap-place-search",
                "sourceNote": "real-provider-shape",
                "confidence": 0.86,
            }
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000A8W5B1",
                        name="北京英国学校(顺义校区)",
                        type="科教文化服务;学校;学校",
                        address="天竺开发区安华街9号南院",
                        longitude=116.519917,
                        latitude=40.082753,
                        **common,
                    ),
                    MapPoiResponse(
                        id="B0FFF0EFZY",
                        name="清华大学工字厅",
                        type="科教文化服务;学校;高等院校",
                        address="双清路30号清华大学",
                        longitude=116.323079,
                        latitude=40.002365,
                        **common,
                    ),
                ],
            )

    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "09:00-11:00",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                }
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "preferredTypes": ["大学", "高等院校"],
                    "rejectedTypes": [],
                    "routePreference": {},
                    "assignToSlots": ["day1_campus"],
                }
            ],
        }
    )

    plans, events = SimpleOpenItineraryExecutor(Provider()).build_segment_plans(
        initial_plan,
        city="北京",
        transport_mode="public_transit",
    )

    assert plans[0].selected_poi is not None
    assert plans[0].selected_poi.name == "清华大学工字厅"
    assert plans[0].selected_poi.amap_id == "B0FFF0EFZY"
    assert plans[0].grounding_status == "verified_amap"
    search_event = next(event for event in events if event["type"] == "simple_open_tool_call")
    assert search_event["status"] == "completed"
    assert search_event["metadata"]["providerOutcome"] == "success"
    assert search_event["metadata"]["cacheHit"] is False
    assert search_event["metadata"]["resultCount"] == 2
    assert search_event["metadata"]["selectedAmapId"] == "B0FFF0EFZY"
    assert len(search_event["metadata"]["queryFingerprint"]) == 16
    assert "errorType" not in search_event["metadata"]
    slot_event = next(event for event in events if event["type"] == "simple_open_slot_grounded")
    assert slot_event["metadata"]["selectedAmapId"] == "B0FFF0EFZY"


def test_simple_open_search_failure_is_not_recorded_as_completed_provider_call() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class FailingProvider:
        def search(self, city, *, keyword, category, limit):
            del city, keyword, category, limit
            raise TimeoutError("provider transport timeout")

    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "09:00-11:00",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                }
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "candidateHints": ["北京高校"],
                    "preferredTypes": ["大学", "高等院校"],
                    "rejectedTypes": [],
                    "routePreference": {},
                    "assignToSlots": ["day1_campus"],
                }
            ],
        }
    )

    plans, events = SimpleOpenItineraryExecutor(FailingProvider()).build_segment_plans(
        initial_plan,
        city="北京",
        transport_mode="public_transit",
    )

    assert plans[0].selected_poi is None
    search_event = next(event for event in events if event["type"] == "simple_open_tool_call")
    assert search_event["status"] == "failed"
    assert search_event["metadata"]["providerOutcome"] == "failure"
    assert search_event["metadata"]["errorType"] == "TimeoutError"
    assert search_event["metadata"]["resultCount"] == 0
    assert search_event["metadata"]["selectedAmapId"] is None
    assert search_event["metadata"]["cacheHit"] is None


def test_explicit_low_detour_rejects_semantically_inverted_controller_sequence() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class RecordingProvider:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        @staticmethod
        def _response(city, keyword, category, poi_id, name, poi_type, longitude, latitude):
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=poi_id,
                        name=name,
                        type=poi_type,
                        address="测试地址",
                        longitude=longitude,
                        latitude=latitude,
                        city=city,
                        district="海淀区",
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        providerTypeCode=(
                            "050100"
                            if "北京菜" in poi_type
                            else "110101"
                            if "公园" in poi_type
                            else None
                        ),
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="f" * 64,
                    )
                ],
            )

        def search(self, city, *, keyword, category, limit):
            del limit
            self.calls.append(("text", keyword))
            return self._response(
                city,
                keyword,
                category,
                "B000CAMPUS1",
                "测试大学",
                "科教文化服务;学校;高等院校",
                116.31,
                39.99,
            )

        def search_nearby(
            self,
            city,
            longitude,
            latitude,
            keyword,
            category="all",
            radius=1500,
            limit=12,
            **_kwargs,
        ):
            del limit
            self.calls.append(("nearby", keyword, longitude, latitude, radius))
            if keyword in {"当地特色美食", "餐厅"}:
                response = self._response(
                    city,
                    keyword,
                    category,
                    "B000MEAL001",
                    "测试京味餐厅",
                    "餐饮服务;中餐厅;北京菜",
                    116.33,
                    39.98,
                )
                return response.model_copy(
                    update={
                        "pois": [
                            *response.pois,
                            response.pois[0].model_copy(
                                update={
                                    "id": "B000MEAL002",
                                    "name": "备选顺路餐厅",
                                    "longitude": 116.335,
                                }
                            ),
                        ]
                    }
                )
            if round(float(longitude), 2) == 116.31:
                return self._response(
                    city,
                    keyword,
                    category,
                    "B000PARKFAR",
                    "校园旁绕路公园",
                    "风景名胜;公园广场;公园",
                    116.30,
                    39.99,
                )
            return self._response(
                city,
                keyword,
                category,
                "B000PARKNEAR",
                "餐后顺路公园",
                "风景名胜;公园广场;公园",
                116.34,
                39.98,
            )

    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "",
                    "durationMinutes": 0,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                },
                {
                    "slotId": "day1_park",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "",
                    "durationMinutes": 0,
                    "kind": "park",
                    "rawNeed": "晚间公园",
                    "routeAnchor": True,
                },
                {
                    "slotId": "day1_meal",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "",
                    "durationMinutes": 0,
                    "kind": "meal",
                    "rawNeed": "当地特色美食",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_campus"],
                },
                {
                    "poolId": "park_pool",
                    "rawNeed": "晚间公园",
                    "city": "北京",
                    "intentType": "park",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_park"],
                },
                {
                    "poolId": "meal_pool",
                    "rawNeed": "当地特色美食",
                    "city": "北京",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_meal"],
                },
            ],
        }
    )
    lineage = {
        "day1_campus": {
            "requirementLevel": "hard",
            "goalId": "goal_campus",
            "sourceGoalId": "goal_campus",
            "occurrenceId": "occ:campus:day:1",
            "poolId": "campus_pool",
            "lineageAuthority": "goal_occurrence_compiler",
            "schedulePreference": {
                "dayPart": "morning",
                "sequence": 1,
                "sequenceSource": "controller_schedule_hint",
                "userExplicit": True,
            },
        },
        "day1_park": {
            "requirementLevel": "hard",
            "goalId": "goal_park",
            "sourceGoalId": "goal_park",
            "occurrenceId": "occ:park:day:1",
            "poolId": "park_pool",
            "lineageAuthority": "goal_occurrence_compiler",
            "schedulePreference": {
                "dayPart": "evening",
                "sequence": 2,
                "sequenceSource": "controller_schedule_hint",
                "userExplicit": True,
            },
        },
        "day1_meal": {
            "requirementLevel": "hard",
            "goalId": "goal_meal",
            "sourceGoalId": "goal_meal",
            "occurrenceId": "occ:meal:day:1",
            "poolId": "meal_pool",
            "lineageAuthority": "goal_occurrence_compiler",
            "schedulePreference": {
                "dayPart": "noon",
                "sequence": 3,
                "sequenceSource": "controller_schedule_hint",
                "userExplicit": True,
            },
        },
    }
    route_contract = {
        "schemaVersion": "route-decision-contract-v2",
        "status": "ready",
        "missingFields": [],
        "fingerprint": "route-low-detour",
        "source": "request_intent_contract",
        "detourToleranceSource": "controller_semantic_choice",
        "detourTolerance": {"maxGeneralizedCostDelta": 10, "maxDetourRatio": 0.2},
        "adjacentLegConstraint": {
            "candidateSearchRadiusMeters": 8000,
            "maxProviderTravelMinutes": 45,
        },
        "topologyConstraint": {"maxBacktrackRatio": 0.15},
        "mobilityProfile": {
            "transportMode": "transit",
            "paceClass": "standard",
            "walkingPenaltyMinutesPerKm": 1.8,
            "transferPenaltyMinutes": 6.0,
            "waitTimeMultiplier": 1.0,
            "riskPenaltyMultiplier": 1.0,
        },
    }
    provider = RecordingProvider()
    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial_plan,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        route_decision_contract=route_contract,
        route_budget=0,
    )

    assert [item.intent_type for item in plans] == ["campus_visit", "meal", "park"]
    assert provider.calls[0] == ("text", "高校参观")
    assert provider.calls[1] == ("nearby", "餐厅", 116.31, 39.99, 5000)
    assert provider.calls[2] == ("nearby", "晚间公园", 116.33, 39.98, 5000)
    assert plans[-1].selected_poi is not None
    assert plans[-1].selected_poi.amap_id == "B000PARKNEAR"
    nearby_events = [
        event for event in events if event["type"] == "simple_open_tool_call" and event["metadata"].get("searchScope")
    ]
    assert [event["metadata"]["searchScope"] for event in nearby_events] == [
        "nearby_low_detour",
        "nearby_low_detour",
    ]
    assert all(event["metadata"]["radiusMeters"] == 5000 for event in nearby_events)
    assert [event["metadata"]["daySeedAmapId"] for event in nearby_events] == [
        "B000CAMPUS1",
        "B000CAMPUS1",
    ]
    assert [event["metadata"]["predecessorAmapId"] for event in nearby_events] == [
        "B000CAMPUS1",
        "B000MEAL001",
    ]
    assert all(event["metadata"]["searchCenterStrategy"] == "predecessor" for event in nearby_events)
    frontier_event = next(event for event in events if event["type"] == "simple_direction_frontier_outcomes")
    remaining_park_scopes = [
        item for item in frontier_event["metadata"]["remainingQueryScopes"] if item["slotId"] == "day1_park"
    ]
    assert [
        (item["centerRole"], item["predecessorAmapId"], item["predecessorBeamRank"]) for item in remaining_park_scopes
    ] == [("predecessor", "B000MEAL002", 2)]
    assert remaining_park_scopes[0]["currentPartialCompletionSlot"] is False
    route_audit = plans[0].schedule_constraints["routeAssignment"]
    assert route_audit["detourCompliance"] == "not_evaluated"
    assert route_audit["topologyCompliance"] == "verified"
    assert route_audit["failureReason"] == "provider_route_budget_insufficient"


def test_generic_meal_query_is_category_safe_but_exact_meal_identity_is_preserved() -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    assert (
        SimpleOpenItineraryExecutor._safe_query_for_intent(
            "当地特色美食",
            city="北京",
            intent_type="meal",
        )
        == "餐厅"
    )
    assert (
        SimpleOpenItineraryExecutor._safe_query_for_intent(
            "南京大牌档(中关村领展广场店)",
            city="北京",
            intent_type="meal",
            preserve_exact_entity=True,
        )
        == "南京大牌档(中关村领展广场店)"
    )


def test_local_food_contract_keeps_provider_type_out_of_the_search_keyword() -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    local_food_policy = {
        "localExperienceConstraint": {
            "experienceType": "local_cuisine",
            "evidencePolicy": "provider_city_specific_fact",
            "locality": {"city": "北京", "source": "request_destination"},
        }
    }
    for query in (
        "北京 当地特色餐厅",
        "北京 地方风味餐厅",
        "北京 传统市场周边餐饮",
        "北京 午餐 3个不同地点 餐厅",
    ):
        assert SimpleOpenItineraryExecutor._safe_query_for_intent(
            query,
            city="北京市",
            intent_type="meal",
            experience_policy=local_food_policy,
        ) == "餐厅"
    assert (
        SimpleOpenItineraryExecutor._safe_query_for_intent(
            "北京 北京菜 烤鸭",
            city="北京市",
            intent_type="meal",
            experience_policy=local_food_policy,
        )
        == "北京 北京菜 烤鸭"
    )
    assert (
        SimpleOpenItineraryExecutor._safe_query_for_intent(
            "当地特色美食",
            city="未验证市",
            intent_type="meal",
            experience_policy={
                "localExperienceConstraint": {
                    "experienceType": "local_cuisine",
                    "evidencePolicy": "provider_city_specific_fact",
                    "locality": {"city": "未验证", "source": "request_destination"},
                }
            },
        )
        == "餐厅"
    )


def test_local_food_contract_passes_city_cuisine_as_provider_type_filter() -> None:
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.models.poi import POI
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def __init__(self) -> None:
            self.provider_types = None

        def search_nearby(self, city, longitude, latitude, keyword, *, provider_types, **kwargs):
            del longitude, latitude, kwargs
            self.provider_types = provider_types
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category="food",
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000LOCAL01",
                        name="全聚德(昌平店)",
                        city="北京市",
                        district="昌平区",
                        category="food",
                        type="餐饮服务;中餐厅;北京菜",
                        providerTypeCode="050111",
                        tags=["烤鸭", "京味"],
                        address="鼓楼南街",
                        longitude=116.22,
                        latitude=40.21,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=0.9,
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="f" * 64,
                    )
                ],
            )

    provider = Provider()
    anchor = POI(
        id="poi_anchor",
        amap_id="B000CAMPUS1",
        name="北京大学(昌平校区)",
        city="北京",
        category="campus",
        latitude=40.247449,
        longitude=116.189912,
        source="amap-place-search",
        confidence=1.0,
    )
    result = SimpleOpenItineraryExecutor(provider)._search_candidate(
        city="北京",
        query="北京菜",
        category="food",
        intent_type="meal",
        raw_need="当地特色美食",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        experience_policy={
            "localExperienceConstraint": {
                "experienceType": "local_cuisine",
                "evidencePolicy": "provider_city_specific_fact",
                "locality": {"city": "北京", "source": "request_destination"},
            }
        },
        nearby_anchor=anchor,
        nearby_radius=5000,
    )

    assert provider.provider_types == "北京菜"
    assert result[1] is not None
    assert result[1].provider_type_code == "050111"


def test_explicit_every_day_occurrence_lineage_seals_completion_required(monkeypatch) -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.services.agent_service import AgentService

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day2_meal",
                    "dayNumber": 2,
                    "date": "2026-10-02",
                    "startTime": "12:00",
                    "durationMinutes": 75,
                    "kind": "meal",
                    "rawNeed": "当地特色美食",
                    "routeAnchor": False,
                }
            ],
            "intentPools": [
                {
                    "poolId": "meal_pool",
                    "rawNeed": "当地特色美食",
                    "city": "北京",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "optional",
                    "goalId": "goal_meal",
                    "assignToSlots": ["day2_meal"],
                }
            ],
        }
    )
    service = AgentService.__new__(AgentService)
    monkeypatch.setattr(
        service,
        "_simple_open_authoritative_occurrences",
        lambda _context: [
            {
                "occurrenceId": "occ:goal_meal:day:2",
                "sourceGoalId": "goal_meal",
                "intentType": "meal",
                "dayNumber": 2,
                "requirementLevel": "explicit_soft",
                "userExplicit": True,
                "lineageAuthority": "goal_occurrence_compiler",
            }
        ],
    )
    monkeypatch.setattr(service, "_simple_open_schedule_hints_by_goal_day", lambda _context: {})
    context = {
        "requestIntentContract": {
            "requiredIntents": [
                {
                    "goalId": "goal_meal",
                    "intentType": "meal",
                    "userExplicit": True,
                    "distributionPolicy": "every_allowed_day",
                    "cardinalitySource": "explicit_every_day",
                    "allowedDayNumbers": [1, 2],
                }
            ]
        }
    }

    lineage = service._simple_open_slot_occurrence_lineage(initial, context)["day2_meal"]

    assert lineage["completionRequired"] is True
    assert lineage["distributionPolicy"] == "every_allowed_day"
    assert lineage["cardinalitySource"] == "explicit_every_day"
    assert lineage["allowedDayNumbers"] == [1, 2]


def test_adjacent_recall_uses_midpoint_when_later_fixed_stop_is_already_grounded() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.models.poi import POI
    from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        @staticmethod
        def response(city, keyword, category, poi_id, name, poi_type, longitude, latitude, typecode):
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=poi_id,
                        name=name,
                        type=poi_type,
                        providerTypeCode=typecode,
                        city=city,
                        district="测试区",
                        address="测试路",
                        longitude=longitude,
                        latitude=latitude,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="a" * 64,
                    )
                ],
            )

        def search(self, city, *, keyword, category, limit, **kwargs):
            self.calls.append({"kind": "text", "keyword": keyword, "limit": limit, **kwargs})
            if "大学" in keyword:
                return self.response(
                    city,
                    keyword,
                    category,
                    "B000MIDCAMP",
                    "中点测试大学",
                    "科教文化服务;学校;高等院校",
                    116.30,
                    39.99,
                    "141200",
                )
            return self.response(
                city,
                keyword,
                category,
                "B000MIDMUSE",
                "固定博物馆",
                "科教文化服务;博物馆",
                116.36,
                39.99,
                "140100",
            )

        def search_nearby(self, city, longitude, latitude, keyword, **kwargs):
            self.calls.append(
                {
                    "kind": "nearby",
                    "keyword": keyword,
                    "longitude": longitude,
                    "latitude": latitude,
                    **kwargs,
                }
            )
            if "餐" in keyword:
                return self.response(
                    city,
                    keyword,
                    str(kwargs.get("category") or "food"),
                    "B000MIDMEAL",
                    "顺路餐厅",
                    "餐饮服务;中餐厅",
                    116.32,
                    39.99,
                    "050100",
                )
            return self.response(
                city,
                keyword,
                str(kwargs.get("category") or "park"),
                "B000MIDPARK",
                "中点公园",
                "风景名胜;公园广场;公园",
                116.34,
                39.99,
                "110101",
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": slot_id,
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": start_time,
                    "durationMinutes": 60,
                    "kind": kind,
                    "rawNeed": raw_need,
                    "routeAnchor": True,
                }
                for slot_id, start_time, kind, raw_need in (
                    ("campus", "09:00", "campus", "大学参观"),
                    ("meal", "12:00", "meal", "午餐餐馆"),
                    ("park", "14:00", "park", "午后公园"),
                    ("museum", "17:00", "museum", "固定博物馆"),
                )
            ],
            "intentPools": [
                {
                    "poolId": f"{slot_id}_pool",
                    "rawNeed": raw_need,
                    "city": "北京",
                    "intentType": intent_type,
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": [slot_id],
                    "candidateHints": [raw_need],
                    **(
                        {"entityBindingMode": "exact_entity", "exactEntity": raw_need}
                        if slot_id == "museum"
                        else {"entityBindingMode": "category"}
                    ),
                }
                for slot_id, intent_type, raw_need in (
                    ("campus", "campus_visit", "大学参观"),
                    ("meal", "meal", "午餐餐馆"),
                    ("park", "park", "午后公园"),
                    ("museum", "museum", "固定博物馆"),
                )
            ],
        }
    )
    lineage = {
        slot_id: {
            "occurrenceId": f"occ:{slot_id}",
            "requirementLevel": "hard",
            "schedulePreference": {
                "sequence": sequence,
                "sequenceSource": "controller_schedule_hint",
            },
        }
        for sequence, slot_id in enumerate(("campus", "meal", "park", "museum"), start=1)
    }
    fixed_museum = POI(
        id="poi_fixed_museum",
        amap_id="B000MIDMUSE",
        name="固定博物馆",
        city="北京",
        category="museum",
        latitude=39.99,
        longitude=116.36,
        source="amap-place-search",
        confidence=1.0,
        type="科教文化服务;博物馆",
    )
    provider = Provider()
    route_contract = {
        "status": "ready",
        "fingerprint": "midpoint-route-contract",
        "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000},
    }
    slot_frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_midpoint",
        request_contract_fingerprint="c" * 64,
        evidence={"schemaVersion": "test-evidence-v1", "entities": []},
        locality="北京",
        max_pages_per_query=3,
    )

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        prior_required_candidates_by_occurrence={"occ:museum": [fixed_museum]},
        route_decision_contract=route_contract,
        route_budget=0,
        frontier_assignment={"slotFrontierSnapshot": slot_frontier},
    )

    assert len(provider.calls) == 4
    assert all(call["limit"] == 5 for call in provider.calls)
    park_call = next(call for call in provider.calls if call["keyword"] == "午后公园")
    assert park_call["kind"] == "nearby"
    assert park_call["longitude"] == pytest.approx(116.34)
    assert park_call["latitude"] == pytest.approx(39.99)
    assert park_call["radius"] == 5000
    assert [plan.selected_poi.amap_id for plan in plans if plan.selected_poi is not None] == [
        "B000MIDCAMP",
        "B000MIDMEAL",
        "B000MIDPARK",
        "B000MIDMUSE",
    ]
    park_event = next(
        event
        for event in events
        if event["type"] == "simple_open_tool_call" and event["metadata"].get("slotKey") == "park"
    )
    assert park_event["metadata"]["daySeedAmapId"] == "B000MIDCAMP"
    assert park_event["metadata"]["predecessorAmapId"] == "B000MIDMEAL"
    assert park_event["metadata"]["successorAmapId"] == "B000MIDMUSE"
    assert park_event["metadata"]["searchCenterStrategy"] == "midpoint"
    frontier_event = next(event for event in events if event["type"] == "simple_direction_frontier_outcomes")
    remaining_park_scopes = [
        item for item in frontier_event["metadata"]["remainingQueryScopes"] if item["slotId"] == "park"
    ]
    assert [item["centerRole"] for item in remaining_park_scopes] == ["predecessor", "successor"]
    assert len({item["queryScopeFingerprint"] for item in remaining_park_scopes}) == 2

    park_outcome = next(
        item for item in frontier_event["metadata"]["slotQueryOutcomes"] if item["query"]["slotId"] == "park"
    )
    continued_frontier = SimpleDirectionFrontierService.record_slot_query(
        slot_frontier,
        query=park_outcome["query"],
        provider_outcome=park_outcome["providerOutcome"],
        admitted_physical_groups=park_outcome["admittedPhysicalGroups"],
        rejected_physical_groups=park_outcome["rejectedPhysicalGroups"],
    )
    continuation_provider = Provider()
    _continued_plans, continued_events = SimpleOpenItineraryExecutor(continuation_provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        prior_required_candidates_by_occurrence={"occ:museum": [fixed_museum]},
        route_decision_contract=route_contract,
        route_budget=0,
        frontier_assignment={"slotFrontierSnapshot": continued_frontier},
    )
    continued_park_call = next(call for call in continuation_provider.calls if call["keyword"] == "午后公园")
    assert continued_park_call["longitude"] == pytest.approx(116.32)
    continued_park_event = next(
        event
        for event in continued_events
        if event["type"] == "simple_open_tool_call" and event["metadata"].get("slotKey") == "park"
    )
    assert continued_park_event["metadata"]["searchCenterStrategy"] == "predecessor"

    mismatched_provider = Provider()
    mismatched_plans, mismatched_events = SimpleOpenItineraryExecutor(mismatched_provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        prior_required_candidates_by_occurrence={"occ:museum": [fixed_museum]},
        route_decision_contract=route_contract,
        route_budget=0,
        frontier_assignment={
            "slotQueries": {
                "park": {
                    "slotId": "park",
                    "queryScopeFingerprint": "f" * 64,
                    "page": 1,
                    "offset": 5,
                }
            }
        },
    )
    assert not any(call["keyword"] == "午后公园" for call in mismatched_provider.calls)
    mismatched_park = next(plan for plan in mismatched_plans if plan.planning_slot_id == "park")
    assert mismatched_park.selected_poi is None
    mismatched_slot_event = next(
        event
        for event in mismatched_events
        if event["type"] == "simple_open_slot_unresolved" and event["metadata"].get("slotKey") == "park"
    )
    assert mismatched_slot_event["metadata"]["reasonCode"] == "simple_direction_claimed_adjacent_scope_mismatch"

    deferred_provider = Provider()
    claimed_scope_batches: list[list[dict]] = []

    def claim_current_anchor_scope(scopes: list[dict]) -> dict:
        normalized = SimpleDirectionFrontierService.normalize_remaining_query_scopes(scopes)
        assert normalized
        assert not any(call["keyword"] == normalized[0]["queryText"] for call in deferred_provider.calls)
        claimed_scope_batches.append(normalized)
        current_frontier = SimpleDirectionFrontierService.create(
            planning_root_id="root_current_anchor",
            request_contract_fingerprint="c" * 64,
            evidence={"schemaVersion": "test-evidence-v1", "entities": []},
            locality="北京",
            max_pages_per_query=3,
        )
        current_frontier["remainingQueryScopes"] = normalized
        current_frontier["remainingQueryScopesAuthoritative"] = True
        claimed = SimpleDirectionFrontierService.claim_slot_queries(current_frontier)
        return claimed[normalized[0]["slotId"]]

    _deferred_plans, deferred_events = SimpleOpenItineraryExecutor(deferred_provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="transit",
        slot_lineage=lineage,
        prior_required_candidates_by_occurrence={"occ:museum": [fixed_museum]},
        route_decision_contract=route_contract,
        route_budget=0,
        frontier_assignment={"deferredSlotScopeClaim": True},
        adjacent_scope_claimer=claim_current_anchor_scope,
    )
    assert {batch[0]["slotId"] for batch in claimed_scope_batches} == {"meal", "park"}
    assert all(batch[0]["daySeedAmapId"] == "B000MIDCAMP" for batch in claimed_scope_batches)
    assert any(call["keyword"] == "午后公园" for call in deferred_provider.calls)
    deferred_park_event = next(
        event
        for event in deferred_events
        if event["type"] == "simple_open_tool_call" and event["metadata"].get("slotKey") == "park"
    )
    assert deferred_park_event["metadata"]["daySeedAmapId"] == "B000MIDCAMP"
    assert deferred_park_event["metadata"]["providerOutcome"] == "success"


def test_adjacent_scope_bounds_active_center_and_uses_nearest_known_fixed_successor() -> None:
    from types import SimpleNamespace

    from src.models.poi import POI
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    def poi(amap_id: str, longitude: float) -> POI:
        return POI(
            id=f"poi_{amap_id}",
            amap_id=amap_id,
            name=amap_id,
            city="北京",
            category="scenic",
            latitude=39.99,
            longitude=longitude,
            source="amap-place-search",
            confidence=1.0,
        )

    day_seed = poi("B000BOUND01", 116.30)
    predecessor = poi("B000BOUND02", 116.35)
    next_candidate = poi("B000BOUND03", 116.39)
    scope = SimpleOpenItineraryExecutor._adjacent_candidate_scope(
        day_seed=day_seed,
        predecessor=predecessor,
        successor=None,
    )
    assert scope is not None
    assert not SimpleOpenItineraryExecutor._candidate_within_nearby_scope(next_candidate, day_seed, 5000)
    assert SimpleOpenItineraryExecutor._candidate_within_adjacent_scope(next_candidate, scope, 5000)

    nearest_fixed = poi("B000FIXED01", 116.40)
    later_fixed = poi("B000FIXED02", 116.45)
    slot_inputs = [
        (SimpleNamespace(day_number=1, slot_id="current"), None, {}, "park", "公园", "hard", True),
        (
            SimpleNamespace(day_number=1, slot_id="nearest"),
            SimpleNamespace(entity_binding_mode="exact_entity"),
            {"occurrenceId": "occ:nearest"},
            "museum",
            "最近固定点",
            "hard",
            True,
        ),
        (
            SimpleNamespace(day_number=1, slot_id="later"),
            SimpleNamespace(entity_binding_mode="exact_entity"),
            {"occurrenceId": "occ:later"},
            "museum",
            "更远固定点",
            "hard",
            True,
        ),
    ]
    successor = SimpleOpenItineraryExecutor._known_fixed_successor(
        slot_inputs,
        current_index=0,
        persisted_required_candidates={
            "occ:nearest": [nearest_fixed],
            "occ:later": [later_fixed],
        },
        frontier_slots={},
    )
    assert successor is not None
    assert successor.amap_id == "B000FIXED01"


@pytest.mark.parametrize(
    "source",
    [
        "controller_semantic_choice",
        "user_explicit",
        "opaque_clarification_answer",
        "clarification_answer",
        "explicit_numeric_user_request",
    ],
)
def test_adjacent_leg_contract_enables_nearby_scope_without_source_specific_fallback(source: str) -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    assert (
        SimpleOpenItineraryExecutor._strict_low_detour_nearby_radius(
            {
                "status": "ready",
                "detourToleranceSource": source,
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 15,
                    "maxDetourRatio": 0.15,
                },
                "adjacentLegConstraint": {
                    "candidateSearchRadiusMeters": 5000,
                    "maxProviderTravelMinutes": 45,
                },
            }
        )
        == 5000
    )


def test_controller_estimate_does_not_claim_user_selected_low_detour_scope() -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    assert (
        SimpleOpenItineraryExecutor._strict_low_detour_nearby_radius(
            {
                "status": "ready",
                "detourToleranceSource": "controller_estimate",
                "detourTolerance": {
                    "maxGeneralizedCostDelta": 15,
                    "maxDetourRatio": 0.15,
                },
            }
        )
        is None
    )


def test_simple_open_partial_is_a_persisted_terminal_without_second_decision() -> None:
    coordinator = AgentTurnCoordinator(_registry())
    calls: list[int] = []
    fixed_context = {"latestUserMessage": "北京两日游", "serverExecutionProfile": "simple_open_v1"}

    def decide(_context, _observation, cycle):
        from src.services.agent_autonomy_service import AgentDecision, AgentDecisionResult, GatedAgentDecision

        calls.append(cycle)
        decision = AgentDecision.model_validate(
            {
                "schemaVersion": "agent-decision-v2",
                "primaryAction": "draft_itinerary",
                "confidence": 0.99,
                "decisionSummary": "创建首份行程",
                "requiredTools": ["resolve_poi"],
                "actionDirective": {
                    "type": "draft_itinerary",
                    "dayStrategies": [
                        {
                            "dayNumber": 1,
                            "theme": "城市探索",
                            "requiredGoalIds": [],
                            "requiredGoalCounts": {},
                            "optionalGoalIds": [],
                        }
                    ],
                },
                "stopCondition": {"type": "verified_write_then_reobserve"},
            }
        )
        return AgentDecisionResult(
            decision=decision,
            gated_decision=GatedAgentDecision.model_validate(
                {"decision": decision, "effectiveWriteRisk": "high", "effectiveTools": [], "accepted": True}
            ),
            source="test",
            controller_error=None,
            planner_plan={},
            controller_full_called=True,
            controller_full_succeeded=True,
            decision_path="full",
        )

    result = coordinator.run(
        fixed_context,
        observe=lambda context, cycle, _outcome: AgentObservationBuilder().build(context, cycle_index=cycle),
        decide=decide,
        reload_context=lambda context, _outcome, _cycle: context,
        max_cycles=3,
    )

    assert calls == [0]
    assert result.stop_reason == "simple_open_terminal"
    assert result.outcomes[0].execution_route == "simple_open_initial_pipeline"


def test_initial_planning_mode_defaults_to_strict_portfolio() -> None:
    assert Settings().agent_initial_planning_mode == "strict_portfolio"


def test_initial_planning_profile_reads_server_environment(monkeypatch) -> None:
    from src.core.config import get_settings

    monkeypatch.setenv("AGENT_INITIAL_PLANNING_MODE", "simple_open_v1")
    get_settings.cache_clear()
    try:
        assert get_settings().agent_initial_planning_mode == "simple_open_v1"
    finally:
        get_settings.cache_clear()


def test_simple_open_budget_reserves_two_bounded_daily_completion_searches() -> None:
    import sqlite3

    from src.services.agent_service import AgentService

    budget = AgentService(sqlite3.connect(":memory:"))._simple_open_amap_call_budget()

    assert budget.place_text_max == 8
    assert budget.place_around_max == 8
    assert budget.route_refresh_max == 8
    assert budget.total_external_max == 16
    assert budget.source == "simple_open_initial_pipeline_with_daily_completion"


def test_simple_open_profile_cannot_resume_a_portfolio_checkpoint() -> None:
    from src.services.agent_service import AgentService

    checkpoint = {
        "constraintLedger": {},
        "creativePortfolio": {},
        "groundingCheckpoint": {},
    }
    retry = {"retryExecutionPlan": {"kind": "resume_incomplete_stage"}}

    assert (
        AgentService._checkpoint_portfolio_resume_allowed(
            {**retry, "serverExecutionProfile": "simple_open_v1"}, checkpoint
        )
        is False
    )
    assert (
        AgentService._checkpoint_portfolio_resume_allowed(
            {**retry, "serverExecutionProfile": "strict_portfolio"}, checkpoint
        )
        is True
    )


def test_simple_open_profile_rejects_structured_portfolio_resume_even_with_checkpoint_payload() -> None:
    from src.services.agent_service import AgentService

    checkpoint = {
        "constraintLedger": {},
        "creativePortfolio": {},
        "groundingCheckpoint": {},
    }

    assert (
        AgentService._portfolio_resume_allowed(
            {
                "serverExecutionProfile": "simple_open_v1",
                "structuredPlanningChoiceResume": True,
            },
            checkpoint,
        )
        is False
    )
    assert (
        AgentService._portfolio_resume_allowed(
            {
                "serverExecutionProfile": "strict_portfolio",
                "structuredPlanningChoiceResume": True,
            },
            checkpoint,
        )
        is True
    )


def test_simple_open_profile_drops_portfolio_checkpoint_but_keeps_own_retry_checkpoint() -> None:
    from src.services.agent_service import AgentService

    portfolio_attempt = {
        "enabled": True,
        "reuseInitialPlan": True,
        "initialPlan": {"daySlots": [{"slotId": "old-portfolio-slot"}]},
        "pipelineContext": {
            "creativePortfolioMode": True,
            "portfolioDensityContinuation": {"rootPortfolioId": "old-portfolio"},
        },
        "creativePortfolio": {},
    }
    simple_attempt = {
        "enabled": True,
        "reuseInitialPlan": True,
        "initialPlan": {"daySlots": [{"slotId": "simple-retry-slot"}]},
        "pipelineContext": {"simpleOpenRetry": True},
    }

    assert (
        AgentService._resumable_plan_from_context(
            None,
            {
                "serverExecutionProfile": "simple_open_v1",
                "structuredPlanningChoiceResume": True,
                "resumePlanningAttempt": portfolio_attempt,
            },
        )
        is None
    )
    resumed = AgentService._resumable_plan_from_context(
        None,
        {
            "serverExecutionProfile": "simple_open_v1",
            "resumePlanningAttempt": simple_attempt,
        },
    )
    assert resumed is not None
    assert resumed["initialPlan"]["daySlots"][0]["slotId"] == "simple-retry-slot"


def test_simple_open_service_rejects_persisted_density_choice_before_provider_or_write(monkeypatch) -> None:
    from fastapi import HTTPException

    from backend.tests.unit.test_agent_service import (
        FakePoiResolver,
        StagedInitialProvider,
        clear_database,
        open_db,
        two_day_initial_day_slot_output,
    )
    from src.api.schemas.agent import AgentMessageRequest
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService

    clear_database()
    provider = StagedInitialProvider(two_day_initial_day_slot_output())
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple rejects density")
        connection.execute(
            "UPDATE conversation_sessions SET active_version_id = ? WHERE id = ?",
            ("old-strict-version", session.session_id),
        )
        connection.commit()
        service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
        service.initial_planning_mode = "simple_open_v1"
        monkeypatch.setattr(
            service,
            "_build_request_context",
            lambda *_args, **_kwargs: {
                "latestUserMessage": "继续补地点",
                "selectedAgentChoice": {
                    "sourceAssistantTurnId": "old-strict-turn",
                    "choiceId": "old-density-choice",
                    "action": "refresh_density_candidates",
                    "option": {
                        "kind": "portfolio_density_retry",
                        "action": "refresh_density_candidates",
                    },
                },
            },
        )
        monkeypatch.setattr(service, "_restore_request_intent_contract", lambda *_args, **_kwargs: None)

        with pytest.raises(HTTPException) as exc_info:
            service.send_message(
                session.session_id,
                AgentMessageRequest(content="继续补地点"),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "agent_choice_execution_profile_mismatch"
        assert provider.calls == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_versions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM itinerary_patches").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("planning_context", "route_status", "expected"),
    [
        (
            {
                "simpleOpenGroundedSegmentCount": 4,
                "simpleOpenUnresolvedSegmentCount": 0,
                "simpleOpenProvisionalSegmentCount": 0,
            },
            "ready",
            "READY",
        ),
        (
            {
                "simpleOpenGroundedSegmentCount": 4,
                "simpleOpenUnresolvedSegmentCount": 0,
                "simpleOpenProvisionalSegmentCount": 1,
            },
            "ready",
            "PARTIAL",
        ),
        (
            {
                "simpleOpenGroundedSegmentCount": 4,
                "simpleOpenUnresolvedSegmentCount": 0,
                "simpleOpenProvisionalSegmentCount": 0,
            },
            "provider_failed",
            "PARTIAL",
        ),
        ({"simpleOpenGroundedSegmentCount": 0}, "provider_failed", "FAILED"),
    ],
)
def test_simple_open_result_classification_is_truthful(
    planning_context: dict, route_status: str, expected: str
) -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    assert SimpleOpenItineraryExecutor.classify_result(planning_context, route_status) == expected


def test_route_service_accepts_trusted_semantic_anchor_for_non_legacy_kind() -> None:
    from src.models.itinerary_segment import ItinerarySegment
    from src.models.poi import POI
    from src.services.route_service import RouteService

    segment = ItinerarySegment(
        id="seg_campus",
        day_id="day_1",
        segment_order=0,
        kind="campus",
        start_time="09:00",
        end_time="11:00",
        poi_id="poi_campus",
        transport_mode="public_transit",
        estimated_cost=0,
        notes="",
        semantic_metadata={"routeAnchor": True, "groundingStatus": "verified_amap"},
    )
    poi = POI(
        id="poi_campus",
        amap_id="B000A1B2C3D4",
        name="清华大学",
        city="北京",
        category="campus",
        latitude=39.999,
        longitude=116.326,
        source="amap-place-search",
        source_note="routeAnchor=false",
    )

    service = RouteService(map_provider_key="fixture")
    assert service._is_route_anchor_segment(segment, poi) is False
    assert service._is_route_anchor_segment(segment, poi, allow_semantic_route_anchor=True) is True


def test_route_service_rejects_unresolved_semantic_anchor() -> None:
    from src.models.itinerary_segment import ItinerarySegment
    from src.models.poi import POI
    from src.services.route_service import RouteService

    segment = ItinerarySegment(
        id="seg_unresolved",
        day_id="day_1",
        segment_order=0,
        kind="campus",
        start_time="09:00",
        end_time="11:00",
        poi_id="poi_unresolved",
        transport_mode="public_transit",
        estimated_cost=0,
        notes="",
        semantic_metadata={"routeAnchor": True, "groundingStatus": "unresolved"},
    )
    poi = POI(
        id="poi_unresolved",
        amap_id="B000A1B2C3D4",
        name="待确认地点",
        city="北京",
        category="campus",
        latitude=39.999,
        longitude=116.326,
        source="amap-place-search",
        source_note="routeAnchor=false",
    )

    assert (
        RouteService(map_provider_key="fixture")._is_route_anchor_segment(
            segment, poi, allow_semantic_route_anchor=True
        )
        is False
    )


def test_strict_route_group_does_not_promote_non_route_meal_from_semantic_metadata() -> None:
    from src.models.itinerary_segment import ItinerarySegment
    from src.models.poi import POI
    from src.services.route_service import RouteService

    segment = ItinerarySegment(
        id="seg_meal",
        day_id="day_1",
        segment_order=0,
        kind="meal",
        start_time="12:00",
        end_time="13:00",
        poi_id="poi_meal",
        transport_mode="public_transit",
        estimated_cost=0,
        notes="",
        semantic_metadata={"routeAnchor": True, "groundingStatus": "verified_amap"},
    )
    poi = POI(
        id="poi_meal",
        amap_id="B000A1B2C3D4",
        name="普通午餐",
        city="北京",
        category="food",
        latitude=39.999,
        longitude=116.326,
        source="amap-place-search",
        source_note="routeAnchor=false",
    )

    assert RouteService(map_provider_key="fixture")._is_route_anchor_segment(segment, poi) is False


def test_simple_open_rejects_parent_child_duplicate_provider_identities() -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class ParentChildMapProvider:
        def search(self, city, keyword, category="all", limit=5):
            del limit
            parent_id = "B000000009"
            item = MapPoiResponse(
                id=parent_id if keyword == "清华大学" else "B000000002",
                parentPoiId=None if keyword == "清华大学" else parent_id,
                name="清华大学",
                type="科教文化服务;学校;高等院校",
                city=city,
                district="海淀区",
                address="同址",
                longitude=116.3,
                latitude=40.0,
                category=category,
                source="amap-place-search",
                sourceNote="provider",
                confidence=1.0,
            )
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[item],
            )

    raw = two_day_initial_day_slot_output()
    raw["daySlots"] = [
        {**raw["daySlots"][0], "slotId": "s1", "rawNeed": "清华大学"},
        {**raw["daySlots"][1], "slotId": "s2", "rawNeed": "中信大厦"},
    ]
    raw["intentPools"] = [
        {**raw["intentPools"][0], "assignToSlots": ["s1"], "candidateHints": ["清华大学"]},
        {**raw["intentPools"][1], "assignToSlots": ["s2"], "candidateHints": ["中信大厦"]},
    ]

    plans, _events = SimpleOpenItineraryExecutor(ParentChildMapProvider()).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert plans[0].selected_poi is not None
    assert plans[0].selected_poi.amap_id == "B000000009"
    assert plans[0].selected_poi.parent_poi_id is None
    assert plans[1].selected_poi is None
    assert plans[1].grounding_status == "unresolved"


def test_simple_open_persists_parent_identity_and_rejects_sibling_child_in_next_direction() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    parent_id = "B000PARENT1"

    class ChildProvider:
        def __init__(self, child_id: str, child_name: str) -> None:
            self.child_id = child_id
            self.child_name = child_name

        def search(self, city, keyword, category="all", limit=5):
            del keyword, limit
            return MapPoiSearchResponse(
                city=city,
                keyword=self.child_name,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=self.child_id,
                        parentPoiId=parent_id,
                        name=self.child_name,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=f"{self.child_name}独立地址",
                        longitude=116.31 if self.child_id.endswith("1") else 116.41,
                        latitude=39.91 if self.child_id.endswith("1") else 39.99,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    raw = {
        "reply": "one campus",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "campus_slot",
                "dayNumber": 1,
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 90,
                "kind": "campus",
                "rawNeed": "高校校园",
                "routeAnchor": True,
            }
        ],
        "intentPools": [
            {
                "poolId": "campus_pool",
                "rawNeed": "高校校园",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "requirementLevel": "required",
                "assignToSlots": ["campus_slot"],
                "candidateHints": ["高校校园"],
            }
        ],
    }
    first, _events = SimpleOpenItineraryExecutor(ChildProvider("B000CHILD01", "高校东门校区")).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert first[0].selected_poi is not None
    assert first[0].selected_poi.parent_poi_id == parent_id

    second, _events = SimpleOpenItineraryExecutor(ChildProvider("B000CHILD02", "高校西门校区")).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        excluded_physical_aliases={f"amap:{parent_id}"},
    )

    assert second[0].selected_poi is None
    assert second[0].grounding_status == "unresolved"


def test_simple_open_bounds_model_slot_amplification_and_provider_calls() -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        MAX_SIMPLE_OPEN_SLOTS,
        SimpleOpenItineraryExecutor,
    )

    class CountingMapProvider:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, city, keyword, category="all", limit=5):
            del keyword, limit
            self.calls += 1
            return MapPoiSearchResponse(
                city=city,
                keyword=f"bounded-{self.calls}",
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=f"B{self.calls:011d}",
                        name=f"北京测试大学{self.calls}",
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=f"测试路{self.calls}号",
                        longitude=116.3 + self.calls / 10000,
                        latitude=40.0,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    raw = two_day_initial_day_slot_output()
    template = raw["daySlots"][0]
    raw["daySlots"] = [
        {
            **template,
            "slotId": f"amplified-{index}",
            "startTime": f"{index % 24:02d}:00",
            "timeWindow": f"{index % 24:02d}:00-{(index + 1) % 24:02d}:00",
        }
        for index in range(100)
    ]
    raw["intentPools"] = [
        {
            **raw["intentPools"][0],
            "assignToSlots": [item["slotId"] for item in raw["daySlots"]],
            "candidateHints": ["北京高校"],
        }
    ]
    provider = CountingMapProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert len(plans) == MAX_SIMPLE_OPEN_SLOTS
    assert provider.calls == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert sum(event["type"] == "simple_open_tool_call" for event in events) == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert sum(plan.selected_poi is not None for plan in plans) == MAX_SIMPLE_OPEN_POI_SEARCHES
    unresolved = [plan for plan in plans if plan.selected_poi is None]
    assert len(unresolved) == MAX_SIMPLE_OPEN_SLOTS - MAX_SIMPLE_OPEN_POI_SEARCHES
    assert all(plan.grounding_status == "unresolved" and "预算" in plan.notes for plan in unresolved)


def test_simple_open_rejects_authoritative_slot_budget_overflow_before_provider_call() -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_SLOTS,
        SimpleOpenItineraryExecutor,
    )

    class UnexpectedMapProvider:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, city, keyword, category="all", limit=5):
            del city, keyword, category, limit
            self.calls += 1
            raise AssertionError("authoritative slot overflow must fail before provider search")

    raw = two_day_initial_day_slot_output()
    template = raw["daySlots"][0]
    slot_ids = [f"authoritative-required-{index}" for index in range(1, MAX_SIMPLE_OPEN_SLOTS + 2)]
    raw["daySlots"] = [
        {
            **template,
            "slotId": slot_id,
            "startTime": f"{index:02d}:00",
            "timeWindow": f"{index:02d}:00-{index + 1:02d}:00",
            "rawNeed": f"服务端必选地点 {index}",
        }
        for index, slot_id in enumerate(slot_ids, start=7)
    ]
    raw["intentPools"] = [
        {
            **raw["intentPools"][0],
            "requirementLevel": "required",
            "targetCount": len(slot_ids),
            "assignToSlots": slot_ids,
            "candidateHints": [f"服务端必选地点 {index}" for index in range(1, len(slot_ids) + 1)],
        }
    ]
    slot_lineage = {
        slot_id: {
            "requirementLevel": "required",
            "poolId": raw["intentPools"][0]["poolId"],
            "goalId": "authoritative-required-goal",
            "occurrenceId": f"authoritative-required-occurrence-{index}",
            "lineageAuthority": "server_compiled",
        }
        for index, slot_id in enumerate(slot_ids, start=1)
    }
    provider = UnexpectedMapProvider()

    with pytest.raises(ValueError, match="^simple_open_authoritative_slot_budget_exceeded$"):
        SimpleOpenItineraryExecutor(provider).build_segment_plans(
            AgentInitialPlanOutput.model_validate(raw),
            city="北京",
            transport_mode="public_transit",
            slot_lineage=slot_lineage,
            authoritative_lineage_required=True,
        )

    assert provider.calls == 0


def test_simple_open_reserves_fixed_search_budget_for_late_required_slot() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    class RecordingMapProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            ordinal = len(self.keywords)
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=f"B{ordinal:011d}",
                        name=keyword,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=f"测试路{ordinal}号",
                        longitude=116.3 + ordinal / 10000,
                        latitude=40.0,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    optional_slot_ids = [f"optional-{index}" for index in range(1, 7)]
    required_slot_id = "required-late"
    raw = {
        "reply": "已生成有界槽位。",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": slot_id,
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": f"{index:02d}:00-{index + 1:02d}:00",
                "startTime": f"{index:02d}:00",
                "durationMinutes": 60,
                "kind": "campus",
                "rawNeed": f"可选高校 {index}",
                "routeAnchor": True,
            }
            for index, slot_id in enumerate(optional_slot_ids, start=8)
        ]
        + [
            {
                "slotId": required_slot_id,
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "20:00-21:00",
                "startTime": "20:00",
                "durationMinutes": 60,
                "kind": "campus",
                "rawNeed": "必选高校",
                "routeAnchor": True,
            }
        ],
        "intentPools": [
            {
                "poolId": "optional-campus-pool",
                "rawNeed": "可选高校",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": len(optional_slot_ids),
                "requirementLevel": "optional",
                "assignToSlots": optional_slot_ids,
                "candidateHints": [f"可选高校 {index}" for index in range(1, 7)],
                "hintPolicy": "llm_common_knowledge_hint",
            },
            {
                "poolId": "required-campus-pool",
                "rawNeed": "必选高校",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "requirementLevel": "required",
                "assignToSlots": [required_slot_id],
                "candidateHints": ["必选高校"],
                "hintPolicy": "user_explicit_hint",
            },
        ],
        "warnings": [],
    }
    provider = RecordingMapProvider()
    slot_lineage = {
        slot_id: {"requirementLevel": "optional", "poolId": "optional-campus-pool"} for slot_id in optional_slot_ids
    }
    slot_lineage[required_slot_id] = {
        "requirementLevel": "required",
        "poolId": "required-campus-pool",
        "goalId": "required-campus-goal",
        "occurrenceId": "required-campus-occurrence-1",
        "lineageAuthority": "server_compiled",
    }

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=slot_lineage,
        authoritative_lineage_required=True,
    )

    assert len(provider.keywords) == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert "必选高校" in provider.keywords
    assert plans[-1].planning_slot_id == required_slot_id
    assert plans[-1].required is True
    skipped_optional = next(plan for plan in plans if plan.planning_slot_id == optional_slot_ids[-1])
    assert skipped_optional.selected_poi is None
    assert "必选" in skipped_optional.notes
    assert sum(event["type"] == "simple_open_tool_call" for event in events) == MAX_SIMPLE_OPEN_POI_SEARCHES


@pytest.mark.parametrize("alternative_succeeds", [True, False])
def test_simple_open_cached_duplicate_anchor_uses_at_most_one_safe_alternative_query(
    alternative_succeeds: bool,
) -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    class CachedDuplicateProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            if len(self.keywords) <= 2:
                amap_id = "B000A6EA36"
                name = "清华大学"
                address = "双清路30号"
            elif alternative_succeeds:
                amap_id = "B000A7O5PK"
                name = "北京大学"
                address = "颐和园路5号"
            else:
                amap_id = "B000A6EA36"
                name = "清华大学"
                address = "双清路30号"
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=len(self.keywords) == 2,
                pois=[
                    MapPoiResponse(
                        id=amap_id,
                        name=name,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=address,
                        longitude=116.32 + len(self.keywords) / 1000,
                        latitude=39.99,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    raw = {
        "reply": "两个高校 occurrence",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "day1_required_campus",
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
                "slotId": "day2_optional_campus",
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
                "poolId": "campus_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 2,
                "requirementLevel": "required",
                "assignToSlots": ["day1_required_campus", "day2_optional_campus"],
                "candidateHints": ["北京高校", "北京高校"],
            }
        ],
    }
    slot_lineage = {
        "day1_required_campus": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": "occ:goal_campus_visit:day:1",
            "poolId": "campus_pool",
            "planningSlotId": "day1_required_campus",
            "dayNumber": 1,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        },
        "day2_optional_campus": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": "occ:goal_campus_visit:day:2",
            "poolId": "campus_pool",
            "planningSlotId": "day2_optional_campus",
            "dayNumber": 2,
            "requirementLevel": "explicit_soft",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        },
    }
    provider = CachedDuplicateProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=slot_lineage,
        authoritative_lineage_required=True,
    )

    assert len(provider.keywords) == 3
    assert len(provider.keywords) <= MAX_SIMPLE_OPEN_POI_SEARCHES
    assert provider.keywords[0] == provider.keywords[1]
    assert provider.keywords[2] != provider.keywords[1]
    assert sum(event["type"] == "simple_open_tool_call" for event in events) == 3
    alternative_events = [
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "safe_alternative"
    ]
    assert len(alternative_events) == 1
    assert plans[0].required is True
    assert plans[0].occurrence_id == "occ:goal_campus_visit:day:1"
    assert plans[1].required is False
    assert plans[1].requirement_level == "explicit_soft"
    assert plans[1].occurrence_id == "occ:goal_campus_visit:day:2"
    assert plans[1].lineage_authority == "goal_occurrence_compiler"
    if alternative_succeeds:
        assert plans[1].selected_poi is not None
        assert plans[1].selected_poi.amap_id == "B000A7O5PK"
        assert plans[1].grounding_status == "verified_amap"
    else:
        assert plans[1].selected_poi is None
        assert plans[1].grounding_status == "unresolved"
        assert "第 2 天" in plans[1].notes
        assert "高校" in plans[1].notes
        assert "安全替代查询" in plans[1].notes


@pytest.mark.parametrize(
    ("night_requirement_level", "primary_result_mode", "expected_safe_alternative"),
    [
        ("hard", "semantic_rejected", True),
        ("hard", "empty", True),
        ("hard", "provider_failure", False),
        ("explicit_soft", "semantic_rejected", False),
        ("explicit_soft", "empty", False),
    ],
    ids=[
        "hard-semantic-rejected",
        "hard-empty",
        "hard-provider-failure",
        "explicit-soft-semantic-rejected",
        "explicit-soft-empty",
    ],
)
def test_nearby_night_safe_alternative_requires_hard_occurrence(
    night_requirement_level: str,
    primary_result_mode: str,
    expected_safe_alternative: bool,
) -> None:
    from backend.tests.unit.test_agent_service import open_db
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    class TraceShapedNightProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []
            self.nearby_calls: list[dict[str, object]] = []

        def search_nearby(
            self,
            city,
            longitude,
            latitude,
            keyword,
            category="all",
            limit=5,
            **kwargs,
        ):
            self.nearby_calls.append(
                {
                    "city": city,
                    "longitude": longitude,
                    "latitude": latitude,
                    "keyword": keyword,
                    "radius": kwargs.get("radius"),
                }
            )
            if keyword == "北京 夜景 观景台":
                self.keywords.append(keyword)
                return MapPoiSearchResponse(
                    city=city,
                    keyword=keyword,
                    category=category,
                    providerName="amap-place-search",
                    queriedAt=datetime.now(timezone.utc),
                    pois=[
                        MapPoiResponse(
                            id="B0JNEARBY01",
                            name="海淀观景台测试候选",
                            type="风景名胜;风景名胜;观景点",
                            providerTypeCode="110206",
                            city=city,
                            district="海淀区",
                            address="附近查询测试地址",
                            longitude=116.32028,
                            latitude=39.99894,
                            category=category,
                            source="amap-place-search",
                            sourceNote="provider-fixture",
                            confidence=1.0,
                            openTimeToday="08:00-23:00",
                        )
                    ],
                )
            return self.search(city, keyword, category=category, limit=limit)

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            if keyword == "北京大学":
                pois = [
                    MapPoiResponse(
                        id="B000A7O5PK",
                        name="北京大学",
                        type="科教文化服务;学校;高等院校",
                        providerTypeCode="141200",
                        city=city,
                        district="海淀区",
                        address="颐和园路5号",
                        longitude=116.31088,
                        latitude=39.99281,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        openTimeToday="08:00-23:00",
                    )
                ]
            elif keyword == "night view":
                if primary_result_mode == "provider_failure":
                    raise RuntimeError("bounded nearby provider failure")
                pois = (
                    []
                    if primary_result_mode == "empty"
                    else [
                        MapPoiResponse(
                            id=f"B000NIGHT0{index}",
                            name=name,
                            type=candidate_type,
                            providerTypeCode=provider_type_code,
                            city=city,
                            district="朝阳区",
                            address=f"测试地址{index}号",
                            longitude=116.31088 + index / 10000,
                            latitude=39.99281 + index / 10000,
                            category=category,
                            source="amap-place-search",
                            sourceNote="provider",
                            confidence=1.0,
                        )
                        for index, (name, candidate_type, provider_type_code) in enumerate(
                            [
                                ("8Night男士理发Barbershop", "生活服务;美容美发店", "071100"),
                                ("GrandView观局酒馆", "餐饮服务;酒吧", "050500"),
                                ("GrooveAllNight海淀店", "餐饮服务;酒吧", "050500"),
                                ("TheView3912CBD高空观景店", "餐饮服务;酒吧", "050500"),
                                ("TheView4109大望路店", "餐饮服务;酒吧", "050500"),
                            ],
                            start=1,
                        )
                    ]
                )
            elif keyword == "北京 夜景 观景台":
                pois = [
                    MapPoiResponse(
                        id="B0JDPFC6DW",
                        name="中央广播电视塔观景台",
                        type="风景名胜;风景名胜;观景点",
                        providerTypeCode="110206",
                        city=city,
                        district="海淀区",
                        address="西三环中路11号",
                        longitude=116.30028,
                        latitude=39.91894,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        openTimeToday="08:00-23:00",
                    )
                ]
            else:
                raise AssertionError(f"unexpected query: {keyword}")
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=pois,
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "Day 1 高校与 hard 夜景槽位",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_goal_campus_visit_1",
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
                    "slotId": "day1_goal_night_view_2",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "19:00-21:00",
                    "startTime": "19:00",
                    "durationMinutes": 120,
                    "kind": "night_view",
                    "rawNeed": "night view",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "goal_campus_visit_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_goal_campus_visit_1"],
                    "candidateHints": ["北京大学"],
                },
                {
                    "poolId": "goal_night_view_pool",
                    "rawNeed": "night view",
                    "city": "北京",
                    "intentType": "night_view",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_goal_night_view_2"],
                    "candidateHints": ["night view"],
                },
            ],
        }
    )
    slot_lineage = {
        "day1_goal_campus_visit_1": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": "occ:goal_campus_visit:day:1",
            "poolId": "goal_campus_visit_pool",
            "planningSlotId": "day1_goal_campus_visit_1",
            "dayNumber": 1,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        },
        "day1_goal_night_view_2": {
            "goalId": "goal_night_view",
            "sourceGoalId": "goal_night_view",
            "occurrenceId": "occ:goal_night_view:day:1",
            "poolId": "goal_night_view_pool",
            "planningSlotId": "day1_goal_night_view_2",
            "dayNumber": 1,
            "requirementLevel": night_requirement_level,
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
            "schedulePreference": {"dayPart": "evening"},
        },
    }

    def formal_write_counts() -> dict[str, int]:
        with open_db() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "agent_plan_proposals",
                    "itinerary_versions",
                    "itinerary_patches",
                    "route_options",
                )
            }

    before_writes = formal_write_counts()
    provider = TraceShapedNightProvider()
    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="public_transit",
        slot_lineage=slot_lineage,
        authoritative_lineage_required=True,
        route_decision_contract={
            "status": "ready",
            "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000},
        },
    )
    after_writes = formal_write_counts()

    assert (
        before_writes
        == after_writes
        == {
            "agent_plan_proposals": 0,
            "itinerary_versions": 0,
            "itinerary_patches": 0,
            "route_options": 0,
        }
    )
    assert len(provider.keywords) <= MAX_SIMPLE_OPEN_POI_SEARCHES
    expected_keywords = ["北京大学", "night view"]
    if expected_safe_alternative:
        expected_keywords.append("北京 夜景 观景台")
    assert provider.keywords == expected_keywords
    assert len(provider.nearby_calls) == (2 if expected_safe_alternative else 1)
    assert provider.nearby_calls[0]["keyword"] == "night view"
    if expected_safe_alternative:
        assert provider.nearby_calls[1] == {
            "city": provider.nearby_calls[0]["city"],
            "longitude": provider.nearby_calls[0]["longitude"],
            "latitude": provider.nearby_calls[0]["latitude"],
            "keyword": "北京 夜景 观景台",
            "radius": 5000,
        }
    primary_night_event = next(
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and str(event.get("detail") or "").startswith("night view")
        and (event.get("metadata") or {}).get("queryRole") != "safe_alternative"
    )
    assert primary_night_event["metadata"]["searchScope"] == "nearby_low_detour"
    assert primary_night_event["metadata"]["resultCount"] == (5 if primary_result_mode == "semantic_rejected" else 0)
    assert primary_night_event["metadata"]["selectedAmapId"] is None
    if primary_result_mode == "provider_failure":
        assert primary_night_event["status"] == "failed"
        assert primary_night_event["metadata"]["providerOutcome"] == "failure"
        assert primary_night_event["metadata"]["errorType"] == "RuntimeError"
        assert "candidateAdmissionRejectionReasonCounts" not in primary_night_event["metadata"]
    else:
        expected_rejections = {} if primary_result_mode == "empty" else {"provider_type_intent_mismatch": 5}
        assert primary_night_event["metadata"]["candidateAdmissionRejectionReasonCounts"] == expected_rejections
    alternative_events = [
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "safe_alternative"
    ]
    assert len(alternative_events) == (1 if expected_safe_alternative else 0)
    if expected_safe_alternative:
        assert (
            alternative_events[0]["metadata"]["primaryQueryFingerprint"]
            != alternative_events[0]["metadata"]["queryFingerprint"]
        )
        assert alternative_events[0]["metadata"]["searchScope"] == "nearby_low_detour"
        assert alternative_events[0]["metadata"]["anchorAmapId"] == "B000A7O5PK"
        assert alternative_events[0]["metadata"]["radiusMeters"] == 5000
        assert alternative_events[0]["metadata"]["queryScopeFingerprint"]
        assert alternative_events[0]["metadata"]["distanceLimitIsRouteEvidence"] is False
        assert alternative_events[0]["metadata"]["budgetBefore"] == {"poiSearchRemaining": 4}
        assert alternative_events[0]["metadata"]["budgetAfter"] == {"poiSearchRemaining": 3}
        assert alternative_events[0]["metadata"]["selectedAmapId"] == "B0JNEARBY01"
        assert alternative_events[0]["metadata"]["candidateAdmissionRejectionReasonCounts"] == {}
    assert plans[0].selected_poi is not None and plans[0].route_anchor is True
    night_plan = plans[1]
    assert night_plan.required is (night_requirement_level == "hard")
    assert night_plan.requirement_level == night_requirement_level
    assert night_plan.route_anchor is True
    assert night_plan.planning_slot_id == "day1_goal_night_view_2"
    assert (night_plan.selected_poi is not None) is expected_safe_alternative


def test_later_hard_campus_occurrence_keeps_its_safe_alternative_after_night_alternative() -> None:
    """A prior hard occurrence must not consume another occurrence's alternative frontier."""

    from backend.tests.unit.test_agent_service import open_db
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    class TraceShapedProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def _poi(
            self,
            *,
            city: str,
            category: str,
            amap_id: str,
            name: str,
            provider_type: str,
            longitude: float,
            open_time_today: str | None = None,
        ) -> MapPoiResponse:
            return MapPoiResponse(
                id=amap_id,
                name=name,
                type=provider_type,
                city=city,
                district="海淀区",
                address=f"{name}测试地址",
                longitude=longitude,
                latitude=39.99,
                category=category,
                source="amap-place-search",
                sourceNote="provider",
                confidence=1.0,
                openTimeToday=open_time_today,
                providerTypeCode="050111" if "北京菜" in provider_type else None,
                tags=["北京菜", "地方风味"] if "北京菜" in provider_type else [],
                sourceClaims=(
                    [
                        {
                            "claimKey": "local_food",
                            "stance": "support",
                            "locality": "北京",
                            "evidenceSource": "provider_city_specific_fact",
                        }
                    ]
                    if "北京菜" in provider_type
                    else []
                ),
            )

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            repeated_campus_query = sum(item == "高校参观" for item in self.keywords) > 1
            if keyword == "高校参观":
                pois = [
                    self._poi(
                        city=city,
                        category=category,
                        amap_id="B0FFF0EFZY",
                        name="清华大学工字厅",
                        provider_type="科教文化服务;学校;高等院校",
                        longitude=116.326,
                    ),
                    self._poi(
                        city=city,
                        category=category,
                        amap_id="B0FFHL2A77",
                        name="北京英国学校顺义校区",
                        provider_type="科教文化服务;学校;中学",
                        longitude=116.520,
                    ),
                ]
            elif keyword == "餐厅":
                pois = [
                    self._poi(
                        city=city,
                        category=category,
                        amap_id="B000MEAL01",
                        name="北京风味餐厅",
                        provider_type="餐饮服务;中餐厅;北京菜",
                        longitude=116.331,
                        open_time_today="10:00-22:00",
                    )
                ]
            elif keyword == "night view":
                pois = [
                    self._poi(
                        city=city,
                        category=category,
                        amap_id=f"B000NIGHT0{index}",
                        name=name,
                        provider_type=provider_type,
                        longitude=116.45 + index / 1000,
                    )
                    for index, (name, provider_type) in enumerate(
                        [
                            ("8Night男士理发Barbershop", "生活服务;美容美发店"),
                            ("GrandView观局酒馆", "餐饮服务;酒吧"),
                            ("GrooveAllNight海淀店", "餐饮服务;酒吧"),
                            ("TheView3912CBD高空观景店", "餐饮服务;酒吧"),
                            ("TheView4109大望路店", "餐饮服务;酒吧"),
                        ],
                        start=1,
                    )
                ]
            elif keyword == "北京 夜景 观景台":
                pois = [
                    self._poi(
                        city=city,
                        category=category,
                        amap_id="B0JDPFC6DW",
                        name="中央广播电视塔观景台",
                        provider_type="风景名胜;风景名胜;观景点",
                        longitude=116.300,
                        open_time_today="08:00-23:00",
                    )
                ]
            elif keyword == "北京 高等院校 校区":
                pois = [
                    self._poi(
                        city=city,
                        category=category,
                        amap_id="B000A7O5PK",
                        name="北京大学",
                        provider_type="科教文化服务;学校;高等院校",
                        longitude=116.311,
                    )
                ]
            else:
                raise AssertionError(f"unexpected query: {keyword}")
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=repeated_campus_query,
                pois=pois,
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "两日高校、午餐与夜景",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_goal_campus_visit_1",
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
                    "slotId": "day1_goal_meal_1",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "12:00-13:00",
                    "startTime": "12:00",
                    "durationMinutes": 60,
                    "kind": "meal",
                    "rawNeed": "当地特色午餐",
                    "routeAnchor": True,
                },
                {
                    "slotId": "day1_goal_night_view_2",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "timeWindow": "19:00-21:00",
                    "startTime": "19:00",
                    "durationMinutes": 120,
                    "kind": "night_view",
                    "rawNeed": "night view",
                    "routeAnchor": True,
                },
                {
                    "slotId": "day2_goal_campus_visit_1",
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
                    "poolId": "goal_campus_visit_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 2,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_goal_campus_visit_1", "day2_goal_campus_visit_1"],
                    "candidateHints": ["高校参观", "高校参观"],
                },
                {
                    "poolId": "goal_meal_pool",
                    "rawNeed": "当地特色午餐",
                    "city": "北京",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_goal_meal_1"],
                    "candidateHints": ["当地特色午餐"],
                },
                {
                    "poolId": "goal_night_view_pool",
                    "rawNeed": "night view",
                    "city": "北京",
                    "intentType": "night_view",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_goal_night_view_2"],
                    "candidateHints": ["night view"],
                },
            ],
        }
    )
    slot_lineage = {
        slot.slot_id: {
            "goalId": f"goal_{'campus_visit' if slot.kind == 'campus' else slot.kind}",
            "sourceGoalId": f"goal_{'campus_visit' if slot.kind == 'campus' else slot.kind}",
            "occurrenceId": f"occ:{slot.slot_id}",
            "poolId": next(pool.pool_id for pool in initial.intent_pools if slot.slot_id in pool.assign_to_slots),
            "planningSlotId": slot.slot_id,
            "dayNumber": slot.day_number,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
            "schedulePreference": {"dayPart": "evening"} if slot.kind == "night_view" else {},
        }
        for slot in initial.day_slots
    }

    def formal_write_counts() -> dict[str, int]:
        with open_db() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "agent_plan_proposals",
                    "itinerary_versions",
                    "itinerary_patches",
                    "route_options",
                )
            }

    before_writes = formal_write_counts()
    provider = TraceShapedProvider()
    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="北京",
        transport_mode="public_transit",
        slot_lineage=slot_lineage,
        authoritative_lineage_required=True,
    )
    after_writes = formal_write_counts()

    assert (
        before_writes
        == after_writes
        == {
            "agent_plan_proposals": 0,
            "itinerary_versions": 0,
            "itinerary_patches": 0,
            "route_options": 0,
        }
    )
    assert provider.keywords == [
        "高校参观",
        "餐厅",
        "night view",
        "北京 夜景 观景台",
        "高校参观",
        "北京 高等院校 校区",
    ]
    assert len(provider.keywords) == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert [plan.selected_poi.amap_id if plan.selected_poi else None for plan in plans] == [
        "B0FFF0EFZY",
        "B000MEAL01",
        "B0JDPFC6DW",
        "B000A7O5PK",
    ]
    alternatives = [
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "safe_alternative"
    ]
    assert [event["detail"] for event in alternatives] == ["北京 夜景 观景台", "北京 高等院校 校区"]
    campus_primary = next(
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and event.get("detail") == "高校参观"
        and (event.get("metadata") or {}).get("cacheHit") is True
    )
    assert campus_primary["metadata"]["selectedAmapId"] is None
    assert campus_primary["metadata"]["budgetAfter"] == {"poiSearchRemaining": 1}


def test_safe_alternative_query_reuses_remaining_admitted_candidates_for_later_occurrence() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    class CandidateUniverseProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            if len(self.keywords) == 1:
                rows = [("B000A6EA36", "清华大学", "双清路30号", 116.326)]
                cache_hit = True
            else:
                rows = [
                    ("B000A7O5PK", "北京大学", "颐和园路5号", 116.310),
                    ("B000A7BD6X", "中国人民大学", "中关村大街59号", 116.318),
                ]
                cache_hit = False
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=cache_hit,
                pois=[
                    MapPoiResponse(
                        id=amap_id,
                        name=name,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=address,
                        longitude=longitude,
                        latitude=39.99,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                    for amap_id, name, address, longitude in rows
                ],
            )

    raw = {
        "reply": "两个高校 occurrence",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": f"day{day}_campus",
                "dayNumber": day,
                "date": f"2026-10-0{day}",
                "timeWindow": "morning",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
            }
            for day in (1, 2)
        ],
        "intentPools": [
            {
                "poolId": "campus_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 2,
                "requirementLevel": "required",
                "assignToSlots": ["day1_campus", "day2_campus"],
                "candidateHints": ["北京高校", "北京高校"],
            }
        ],
    }
    lineage = {
        f"day{day}_campus": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": f"occ:goal_campus_visit:day:{day}",
            "poolId": "campus_pool",
            "planningSlotId": f"day{day}_campus",
            "dayNumber": day,
            "requirementLevel": "hard" if day == 1 else "explicit_soft",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        }
        for day in (1, 2)
    }
    provider = CandidateUniverseProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        excluded_physical_aliases={"amap:B000A6EA36"},
    )

    assert len(provider.keywords) == 2
    assert len(provider.keywords) <= MAX_SIMPLE_OPEN_POI_SEARCHES
    assert provider.keywords[0] != provider.keywords[1]
    assert [plan.selected_poi.amap_id for plan in plans] == ["B000A7O5PK", "B000A7BD6X"]
    assert [plan.occurrence_id for plan in plans] == [
        "occ:goal_campus_visit:day:1",
        "occ:goal_campus_visit:day:2",
    ]
    pool_reuse = [event for event in events if event["type"] == "simple_open_candidate_pool_reused"]
    assert len(pool_reuse) == 1
    assert pool_reuse[0]["metadata"]["providerCalled"] is False
    assert pool_reuse[0]["metadata"]["remainingSearchBudget"] == MAX_SIMPLE_OPEN_POI_SEARCHES - 2
    assert pool_reuse[0]["metadata"]["selectedAmapId"] == "B000A7BD6X"


@pytest.mark.parametrize(
    "provider_returns_prior",
    [True, False],
    ids=["current_provider_page", "persisted_prior_proposal"],
)
def test_required_non_exact_slot_reuses_prior_identity_when_novel_candidate_frontier_is_exhausted(
    provider_returns_prior: bool,
) -> None:
    """Novelty may vary soft slots, but must never erase a required anchor."""

    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.models.poi import POI
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    prior_id = "B000PRIOR1"

    class PriorOnlyProvider:
        @staticmethod
        def search(city, keyword, category="all", limit=5):
            del limit
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=(
                    [
                        MapPoiResponse(
                            id=prior_id,
                            name="测试大学",
                            type="科教文化服务;学校;高等院校",
                            city=city,
                            district="测试区",
                            address="测试路1号",
                            longitude=116.31,
                            latitude=39.99,
                            category=category,
                            source="amap-place-search",
                            sourceNote="provider",
                            confidence=1.0,
                        )
                    ]
                    if provider_returns_prior
                    else []
                ),
            )

    initial_plan = AgentInitialPlanOutput.model_validate(
        {
            "reply": "required anchor",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "day1_campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                }
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "北京",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["day1_campus"],
                    "candidateHints": ["高校参观"],
                    "entityBindingMode": "category",
                }
            ],
        }
    )
    lineage = {
        "day1_campus": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": "occ:goal_campus_visit:day:1",
            "poolId": "campus_pool",
            "planningSlotId": "day1_campus",
            "dayNumber": 1,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        }
    }

    plans, events = SimpleOpenItineraryExecutor(PriorOnlyProvider()).build_segment_plans(
        initial_plan,
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        excluded_physical_aliases={f"amap:{prior_id}"},
        prior_required_candidates_by_occurrence={
            "occ:goal_campus_visit:day:1": [
                POI(
                    id="poi_prior",
                    amap_id=prior_id,
                    name="测试大学",
                    city="北京",
                    category="campus",
                    type="科教文化服务;学校;高等院校",
                    district="测试区",
                    address="测试路1号",
                    longitude=116.31,
                    latitude=39.99,
                    source="amap-place-search",
                    confidence=1.0,
                )
            ]
        },
    )

    assert len(plans) == 1
    assert plans[0].selected_poi is not None
    assert plans[0].selected_poi.amap_id == prior_id
    reuse = [event for event in events if event["type"] == "simple_open_required_identity_reused"]
    assert len(reuse) == 1
    assert reuse[0]["metadata"]["selectedAmapId"] == prior_id
    assert reuse[0]["metadata"]["noveltySatisfiedByThisReuse"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"source": "agent-text-timeline"},
        {"amap_id": "invalid-amap-id"},
        {"latitude": float("nan")},
        {"latitude": 999.0},
        {"latitude": 0.0},
    ],
    ids=["source", "amap_id", "nan", "out_of_range", "zero_coordinate"],
)
def test_required_prior_candidate_fallback_rejects_invalid_amap_material(override: dict) -> None:
    from src.models.poi import POI
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    material = {
        "id": "poi_prior",
        "amap_id": "B000PRIOR1",
        "name": "测试大学",
        "city": "北京",
        "category": "campus",
        "latitude": 39.99,
        "longitude": 116.31,
        "source": "amap-place-search",
        **override,
    }
    candidate = POI(**material)

    selected = SimpleOpenItineraryExecutor._required_prior_candidate_fallback(
        [candidate],
        prior_identity_ids={str(candidate.amap_id or "").upper()},
        prior_physical_keys=set(),
        current_plans=[],
    )

    assert selected is None


def test_primary_query_reuses_remaining_admitted_candidates_without_another_provider_call() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class TwoCampusProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=True,
                pois=[
                    MapPoiResponse(
                        id=amap_id,
                        name=name,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=address,
                        longitude=longitude,
                        latitude=39.99,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                    for amap_id, name, address, longitude in (
                        ("B000A7O5PK", "北京大学", "颐和园路5号", 116.310),
                        ("B000A7BD6X", "中国人民大学", "中关村大街59号", 116.318),
                    )
                ],
            )

    raw = {
        "reply": "两个高校 occurrence",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": f"day{day}_campus",
                "dayNumber": day,
                "date": f"2026-10-0{day}",
                "timeWindow": "",
                "startTime": "",
                "durationMinutes": 0,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
            }
            for day in (1, 2)
        ],
        "intentPools": [
            {
                "poolId": "campus_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 2,
                "requirementLevel": "required",
                "assignToSlots": ["day1_campus", "day2_campus"],
                "candidateHints": ["北京高校", "北京高校"],
            }
        ],
    }
    lineage = {
        f"day{day}_campus": {
            "goalId": "goal_campus_visit",
            "sourceGoalId": "goal_campus_visit",
            "occurrenceId": f"occ:goal_campus_visit:day:{day}",
            "poolId": "campus_pool",
            "planningSlotId": f"day{day}_campus",
            "dayNumber": day,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        }
        for day in (1, 2)
    }
    provider = TwoCampusProvider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
    )

    assert provider.keywords == ["北京高校"]
    assert [plan.selected_poi.amap_id for plan in plans] == ["B000A7O5PK", "B000A7BD6X"]
    reuse = [event for event in events if event["type"] == "simple_open_candidate_pool_reused"]
    assert len(reuse) == 1
    assert reuse[0]["metadata"]["queryRole"] == "primary_candidate_pool_reuse"


def test_candidate_pool_reuse_rechecks_the_later_occurrence_exact_entity_policy() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class AlternativeUniverseProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            rows = (
                [("B000OLD001", "既有高校", "旧址", 116.300)]
                if len(self.keywords) == 1
                else [
                    ("B000A7O5PK", "北京大学", "颐和园路5号", 116.310),
                    ("B000A7BD6X", "中国人民大学", "中关村大街59号", 116.318),
                ]
            )
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=len(self.keywords) == 1,
                pois=[
                    MapPoiResponse(
                        id=amap_id,
                        name=name,
                        type="科教文化服务;学校;高等院校",
                        city=city,
                        district="海淀区",
                        address=address,
                        longitude=longitude,
                        latitude=39.99,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                    for amap_id, name, address, longitude in rows
                ],
            )

    raw = {
        "reply": "泛化高校与指定高校 occurrence",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "day1_generic",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "",
                "startTime": "",
                "durationMinutes": 0,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
            },
            {
                "slotId": "day2_exact",
                "dayNumber": 2,
                "date": "2026-10-02",
                "timeWindow": "",
                "startTime": "",
                "durationMinutes": 0,
                "kind": "campus",
                "rawNeed": "北京大学",
                "routeAnchor": True,
            },
        ],
        "intentPools": [
            {
                "poolId": "generic_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "requirementLevel": "required",
                "assignToSlots": ["day1_generic"],
                "candidateHints": ["高校参观"],
            },
            {
                "poolId": "exact_pool",
                "rawNeed": "北京大学",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "requirementLevel": "required",
                "assignToSlots": ["day2_exact"],
                "candidateHints": ["北京大学"],
                "entityBindingMode": "exact_entity",
                "exactEntity": "北京大学",
            },
        ],
    }
    lineage = {
        slot_id: {
            "goalId": goal_id,
            "sourceGoalId": goal_id,
            "occurrenceId": f"occ:{goal_id}:day:{day}",
            "poolId": pool_id,
            "planningSlotId": slot_id,
            "dayNumber": day,
            "requirementLevel": "hard",
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        }
        for slot_id, goal_id, pool_id, day in (
            ("day1_generic", "goal_generic", "generic_pool", 1),
            ("day2_exact", "goal_pku", "exact_pool", 2),
        )
    }
    provider = AlternativeUniverseProvider()

    plans, _events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        excluded_physical_aliases={"amap:B000OLD001"},
    )

    assert plans[0].selected_poi is not None
    assert plans[0].selected_poi.name == "北京大学"
    assert plans[1].selected_poi is None
    assert plans[1].notes


def test_trace_shaped_second_direction_excludes_prior_physical_identities_before_admission() -> None:
    """A cached A universe must not be admitted as a physically new B direction."""

    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import (
        MAX_SIMPLE_OPEN_POI_SEARCHES,
        SimpleOpenItineraryExecutor,
    )

    prior_ids = {
        "B000A6EA36",
        "B0J1CRCYMW",
        "B0G2J5IJ9U",
        "B0FFG967HI",
    }
    replacement_by_query = {
        "北京 高等院校 校区": (
            "B000NEWC01",
            "北京理工大学",
            "科教文化服务;学校;高等院校",
        ),
        "餐厅": (
            "B000NEWM01",
            "京味小馆",
            "餐饮服务;中餐厅;北京菜",
        ),
        "北京 公共城市夜景空间 夜景": (
            "B000NEWN01",
            "北京城市观景台",
            "风景名胜;风景名胜;风景名胜",
        ),
        "北京外国语大学": (
            "B000NEWC02",
            "北京语言大学",
            "科教文化服务;学校;高等院校",
        ),
        "北京 地方风味餐厅": (
            "B000NEWM02",
            "北京风味餐厅",
            "餐饮服务;中餐厅;北京菜",
        ),
    }
    prior_by_query = {
        "中央财经大学": ("B000A6EA36", "中央财经大学", "科教文化服务;学校;高等院校"),
        "餐厅": ("B0J1CRCYMW", "再疆胡", "餐饮服务;中餐厅;清真菜馆"),
        "北京外国语大学": ("B0G2J5IJ9U", "北京外国语大学", "科教文化服务;学校;高等院校"),
        "北京 地方风味餐厅": ("B0FFG967HI", "揽月斋", "餐饮服务;中餐厅;清真菜馆"),
    }

    class TraceShapedCachedProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []
            self.restaurant_query_count = 0

        @staticmethod
        def poi(identity: str, name: str, provider_type: str, ordinal: int) -> MapPoiResponse:
            return MapPoiResponse(
                id=identity,
                name=name,
                type=provider_type,
                city="北京",
                district="海淀区",
                address=f"测试地址 {identity}",
                longitude=116.30 + (sum(ord(char) for char in identity) % 100) / 1000,
                latitude=39.90 + (sum(ord(char) for char in identity[::-1]) % 100) / 1000,
                category="all",
                source="amap-place-search",
                sourceNote="recorded live-shaped provider result",
                confidence=1.0,
                providerTypeCode="050111" if "北京菜" in provider_type else None,
                openTimeToday="10:00-22:00" if "北京菜" in provider_type else None,
                businessStatus="营业中" if "北京菜" in provider_type else None,
                providerQueriedAt=datetime.now(timezone.utc) if "北京菜" in provider_type else None,
                providerQueryReceiptFingerprint="f" * 64 if "北京菜" in provider_type else None,
                tags=(
                    ["北京菜", "京味小吃"]
                    if identity == "B000NEWM01"
                    else (["北京菜", "传统面食"] if identity == "B000NEWM02" else ["北京菜", "地方风味"])
                    if "北京菜" in provider_type
                    else []
                ),
                sourceClaims=(
                    [
                        {
                            "claimKey": "local_food",
                            "stance": "support",
                            "locality": "北京",
                            "evidenceSource": "provider_city_specific_fact",
                        }
                    ]
                    if "北京菜" in provider_type
                    else []
                ),
            )

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            if keyword == "餐厅":
                self.restaurant_query_count += 1
            candidates: list[MapPoiResponse] = []
            if keyword in prior_by_query:
                candidates.append(self.poi(*prior_by_query[keyword], 1))
            if keyword == "北京 公共城市夜景空间 夜景":
                candidates.append(self.poi("B000PHOTO1", "夜景摄影工作室", "生活服务;摄影冲印店;摄影冲印", 2))
            replacement = replacement_by_query.get(keyword)
            if keyword == "餐厅" and self.restaurant_query_count > 1:
                replacement = (
                    "B000NEWM02",
                    "全聚德烤鸭店(北京测试二店)",
                    "餐饮服务;中餐厅;北京菜",
                )
            if replacement is not None:
                candidates.append(self.poi(*replacement, 3))
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                cacheHit=keyword != "北京 高等院校 校区",
                pois=candidates,
            )

    slots = [
        ("day1_campus", 1, "09:00", "campus", "中央财经大学", "campus_visit", "hard"),
        ("day1_meal", 1, "12:00", "meal", "北京 当地特色餐厅", "meal", "explicit_soft"),
        (
            "day1_night",
            1,
            "19:00",
            "night_view",
            "北京 公共城市夜景空间 夜景",
            "night_view",
            "hard",
        ),
        ("day2_campus", 2, "09:00", "campus", "北京外国语大学", "campus_visit", "explicit_soft"),
        ("day2_meal", 2, "12:00", "meal", "北京 地方风味餐厅", "meal", "explicit_soft"),
    ]
    raw = {
        "reply": "trace-shaped second direction",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": slot_id,
                "dayNumber": day,
                "date": f"2026-10-0{day}",
                "timeWindow": f"{start}-22:00",
                "startTime": start,
                "durationMinutes": 90,
                "kind": kind,
                "rawNeed": query,
                "routeAnchor": True,
            }
            for slot_id, day, start, kind, query, _intent, _level in slots
        ],
        "intentPools": [
            {
                "poolId": f"pool_{slot_id}",
                "rawNeed": query,
                "city": "北京",
                "intentType": intent,
                "targetCount": 1,
                "requirementLevel": "required" if level == "hard" else "optional",
                "assignToSlots": [slot_id],
                "candidateHints": [query],
            }
            for slot_id, _day, _start, _kind, query, intent, level in slots
        ],
    }
    lineage = {
        slot_id: {
            "goalId": f"goal_{intent}",
            "sourceGoalId": f"goal_{intent}",
            "occurrenceId": f"occ:goal_{intent}:day:{day}",
            "poolId": f"pool_{slot_id}",
            "planningSlotId": slot_id,
            "dayNumber": day,
            "requirementLevel": level,
            "lineageAuthority": "goal_occurrence_compiler",
            "futureRouteAnchor": True,
            "routeAnchorExpected": True,
        }
        for slot_id, day, _start, _kind, _query, intent, level in slots
    }
    provider = TraceShapedCachedProvider()
    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
        slot_lineage=lineage,
        authoritative_lineage_required=True,
        excluded_physical_aliases={f"amap:{identity}" for identity in prior_ids},
    )

    selected_ids = [plan.selected_poi.amap_id for plan in plans if plan.selected_poi is not None]
    assert len(selected_ids) == 5
    assert prior_ids.isdisjoint(selected_ids)
    assert len(provider.keywords) == MAX_SIMPLE_OPEN_POI_SEARCHES
    assert provider.keywords == [
        "中央财经大学",
        "北京 高等院校 校区",
        "餐厅",
        "北京 公共城市夜景空间 夜景",
        "北京外国语大学",
        "餐厅",
    ]
    alternatives = [
        event
        for event in events
        if event["type"] == "simple_open_tool_call"
        and (event.get("metadata") or {}).get("queryRole") == "safe_alternative"
    ]
    assert len(alternatives) == 1
    assert alternatives[0]["metadata"]["queryFingerprint"] != alternatives[0]["metadata"]["primaryQueryFingerprint"]
    assert alternatives[0]["metadata"]["budgetBefore"] == {"poiSearchRemaining": 5}
    assert alternatives[0]["metadata"]["budgetAfter"] == {"poiSearchRemaining": 4}


def test_simple_open_rejects_distinct_ids_for_same_physical_place() -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class DuplicatePhysicalMapProvider:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, city, keyword, category="all", limit=5):
            del keyword, limit
            self.calls += 1
            item = MapPoiResponse(
                id=f"B{self.calls:011d}",
                name="清华大学",
                type="科教文化服务;学校;高等院校",
                city=city,
                district="海淀区",
                address="同一地址",
                longitude=116.3,
                latitude=40.0,
                category=category,
                source="amap-place-search",
                sourceNote="provider",
                confidence=1.0,
            )
            return MapPoiSearchResponse(
                city=city,
                keyword="same-place",
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[item],
            )

    raw = two_day_initial_day_slot_output()
    raw["daySlots"] = [
        {**raw["daySlots"][0], "slotId": "same-1"},
        {**raw["daySlots"][0], "slotId": "same-2", "startTime": "14:00", "timeWindow": "14:00-16:00"},
    ]
    raw["intentPools"] = [
        {
            **raw["intentPools"][0],
            "assignToSlots": ["same-1", "same-2"],
            "candidateHints": ["同一地点"],
        }
    ]

    plans, _events = SimpleOpenItineraryExecutor(DuplicatePhysicalMapProvider()).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert plans[0].selected_poi is not None
    assert plans[1].selected_poi is None
    assert plans[1].grounding_status == "unresolved"


@pytest.mark.parametrize(
    ("intent_type", "kind", "candidate_type", "candidate_name"),
    [
        ("night_view", "night_view", "餐饮服务;中餐厅", "北京某餐厅"),
        ("night_view", "night_view", "生活服务;摄影冲印店;摄影冲印", "三川影像婚纱摄影(北京店)"),
        ("meal", "meal", "风景名胜;旅游景点", "北京某景点"),
        ("campus_visit", "campus", "餐饮服务;中餐厅", "北京某餐厅"),
        ("park", "visit", "餐饮服务;中餐厅", "北京某餐厅"),
    ],
)
def test_simple_open_rejects_provider_type_conflicting_with_intent(
    intent_type: str, kind: str, candidate_type: str, candidate_name: str
) -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class ConflictingTypeMapProvider:
        def search(self, city, keyword, category="all", limit=5):
            del keyword, limit
            return MapPoiSearchResponse(
                city=city,
                keyword="conflicting-type",
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000000001",
                        name=candidate_name,
                        type=candidate_type,
                        city=city,
                        district="测试区",
                        address="测试地址",
                        longitude=116.3,
                        latitude=40.0,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    raw = two_day_initial_day_slot_output()
    raw["daySlots"] = [{**raw["daySlots"][0], "slotId": "conflict", "kind": kind}]
    raw["intentPools"] = [
        {
            **raw["intentPools"][0],
            "intentType": intent_type,
            "assignToSlots": ["conflict"],
            "candidateHints": ["冲突候选"],
        }
    ]

    plans, _events = SimpleOpenItineraryExecutor(ConflictingTypeMapProvider()).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert plans[0].selected_poi is None
    assert plans[0].grounding_status == "unresolved"
    assert "类型" in plans[0].notes


def test_simple_open_keeps_public_scenic_night_candidate_provisional() -> None:
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    assert SimpleOpenItineraryExecutor._candidate_type_matches_intent(
        {
            "id": "B000A7O5PK",
            "amapId": "B000A7O5PK",
            "name": "什刹海",
            "type": "风景名胜;风景名胜;国家级景点",
            "city": "北京",
            "source": "amap-place-search",
            "latitude": 39.941,
            "longitude": 116.388,
        },
        "night_view",
    )


def test_simple_open_replaces_photography_theme_hint_before_first_night_search() -> None:
    from backend.tests.unit.test_agent_service import two_day_initial_day_slot_output
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class RecordingMapProvider:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def search(self, city, keyword, category="all", limit=5):
            del limit
            self.keywords.append(keyword)
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[],
            )

    raw = two_day_initial_day_slot_output()
    raw["daySlots"] = [
        {
            **raw["daySlots"][0],
            "slotId": "night-photo-hint",
            "kind": "night_view",
            "rawNeed": "晚上看城市夜景",
        }
    ]
    raw["intentPools"] = [
        {
            **raw["intentPools"][0],
            "intentType": "night_view",
            "assignToSlots": ["night-photo-hint"],
            "candidateHints": ["北京 摄影"],
        }
    ]
    provider = RecordingMapProvider()

    SimpleOpenItineraryExecutor(provider).build_segment_plans(
        AgentInitialPlanOutput.model_validate(raw),
        city="北京",
        transport_mode="public_transit",
    )

    assert provider.keywords == ["北京 夜景 观景台"]
    assert "摄影" not in provider.keywords[0]


def test_simple_open_cannot_resume_strict_waiting_initial_plan() -> None:
    from backend.tests.unit.test_agent_service import FakeProvider, clear_database, open_db
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "profile-isolated waiting plan")
        service = AgentService(connection, provider=FakeProvider({}))
        strict_turn = service._insert_turn(
            session.session_id,
            "assistant",
            "strict waiting",
            "active",
            agent_response_json={
                "mode": "staged_initial_pipeline_waiting",
                "initialPlan": {"mode": "day_slots", "daySlots": [], "intentPools": []},
                "pipelineContext": {
                    "serverExecutionProfile": "strict_portfolio",
                    "actualExecutionRoute": "staged_initial_pipeline",
                },
            },
        )
        simple_turn = service._insert_turn(
            session.session_id,
            "assistant",
            "simple waiting",
            "active",
            agent_response_json={
                "mode": "staged_initial_pipeline_waiting",
                "initialPlan": {"mode": "day_slots", "daySlots": [], "intentPools": []},
                "pipelineContext": {
                    "serverExecutionProfile": "simple_open_v1",
                    "actualExecutionRoute": "simple_open_initial_pipeline",
                },
            },
        )
        connection.commit()

        strict_result = service._last_waiting_staged_plan(
            session.session_id,
            strict_turn,
            expected_execution_profile="simple_open_v1",
        )
        simple_result = service._last_waiting_staged_plan(
            session.session_id,
            simple_turn,
            expected_execution_profile="simple_open_v1",
        )

    assert strict_result is None
    assert simple_result is not None
    assert simple_result["pipelineContext"]["serverExecutionProfile"] == "simple_open_v1"


@pytest.mark.parametrize(
    ("route_provider_fails", "persisted_reload_corruption"),
    [(False, False), (True, False), (False, True)],
    ids=["route_ready", "route_provider_failed", "persisted_reload_corruption"],
)
def test_simple_open_golden_fixture_persists_one_editable_two_day_itinerary(
    monkeypatch, route_provider_fails: bool, persisted_reload_corruption: bool
) -> None:
    """A direction stays zero-write until its opaque confirm capability is used."""
    from backend.tests.unit.test_agent_service import (
        FakePoiResolver,
        StagedInitialProvider,
        clear_database,
        fake_amap_search_nearby_route_compatible,
        fake_amap_search_with_keyword_candidate,
        open_db,
        two_day_initial_day_slot_output,
    )
    from fastapi import HTTPException
    from src.api.schemas.agent import AgentMessageRequest
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService
    from src.services.itinerary_patch_service import ItineraryPatchService
    from src.services.itinerary_service import ItineraryService
    from src.services.map_poi_service import MapPoiService
    from src.services.route_service import RouteService
    from src.models.route_option import RouteOption

    clear_database()
    search_index = 0
    nearby_search_count = 0
    meal_nearby_search_count = 0

    def recorded_amap_search(*args, **kwargs):
        nonlocal search_index
        search_index += 1
        result = fake_amap_search_with_keyword_candidate(*args, **kwargs)
        result = result.model_copy(
            update={
                "pois": [
                    poi.model_copy(
                        update={"category": "food" if "餐饮服务" in str(poi.type or "") else poi.category}
                    )
                    for poi in result.pois
                ]
            }
        )
        return result.model_copy(
            update={
                "providerName": "amap-place-search",
                "pois": [
                    poi.model_copy(
                        update={
                            "id": f"B{search_index * 100 + index:011d}",
                            "source": "amap-place-search",
                            "type": "餐饮服务;中餐厅;北京菜"
                            if str(poi.category or "") == "food"
                            else poi.type,
                            "name": (
                                "京味小吃坊(搜索测试店)"
                                if search_index % 2
                                else "老北京面馆(搜索测试店)"
                            )
                            if str(poi.category or "") == "food"
                            else poi.name,
                            "name": (
                                "京味小吃坊(搜索测试店)"
                                if search_index % 2
                                else "老北京面馆(搜索测试店)"
                            )
                            if str(poi.category or "") == "food"
                            else poi.name,
                            "address": f"北京测试路{search_index}号"
                            if str(poi.category or "") == "food"
                            else poi.address,
                            "longitude": float(poi.longitude) + search_index / 1000
                            if str(poi.category or "") == "food"
                            else poi.longitude,
                            "latitude": float(poi.latitude) + search_index / 1000
                            if str(poi.category or "") == "food"
                            else poi.latitude,
                            "provider_type_code": "050111"
                            if str(poi.category or "") == "food"
                            else poi.provider_type_code,
                            "tags": ["北京菜", "京味小吃" if search_index % 2 else "传统面食"]
                            if str(poi.category or "") == "food"
                            else poi.tags,
                            "source_claims": [
                                {
                                    "claimKey": "local_food",
                                    "stance": "support",
                                    "locality": "北京",
                                    "evidenceSource": "provider_city_specific_fact",
                                }
                            ]
                            if str(poi.category or "") == "food"
                            else poi.source_claims,
                            "open_time_today": "10:00-22:00"
                            if str(poi.category or "") == "food"
                            else poi.open_time_today,
                            "business_status": "营业中"
                            if str(poi.category or "") == "food"
                            else poi.business_status,
                            "provider_queried_at": datetime.now(timezone.utc),
                            "provider_query_receipt_fingerprint": "f" * 64,
                        }
                    )
                    for index, poi in enumerate(result.pois, start=1)
                ],
            }
        )

    def recorded_amap_nearby(*args, **kwargs):
        nonlocal meal_nearby_search_count, nearby_search_count
        nearby_search_count += 1
        result = fake_amap_search_nearby_route_compatible(*args, **kwargs)
        if str(kwargs.get("provider_types") or "") == "北京菜":
            meal_nearby_search_count += 1
            result = result.model_copy(
                update={
                    "pois": [
                        poi.model_copy(
                            update={
                                "category": "food",
                                "type": "餐饮服务;中餐厅;北京菜",
                                "name": (
                                    "京味小吃坊(路线测试店)"
                                    if meal_nearby_search_count % 2
                                    else "老北京面馆(路线测试店)"
                                ),
                                "address": f"北京路线附近{nearby_search_count}号",
                                "longitude": float(poi.longitude) + nearby_search_count / 10000,
                                "latitude": float(poi.latitude) + nearby_search_count / 10000,
                            }
                        )
                        for poi in result.pois
                    ]
                }
            )
        elif re.search(r"(?:夜景|观景|灯光|奥林匹克塔|中信大厦)", str(result.keyword or "")):
            result = result.model_copy(
                update={
                    "pois": [
                        poi.model_copy(
                            update={
                                "name": f"城市公共观景台·{nearby_search_count}",
                                "type": "风景名胜;观景点",
                            }
                        )
                        for poi in result.pois
                    ]
                }
            )
        elif any("公园广场;公园" in str(poi.type or "") for poi in result.pois):
            result = result.model_copy(
                update={
                    "pois": [
                        poi.model_copy(
                            update={
                                "name": f"城市公共公园·{nearby_search_count}",
                                "type": "风景名胜;公园广场;公园",
                            }
                        )
                        for poi in result.pois
                    ]
                }
            )
        return result.model_copy(
            update={
                "providerName": "amap-place-search",
                # Preserve the fixture's query/anchor-specific canonical ID;
                # replacing it with B101 on every call would make distinct
                # meal/park slots look like the same physical POI.  The
                # production admission contract still requires an AMap-shaped
                # canonical ID, so derive one from the fixture evidence.
                "pois": [
                    poi.model_copy(
                        update={
                            "id": "B"
                            + hashlib.sha256(
                                "|".join(
                                    map(
                                        str,
                                        (result.keyword, poi.name, poi.longitude, poi.latitude),
                                    )
                                ).encode("utf-8")
                            )
                            .hexdigest()[:12]
                            .upper(),
                            "source": "amap-place-search",
                            "provider_type_code": "050111"
                            if str(poi.category or "") == "food"
                            else (
                                poi.provider_type_code
                                or ("110101" if "公园广场;公园" in str(poi.type or "") else None)
                            ),
                            "tags": [
                                "北京菜",
                                "京味小吃" if meal_nearby_search_count % 2 else "传统面食",
                            ]
                            if str(poi.category or "") == "food"
                            else poi.tags,
                            "source_claims": [
                                {
                                    "claimKey": "local_food",
                                    "stance": "support",
                                    "locality": "北京",
                                    "evidenceSource": "provider_city_specific_fact",
                                }
                            ]
                            if str(poi.category or "") == "food"
                            else poi.source_claims,
                            "open_time_today": "10:00-22:00"
                            if str(poi.category or "") == "food"
                            else poi.open_time_today,
                            "business_status": "营业中"
                            if str(poi.category or "") == "food"
                            else poi.business_status,
                            "provider_queried_at": datetime.now(timezone.utc),
                            "provider_query_receipt_fingerprint": "f" * 64,
                        }
                    )
                    for poi in result.pois
                ],
            }
        )

    monkeypatch.setattr(MapPoiService, "search", recorded_amap_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", recorded_amap_nearby)
    route_attempt_count = 0

    def fail_route_provider(*_args, **_kwargs):
        nonlocal route_attempt_count
        route_attempt_count += 1
        raise RuntimeError("recorded route provider outage")

    def recorded_build_routes(
        _self,
        plan_id,
        pois,
        transport_mode="transit",
        segments=None,
        route_pairs=None,
        **_kwargs,
    ):
        nonlocal route_attempt_count
        route_attempt_count += 1
        segment_by_id = {segment.id: segment for segment in segments or []}
        poi_by_id = {poi.id: poi for poi in pois or []}
        pairs = list(route_pairs or [])
        if not pairs:
            pairs = [
                (left.id, right.id)
                for left, right, _left_poi, _right_poi in _self._route_groups(
                    list(pois or []),
                    list(segments or []),
                    allow_semantic_route_anchors=bool(_kwargs.get("allow_semantic_route_anchors")),
                )
                if left is not None and right is not None
            ]
        routes = []
        for index, (left_id, right_id) in enumerate(pairs):
            left = segment_by_id[left_id]
            right = segment_by_id[right_id]
            left_poi = poi_by_id[left.poi_id]
            right_poi = poi_by_id[right.poi_id]
            routes.append(
                RouteOption(
                    id=f"route_simple_open_{index}_{left_id}_{right_id}",
                    plan_id=plan_id,
                    from_segment_id=left_id,
                    to_segment_id=right_id,
                    from_poi_id=left.poi_id,
                    to_poi_id=right.poi_id,
                    distance_meters=1200,
                    duration_seconds=900,
                    mode=transport_mode,
                    provider="amap-webservice",
                    is_selected=True,
                    polyline=[
                        [left_poi.longitude, left_poi.latitude],
                        [right_poi.longitude, right_poi.latitude],
                    ],
                    steps=[],
                    provider_payload={"fixture": "simple_open_golden"},
                    queried_at=datetime.now(timezone.utc),
                )
            )
        return routes

    monkeypatch.setattr(
        RouteService,
        "build_routes",
        fail_route_provider if route_provider_fails else recorded_build_routes,
    )
    provider = StagedInitialProvider(two_day_initial_day_slot_output())

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple open golden")
        service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
        service.initial_planning_mode = "simple_open_v1"
        proposal_response = service.send_message(
            session.session_id,
            AgentMessageRequest(content=(GOLDEN_INPUT)),
        )
        # GOLDEN_INPUT intentionally states public transport but no detour
        # tolerance.  The route contract must be completed through opaque
        # server choices before either POI grounding or proposal persistence.
        for _ in range(3):
            if any(
                item.get("action") == "select_plan_proposal" for item in proposal_response.assistant_turn.choice_options
            ):
                break
            clarification = next(
                (
                    item
                    for item in proposal_response.assistant_turn.choice_options
                    if item.get("action") in {"continue_clarification", "submit_clarification_batch"}
                ),
                None,
            )
            if clarification is None and route_provider_fails:
                break
            assert clarification is not None, {
                "content": proposal_response.assistant_turn.content,
                "terminalStatus": proposal_response.terminal_status,
                "choiceOptions": proposal_response.assistant_turn.choice_options,
                "assistantTurn": proposal_response.assistant_turn.model_dump(),
            }
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM agent_plan_proposals",
                ).fetchone()[0]
                == 0
            )
            assert route_attempt_count == 0
            assert provider.calls == 0
            assert search_index == 0
            selected_choice = {
                "sourceAssistantTurnId": proposal_response.assistant_turn.id,
                "choiceId": clarification["id"],
            }
            if clarification.get("action") == "submit_clarification_batch":
                selected_choice["batchSelections"] = [
                    {
                        "dimensionId": question["dimensionId"],
                        "optionId": question["options"][0]["id"],
                    }
                    for question in (proposal_response.assistant_turn.clarification_checkpoint or {}).get(
                        "questions", []
                    )
                ]
            proposal_response = service.send_message(
                session.session_id,
                AgentMessageRequest(
                    content=str(clarification.get("label") or "按推荐偏好"),
                    context={"selectedAgentChoice": selected_choice},
                ),
            )
        if route_provider_fails:
            assert not any(
                item.get("action") == "select_plan_proposal" for item in proposal_response.assistant_turn.choice_options
            )
            assert proposal_response.terminal_status == "candidate_refresh_required"
            assert proposal_response.assistant_turn.comparison_projections
            assert proposal_response.assistant_turn.comparison_projections[0]["adoptionReady"] is False
            # Coverage-first route assignment gives each planned day one
            # bounded first-topology attempt before spending budget on same-day
            # alternatives. A provider-wide outage therefore reaches both days
            # and still leaves the proposal fail-closed and zero-write.
            assert route_attempt_count == 2
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert connection.execute("SELECT COUNT(*) FROM route_options").fetchone()[0] == 0
            return

        assert any(
            item.get("action") == "select_plan_proposal" for item in proposal_response.assistant_turn.choice_options
        ), "route clarification did not reach a persisted Simple direction proposal"
        preconfirm_version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        preconfirm_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        preconfirm_route_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
        ).fetchone()[0]
        preconfirm_active_version = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        choice = next(
            item
            for item in proposal_response.assistant_turn.choice_options
            if item.get("action") == "select_plan_proposal"
        )

        assert proposal_response.terminal_status == "needs_confirmation"
        assert proposal_response.version is None
        assert preconfirm_active_version is None
        assert preconfirm_version_count == 0
        assert preconfirm_patch_count == 0
        assert preconfirm_route_count == 0
        proposal_snapshot = json.loads(
            connection.execute("SELECT snapshot_json FROM agent_plan_proposals").fetchone()[0]
        )
        route_audit = proposal_snapshot["simpleOpenRouteAssignment"]
        assert route_attempt_count == len(route_audit["expectedPairs"])
        assert route_attempt_count <= 4
        assert route_audit["routeCoverageComplete"] is True
        assert provider.calls == 1
        # Only the two day seeds use city-wide search. Both daily meals, the
        # confirmed night slot, and the one server-sealed route-local
        # completion stop use the frozen day-anchor scope. A grounded meal no
        # longer burns a redundant generic-food fallback call.
        assert search_index == 2
        assert nearby_search_count == 4
        daily_completion_segments = [
            segment
            for day in proposal_snapshot["days"]
            for segment in day["segments"]
            if (segment.get("semanticMetadata") or {}).get("lineageAuthority")
            == "simple_open_daily_completion_policy"
        ]
        assert len(daily_completion_segments) == 1
        daily_completion_metadata = daily_completion_segments[0]["semanticMetadata"]
        assert daily_completion_metadata["dayCompletionRequired"] is True
        assert daily_completion_metadata["completionRequired"] is False
        assert daily_completion_metadata["userExplicit"] is False
        assert daily_completion_metadata["groundingStatus"] == "verified_amap"
        assert proposal_response.assistant_turn.comparison_projection_update_mode == "replace"
        assert "尚未写入正式时间轴" in proposal_response.assistant_turn.content
        assert any(
            event.type == "agent_action_outcome"
            and event.metadata.get("resultPreview", {}).get("candidateSummary", {}).get("proposalOnly") is True
            for event in proposal_response.planning_steps
        )

        if persisted_reload_corruption:
            original_apply_patch = ItineraryPatchService.apply_patch

            def persist_then_corrupt_snapshot(writer, *args, **kwargs):
                result = original_apply_patch(writer, *args, **kwargs)
                row = writer.db.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                    (result.version.id,),
                ).fetchone()
                corrupted = json.loads(row["snapshot_json"])
                corrupted["days"] = []
                writer.db.execute(
                    "UPDATE itinerary_versions SET snapshot_json = ? WHERE id = ?",
                    (json.dumps(corrupted, ensure_ascii=False), result.version.id),
                )
                writer.db.commit()
                return result

            monkeypatch.setattr(ItineraryPatchService, "apply_patch", persist_then_corrupt_snapshot)
            with pytest.raises(HTTPException) as exc_info:
                service.send_message(
                    session.session_id,
                    AgentMessageRequest(
                        content="确认编辑",
                        context={
                            "selectedAgentChoice": {
                                "sourceAssistantTurnId": proposal_response.assistant_turn.id,
                                "choiceId": choice["id"],
                            }
                        },
                    ),
                )
            assert exc_info.value.status_code == 409
            assert (
                connection.execute(
                    "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
                ).fetchone()[0]
                is None
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
                ).fetchone()[0]
                == 0
            )
            return

        response = service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="确认编辑",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": proposal_response.assistant_turn.id,
                        "choiceId": choice["id"],
                    }
                },
            ),
        )
        version_rows = connection.execute(
            "SELECT id FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchall()
        patch_rows = connection.execute(
            "SELECT id, result_version_id, validation_status FROM itinerary_patches WHERE session_id = ?",
            (session.session_id,),
        ).fetchall()
        route_write_count = connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (session.active_plan_id,)
        ).fetchone()[0]
        active_version = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]
        assert active_version is not None, json.dumps(
            {
                "terminalStatus": response.terminal_status,
                "failureReason": response.assistant_turn.failure_reason,
                "events": [
                    {"type": event.type, "detail": event.detail, "metadata": event.metadata}
                    for event in response.planning_steps
                    if event.type in {"basic_verifier", "agent_action_outcome", "agent_stop"}
                    or event.type.startswith("simple_open_")
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        persisted_snapshot = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ?", (active_version,)
            ).fetchone()[0]
        )
        persisted_request = connection.execute(
            "SELECT agent_request_json FROM conversation_turns WHERE id = ?", (response.assistant_turn.id,)
        ).fetchone()[0]

        assert response.terminal_status in {
            "success",
            "ready",
            "partial",
            "partial_success",
            "draft_pending_grounding",
            "needs_confirmation",
        }, (
            f"failure={response.assistant_turn.failure_reason}; "
            f"profile={json.loads(persisted_request or '{}').get('serverExecutionProfile')}; "
            f"route={json.loads(persisted_request or '{}').get('actualExecutionRoute')}; "
            f"basic={[event.detail for event in response.planning_steps if event.type == 'basic_verifier']}; "
            f"simple={[event.metadata for event in response.planning_steps if event.type.startswith('simple_open_')]}"
        )
        first_morning_segment = response.itinerary.days[0].segments[0]
        initial_search_count = search_index
        active_choice = next(
            item for item in response.assistant_turn.choice_options if item.get("proposalId") == choice["proposalId"]
        )
        followup_service = AgentService(
            connection,
            provider=GoldenFollowupProvider(response.version.id, first_morning_segment.id),
        )
        followup_service.initial_planning_mode = "simple_open_v1"
        followup = followup_service.send_message(
            session.session_id,
            AgentMessageRequest(
                content="把第一天上午行程提前到八点",
                context={
                    "viewContext": {
                        "schemaVersion": "agent-view-context-v1",
                        "activeView": "overview",
                        "editingProposal": {
                            "planningSelectionRootTurnId": active_choice["planningSelectionRootTurnId"],
                            "rootPortfolioId": active_choice["rootPortfolioId"],
                            "proposalId": active_choice["proposalId"],
                            "sourceAssistantTurnId": response.assistant_turn.id,
                            "activeVersionId": response.version.id,
                        },
                    }
                },
            ),
        )
        total_version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        total_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        followup_patch_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT validation_status, validation_errors_json, operations_json "
                "FROM itinerary_patches WHERE session_id = ? ORDER BY created_at",
                (session.session_id,),
            ).fetchall()
        ]
        followup_turn_row = connection.execute(
            "SELECT agent_request_json, agent_response_json FROM conversation_turns WHERE id = ?",
            (followup.assistant_turn.id,),
        ).fetchone()
        followup_request_payload = json.loads(followup_turn_row["agent_request_json"] or "{}")
        followup_response_payload = json.loads(followup_turn_row["agent_response_json"] or "{}")
        followup_snapshot = (
            json.loads(
                connection.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ?",
                    (followup.version.id,),
                ).fetchone()["snapshot_json"]
            )
            if followup.version is not None
            else {}
        )
    assert response.version is not None
    assert response.itinerary is not None
    assert "已确认该方向并创建可编辑行程" in response.assistant_turn.content
    assert len(response.itinerary.days) == 2
    assert len(version_rows) == 1
    assert len(patch_rows) == 1
    assert patch_rows[0]["validation_status"] == "accepted"
    assert patch_rows[0]["result_version_id"] == response.version.id
    assert active_version == response.version.id
    assert persisted_snapshot["routeDecisionContract"]["schemaVersion"] == "route-decision-contract-v2"
    assert persisted_snapshot["routeDecisionContract"]["status"] == "ready"
    assert persisted_snapshot["routeDecisionContract"]["missingFields"] == []
    assert [
        (
            day["id"],
            [(segment["id"], segment["startTime"]) for segment in day["segments"]],
        )
        for day in persisted_snapshot["days"]
    ] == [
        (
            day.id,
            [(segment.id, segment.start_time) for segment in day.segments],
        )
        for day in response.itinerary.days
    ]
    assert persisted_snapshot["simpleOpenRouteStatus"] == ("provider_failed" if route_provider_fails else "ready")
    assert (route_write_count == 0) if route_provider_fails else (route_write_count > 0)
    assert route_attempt_count >= 1
    assert provider.calls == 1
    assert initial_search_count == 2
    persisted_night_segments = [
        segment
        for day in persisted_snapshot["days"]
        for segment in day["segments"]
        if (segment.get("semanticMetadata") or {}).get("intentType") == "night_view"
    ]
    assert len(persisted_night_segments) == 1
    assert all(
        (segment.get("semanticMetadata") or {}).get("groundingStatus") == "verified_amap"
        for segment in persisted_night_segments
    )
    assert all(
        ((segment.get("semanticMetadata") or {}).get("scheduleDecision") or {}).get("scheduleConfidence")
        == "provisional"
        for segment in persisted_night_segments
    )
    assert all(
        any(
            segment.route_anchor
            and segment.grounding_status == "verified_amap"
            and segment.poi.amap_id
            and segment.poi.latitude is not None
            and segment.poi.longitude is not None
            and segment.poi.source == "amap-place-search"
            for segment in day.segments
        )
        for day in response.itinerary.days
    )
    proposal_event_types = {event.type for event in proposal_response.planning_steps}
    assert {
        "agent_decision",
        "agent_policy_gate",
        "agent_action_outcome",
        "agent_stop",
    } <= proposal_event_types
    assert {
        "simple_direction_activation_verified",
        "proposal_adoption_started",
        "proposal_adoption_committed",
    } <= {event.type for event in response.planning_steps}
    assert followup.version is not None, json.dumps(
        {
            "terminalStatus": followup.terminal_status,
            "failureReason": followup.assistant_turn.failure_reason,
            "assistantContent": followup.assistant_turn.content,
            "choiceOptions": followup.assistant_turn.choice_options,
            "eventTypes": [event.type for event in followup.planning_steps],
        },
        ensure_ascii=False,
        indent=2,
    )
    assert followup.version.id != response.version.id
    assert followup.itinerary.days[0].segments[0].id == first_morning_segment.id
    assert followup.itinerary.days[0].segments[0].start_time == "08:00"
    assert followup_snapshot["routeDecisionContract"].get("status") == "ready", json.dumps(
        {
            "snapshotRouteDecisionContract": followup_snapshot["routeDecisionContract"],
            "requestRouteDecisionContract": (followup_request_payload.get("requestIntentContract") or {}).get(
                "routeDecisionContract"
            ),
            "topLevelRouteDecisionContract": followup_request_payload.get("routeDecisionContract"),
            "activeSimpleProfile": persisted_snapshot.get("simpleOpenExecutionProfile"),
            "activeSimpleRoute": persisted_snapshot.get("simpleOpenExecutionRoute"),
        },
        ensure_ascii=False,
        indent=2,
    )
    assert followup_request_payload["viewResolution"]["resolvedAction"] == "edit_active_direction"
    assert followup_response_payload["viewResolution"]["resolvedAction"] == "edit_active_direction"
    view_event = next(event for event in followup.planning_steps if event.type == "view_context_resolved")
    assert view_event.metadata["resolvedAction"] == "edit_active_direction"
    assert view_event.metadata["inputViewContext"]["activeView"] == "overview"
    assert total_version_count == 2
    assert total_patch_count == 2
    assert json.loads(persisted_request or "{}")["actualExecutionRoute"] == "simple_open_initial_pipeline"


def test_simple_open_all_amap_queries_unavailable_keeps_zero_formal_writes(monkeypatch) -> None:
    from backend.tests.unit.test_agent_service import (
        FakePoiResolver,
        StagedInitialProvider,
        clear_database,
        fake_amap_search_nearby_route_compatible,
        fake_amap_search_with_keyword_candidate,
        open_db,
        two_day_initial_day_slot_output,
    )
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService
    from src.services.map_poi_service import MapPoiService

    clear_database()

    def empty_amap_search(*args, **kwargs):
        result = fake_amap_search_with_keyword_candidate(*args, **kwargs)
        return result.model_copy(update={"providerName": "amap-place-search", "pois": []})

    monkeypatch.setattr(MapPoiService, "search", empty_amap_search)
    provider = StagedInitialProvider(two_day_initial_day_slot_output())

    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple open no poi")
        service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
        service.initial_planning_mode = "simple_open_v1"
        response = _send_after_route_clarification(service, session.session_id, GOLDEN_INPUT)
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        accepted_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND validation_status = 'accepted'",
            (session.session_id,),
        ).fetchone()[0]
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]

    assert response.version is None
    assert response.terminal_status in {"failed", "needs_confirmation"}
    assert version_count == 0
    assert accepted_patch_count == 0
    assert active_version_id is None
    assert provider.calls == 1
    event_types = {event.type for event in response.planning_steps}
    assert {"agent_action_outcome", "agent_stop"} <= event_types


def test_simple_open_park_admission_requires_standalone_provider_evidence() -> None:
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.models.poi import POI
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    anchor = POI(
        id="poi_anchor",
        amap_id="B000000099",
        name="示例大学",
        city="测试",
        category="campus",
        latitude=40.0,
        longitude=116.3,
        source="amap-place-search",
        type="科教文化服务;学校;高等院校",
    )

    class Provider:
        def search_nearby(self, city, longitude, latitude, keyword, **kwargs):
            del longitude, latitude, kwargs

            def poi(poi_id, name, provider_type, *, typecode=None, address="测试路", cpid=None):
                return MapPoiResponse(
                    id=poi_id,
                    name=name,
                    type=provider_type,
                    providerTypeCode=typecode,
                    indoorParentPoiId=cpid,
                    city=f"{city}市",
                    district="测试区",
                    address=address,
                    longitude=116.301,
                    latitude=40.001,
                    category="park",
                    source="amap-place-search",
                    sourceNote="provider",
                    confidence=1.0,
                    providerQueriedAt=datetime.now(timezone.utc),
                    providerQueryReceiptFingerprint="f" * 64,
                )

            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category="park",
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    poi(
                        "B000000001",
                        "校内花园",
                        "风景名胜;公园广场;公园",
                        typecode="110101",
                        address="示例大学内",
                    ),
                    poi("B000000002", "校园旁景观", "风景名胜", typecode="110000"),
                    poi(
                        "B000000003",
                        "独立城市公园",
                        "风景名胜;公园广场;公园",
                        typecode="110101",
                    ),
                ],
            )

    result = SimpleOpenItineraryExecutor(Provider())._search_candidate(
        city="测试",
        query="公园",
        category="park",
        intent_type="park",
        raw_need="独立公共公园",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        nearby_anchor=anchor,
        day_anchor=anchor,
        nearby_radius=5000,
        query_scope_fingerprint="a" * 64,
    )
    _search, selected, _duplicate, _semantic, _remaining, _baseline, diagnostics = result

    assert selected is not None
    assert selected.amap_id == "B000000003"
    assert selected.experience_independence_evidence["status"] == "standalone_verified"
    assert [item["status"] for item in diagnostics] == [
        "embedded_in_day_anchor",
        "independence_pending",
    ]
    assert diagnostics[0]["reasonCodes"] == ["provider_address_inside_day_anchor"]
    assert "standalone_park_category_missing" in diagnostics[1]["reasonCodes"]


def test_simple_open_exact_park_can_be_first_day_anchor_only_with_standalone_type() -> None:
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def __init__(self, provider_type_code: str) -> None:
            self.provider_type_code = provider_type_code

        def search(self, city, *, keyword, category, limit):
            del limit
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000000003",
                        name=keyword,
                        type="风景名胜;公园广场;公园",
                        providerTypeCode=self.provider_type_code,
                        city=f"{city}市",
                        district="测试区",
                        address="测试路",
                        longitude=116.301,
                        latitude=40.001,
                        category="park",
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="f" * 64,
                    )
                ],
            )

    def search(provider_type_code: str):
        return SimpleOpenItineraryExecutor(Provider(provider_type_code))._search_candidate(
            city="测试",
            query="独立城市公园",
            category="park",
            intent_type="park",
            raw_need="独立城市公园",
            exact_entity="独立城市公园",
            optional_experience_family="",
            used_identity_ids=set(),
            used_physical_keys=set(),
            day_anchor=None,
        )

    _search, selected, *_rest, diagnostics = search("110101")
    assert selected is not None
    assert selected.experience_independence_evidence["status"] == "standalone_verified"
    assert selected.experience_independence_evidence["dayAnchorAmapId"] == ""
    assert diagnostics == []

    _search, selected, *_rest, diagnostics = search("110200")
    assert selected is None
    assert diagnostics[0]["status"] == "independence_pending"
    assert "standalone_park_category_missing" in diagnostics[0]["reasonCodes"]
    assert "day_anchor_missing" in diagnostics[0]["reasonCodes"]


def test_simple_open_persists_park_independence_evidence_in_schedule_constraints() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def search(self, city, *, keyword, category, limit):
            del limit
            is_campus = category == "campus"
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000000099" if is_campus else "B000000003",
                        name="示例大学" if is_campus else "独立城市公园",
                        type=("科教文化服务;学校;高等院校" if is_campus else "风景名胜;公园广场;公园"),
                        providerTypeCode="141200" if is_campus else "110101",
                        city=f"{city}市",
                        district="测试区",
                        address="大学路" if is_campus else "公园路",
                        longitude=116.3 if is_campus else 116.31,
                        latitude=40.0 if is_campus else 40.01,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="f" * 64,
                    )
                ],
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                },
                {
                    "slotId": "park",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "18:00",
                    "durationMinutes": 90,
                    "kind": "park",
                    "rawNeed": "独立公共公园",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "测试",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "assignToSlots": ["campus"],
                    "candidateHints": ["示例大学"],
                },
                {
                    "poolId": "park_pool",
                    "rawNeed": "独立公共公园",
                    "city": "测试",
                    "intentType": "park",
                    "targetCount": 1,
                    "assignToSlots": ["park"],
                    "candidateHints": ["城市公园"],
                },
            ],
        }
    )

    plans, _events = SimpleOpenItineraryExecutor(Provider()).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
    )

    park = next(plan for plan in plans if plan.intent_type == "park")
    evidence = park.schedule_constraints["experienceIndependenceEvidence"]
    assert park.selected_poi is not None
    assert evidence["status"] == "standalone_verified"
    assert evidence["physicalGroupId"] == "B000000003"


def test_frontier_assignment_controls_campus_query_page_and_disables_generic_fallback() -> None:
    import copy

    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def __init__(self, candidate_name: str) -> None:
            self.candidate_name = candidate_name
            self.calls: list[dict] = []

        def search(self, city, *, keyword, category, limit, **kwargs):
            self.calls.append({"keyword": keyword, "category": category, "limit": limit, **kwargs})
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id="B000000099",
                        name=self.candidate_name,
                        type="科教文化服务;学校;高等院校",
                        providerTypeCode="141200",
                        city=f"{city}市",
                        district="测试区",
                        address="大学路",
                        longitude=116.3,
                        latitude=40.0,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                }
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "测试",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["campus"],
                    "candidateHints": ["旧的固定提示"],
                }
            ],
        }
    )
    assignment = {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "campusAssignments": [
            {
                "slotId": "campus",
                "canonicalName": "资格大学乙",
                "evidenceEntityFingerprint": "e" * 64,
                "page": 2,
                "offset": 7,
                "queryFingerprint": "d" * 64,
                "queryScopeFingerprint": "f" * 64,
            }
        ],
    }
    provider = Provider("资格大学乙")

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        frontier_assignment=assignment,
    )

    assert plans[0].selected_poi is not None
    assert provider.calls == [
        {
            "keyword": "资格大学乙",
            "category": "campus",
            "limit": 5,
            "page": 2,
            "offset": 7,
            "query_scope_fingerprint": "f" * 64,
        }
    ]
    outcome_event = next(event for event in events if event["type"] == "simple_direction_frontier_outcomes")
    assert outcome_event["metadata"]["outcomes"] == [
        {
            "slotId": "campus",
            "evidenceEntityFingerprint": "e" * 64,
            "providerOutcome": "success",
            "selectedAmapId": "B000000099",
            "queryFingerprint": "d" * 64,
            "page": 2,
            "reasonCode": None,
        }
    ]

    rejected_provider = Provider("其他高校")
    rejected_plans, rejected_events = SimpleOpenItineraryExecutor(rejected_provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        frontier_assignment=assignment,
    )
    assert rejected_plans[0].selected_poi is None
    assert len(rejected_provider.calls) == 1
    rejected_outcome = next(
        event for event in rejected_events if event["type"] == "simple_direction_frontier_outcomes"
    )["metadata"]["outcomes"][0]
    assert rejected_outcome["providerOutcome"] == "rejected"
    assert rejected_outcome["reasonCode"] == "campus_assignment_candidate_rejected"
    assert rejected_outcome["rejectionReasonCodes"] == ["exact_entity_mismatch"]
    rejected_search = next(
        event
        for event in rejected_events
        if (event.get("metadata") or {}).get("providerOutcome") == "success"
        and (event.get("metadata") or {}).get("resultCount") == 1
    )
    assert rejected_search["metadata"]["candidateAdmissionRejectionReasonCounts"] == {"exact_entity_mismatch": 1}
    rejected_slot = next(event for event in rejected_events if event["type"] == "simple_open_slot_unresolved")
    assert "指定高校身份、资格与高校主地点核验" in rejected_slot["detail"]
    assert "类型与该行程意图明显冲突" not in rejected_slot["detail"]

    class FailingProvider(Provider):
        def search(self, city, *, keyword, category, limit, **kwargs):
            self.calls.append({"keyword": keyword, "category": category, "limit": limit, **kwargs})
            raise RuntimeError("provider unavailable")

    failing_provider = FailingProvider("资格大学乙")
    failed_plans, failed_events = SimpleOpenItineraryExecutor(failing_provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        frontier_assignment=assignment,
    )
    assert failed_plans[0].selected_poi is None
    assert len(failing_provider.calls) == 1
    failed_outcome = next(event for event in failed_events if event["type"] == "simple_direction_frontier_outcomes")[
        "metadata"
    ]["outcomes"][0]
    assert failed_outcome["providerOutcome"] == "failure"
    assert failed_outcome["reasonCode"] == "RuntimeError"
    assert failed_outcome["queryFingerprint"] == "d" * 64

    frozen_assignment = copy.deepcopy(assignment)
    frozen_assignment["campusAssignments"][0]["priorCanonicalAmapId"] = "B000000099"

    class DriftedIdentityProvider(Provider):
        def search(self, city, *, keyword, category, limit, **kwargs):
            result = super().search(city, keyword=keyword, category=category, limit=limit, **kwargs)
            return result.model_copy(update={"pois": [result.pois[0].model_copy(update={"id": "B000000100"})]})

    drifted = DriftedIdentityProvider("资格大学乙")
    drifted_plans, drifted_events = SimpleOpenItineraryExecutor(drifted).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        frontier_assignment=frozen_assignment,
    )
    assert drifted_plans[0].selected_poi is None
    drifted_outcome = next(event for event in drifted_events if event["type"] == "simple_direction_frontier_outcomes")[
        "metadata"
    ]["outcomes"][0]
    assert drifted_outcome["providerOutcome"] == "rejected"
    assert drifted_outcome["selectedAmapId"] is None

    class FrozenDetailProvider(Provider):
        def __init__(self) -> None:
            super().__init__("资格大学乙")
            self.detail_calls: list[str] = []

        def search(self, *_args, **_kwargs):
            raise AssertionError("collision retry must use the frozen AMap identity")

        def detail(self, amap_id: str):
            self.detail_calls.append(amap_id)
            return MapPoiResponse(
                id=amap_id,
                name="资格大学乙",
                type="科教文化服务;学校;高等院校",
                providerTypeCode="141200",
                city="测试市",
                district="测试区",
                address="大学路",
                longitude=116.3,
                latitude=40.0,
                category="campus",
                source="amap-place-search",
                sourceNote="provider detail",
                confidence=1.0,
                providerQueriedAt=datetime.now(timezone.utc),
                providerQueryReceiptFingerprint="a" * 64,
            )

    frozen_provider = FrozenDetailProvider()
    frozen_plans, frozen_events = SimpleOpenItineraryExecutor(frozen_provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        frontier_assignment=frozen_assignment,
    )
    assert frozen_provider.detail_calls == ["B000000099"]
    assert frozen_plans[0].selected_poi is not None
    frozen_event = next(event for event in frozen_events if event["type"] == "simple_open_tool_call")
    assert frozen_event["metadata"]["searchScope"] == "canonical_amap_detail"
    assert frozen_event["metadata"]["campusIdentityMayChange"] is False


def test_frontier_snapshot_drives_adjacent_scoped_nearby_page_and_emits_cursor_outcome() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.models.poi import POI
    from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    route_contract = {
        "status": "ready",
        "fingerprint": "r" * 64,
        "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000},
    }
    campus = POI(
        id="campus_local",
        amap_id="B000000099",
        name="资格大学乙",
        city="测试市",
        category="campus",
        latitude=40.0,
        longitude=116.3,
        source="amap-place-search",
        confidence=1.0,
        type="科教文化服务;学校;高等院校",
    )
    adjacent_scope = SimpleOpenItineraryExecutor._adjacent_candidate_scope(
        day_seed=campus,
        predecessor=campus,
        successor=None,
    )
    assert adjacent_scope is not None
    frontier_scope = SimpleOpenItineraryExecutor._nearby_frontier_scope_fingerprint(
        route_contract,
        spatial_preference={},
        slot_id="meal",
        day_number=1,
        query="地方餐馆",
        scope=adjacent_scope,
        radius=5000,
    )
    page_one_scope = SimpleOpenItineraryExecutor._nearby_query_scope_fingerprint(
        route_contract,
        spatial_preference={},
        slot_id="meal",
        day_number=1,
        query="地方餐馆",
        scope=adjacent_scope,
        radius=5000,
        page=1,
    )
    page_two_scope = SimpleOpenItineraryExecutor._nearby_query_scope_fingerprint(
        route_contract,
        spatial_preference={},
        slot_id="meal",
        day_number=1,
        query="地方餐馆",
        scope=adjacent_scope,
        radius=5000,
        page=2,
    )
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_test",
        request_contract_fingerprint="c" * 64,
        evidence={
            "qualificationEvidenceFingerprint": "e" * 64,
            "entities": [
                {
                    "evidenceEntityFingerprint": "a" * 64,
                    "canonicalName": "资格大学乙",
                }
            ],
        },
        locality="测试",
        max_pages_per_query=3,
    )
    page_one = SimpleDirectionFrontierService.begin_slot_query(
        frontier,
        day_number=1,
        slot_id="meal",
        day_seed_amap_id="B000000099",
        query_scope_fingerprint=frontier_scope,
    )
    frontier = SimpleDirectionFrontierService.record_slot_query(
        frontier,
        query=page_one,
        provider_outcome="success",
        admitted_physical_groups=[],
        rejected_physical_groups=[],
    )

    class Provider:
        def __init__(self) -> None:
            self.nearby_calls: list[dict] = []

        def search(self, city, *, keyword, category, limit, **kwargs):
            del kwargs
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=campus.amap_id,
                        name=campus.name,
                        type=campus.type,
                        providerTypeCode="141200",
                        city=campus.city,
                        district="测试区",
                        address="大学路",
                        longitude=campus.longitude,
                        latitude=campus.latitude,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                    )
                ],
            )

        def search_nearby(self, city, longitude, latitude, keyword, **kwargs):
            self.nearby_calls.append(
                {
                    "city": city,
                    "longitude": longitude,
                    "latitude": latitude,
                    "keyword": keyword,
                    **kwargs,
                }
            )
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=str(kwargs.get("category") or "food"),
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[],
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "高校参观",
                    "routeAnchor": True,
                },
                {
                    "slotId": "meal",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "12:00",
                    "durationMinutes": 75,
                    "kind": "meal",
                    "rawNeed": "地方餐馆",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "高校参观",
                    "city": "测试",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["campus"],
                    "candidateHints": ["旧提示"],
                },
                {
                    "poolId": "meal_pool",
                    "rawNeed": "地方餐馆",
                    "city": "测试",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["meal"],
                    "candidateHints": ["地方餐馆"],
                },
            ],
        }
    )
    assignment = {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "campusAssignments": [
            {
                "slotId": "campus",
                "dayNumber": 1,
                "canonicalName": campus.name,
                "evidenceEntityFingerprint": "a" * 64,
                "page": 1,
                "offset": 5,
                "queryFingerprint": "d" * 64,
                "queryScopeFingerprint": "f" * 64,
            }
        ],
        "slotFrontierSnapshot": {
            "schemaVersion": frontier["schemaVersion"],
            "requestContractFingerprint": frontier["requestContractFingerprint"],
            "executionProfile": frontier["executionProfile"],
            "slotFrontiers": frontier["slotFrontiers"],
        },
    }
    provider = Provider()
    _plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        route_decision_contract=route_contract,
        frontier_assignment=assignment,
        slot_lineage={
            "meal": {
                "occurrenceId": "occ:goal_meal:day:1",
                "sourceGoalId": "goal_meal",
                "requirementLevel": "explicit_soft",
                "completionRequired": True,
            }
        },
    )

    assert len(provider.nearby_calls) == 1
    assert provider.nearby_calls[0]["page"] == 2
    assert provider.nearby_calls[0].get("offset", 5) == frontier["executionProfile"]["pageOffset"]
    assert page_one_scope != page_two_scope
    assert provider.nearby_calls[0]["query_scope_fingerprint"] == page_two_scope
    outcome_event = next(event for event in events if event["type"] == "simple_direction_frontier_outcomes")
    assert outcome_event["metadata"]["slotQueryOutcomes"][0]["query"]["page"] == 2
    assert outcome_event["metadata"]["slotQueryOutcomes"][0]["providerOutcome"] == "success"
    remaining_meal_scopes = [
        item for item in outcome_event["metadata"]["remainingQueryScopes"] if item["slotId"] == "meal"
    ]
    assert remaining_meal_scopes
    assert all(item["currentPartialCompletionSlot"] is True for item in remaining_meal_scopes)


def test_rejected_frontier_campus_blocks_dependent_slot_queries_and_day_seed_drift() -> None:
    from src.api.schemas.agent import AgentInitialPlanOutput
    from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
    from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
    from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor

    class Provider:
        def __init__(self) -> None:
            self.text_calls: list[dict] = []
            self.nearby_calls: list[dict] = []

        def search(self, city, *, keyword, category, limit, **kwargs):
            self.text_calls.append({"keyword": keyword, "category": category, "limit": limit, **kwargs})
            if keyword == "资格大学乙":
                name, poi_type, typecode = "其他高校", "科教文化服务;学校;高等院校", "141200"
            elif keyword == "固定博物馆":
                name, poi_type, typecode = "固定博物馆", "科教文化服务;博物馆", "140100"
            else:
                name, poi_type, typecode = "地方餐馆", "餐饮服务;中餐厅", "050100"
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[
                    MapPoiResponse(
                        id=f"B0000000{len(self.text_calls):02d}",
                        name=name,
                        type=poi_type,
                        providerTypeCode=typecode,
                        city=f"{city}市",
                        district="测试区",
                        address="测试路",
                        longitude=116.3,
                        latitude=40.0,
                        category=category,
                        source="amap-place-search",
                        sourceNote="provider",
                        confidence=1.0,
                        providerQueriedAt=datetime.now(timezone.utc),
                        providerQueryReceiptFingerprint="f" * 64,
                    )
                ],
            )

        def search_nearby(self, city, longitude, latitude, keyword, **kwargs):
            self.nearby_calls.append(
                {
                    "city": city,
                    "longitude": longitude,
                    "latitude": latitude,
                    "keyword": keyword,
                    **kwargs,
                }
            )
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=str(kwargs.get("category") or "park"),
                providerName="amap-place-search",
                queriedAt=datetime.now(timezone.utc),
                pois=[],
            )

    initial = AgentInitialPlanOutput.model_validate(
        {
            "reply": "draft",
            "mode": "day_slots",
            "daySlots": [
                {
                    "slotId": "campus",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "09:00",
                    "durationMinutes": 120,
                    "kind": "campus",
                    "rawNeed": "985 高校参观",
                    "routeAnchor": True,
                },
                {
                    "slotId": "meal",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "12:00",
                    "durationMinutes": 75,
                    "kind": "meal",
                    "rawNeed": "地方餐馆",
                    "routeAnchor": True,
                },
                {
                    "slotId": "park",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "18:00",
                    "durationMinutes": 90,
                    "kind": "park",
                    "rawNeed": "独立公共公园",
                    "routeAnchor": True,
                },
                {
                    "slotId": "museum",
                    "dayNumber": 1,
                    "date": "2026-10-01",
                    "startTime": "20:00",
                    "durationMinutes": 60,
                    "kind": "museum",
                    "rawNeed": "固定博物馆",
                    "routeAnchor": True,
                },
            ],
            "intentPools": [
                {
                    "poolId": "campus_pool",
                    "rawNeed": "985 高校参观",
                    "city": "测试",
                    "intentType": "campus_visit",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["campus"],
                    "candidateHints": ["旧提示"],
                },
                {
                    "poolId": "meal_pool",
                    "rawNeed": "地方餐馆",
                    "city": "测试",
                    "intentType": "meal",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["meal"],
                    "candidateHints": ["地方餐馆"],
                },
                {
                    "poolId": "park_pool",
                    "rawNeed": "独立公共公园",
                    "city": "测试",
                    "intentType": "park",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["park"],
                    "candidateHints": ["城市公园"],
                },
                {
                    "poolId": "museum_pool",
                    "rawNeed": "固定博物馆",
                    "city": "测试",
                    "intentType": "museum",
                    "targetCount": 1,
                    "requirementLevel": "required",
                    "assignToSlots": ["museum"],
                    "candidateHints": ["固定博物馆"],
                    "entityBindingMode": "exact_entity",
                    "exactEntity": "固定博物馆",
                },
            ],
        }
    )
    frontier = SimpleDirectionFrontierService.create(
        planning_root_id="root_test",
        request_contract_fingerprint="c" * 64,
        evidence={
            "qualificationEvidenceFingerprint": "e" * 64,
            "entities": [{"evidenceEntityFingerprint": "a" * 64, "canonicalName": "资格大学乙"}],
        },
        locality="测试",
        max_pages_per_query=3,
    )
    assignment = {
        "schemaVersion": "simple-direction-frontier-attempt-v1",
        "campusAssignments": [
            {
                "slotId": "campus",
                "dayNumber": 1,
                "canonicalName": "资格大学乙",
                "evidenceEntityFingerprint": "a" * 64,
                "page": 1,
                "offset": 5,
                "queryFingerprint": "d" * 64,
                "queryScopeFingerprint": "f" * 64,
            }
        ],
        "slotFrontierSnapshot": {
            "schemaVersion": frontier["schemaVersion"],
            "requestContractFingerprint": frontier["requestContractFingerprint"],
            "executionProfile": frontier["executionProfile"],
            "slotFrontiers": frontier["slotFrontiers"],
        },
    }
    provider = Provider()

    plans, events = SimpleOpenItineraryExecutor(provider).build_segment_plans(
        initial,
        city="测试",
        transport_mode="transit",
        route_decision_contract={
            "status": "ready",
            "fingerprint": "r" * 64,
            "adjacentLegConstraint": {"candidateSearchRadiusMeters": 5000},
        },
        frontier_assignment=assignment,
    )

    assert [call["keyword"] for call in provider.text_calls] == ["资格大学乙", "固定博物馆"]
    assert provider.nearby_calls == []
    assert next(plan for plan in plans if plan.planning_slot_id == "museum").selected_poi is not None
    assert all(plan.selected_poi is None for plan in plans if plan.planning_slot_id in {"campus", "meal", "park"})
    frontier_event = next(event for event in events if event["type"] == "simple_direction_frontier_outcomes")
    assert frontier_event["metadata"]["outcomes"][0]["providerOutcome"] == "rejected"
    assert frontier_event["metadata"]["slotQueryOutcomes"] == []
    blocked = [event for event in events if event["type"] == "simple_open_slot_unresolved"]
    assert sum("当天高校锚点尚未通过高德身份核验" in event["detail"] for event in blocked) == 2


def test_simple_open_one_meal_without_candidate_is_read_only_with_zero_writes(monkeypatch) -> None:
    from backend.tests.unit.test_agent_service import (
        FakePoiResolver,
        StagedInitialProvider,
        clear_database,
        fake_amap_search_nearby_route_compatible,
        fake_amap_search_with_keyword_candidate,
        open_db,
        two_day_initial_day_slot_output,
    )
    from src.api.schemas.agent import AgentMessageRequest
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService
    from src.services.map_poi_service import MapPoiService

    clear_database()
    search_index = 0

    def meal_degraded_search(*args, **kwargs):
        nonlocal search_index
        search_index += 1
        result = fake_amap_search_with_keyword_candidate(*args, **kwargs)
        if kwargs.get("category") == "food":
            return result.model_copy(update={"providerName": "amap-place-search", "pois": []})
        return result.model_copy(
            update={
                "providerName": "amap-place-search",
                "pois": [
                    poi.model_copy(
                        update={
                            "id": f"B{search_index * 100 + index:011d}",
                            "source": "amap-place-search",
                            "type": "餐饮服务;中餐厅;北京菜"
                            if str(poi.category or "") == "food"
                            else poi.type,
                            "name": f"全聚德烤鸭店(北京测试{search_index}店)"
                            if str(poi.category or "") == "food"
                            else poi.name,
                            "address": f"北京测试路{search_index}号"
                            if str(poi.category or "") == "food"
                            else poi.address,
                            "longitude": float(poi.longitude) + search_index / 1000
                            if str(poi.category or "") == "food"
                            else poi.longitude,
                            "latitude": float(poi.latitude) + search_index / 1000
                            if str(poi.category or "") == "food"
                            else poi.latitude,
                            "provider_type_code": "050111"
                            if str(poi.category or "") == "food"
                            else poi.provider_type_code,
                            "tags": ["北京菜", "京味小吃" if search_index % 2 else "传统面食"]
                            if str(poi.category or "") == "food"
                            else poi.tags,
                            "source_claims": [
                                {
                                    "claimKey": "local_food",
                                    "stance": "support",
                                    "locality": "北京",
                                    "evidenceSource": "provider_city_specific_fact",
                                }
                            ]
                            if str(poi.category or "") == "food"
                            else poi.source_claims,
                        }
                    )
                    for index, poi in enumerate(result.pois, start=1)
                ],
            }
        )

    def meal_degraded_nearby(*args, **kwargs):
        nonlocal search_index
        search_index += 1
        result = fake_amap_search_nearby_route_compatible(*args, **kwargs)
        if kwargs.get("category") == "food":
            return result.model_copy(update={"providerName": "amap-place-search", "pois": []})
        return result.model_copy(
            update={
                "providerName": "amap-place-search",
                "pois": [
                    poi.model_copy(
                        update={
                            "id": "B"
                            + hashlib.sha256(
                                f"{result.keyword}|{poi.name}|{poi.longitude}|{poi.latitude}".encode("utf-8")
                            )
                            .hexdigest()[:12]
                            .upper(),
                            "source": "amap-place-search",
                            "provider_type_code": (
                                poi.provider_type_code or ("110101" if "公园广场;公园" in str(poi.type or "") else None)
                            ),
                            "provider_queried_at": datetime.now(timezone.utc),
                            "provider_query_receipt_fingerprint": "f" * 64,
                        }
                    )
                    for poi in result.pois
                ],
            }
        )

    monkeypatch.setattr(MapPoiService, "search", meal_degraded_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", meal_degraded_nearby)
    provider = StagedInitialProvider(two_day_initial_day_slot_output())
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple open meal partial")
        service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
        service.initial_planning_mode = "simple_open_v1"
        proposal_response = _send_after_route_clarification(service, session.session_id, GOLDEN_INPUT)
        assert proposal_response.version is None
        assert proposal_response.terminal_status == "candidate_refresh_required"
        assert not any(
            item.get("action") == "select_plan_proposal" for item in proposal_response.assistant_turn.choice_options
        )
        assert proposal_response.assistant_turn.comparison_projections
        projection = proposal_response.assistant_turn.comparison_projections[0]
        assert projection["adoptionReady"] is False
        assert any(item.get("intentType") == "meal" for item in projection["pendingSlots"])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?", (session.session_id,)
            ).fetchone()[0]
            == 0
        )
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        accepted_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND validation_status = 'accepted'",
            (session.session_id,),
        ).fetchone()[0]

    assert version_count == 0
    assert accepted_patch_count == 0
    assert "proposal_adoption_committed" not in {event.type for event in proposal_response.planning_steps}


def test_client_context_cannot_authorize_simple_open_route_policy() -> None:
    from backend.tests.unit.test_agent_service import clear_database, open_db
    from src.services.conversation_service import ConversationService
    from src.services.itinerary_patch_service import ItineraryPatchService

    clear_database()
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "spoof guard")
        authorized = ItineraryPatchService(connection)._authorized_simple_open_route_policy(
            session_id=session.session_id,
            source_type="agent",
            source_turn_id="turn_client_spoof",
            base_version_id=None,
            planning_context={
                "serverExecutionProfile": "simple_open_v1",
                "actualExecutionRoute": "simple_open_initial_pipeline",
                "simpleOpenNonBlockingRoutes": True,
            },
        )

    assert authorized is False


def test_simple_open_writer_error_leaves_zero_successful_versions(monkeypatch) -> None:
    from backend.tests.unit.test_agent_service import (
        FakePoiResolver,
        StagedInitialProvider,
        clear_database,
        fake_amap_search_nearby_route_compatible,
        fake_amap_search_with_keyword_candidate,
        open_db,
        two_day_initial_day_slot_output,
    )
    from fastapi import HTTPException
    from src.api.schemas.agent import AgentMessageRequest
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService
    from src.services.itinerary_patch_service import ItineraryPatchService
    from src.services.map_poi_service import MapPoiService
    from src.services.provider_route_insertion_service import ProviderRouteInsertionService

    clear_database()
    search_index = 0
    nearby_search_count = 0
    meal_nearby_search_count = 0

    def recorded_amap_search(*args, **kwargs):
        nonlocal search_index
        search_index += 1
        result = fake_amap_search_with_keyword_candidate(*args, **kwargs)
        result = result.model_copy(
            update={
                "pois": [
                    poi.model_copy(
                        update={"category": "food" if "餐饮服务" in str(poi.type or "") else poi.category}
                    )
                    for poi in result.pois
                ]
            }
        )
        return result.model_copy(
            update={
                "providerName": "amap-place-search",
                "pois": [
                    poi.model_copy(
                        update={
                            "id": f"B{search_index * 100 + index:011d}",
                            "source": "amap-place-search",
                            "type": "餐饮服务;中餐厅;北京菜"
                            if str(poi.category or "") == "food"
                            else poi.type,
                            "provider_type_code": "050111"
                            if str(poi.category or "") == "food"
                            else poi.provider_type_code,
                            "tags": ["北京菜", "地方风味"]
                            if str(poi.category or "") == "food"
                            else poi.tags,
                            "source_claims": [
                                {
                                    "claimKey": "local_food",
                                    "stance": "support",
                                    "locality": "北京",
                                    "evidenceSource": "provider_city_specific_fact",
                                }
                            ]
                            if str(poi.category or "") == "food"
                            else poi.source_claims,
                            "open_time_today": "10:00-22:00"
                            if str(poi.category or "") == "food"
                            else poi.open_time_today,
                            "business_status": "营业中"
                            if str(poi.category or "") == "food"
                            else poi.business_status,
                            "provider_queried_at": datetime.now(timezone.utc),
                            "provider_query_receipt_fingerprint": "f" * 64,
                        }
                    )
                    for index, poi in enumerate(result.pois, start=1)
                ],
            }
        )

    def recorded_amap_nearby(*args, **kwargs):
        nonlocal meal_nearby_search_count, nearby_search_count
        nearby_search_count += 1
        result = fake_amap_search_nearby_route_compatible(*args, **kwargs)
        if str(kwargs.get("provider_types") or "") == "北京菜":
            meal_nearby_search_count += 1
            result = result.model_copy(
                update={
                    "pois": [
                        poi.model_copy(
                            update={
                                "category": "food",
                                "type": "餐饮服务;中餐厅;北京菜",
                                "name": (
                                    "京味小吃坊(路线测试店)"
                                    if meal_nearby_search_count % 2
                                    else "老北京面馆(路线测试店)"
                                ),
                                "address": f"北京路线附近{nearby_search_count}号",
                            }
                        )
                        for poi in result.pois
                    ]
                }
            )
        elif re.search(r"(?:夜景|观景|灯光|奥林匹克塔|中信大厦)", str(result.keyword or "")):
            result = result.model_copy(
                update={
                    "pois": [
                        poi.model_copy(
                            update={
                                "name": f"城市公共观景台·{nearby_search_count}",
                                "type": "风景名胜;观景点",
                            }
                        )
                        for poi in result.pois
                    ]
                }
            )
        result = result.model_copy(
            update={
                "providerName": "amap-place-search",
                "pois": [
                    poi.model_copy(
                        update={
                            "id": "B"
                            + hashlib.sha256(
                                f"{result.keyword}|{poi.name}|{poi.longitude}|{poi.latitude}".encode("utf-8")
                            )
                            .hexdigest()[:12]
                            .upper(),
                            "source": "amap-place-search",
                            "provider_type_code": "050111"
                            if str(poi.category or "") == "food"
                            else (
                                poi.provider_type_code
                                or ("110101" if "公园广场;公园" in str(poi.type or "") else None)
                            ),
                            "tags": [
                                "北京菜",
                                "京味小吃" if meal_nearby_search_count % 2 else "传统面食",
                            ]
                            if str(poi.category or "") == "food"
                            else poi.tags,
                            "source_claims": [
                                {
                                    "claimKey": "local_food",
                                    "stance": "support",
                                    "locality": "北京",
                                    "evidenceSource": "provider_city_specific_fact",
                                }
                            ]
                            if str(poi.category or "") == "food"
                            else poi.source_claims,
                            "open_time_today": "10:00-22:00"
                            if str(poi.category or "") == "food"
                            else poi.open_time_today,
                            "business_status": "营业中"
                            if str(poi.category or "") == "food"
                            else poi.business_status,
                            "provider_queried_at": datetime.now(timezone.utc),
                            "provider_query_receipt_fingerprint": "f" * 64,
                        }
                    )
                    for poi in result.pois
                ],
            }
        )
        return result

    def verified_leg(_self, *, plan_id, left, right, transport_mode):
        del plan_id, transport_mode
        return {
            "fromAmapId": left["amapId"],
            "toAmapId": right["amapId"],
            "provider": "amap-webservice",
            "source": "amap-webservice",
            "distanceMeters": 1200,
            "durationSeconds": 900,
            "walkingDistanceMeters": 0,
            "transferCount": 0,
            "waitSeconds": 0,
            "riskPenaltyMinutes": 0,
            "polyline": [
                [left["longitude"], left["latitude"]],
                [right["longitude"], right["latitude"]],
            ],
            "queriedAt": datetime.now(timezone.utc).isoformat(),
        }

    def verified_leg_options(_self, *, plan_id, left, right, transport_mode):
        return [
            verified_leg(
                _self,
                plan_id=plan_id,
                left=left,
                right=right,
                transport_mode=transport_mode,
            )
        ]

    writer_calls = 0

    def fail_writer(*_args, **_kwargs):
        nonlocal writer_calls
        writer_calls += 1
        raise RuntimeError("injected canonical writer failure")

    monkeypatch.setattr(MapPoiService, "search", recorded_amap_search)
    monkeypatch.setattr(MapPoiService, "search_nearby", recorded_amap_nearby)
    monkeypatch.setattr(ProviderRouteInsertionService, "verified_leg", verified_leg)
    monkeypatch.setattr(ProviderRouteInsertionService, "verified_leg_options", verified_leg_options)
    monkeypatch.setattr(ItineraryPatchService, "apply_patch", fail_writer)
    provider = StagedInitialProvider(two_day_initial_day_slot_output())
    with open_db() as connection:
        session = ConversationService(connection).create_session("北京", "simple open writer rollback")
        service = AgentService(connection, provider=provider, poi_resolution_service=FakePoiResolver({}))
        service.initial_planning_mode = "simple_open_v1"
        proposal_response = _send_after_route_clarification(service, session.session_id, GOLDEN_INPUT)
        assert any(
            item.get("action") == "select_plan_proposal"
            for item in proposal_response.assistant_turn.choice_options
        ), "route clarification did not reach a selectable Simple direction proposal"
        choice = next(
            item
            for item in proposal_response.assistant_turn.choice_options
            if item.get("action") == "select_plan_proposal"
        )
        assert proposal_response.version is None
        assert proposal_response.terminal_status == "needs_confirmation"
        assert writer_calls == 0
        with pytest.raises(HTTPException) as exc_info:
            service.send_message(
                session.session_id,
                AgentMessageRequest(
                    content="确认编辑",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": proposal_response.assistant_turn.id,
                            "choiceId": choice["id"],
                        }
                    },
                ),
            )
        version_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        accepted_patch_count = connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ? AND validation_status = 'accepted'",
            (session.session_id,),
        ).fetchone()[0]
        active_version_id = connection.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session.session_id,)
        ).fetchone()[0]

    assert exc_info.value.status_code == 409
    assert writer_calls == 1
    assert version_count == 0
    assert accepted_patch_count == 0
    assert active_version_id is None
