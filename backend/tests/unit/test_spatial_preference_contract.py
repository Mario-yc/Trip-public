from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from src.api.schemas.agent import AgentMessageRequest
from src.api.schemas.maps import MapPoiResponse
from src.core.database import get_db
from src.services.agent_service import AgentService
from src.services.clarification_checkpoint_service import ClarificationCheckpointService
from src.services.conversation_service import ConversationService
from src.services.conversation_intent_router import ConversationIntentRouter
from src.services.deepseek_agent_provider import AUTONOMY_DECISION_SYSTEM_PROMPT
from src.services.map_poi_service import MapPoiService
from src.services.named_boundary_provider import NamedBoundaryCandidate, NamedBoundaryResolutionResult
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor
from src.services.spatial_preference_service import SpatialPreferenceService
from src.models.poi import POI


def test_vague_city_center_is_unresolved_without_default_geography() -> None:
    result = SpatialPreferenceService.from_request_text("行程安排最好在北京市中心附近")
    assert result is not None
    assert result["schemaVersion"] == "spatial-preference-v1"
    assert result["status"] == "unresolved"
    assert result["strength"] == "preferred"
    serialized = str(result)
    assert "三环" not in serialized
    assert "天安门" not in serialized
    assert "radiusMeters" not in serialized
    assert "adcodes" not in serialized


def test_spatial_controller_patch_cannot_mint_provider_identity_or_coordinates() -> None:
    assert ClarificationCheckpointService.valid_semantic_patch(
        {"spatialResolutionInput": {"kind": "named_boundary", "boundaryText": "某条环线", "containment": "inside"}},
        allowed_fields={"spatialResolutionInput"},
    )
    assert ClarificationCheckpointService.valid_semantic_patch(
        {"spatialResolutionInput": {"kind": "reference_point", "referenceText": "某个真实地标"}},
        allowed_fields={"spatialResolutionInput"},
    )
    assert ClarificationCheckpointService.valid_semantic_patch(
        {"spatialResolutionInput": {"kind": "reference_point_radius", "referenceText": "国贸", "radiusMeters": 3000}},
        allowed_fields={"spatialResolutionInput"},
    )
    assert not ClarificationCheckpointService.valid_semantic_patch(
        {
            "spatialResolutionInput": {
                "kind": "reference_point_radius",
                "referenceText": "国贸",
                "radiusMeters": 3000,
                "amapId": "forged",
            }
        },
        allowed_fields={"spatialResolutionInput"},
    )
    assert not ClarificationCheckpointService.valid_semantic_patch(
        {"spatialResolutionInput": {"kind": "map_selection", "longitude": 116.4, "latitude": 39.9}},
        allowed_fields={"spatialResolutionInput"},
    )
    server_bound_map_selection = {
        "spatialResolutionInput": {
            "kind": "map_selection",
            "mapSelectionFingerprint": "a" * 64,
        }
    }
    assert ClarificationCheckpointService.valid_semantic_patch(
        server_bound_map_selection,
        allowed_fields={"spatialResolutionInput"},
    )
    assert not ClarificationCheckpointService.valid_controller_semantic_patch(
        server_bound_map_selection,
        allowed_fields={"spatialResolutionInput"},
    )


def test_spatial_controller_schema_exposes_exact_keys_without_default_geography() -> None:
    schema = ClarificationCheckpointService.semantic_field_schemas(["spatialResolutionInput"])["spatialResolutionInput"]

    assert schema["controllerAuthorableKinds"] == [
        "reference_point",
        "reference_point_radius",
        "administrative_area",
        "named_boundary",
    ]
    assert schema["variants"]["reference_point_radius"]["requiredKeys"] == [
        "kind",
        "referenceText",
        "radiusMeters",
    ]
    assert schema["variants"]["administrative_area"]["requiredKeys"] == [
        "kind",
        "administrativeAreaText",
    ]
    assert schema["checkpointBoundKind"]["authoringPolicy"] == "server_checkpoint_only"
    serialized = json.dumps(schema, ensure_ascii=False)
    assert "city_center" not in serialized
    assert "天安门" not in serialized
    assert "三环" not in serialized


def test_named_boundary_lookup_uses_identity_without_containment_grammar() -> None:
    assert SpatialPreferenceService.named_boundary_identity_text("动态边界以内", "inside") == "动态边界"
    assert SpatialPreferenceService.named_boundary_identity_text("位于 动态边界 范围外", "outside") == "动态边界"
    assert SpatialPreferenceService.named_boundary_identity_text("Inner Loop", "inside") == "Inner Loop"


def test_controller_prompt_uses_canonical_spatial_shape_and_forbids_implicit_center() -> None:
    assert "referenceText" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "administrativeAreaText" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "named_boundary" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "Road rings" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "named_boundary, not administrative_area" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "never supply an implicit center or radius" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "abstract references such as" in AUTONOMY_DECISION_SYSTEM_PROMPT
    assert "city_center" in AUTONOMY_DECISION_SYSTEM_PROMPT


def test_requirement_refinement_is_classified_before_view_context() -> None:
    route = ConversationIntentRouter().classify(
        "高校指的是985大学，行程安排最好在北京市中心附近。继续生成满足要求的方案"
    )
    assert route.classification is not None
    assert route.classification.intent == "create_itinerary"
    assert route.classification.requested_scope == "planning_root"
    assert route.requires_clarification is False


def test_required_resolved_spatial_preference_rejects_outside_candidate() -> None:
    spatial = {
        "schemaVersion": "spatial-preference-v1",
        "status": "resolved",
        "strength": "required",
        "fingerprint": "a" * 64,
        "resolution": {
            "kind": "reference_point_radius",
            "referencePlace": {"latitude": 39.9, "longitude": 116.4},
            "radiusMeters": 1000,
        },
    }
    inside = POI("inside", "inside", "北京", "park", 39.905, 116.405, amap_id="B000INSIDE")
    outside = POI("outside", "outside", "北京", "park", 40.0, 116.5, amap_id="B000OUTSIDE")

    assert SimpleOpenItineraryExecutor._spatial_candidate_allowed(inside, spatial)
    assert not SimpleOpenItineraryExecutor._spatial_candidate_allowed(outside, spatial)
    assert SimpleOpenItineraryExecutor._spatial_candidate_evidence(outside, spatial)["status"] == "outside"


def test_required_named_boundary_polygon_filters_candidates_without_becoming_route_evidence() -> None:
    spatial = {
        "schemaVersion": "spatial-preference-v1",
        "status": "resolved",
        "strength": "required",
        "fingerprint": "b" * 64,
        "resolution": {
            "kind": "named_boundary_polygon",
            "containment": "inside",
            "boundaryEvidenceFingerprint": "c" * 64,
            "polygonGcj02": [[116.3, 39.8], [116.5, 39.8], [116.5, 40.0], [116.3, 40.0], [116.3, 39.8]],
        },
    }
    inside = POI("inside", "inside", "北京", "park", 39.9, 116.4, amap_id="B000INSIDE")
    outside = POI("outside", "outside", "北京", "park", 40.1, 116.6, amap_id="B000OUTSIDE")

    evidence = SimpleOpenItineraryExecutor._spatial_candidate_evidence(inside, spatial)
    assert SimpleOpenItineraryExecutor._spatial_candidate_allowed(inside, spatial)
    assert not SimpleOpenItineraryExecutor._spatial_candidate_allowed(outside, spatial)
    assert evidence["geometryUsedAsRouteFeasibilityEvidence"] is False


def test_administrative_area_identity_comes_from_provider(monkeypatch) -> None:
    service = MapPoiService(map_provider_key="provider-key")
    monkeypatch.setattr(
        service,
        "_fetch_with_limit",
        lambda *_args, **_kwargs: {
            "status": "1",
            "infocode": "10000",
            "districts": [{"name": "海淀区", "adcode": "110108", "level": "district", "center": "116.3,39.9"}],
        },
    )

    assert service.resolve_administrative_area("海淀区") == [
        {"name": "海淀区", "adcode": "110108", "level": "district", "center": "116.3,39.9"}
    ]


def test_map_selection_is_server_bound_to_current_checkpoint_before_grounding(monkeypatch) -> None:
    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        session = ConversationService(connection).create_session("北京", "spatial map selection")
        request_contract = {
            "clarificationDimensions": [
                {
                    "dimensionId": "spatial_focus",
                    "status": "unresolved",
                    "allowedSemanticFields": ["spatialResolutionInput"],
                }
            ]
        }
        checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "chk_spatial_map",
            "planningRootId": "turn_spatial_root",
            "requestFingerprint": ClarificationCheckpointService._fingerprint(request_contract),
            "submissionMode": "batch_atomic",
            "submitChoiceId": "clarification-batch:chk_spatial_map",
            "sourceAssistantTurnId": "turn_spatial_assistant",
            "status": "awaiting_answer",
            "ambiguities": [{"dimensionId": "spatial_focus", "resolved": False}],
            "resolvedAnswers": [],
            "questions": [
                {
                    "dimensionId": "spatial_focus",
                    "question": "活动区域希望限定在哪里？",
                    "whyItMatters": "用于约束候选范围",
                    "required": True,
                    "allowFreeText": True,
                    "options": [],
                }
            ],
        }
        checkpoint["fingerprint"] = ClarificationCheckpointService._fingerprint(checkpoint)
        initial_fingerprint = checkpoint["fingerprint"]
        submit_choice = {
            "id": checkpoint["submitChoiceId"],
            "action": "submit_clarification_batch",
            "kind": "clarification_batch_submit",
            "scopeKind": "clarification",
            "checkpointId": checkpoint["checkpointId"],
            "checkpointFingerprint": initial_fingerprint,
            "sourceAssistantTurnId": "turn_spatial_assistant",
        }
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """INSERT INTO conversation_turns (
                   id, session_id, role, content, turn_index, status,
                   agent_response_json, created_at, updated_at
               ) VALUES (?, ?, 'assistant', ?, 1, 'active', ?, ?, ?)""",
            (
                "turn_spatial_assistant",
                session.session_id,
                "等待你确认活动区域",
                json.dumps(
                    {"clarificationCheckpoint": checkpoint, "choiceOptions": [submit_choice]},
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        connection.commit()
        agent = AgentService(connection)
        bound = agent.bind_spatial_map_selection(
            session_id=session.session_id,
            source_assistant_turn_id="turn_spatial_assistant",
            checkpoint_id="chk_spatial_map",
            checkpoint_fingerprint=initial_fingerprint,
            amap_poi_id="B000A6EA36",
            label="清华大学",
            radius_meters=3000,
        )
        record = bound["checkpoint"]["spatialMapSelections"][bound["mapSelectionFingerprint"]]
        assert "longitude" not in record
        assert "latitude" not in record
        assert record["amapPoiId"] == "B000A6EA36"
        assert bound["checkpoint"]["fingerprint"] == ClarificationCheckpointService._fingerprint(
            {key: value for key, value in bound["checkpoint"].items() if key != "fingerprint"}
        )
        persisted_payload = json.loads(
            connection.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                ("turn_spatial_assistant",),
            ).fetchone()[0]
        )
        assert persisted_payload["choiceOptions"][0]["checkpointFingerprint"] == bound["checkpoint"]["fingerprint"]
        resolved = ClarificationCheckpointService.resolve_batch(
            bound["checkpoint"],
            checkpoint_id="chk_spatial_map",
            planning_root_id="turn_spatial_root",
            source_assistant_turn_id="turn_spatial_assistant",
            request_contract=request_contract,
            selections=[
                {
                    "dimensionId": "spatial_focus",
                    "optionId": bound["optionId"],
                }
            ],
            source_user_turn_id="turn_spatial_map_answer",
        )
        assert resolved is not None
        monkeypatch.setattr(
            agent.map_poi_service,
            "detail",
            lambda amap_id: MapPoiResponse(
                id=amap_id,
                name="清华大学",
                type="科教文化服务;学校;高等院校",
                city="北京",
                district="海淀区",
                address="双清路30号",
                longitude=116.326,
                latitude=40.003,
                category="campus",
                source="amap-place-search",
                sourceNote="AMap detail",
                confidence=1.0,
            ),
        )
        grounded = agent._ground_spatial_resolution(
            {
                "kind": "map_selection",
                "mapSelectionFingerprint": bound["mapSelectionFingerprint"],
            },
            request_context={"clarificationCheckpoint": bound["checkpoint"]},
        )
        assert grounded == {
            "kind": "map_selection",
            "referencePlace": {
                "amapId": "B000A6EA36",
                "name": "清华大学",
                "longitude": 116.326,
                "latitude": 40.003,
            },
            "radiusMeters": 3000.0,
            "mapSelectionFingerprint": bound["mapSelectionFingerprint"],
        }
        assert (
            agent._ground_spatial_resolution(
                {"kind": "map_selection", "mapSelectionFingerprint": "0" * 64},
                request_context={"clarificationCheckpoint": bound["checkpoint"]},
            )
            is None
        )
    finally:
        connection_generator.close()


def test_unresolved_spatial_preference_requires_clarification_even_with_existing_version() -> None:
    context = {
        "requestIntentContract": {
            "clarificationRequired": True,
            "spatialPreference": {"schemaVersion": "spatial-preference-v1", "status": "unresolved"},
        }
    }
    assert AgentService._spatial_clarification_required(context) is True


def test_consumed_spatial_choice_closes_pending_source_turn() -> None:
    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        session = ConversationService(connection).create_session("测试城市", "spatial lifecycle")
        agent = AgentService(connection)
        checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "checkpoint_spatial_lifecycle",
            "planningRootId": "planning_root_spatial_lifecycle",
            "fingerprint": "d" * 64,
            "questions": [],
        }
        choice = {
            "id": "confirm_spatial_lifecycle",
            "action": "confirm_spatial_boundary",
            "kind": "spatial_boundary_confirmation",
            "scopeKind": "clarification",
        }
        source_turn_id = agent._insert_turn(
            session.session_id,
            "assistant",
            "请确认活动区域边界",
            "active",
            agent_request_json={
                "requestIntentContract": {
                    "spatialPreference": {"status": "confirmation_pending"},
                }
            },
            agent_response_json={
                "clarificationCheckpoint": checkpoint,
                "choiceOptions": [choice],
                "consumedChoiceIds": [],
            },
        )
        connection.commit()

        assert agent._latest_pending_spatial_clarification(session.session_id) == {
            "sourceAssistantTurnId": source_turn_id,
            "checkpointId": checkpoint["checkpointId"],
            "checkpointFingerprint": checkpoint["fingerprint"],
            "planningSelectionRootTurnId": checkpoint["planningRootId"],
        }

        agent._consume_selected_agent_choice(
            session.session_id,
            {"sourceAssistantTurnId": source_turn_id, "choiceId": choice["id"]},
        )

        assert agent._latest_pending_spatial_clarification(session.session_id) is None
    finally:
        connection_generator.close()


def test_named_boundary_grounding_stops_at_confirmation_pending(monkeypatch) -> None:
    class FakeBoundaryProvider:
        provider_name = "osm_overpass"
        source_url = "https://example.invalid/overpass"

        def resolve(self, **_kwargs):
            polygon = [(116.3, 39.8), (116.5, 39.8), (116.5, 40.0), (116.3, 40.0), (116.3, 39.8)]
            return NamedBoundaryResolutionResult(
                status="resolved",
                candidates=[
                    NamedBoundaryCandidate(
                        source_entity_id="relation/fixture",
                        canonical_name="动态测试边界",
                        source_version="1",
                        content_hash="a" * 64,
                        connected_component_count=1,
                        closed_cycle_count=1,
                        original_polygon=polygon,
                        simplified_polygon=polygon,
                        simplification_max_deviation_meters=0.0,
                    )
                ],
            )

    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        agent = AgentService(connection, named_boundary_provider=FakeBoundaryProvider())
        monkeypatch.setattr(
            agent.map_poi_service,
            "resolve_city_scope",
            lambda _city: [{"name": "测试城市", "adcode": "fixture", "queryBbox": [39.0, 116.0, 41.0, 117.0]}],
        )
        convert_calls = []

        def convert(points):
            convert_calls.append(points)
            return list(points)

        monkeypatch.setattr(agent.map_poi_service, "convert_wgs84_coordinates", convert)

        outcome = agent._ground_spatial_answer(
            {"kind": "named_boundary", "boundaryText": "动态测试边界", "containment": "inside"},
            request_context={"selectedCity": "测试城市"},
        )

        assert outcome["status"] == "confirmation_pending"
        assert outcome["reason"] == "boundary_confirmation_required"
        assert len(convert_calls) == 1
        assert SpatialPreferenceService.is_executable_resolution(outcome["resolution"])
        assert outcome["boundaryEvidence"]["attribution"] == "© OpenStreetMap contributors"
        assert outcome["boundaryEvidence"]["sourceUrl"].endswith("/relation/fixture")
    finally:
        connection_generator.close()


def test_city_scope_grounding_pending_offers_bounded_retry_and_change(monkeypatch) -> None:
    class FakeBoundaryProvider:
        provider_name = "osm_overpass"
        source_url = "https://example.invalid/overpass"

        def __init__(self) -> None:
            self.calls = 0
            self.boundary_texts = []

        def resolve(self, **kwargs):
            self.calls += 1
            self.boundary_texts.append(kwargs["boundary_text"])
            polygon = [(116.3, 39.8), (116.5, 39.8), (116.5, 40.0), (116.3, 40.0), (116.3, 39.8)]
            return NamedBoundaryResolutionResult(
                status="resolved",
                candidates=[
                    NamedBoundaryCandidate(
                        source_entity_id="relation/retry-fixture",
                        canonical_name="动态重试边界",
                        source_version="1",
                        content_hash="b" * 64,
                        connected_component_count=1,
                        closed_cycle_count=1,
                        original_polygon=polygon,
                        simplified_polygon=polygon,
                        simplification_max_deviation_meters=0.0,
                    )
                ],
            )

    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        session = ConversationService(connection).create_session("测试城市", "city scope retry")
        provider = FakeBoundaryProvider()
        agent = AgentService(connection, named_boundary_provider=provider)
        spatial_input = {
            "kind": "named_boundary",
            "boundaryText": "动态重试边界以内",
            "containment": "inside",
        }
        checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "checkpoint_city_scope_retry",
            "planningRootId": "planning_root_city_scope_retry",
            "fingerprint": "c" * 64,
            "status": "answered",
            "questions": [
                {
                    "dimensionId": "spatial_focus",
                    "question": "请明确本次活动区域。",
                    "whyItMatters": "需要先形成可执行空间范围。",
                    "required": True,
                    "allowFreeText": True,
                    "options": [
                        {
                            "id": "opt_reference",
                            "label": "按参考地点",
                            "semanticValue": {
                                "spatialResolutionInput": {
                                    "kind": "reference_point",
                                    "referenceText": "动态参考地点",
                                }
                            },
                        },
                        {
                            "id": "opt_area",
                            "label": "按行政区域",
                            "semanticValue": {
                                "spatialResolutionInput": {
                                    "kind": "administrative_area",
                                    "administrativeAreaText": "动态行政区域",
                                }
                            },
                        },
                    ],
                }
            ],
        }
        request_contract = {
            "spatialPreference": {
                "schemaVersion": "spatial-preference-v1",
                "status": "grounding_pending",
                "pendingGrounding": {
                    "status": "grounding_pending",
                    "reason": "city_scope_ambiguous",
                    "spatialResolutionInput": spatial_input,
                },
            },
            "clarificationAnswers": [
                {
                    "dimensionId": "spatial_focus",
                    "semanticValue": {"spatialResolutionInput": spatial_input},
                    "source": "free_text_normalized",
                }
            ],
            "clarificationDimensions": [
                {
                    "dimensionId": "spatial_focus",
                    "status": "grounding_pending",
                    "allowedSemanticFields": ["spatialResolutionInput"],
                }
            ],
            "clarificationRequired": True,
            "clarificationReason": "spatial_focus_grounding_required",
        }
        user_turn_id = agent._insert_turn(session.session_id, "user", "使用动态边界", "active")
        session_row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()

        pending_response = agent._spatial_grounding_response_if_needed(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            user_turn=agent._turn_response(user_turn_id),
            session=session_row,
            content="使用动态边界",
            request_context={
                "selectedCity": "测试城市",
                "requestIntentContract": request_contract,
                "clarificationCheckpoint": checkpoint,
            },
        )

        assert pending_response is not None
        assert provider.calls == 0
        assert [item["action"] for item in pending_response.assistant_turn.choice_options] == [
            "retry_spatial_grounding",
            "change_spatial_boundary",
        ]

        monkeypatch.setattr(
            agent.map_poi_service,
            "resolve_city_scope",
            lambda _city: [{"name": "测试城市", "adcode": "fixture", "queryBbox": [39.0, 116.0, 41.0, 117.0]}],
        )
        monkeypatch.setattr(agent.map_poi_service, "convert_wgs84_coordinates", lambda points: list(points))
        retry = pending_response.assistant_turn.choice_options[0]
        with pytest.raises(HTTPException) as forged_error:
            agent.send_message(
                session.session_id,
                AgentMessageRequest(
                    content="伪造其他回复中的重试能力",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": "forged_spatial_source_turn",
                            "choiceId": retry["id"],
                        }
                    },
                ),
            )
        assert forged_error.value.status_code == 404
        assert forged_error.value.detail["code"] == "agent_choice_source_not_found"
        assert provider.calls == 0

        confirmed = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="重新核验活动区域",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": pending_response.assistant_turn.id,
                        "choiceId": retry["id"],
                    }
                },
            ),
        )

        assert provider.calls == 1
        assert provider.boundary_texts == ["动态重试边界"]
        assert confirmed.terminal_status == "needs_confirmation"
        assert confirmed.assistant_turn.spatial_boundary_preview is not None
        assert [item["action"] for item in confirmed.assistant_turn.choice_options] == [
            "confirm_spatial_boundary",
            "change_spatial_boundary",
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0] == 0
    finally:
        connection_generator.close()


def test_grounding_pending_change_action_creates_fresh_spatial_only_checkpoint() -> None:
    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        session = ConversationService(connection).create_session("测试城市", "change pending spatial expression")
        agent = AgentService(connection)
        spatial_input = {
            "kind": "named_boundary",
            "boundaryText": "动态待修改边界",
            "containment": "inside",
        }
        spatial_question = {
            "dimensionId": "spatial_focus",
            "question": "请明确本次活动区域。",
            "whyItMatters": "需要先形成可执行空间范围。",
            "required": True,
            "allowFreeText": True,
            "options": [
                {
                    "id": "opt_reference",
                    "label": "按参考地点",
                    "semanticValue": {
                        "spatialResolutionInput": {
                            "kind": "reference_point",
                            "referenceText": "动态参考地点",
                        }
                    },
                },
                {
                    "id": "opt_area",
                    "label": "按行政区域",
                    "semanticValue": {
                        "spatialResolutionInput": {
                            "kind": "administrative_area",
                            "administrativeAreaText": "动态行政区域",
                        }
                    },
                },
                {
                    "id": "spatial_map_old_checkpoint",
                    "label": "旧检查点的地图选点",
                    "semanticValue": {
                        "spatialResolutionInput": {
                            "kind": "map_selection",
                            "mapSelectionFingerprint": "a" * 64,
                        }
                    },
                },
            ],
        }
        checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "checkpoint_change_pending_spatial",
            "planningRootId": "planning_root_change_pending_spatial",
            "fingerprint": "d" * 64,
            "status": "answered",
            "questions": [spatial_question],
        }
        pending = {
            "status": "grounding_pending",
            "reason": "city_scope_ambiguous",
            "spatialResolutionInput": spatial_input,
        }
        request_contract = {
            "spatialPreference": {
                "schemaVersion": "spatial-preference-v1",
                "status": "grounding_pending",
                "pendingGrounding": pending,
            },
            "clarificationAnswers": [
                {
                    "dimensionId": "spatial_focus",
                    "semanticValue": {"spatialResolutionInput": spatial_input},
                    "source": "free_text_normalized",
                }
            ],
            "clarificationDimensions": [
                {
                    "dimensionId": "spatial_focus",
                    "status": "grounding_pending",
                    "allowedSemanticFields": ["spatialResolutionInput"],
                },
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "status": "resolved",
                    "allowedSemanticFields": ["detourTolerance"],
                },
            ],
            "clarificationRequired": True,
            "clarificationReason": "spatial_focus_grounding_required",
        }
        user_turn_id = agent._insert_turn(session.session_id, "user", "使用动态边界", "active")
        session_row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        pending_response = agent._spatial_grounding_response_if_needed(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            user_turn=agent._turn_response(user_turn_id),
            session=session_row,
            content="使用动态边界",
            request_context={
                "selectedCity": "测试城市",
                "requestIntentContract": request_contract,
                "clarificationCheckpoint": checkpoint,
            },
        )
        assert pending_response is not None
        change = next(
            item for item in pending_response.assistant_turn.choice_options if item["action"] == "change_spatial_boundary"
        )

        changed = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="修改活动区域表达",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": pending_response.assistant_turn.id,
                        "choiceId": change["id"],
                    }
                },
            ),
        )

        assert changed.terminal_status == "needs_confirmation"
        assert changed.assistant_turn.clarification_checkpoint is not None
        assert changed.assistant_turn.clarification_checkpoint["checkpointId"] != checkpoint["checkpointId"]
        assert changed.assistant_turn.clarification_checkpoint["status"] == "awaiting_answer"
        assert [
            item["dimensionId"] for item in changed.assistant_turn.clarification_checkpoint["questions"]
        ] == ["spatial_focus"]
        new_options = changed.assistant_turn.clarification_checkpoint["questions"][0]["options"]
        assert all(
            str((item.get("semanticValue") or {}).get("spatialResolutionInput", {}).get("kind") or "")
            != "map_selection"
            for item in new_options
        )
        assert "spatialMapSelections" not in changed.assistant_turn.clarification_checkpoint
        assert [item["action"] for item in changed.assistant_turn.choice_options] == [
            "submit_clarification_batch"
        ]
        persisted_context = connection.execute(
            "SELECT agent_request_json FROM conversation_turns WHERE id = ?",
            (changed.assistant_turn.id,),
        ).fetchone()
        assert persisted_context is not None
        assert connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0] == 0
    finally:
        connection_generator.close()


def test_ambiguous_boundaries_require_opaque_selection_before_one_coordinate_conversion(
    monkeypatch,
) -> None:
    class AmbiguousBoundaryProvider:
        provider_name = "osm_overpass"
        source_url = "https://example.invalid/overpass"

        def resolve(self, **_kwargs):
            candidates = []
            for index, offset in enumerate((0.0, 0.2), start=1):
                polygon = [
                    (116.0 + offset, 39.8),
                    (116.1 + offset, 39.8),
                    (116.1 + offset, 39.9),
                    (116.0 + offset, 39.9),
                    (116.0 + offset, 39.8),
                ]
                candidates.append(
                    NamedBoundaryCandidate(
                        source_entity_id=f"relation/fixture-{index}",
                        canonical_name=f"动态候选 {index}",
                        source_version=str(index),
                        content_hash=str(index) * 64,
                        connected_component_count=1,
                        closed_cycle_count=1,
                        original_polygon=polygon,
                        simplified_polygon=polygon,
                        simplification_max_deviation_meters=0.0,
                    )
                )
            return NamedBoundaryResolutionResult(
                status="ambiguous",
                candidates=candidates,
                reason="multiple_closed_boundaries",
            )

    connection_generator = get_db()
    connection = next(connection_generator)
    try:
        session = ConversationService(connection).create_session("测试城市", "ambiguous boundary")
        agent = AgentService(connection, named_boundary_provider=AmbiguousBoundaryProvider())
        monkeypatch.setattr(
            agent.map_poi_service,
            "resolve_city_scope",
            lambda _city: [{"name": "测试城市", "adcode": "fixture", "queryBbox": [39.0, 116.0, 41.0, 117.0]}],
        )
        convert_calls = []
        monkeypatch.setattr(
            agent.map_poi_service,
            "convert_wgs84_coordinates",
            lambda points: convert_calls.append(points) or list(points),
        )
        outcome = agent._ground_spatial_answer(
            {"kind": "named_boundary", "boundaryText": "动态边界", "containment": "inside"},
            request_context={"selectedCity": "测试城市"},
        )
        assert outcome["status"] == "grounding_pending"
        assert outcome["reason"] == "multiple_closed_boundaries"
        assert len(outcome["boundaryCandidates"]) == 2
        assert convert_calls == []

        checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "checkpoint_boundary_candidates",
            "planningRootId": "planning_root_boundary_candidates",
            "fingerprint": "e" * 64,
            "questions": [],
        }
        request_contract = {
            "spatialPreference": {
                "schemaVersion": "spatial-preference-v1",
                "status": "grounding_pending",
                "pendingGrounding": outcome,
            }
        }
        user_turn_id = agent._insert_turn(session.session_id, "user", "动态边界以内", "active")
        session_row = connection.execute(
            "SELECT * FROM conversation_sessions WHERE id = ?",
            (session.session_id,),
        ).fetchone()
        first = agent._spatial_grounding_response_if_needed(
            session_id=session.session_id,
            user_turn_id=user_turn_id,
            user_turn=agent._turn_response(user_turn_id),
            session=session_row,
            content="动态边界以内",
            request_context={
                "selectedCity": "测试城市",
                "requestIntentContract": request_contract,
                "clarificationCheckpoint": checkpoint,
            },
        )
        assert first is not None
        assert [item["action"] for item in first.assistant_turn.choice_options] == [
            "select_spatial_boundary_candidate",
            "select_spatial_boundary_candidate",
        ]

        selected = first.assistant_turn.choice_options[0]
        second = agent.send_message(
            session.session_id,
            AgentMessageRequest(
                content="选择这个真实边界",
                context={
                    "selectedAgentChoice": {
                        "sourceAssistantTurnId": first.assistant_turn.id,
                        "choiceId": selected["id"],
                    }
                },
            ),
        )
        assert len(convert_calls) == 1
        assert second.terminal_status == "needs_confirmation"
        assert second.assistant_turn.spatial_boundary_preview is not None
        assert [item["action"] for item in second.assistant_turn.choice_options] == [
            "confirm_spatial_boundary",
            "change_spatial_boundary",
        ]
        plan_id = session.active_plan_id
        assert connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0] == 0

        superseding_checkpoint = {
            "schemaVersion": "clarification-checkpoint-v2",
            "checkpointId": "checkpoint_new_spatial_root",
            "planningRootId": "planning_root_new_spatial",
            "fingerprint": "9" * 64,
            "questions": [],
        }
        agent._insert_turn(
            session.session_id,
            "assistant",
            "新的活动区域仍待确认",
            "active",
            agent_request_json={
                "requestIntentContract": {
                    "spatialPreference": {"status": "unresolved"},
                }
            },
            agent_response_json={"clarificationCheckpoint": superseding_checkpoint},
        )
        stale_confirm = second.assistant_turn.choice_options[0]
        with pytest.raises(HTTPException) as stale_error:
            agent.send_message(
                session.session_id,
                AgentMessageRequest(
                    content="确认旧边界",
                    context={
                        "selectedAgentChoice": {
                            "sourceAssistantTurnId": second.assistant_turn.id,
                            "choiceId": stale_confirm["id"],
                        }
                    },
                ),
            )
        assert stale_error.value.status_code == 409
        assert stale_error.value.detail["code"] == "spatial_boundary_confirmation_identity_mismatch"
    finally:
        connection_generator.close()


def test_amap_coordinate_conversion_rejects_self_intersecting_result(monkeypatch) -> None:
    service = MapPoiService(map_provider_key="provider-key")
    monkeypatch.setattr(
        service,
        "_fetch_with_limit",
        lambda *_args, **_kwargs: {"locations": "0,0;2,2;0,2;2,0;0,0"},
    )

    with pytest.raises(HTTPException) as error:
        service.convert_wgs84_coordinates(
            [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
        )

    assert error.value.status_code == 502
    assert error.value.detail == "AMap coordinate conversion returned an invalid polygon"
