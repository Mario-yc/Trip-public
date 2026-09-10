"""Deterministic support: real Agent, SQLite and capability validators, no network."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.tests.unit.test_agent_service import open_db
from backend.tests.unit.test_simple_open_direction_workflow import _persist_direction_turn, _snapshot
from src.api.schemas.agent import AgentMessageRequest, AgentInitialPlanOutput
from src.services.agent_ingress_journal import AgentIngressJournal
from src.services.conversation_capability_carrier import ConversationCapabilityCarrier
from src.models.source_material import SourceMaterial
from src.services.agent_service import AgentService
from src.services.conversation_service import ConversationService
from src.services.guide_continuation_requirement_service import (
    GuideContinuationRequirementError,
    GuideContinuationRequirementService,
)
from src.services.guide_source_refresh_service import GuideSourceRefreshService
from src.services.shared_travel_source_service import SharedTravelSourceService
from src.services.simple_open_direction_service import SimpleOpenDirectionService
from src.services.social_link_ingestion_service import SocialLinkIngestionService
from src.services.source_material_service import SourceMaterialService
from src.services.travel_guide_advice_service import TravelGuideAdviceService


URL = "https://www.xiaohongshu.com/explore/66abcdef0123456789abcdef"
BODY = "作者的五天攻略，不是用户的旅行要求。\n北京大学需要提前核实访客预约。\n北海公园适合散步。"
BUSINESS_TABLES = ("agent_plan_proposals", "itinerary_versions", "itinerary_patches", "route_options")


def _counts(db, tables=BUSINESS_TABLES):
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _payload(db, turn_id, column="agent_response_json"):
    assert column in {"agent_response_json", "agent_request_json"}
    return json.loads(
        db.execute(f"SELECT {column} FROM conversation_turns WHERE id=?", (turn_id,)).fetchone()[0] or "{}"
    )


def _save_payload(db, turn_id, payload, column="agent_response_json"):
    assert column in {"agent_response_json", "agent_request_json"}
    db.execute(
        f"UPDATE conversation_turns SET {column}=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), turn_id)
    )
    db.commit()


class SemanticProvider:
    """Only the native model transport is replaced; action admission stays real."""

    def __init__(self, db_path, session_id):
        self.calls = []
        self.action = "read_shared_guide"
        self.db_path = db_path
        self.session_id = session_id
        self.claims = []

    def prepare_controller_performance(self, context, performance, *, call_kind):
        performance["providerInvoked"] = True

    def decide_autonomy_lite(self, context, **_kwargs):
        # A separate connection observes the claim before the semantic call.
        with sqlite3.connect(self.db_path) as other:
            row = other.execute(
                "SELECT agent_request_json FROM conversation_turns "
                "WHERE session_id=? AND role='user' ORDER BY turn_index DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            self.claims.append(json.loads(row[0] or "{}").get("ingressJournal"))
        self.calls.append(copy.deepcopy(context))
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {"name": self.action, "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        }


@pytest.fixture
def intake(monkeypatch):
    with open_db() as db:
        session_id = ConversationService(db).create_session("北京", "shared source tests").session_id
        db.commit()
        service = AgentService(db)
        service.initial_planning_mode = "simple_open_v1"
        service.conversation_intent_router.routing_mode = "active-all"
        semantic = SemanticProvider(db.execute("PRAGMA database_list").fetchone()[2], session_id)
        monkeypatch.setattr(service.autonomy_controller, "provider", semantic)
        state = SimpleNamespace(
            db=db,
            agent=service,
            session=session_id,
            semantic=semantic,
            reads=[],
            body=BODY,
            failure=None,
            interrupt=False,
        )

        def ingest(_ingestor, url):
            assert not db.in_transaction, "the intake fence must commit before external I/O"
            state.reads.append(url)
            if state.interrupt:
                raise KeyboardInterrupt("simulated interruption after durable dispatch")
            material = SourceMaterial(
                id=f"source_shared_{len(state.reads)}",
                kind="link",
                link_url=url,
                raw_text=state.body if not state.failure else None,
                metadata={
                    "fetchStatus": "failed" if state.failure else "succeeded",
                    "failureReason": state.failure,
                    "canonicalUrl": URL,
                    "title": "北京五天游记",
                    "images": ["https://images.example/one.jpg"],
                    "extractedAt": "2026-09-06T00:00:00+00:00",
                },
            )
            SourceMaterialService(db)._insert(material)
            db.commit()
            return material

        monkeypatch.setattr(SocialLinkIngestionService, "ingest", ingest)
        yield state


def _send(state, *, content=URL, request_id="shared-intake-1"):
    return state.agent.send_message(
        state.session, AgentMessageRequest(content=content, requestId=request_id, context={})
    )


def _direction(state):
    root_id = state.agent._insert_turn(state.session, "user", "北京高校一日游，公共交通，适度绕行", "active")
    direction = SimpleOpenDirectionService(state.db)
    source_id, proposal_id = _persist_direction_turn(
        connection=state.db,
        service=state.agent,
        direction_service=direction,
        session_id=state.session,
        root_turn_id=root_id,
        source_user_turn_id=root_id,
        title="高校经典线",
        poi_id="B000A",
    )
    return source_id, proposal_id, copy.deepcopy(direction.latest_root(session_id=state.session))


def _selected(state, source_id):
    option = next(
        item for item in _payload(state.db, source_id)["choiceOptions"] if item["action"] == "continue_plan_expansion"
    )
    return {
        "sourceAssistantTurnId": source_id,
        "choiceId": option["id"],
        "action": option["action"],
        "option": copy.deepcopy(option),
    }


@pytest.fixture
def imported(intake):
    source_id, proposal_id, root = _direction(intake)
    response = _send(intake)
    selected = _selected(intake, response.assistant_turn.id)
    return intake, response, selected, source_id, proposal_id, root


def test_bare_link_claim_precedes_one_semantic_read_and_exact_replay(intake):
    before = _counts(intake.db)
    response = _send(intake)
    saved = _payload(intake.db, response.assistant_turn.id)
    source = saved["sharedSource"]
    assert source["status"] == "completed"
    assert source["bodyText"] == BODY
    assert source["contentFingerprint"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert source["imageCount"] == 1 and source["imageStatus"] == "not_read"
    assert response.assistant_turn.guide_advice is None
    assert response.terminal_status == "success"
    assert not saved.get("choiceOptions") and not saved.get("comparisonProjections")
    assert _counts(intake.db) == before
    assert intake.db.execute("SELECT COUNT(*) FROM agent_choice_executions").fetchone()[0] == 0
    journal = _payload(intake.db, response.user_turn.id, "agent_request_json")["sharedSourceRead"]
    assert journal["state"] == "completed" and journal["result"] == source
    assert journal["action"] == "read_shared_travel_guide"
    rows_before_replay = _counts(intake.db, (*BUSINESS_TABLES, "conversation_turns", "source_materials"))
    replay = _send(intake)
    assert replay.assistant_turn.id == response.assistant_turn.id
    assert replay.model_dump(by_alias=True)["requestReplay"]["replayed"] is True
    assert intake.reads == [URL] and len(intake.semantic.calls) == 1
    assert intake.semantic.claims[0]["requestId"] == "shared-intake-1"
    assert intake.semantic.claims[0]["state"] == "claimed"
    assert _counts(intake.db, (*BUSINESS_TABLES, "conversation_turns", "source_materials")) == rows_before_replay
    assert {t["function"]["name"] for t in intake.semantic.calls[0]["tools"]} == {
        "read_shared_guide",
        "explain",
        "clarify",
        "unavailable",
        "cancel",
    }
    with pytest.raises(HTTPException) as changed:
        _send(intake, content=URL + "改成另一个需求")
    assert changed.value.status_code == 409
    assert changed.value.detail["code"] == "request_id_payload_conflict"
    assert intake.reads == [URL] and len(intake.semantic.calls) == 1


def test_semantic_action_outside_intake_catalog_fails_closed_without_fetch(intake):
    intake.semantic.action = "create"
    before = _counts(intake.db)
    response = _send(intake)
    assert not _payload(intake.db, response.assistant_turn.id).get("sharedSource")
    assert intake.reads == [] and len(intake.semantic.calls) == 1
    assert _counts(intake.db) == before


def test_failed_read_is_visible_zero_write_and_replay_does_not_refetch(intake):
    intake.failure = "public_note_login_required"
    before = _counts(intake.db)
    response = _send(intake)
    payload = _payload(intake.db, response.assistant_turn.id)
    assert payload["sharedSource"]["status"] == "needs_user_material"
    assert payload["sharedSource"]["bodyText"] is None
    assert payload["sharedSource"]["failureReason"] == intake.failure
    assert "粘贴攻略文字" in response.assistant_turn.content
    assert not payload.get("guideAdvice")
    _send(intake)
    assert len(intake.reads) == len(intake.semantic.calls) == 1
    assert _counts(intake.db) == before


def test_multiple_links_do_not_fetch_or_fabricate_source(intake):
    before = _counts(intake.db)
    response = _send(intake, content=URL + "\nhttps://xhslink.com/a/other-note")
    source = _payload(intake.db, response.assistant_turn.id)["sharedSource"]
    assert source["status"] == "needs_user_material"
    assert source["failureReason"] == "shared_source_target_ambiguous"
    assert not source.get("bodyText")
    assert intake.reads == [] and _counts(intake.db) == before
    assert intake.db.execute("SELECT COUNT(*) FROM source_materials").fetchone()[0] == 0


def test_read_fence_rejects_foreign_request_and_uncertain_restart_without_fetch(intake):
    request_id = intake.agent._insert_turn(intake.session, "user", URL, "active")
    intake.db.commit()
    reader = SharedTravelSourceService(intake.db)
    with pytest.raises(ValueError, match="shared_source_request_invalid"):
        reader.read("foreign-session", request_id, URL)
    with pytest.raises(ValueError, match="shared_source_request_invalid"):
        reader.read(intake.session, request_id, "https://xhslink.com/a/substituted")
    assert intake.reads == []
    intake.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        reader.read(intake.session, request_id, URL)
    with pytest.raises(ValueError, match="shared_source_read_interrupted"):
        SharedTravelSourceService(intake.db).read(intake.session, request_id, URL)
    assert intake.reads == [URL]


def test_read_rotates_lawful_capabilities_and_binds_one_source_under_frozen_root(imported):
    state, response, selected, previous_id, proposal_id, original_root = imported
    root = SimpleOpenDirectionService(state.db).latest_root(session_id=state.session)
    assert root["requestIntentContract"] == original_root["requestIntentContract"]
    assert root["requestContractFingerprint"] == original_root["requestContractFingerprint"]
    assert root["planningRootId"] == original_root["planningRootId"]
    assert root["sourceAssistantTurnId"] == response.assistant_turn.id
    old = _payload(state.db, previous_id)
    new = _payload(state.db, response.assistant_turn.id)
    assert {c["action"] for c in new["choiceOptions"]} == {c["action"] for c in old["choiceOptions"]}
    assert not {c["id"] for c in new["choiceOptions"]} & {c["id"] for c in old["choiceOptions"]}
    adoption = next(c for c in new["choiceOptions"] if c["action"] == "select_plan_proposal")
    assert adoption["proposalId"] == proposal_id
    assert (
        state.db.execute("SELECT choice_id FROM agent_plan_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        == adoption["id"]
    )
    requirement = GuideContinuationRequirementService(state.db).build(
        session_id=state.session, selected_choice=selected, active_version_id=None
    )
    assert requirement["evidenceKind"] == "shared_public_note"
    assert requirement["identityPolicy"] == "guide-poi-identity-v1"
    assert requirement["minimumNovelGroundedPlaceCount"] == 1
    assert requirement["requestContractFingerprint"] == original_root["requestContractFingerprint"]
    assert requirement["allowedSourceIntentTypes"] == ["campus_visit"]
    assert [hint["mentionText"] for hint in requirement["placeHints"]] == ["北京大学"]
    assert requirement["sourceMaterialId"] == new["sharedSource"]["sourceMaterialId"]
    assert requirement["requirementFingerprint"] == GuideContinuationRequirementService.requirement_fingerprint(
        requirement
    )
    assert _counts(state.db) == {
        "agent_plan_proposals": 1,
        "itinerary_versions": 0,
        "itinerary_patches": 0,
        "route_options": 0,
    }
    assert state.db.execute("SELECT COUNT(*) FROM agent_choice_executions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "tamper", ["stored_body", "journal", "journal_execution_id", "assistant_body", "cross_session", "old_carrier"]
)
def test_shared_requirement_rejects_tampered_or_foreign_lineage(imported, tamper):
    state, response, selected, _, _, _ = imported
    service = GuideContinuationRequirementService(state.db)
    assert service.build(session_id=state.session, selected_choice=selected, active_version_id=None)["placeHints"]
    session_id = state.session
    if tamper == "stored_body":
        state.db.execute("UPDATE source_materials SET raw_text=?", (BODY + "篡改",))
        state.db.commit()
    elif tamper in {"journal", "journal_execution_id"}:
        request = _payload(state.db, response.user_turn.id, "agent_request_json")
        key = "action" if tamper == "journal" else "executionId"
        request["sharedSourceRead"][key] = "substituted-read-identity"
        _save_payload(state.db, response.user_turn.id, request, "agent_request_json")
    elif tamper == "assistant_body":
        payload = _payload(state.db, response.assistant_turn.id)
        payload["sharedSource"]["bodyText"] += "篡改"
        _save_payload(state.db, response.assistant_turn.id, payload)
    elif tamper == "cross_session":
        session_id = ConversationService(state.db).create_session("北京").session_id
    else:
        state.agent._insert_turn(state.session, "assistant", "更新回复没有授权旧按钮", "active")
    before = _counts(state.db)
    with pytest.raises(GuideContinuationRequirementError, match="guide_evidence_lineage_invalid"):
        service.build(session_id=session_id, selected_choice=selected, active_version_id=None)
    assert _counts(state.db) == before and state.reads == [URL]


def test_natural_language_guide_continuation_resolves_latest_import_without_execution(imported):
    state, response, selected, _, _, original_root = imported
    state.semantic.action = "continue_with_guide"
    before = _counts(state.db)
    request, route, resolved = state.agent._route_conversation_turn(
        session=state.agent._session(state.session),
        content="参考这份攻略再生成一个方案",
        payload=AgentMessageRequest(content="参考这份攻略再生成一个方案", context={}),
    )
    assert route.semantic_action["name"] == "continue_with_guide"
    assert route.continuation_mode == "guide_grounded" and not route.requires_clarification
    assert resolved.status == "unique"
    assert request.context.selected_agent_choice.source_assistant_turn_id == response.assistant_turn.id
    assert request.context.selected_agent_choice.choice_id == selected["choiceId"]
    assert (
        SimpleOpenDirectionService(state.db).latest_root(session_id=state.session)["requestIntentContract"]
        == original_root["requestIntentContract"]
    )
    assert state.reads == [URL] and _counts(state.db) == before


def test_refresh_and_same_operation_resume_reuse_intake_body_without_network(imported):
    state, _, selected, _, _, _ = imported
    requirement = GuideContinuationRequirementService(state.db).build(
        session_id=state.session, selected_choice=selected, active_version_id=None
    )
    selected["_serverGuideEvidenceFingerprint"] = requirement["evidenceFingerprint"]
    request_id = state.agent._insert_turn(state.session, "user", "参考这份攻略继续生成方案", "active")
    claim = state.agent._claim_fallback_choice_execution(
        state.session, request_id, selected, guide_binding_requirement=requirement
    )
    state.agent._finish_guide_binding_dispatch(state.session, claim["id"])
    before = _counts(state.db)

    class ForbiddenReader:
        def read(self, *_args, **_kwargs):
            raise AssertionError("an imported source must never be read again during guide refresh")

    refresh = GuideSourceRefreshService(state.db, reader=ForbiddenReader())
    arguments = dict(
        session_id=state.session,
        execution_id=claim["id"],
        requirement=requirement,
        selected_choice=selected,
        active_version_id=None,
    )
    first = refresh.refresh(**arguments)
    assert refresh.refresh(**arguments) == first
    documents = first["sourceDocumentEvidence"]["documents"]
    assert len(documents) == 1 and documents[0]["bodyText"] == BODY
    assert documents[0]["readAttempted"] is False
    assert GuideSourceRefreshService.validate_documents(first)
    assert first["minimumNovelGroundedPlaceCount"] == 1
    assert state.reads == [URL] and _counts(state.db) == before


def test_compatible_intents_are_filtered_before_single_source_hint_limit():
    body = "北京大学要预约。\n北海公园适合散步。"
    document = {
        "refId": "ref-one",
        "sourceFingerprint": "a" * 64,
        "status": "succeeded",
        "bodyText": body,
        "contentKind": "article",
        "contentFingerprint": hashlib.sha256(body.encode()).hexdigest(),
    }
    hints = GuideSourceRefreshService._document_hints([document], "e" * 64, ["park"])
    assert [(hint["mentionText"], hint["intentType"]) for hint in hints] == [("北海公园", "park")]


def test_no_root_read_then_new_requirements_offer_bound_creation_not_unrelated_create(intake):
    response = _send(intake)
    intake.semantic.action = "create_from_shared_guide"
    content = "参考这份攻略，北京一日高校游，公共交通，适度绕行"
    _, route, resolution = intake.agent._route_conversation_turn(
        session=intake.agent._session(intake.session), content=content,
        payload=AgentMessageRequest(content=content, context={"sharedSourceBinding": {"sourceMaterialId": "forged"}}))
    assert not route.requires_clarification
    assert route.semantic_action["name"] == "create_from_shared_guide"
    assert resolution.capability == "create_itinerary"
    proof = route.semantic_action["sharedSourceBinding"]
    assert proof["sourceAssistantTurnId"] == response.assistant_turn.id
    assert proof["sourceMaterialId"] == _payload(intake.db, response.assistant_turn.id)["sharedSource"]["sourceMaterialId"]
    names = {tool["function"]["name"] for tool in intake.semantic.calls[-1]["tools"]}
    assert "create_from_shared_guide" in names and "create" not in names
    assert intake.reads == [URL]


@pytest.mark.parametrize("action,question", [("explain", "刚才读到了什么？"), ("clarify", "我还需要提供哪些条件？")])
def test_readonly_interlude_preserves_direct_pending_source_without_alias_chain(intake, action, question):
    first = _send(intake)
    intake.semantic.action = action
    second = _send(intake, content=question, request_id="shared-interlude-1")
    pending = _payload(intake.db, second.assistant_turn.id).get("pendingSharedSource")
    assert pending and pending["sourceAssistantTurnId"] == first.assistant_turn.id
    third = _send(intake, content=question, request_id="shared-interlude-2")
    assert _payload(intake.db, third.assistant_turn.id)["pendingSharedSource"] == pending
    proof = SharedTravelSourceService(intake.db).initial_source(intake.session)
    assert proof["sourceAssistantTurnId"] == first.assistant_turn.id
    assert proof["carrierAssistantTurnId"] == third.assistant_turn.id
    assert intake.reads == [URL] and _counts(intake.db) == {table: 0 for table in BUSINESS_TABLES}


def test_missing_trip_requirements_clarification_preserves_pending_source(intake):
    first = _send(intake)
    intake.semantic.action = "create_from_shared_guide"
    response = _send(intake, content="参考这份攻略安排北京旅行", request_id="shared-need-details")
    saved = _payload(intake.db, response.assistant_turn.id)
    assert saved.get("pendingSharedSource", {}).get("sourceAssistantTurnId") == first.assistant_turn.id, saved.get("mode")
    assert _counts(intake.db) == {table: 0 for table in BUSINESS_TABLES}


@pytest.fixture
def initial_bound(intake, request):
    source_response = _send(intake)
    intake.semantic.action = "create_from_shared_guide"
    carrier_id = source_response.assistant_turn.id
    if getattr(request, "param", "direct") == "clarification":
        clarification = _send(intake, content="参考这份攻略安排北京旅行", request_id="shared-missing-details")
        carrier_id = clarification.assistant_turn.id
        # This transport double has no Full Controller. The clarification-mode
        # response is a provider failure, not an unanswered business question.
        # The source obligation must still survive the interlude and creation.
        assert _payload(intake.db, carrier_id)["failureReasonCode"] == "controller_action_unavailable"
        intake.semantic.action = "create_from_shared_guide"
    content = "北京高校一日游，2026年10月1日，公共交通，适度绕行，参考这份攻略"
    payload = AgentMessageRequest(content=content, requestId="shared-new-root", context={})
    user_id, _ = AgentIngressJournal(intake.db).claim(intake.session, payload.request_id,
        payload.model_dump(by_alias=True, mode="json"))
    request = _payload(intake.db, user_id, "agent_request_json")
    request["capabilityIngress"] = ConversationCapabilityCarrier(intake.db).capture(intake.session, carrier_id)
    _save_payload(intake.db, user_id, request, "agent_request_json")
    routed_payload, route, resolution = intake.agent._route_conversation_turn(
        session=intake.agent._session(intake.session), content=content, payload=payload)
    assert not route.requires_clarification
    reader = SharedTravelSourceService(intake.db)
    reader.bind_initial_request(intake.session, user_id, route.to_context())
    context = intake.agent._build_request_context(intake.agent._session(intake.session), content, routed_payload,
        current_user_turn_id=user_id, defer_agent_decision=True, allow_plan_expansion_rebind=False,
        conversation_intent_route=route.to_context(), conversation_capability_resolution=resolution.to_context())
    intake.agent._persist_agent_request_context(intake.session, user_id, context)
    yield SimpleNamespace(state=intake, user_id=user_id, content=content, context=context, reader=reader,
                          source_response=source_response, route=route)


def _prepare_initial(bound):
    state = bound.state
    # These source-lineage tests start after the Full boundary. Supply the
    # complete controlled semantic coverage here rather than bypassing the
    # production guard against freezing an uncompiled request contract.
    from src.services.request_activity_coverage_service import RequestActivityCoverageService
    contract = bound.context["requestIntentContract"]
    coverage = contract.get("requestActivityCoverage") or {}
    if coverage.get("status") == "pending":
        proposal = []
        for clause in coverage["clauses"]:
            activities = []
            if clause["text"] == "北京高校一日游":
                activities.append({
                    "goalId": clause["goalIds"][0], "sourceText": clause["text"],
                    "intentType": "campus_visit", "polarity": "required",
                    "allowedDayNumbers": [1], "minCount": 1, "dayPart": "flexible",
                })
            proposal.append({"clauseId": clause["clauseId"],
                             "classification": "activity" if activities else "constraint",
                             "activities": activities})
        bound.context["requestIntentContract"] = RequestActivityCoverageService.compile(contract, proposal)
        state.agent._persist_agent_request_context(state.session, bound.user_id, bound.context)
    assistant_id = state.agent._insert_turn(state.session, "assistant", "正在按需求生成草案", "streaming")
    pipeline = {"city": "北京", "requestIntentContract": copy.deepcopy(bound.context["requestIntentContract"])}
    initial = AgentInitialPlanOutput(reply="", mode="plan", daySlots=[], intentPools=[])
    dates = bound.context["resolvedTripDates"]
    state.agent._prepare_simple_open_direction_frontier(session_id=state.session, user_turn_id=bound.user_id,
        assistant_turn_id=assistant_id, city="北京", active_version_id=None, request_context=bound.context,
        pipeline_context=pipeline, initial_plan=initial, resolved_dates=dates, tool_events=[])
    root = SimpleOpenDirectionService(state.db).latest_root(session_id=state.session)
    return assistant_id, pipeline, initial, dates, root


@pytest.mark.parametrize("initial_bound", ["direct", "clarification"], indirect=True)
def test_first_plan_uses_read_body_and_user_day_count_before_candidates(initial_bound):
    bound = initial_bound
    assert bound.context["sourceMaterialEvidence"][0]["publicText"] == BODY
    assert bound.context["effectiveUserMessage"] == bound.content
    assert len(bound.context["resolvedTripDates"]["dates"]) == 1
    assistant_id, pipeline, _, _, root = _prepare_initial(bound)
    requirement = pipeline["guideContinuationRequirement"]
    assert requirement == bound.context["guideContinuationRequirement"]
    assert requirement["identityPolicy"] == "guide-poi-identity-v1"
    assert requirement["minimumNovelGroundedPlaceCount"] == 1
    assert requirement["planningSelectionRootTurnId"] == bound.user_id
    assert requirement["capabilitySourceAssistantTurnId"] == assistant_id
    assert requirement["sourceDocumentEvidence"]["documents"][0]["bodyText"] == BODY
    assert [hint["mentionText"] for hint in requirement["placeHints"]] == ["北京大学"]
    assert root["requestIntentContract"]["dayCount"] == 1
    assert requirement["requestContractFingerprint"] == root["requestContractFingerprint"]
    assert bound.reader.initial_requirement(bound.state.session, bound.user_id, root) == requirement
    assert bound.state.reads == [URL]
    assert _counts(bound.state.db) == {table: 0 for table in BUSINESS_TABLES}


@pytest.mark.parametrize("tamper", ["source_body", "proof", "root", "foreign_request"])
def test_initial_binding_revalidated_after_root_creation(initial_bound, tamper):
    bound = initial_bound
    state = bound.state
    _, _, _, _, root = _prepare_initial(bound)
    request_id = bound.user_id
    if tamper == "source_body":
        state.db.execute("UPDATE source_materials SET raw_text=?", (BODY + "已替换",))
        state.db.commit()
    elif tamper == "proof":
        request = _payload(state.db, request_id, "agent_request_json")
        request["conversationIntent"]["semanticAction"]["sharedSourceBinding"]["contentFingerprint"] = "f" * 64
        _save_payload(state.db, request_id, request, "agent_request_json")
    elif tamper == "root":
        root["requestIntentContract"]["dayCount"] = 5
    else:
        request_id = state.agent._insert_turn(state.session, "user", "forged request", "active",
            agent_request_json=_payload(state.db, bound.user_id, "agent_request_json"))
    before = _counts(state.db)
    with pytest.raises(ValueError):
        bound.reader.initial_requirement(state.session, request_id, root)
    assert _counts(state.db) == before and state.reads == [URL]


def test_initial_proposal_without_source_place_is_rejected_before_any_proposal_write(initial_bound):
    bound = initial_bound
    state = bound.state
    assistant_id, pipeline, initial, dates, _ = _prepare_initial(bound)
    candidate = _snapshot(state.agent._session(state.session)["active_plan_id"], "未采用来源的清华线", "B000A")
    response = state.agent._simple_open_direction_proposal_response(session_id=state.session,
        user_turn_id=bound.user_id, assistant_turn_id=assistant_id, content=bound.content,
        request_context=bound.context, pipeline_context=pipeline, session_before=state.agent._session(state.session),
        initial_plan=initial, resolved_dates=dates, grounding_report={}, snapshot=candidate, tool_events=[])
    saved = _payload(state.db, response.assistant_turn.id)
    assert saved["reasonCode"] == "guide_grounded_requirement_unsatisfied"
    assert saved["proposalDelta"] == 0
    assert saved["frontierAttemptConsumed"] is False
    assert saved["frontierExecutionId"] is None
    assert _counts(state.db) == {table: 0 for table in BUSINESS_TABLES}


def test_initial_proposal_with_verified_source_place_preserves_direct_continuation(initial_bound):
    from src.services.guide_poi_identity_service import GuidePoiIdentityService
    bound = initial_bound
    state = bound.state
    assistant_id, pipeline, initial, dates, _ = _prepare_initial(bound)
    candidate = _snapshot(state.agent._session(state.session)["active_plan_id"], "来源北京大学线", "B000B")
    candidate["routeDecisionContract"] = copy.deepcopy(bound.context["requestIntentContract"]["routeDecisionContract"])
    candidate["simpleOpenRouteAssignment"] = {
        "routeContractFingerprint": candidate["routeDecisionContract"]["fingerprint"],
        "expectedPairs": [], "verifiedPairs": [], "routeCoverageComplete": True,
        "adjacentLegCompliance": "verified", "topologyCompliance": "verified",
        "providerBaselineCompared": False, "detourCompliance": "not_evaluated"}
    requirement = pipeline["guideContinuationRequirement"]
    hint = requirement["placeHints"][0]
    segment = candidate["days"][0]["segments"][0]
    segment["semanticMetadata"]["scheduleConstraints"] = {"guideEvidence": {
        "schemaVersion": "guide-place-evidence-v1", "mentionText": hint["mentionText"],
        "intentType": hint["intentType"], "sourceRefIds": hint["sourceRefIds"],
        "sourceFingerprints": hint["sourceFingerprints"], "guideEvidenceFingerprint": hint["guideEvidenceFingerprint"],
        "verificationStatus": "verified_amap_grounding", "amapPoiId": "B000A7O5PK",
        "planningSlotId": "slot_B000B", "dayNumber": 1}}
    segment["semanticMetadata"]["scheduleConstraints"]["guideEvidence"]["identityMatch"] = GuidePoiIdentityService.resolve(
        hint=hint, candidates=[segment["poi"]], city="北京",
    )["matches"]["B000A7O5PK"]
    response = state.agent._simple_open_direction_proposal_response(session_id=state.session,
        user_turn_id=bound.user_id, assistant_turn_id=assistant_id, content=bound.content,
        request_context=bound.context, pipeline_context=pipeline, session_before=state.agent._session(state.session),
        initial_plan=initial, resolved_dates=dates, grounding_report={}, snapshot=candidate, tool_events=[])
    saved = _payload(state.db, response.assistant_turn.id)
    assert saved["proposalDelta"] == 1, (saved.get("reasonCode"), saved.get("guideEvidenceUsage", {}).get("rejectionCounts"))
    assert saved["guideEvidenceUsage"]["status"] == "satisfied"
    assert saved["guideEvidenceUsage"]["usedPlaces"][0]["mentionText"] == "北京大学"
    selected = _selected(state, response.assistant_turn.id)
    continued = GuideContinuationRequirementService(state.db).build(session_id=state.session,
        selected_choice=selected, active_version_id=None)
    assert continued["sourceAssistantTurnId"] == bound.source_response.assistant_turn.id
    assert continued["requestContractFingerprint"] == requirement["requestContractFingerprint"]
    assert continued["minimumNovelGroundedPlaceCount"] == 1
    assert _counts(state.db) == {"agent_plan_proposals": 1, "itinerary_versions": 0, "itinerary_patches": 0, "route_options": 0}


def test_legacy_initial_request_revalidation_does_not_upgrade_its_identity_policy(initial_bound):
    bound = initial_bound
    request = _payload(bound.state.db, bound.user_id, "agent_request_json")
    request.pop("sharedGuideIdentityPolicy", None)
    _save_payload(bound.state.db, bound.user_id, request, "agent_request_json")
    _, pipeline, _, _, root = _prepare_initial(bound)
    original = copy.deepcopy(pipeline["guideContinuationRequirement"])
    assert "identityPolicy" not in original
    changes = bound.state.db.total_changes
    assert bound.reader.initial_requirement(bound.state.session, bound.user_id, root) == original
    assert bound.state.db.total_changes == changes
    assert "sharedGuideIdentityPolicy" not in _payload(bound.state.db, bound.user_id, "agent_request_json")


def test_initial_source_cannot_be_admitted_after_a_newer_carrier(intake):
    response = _send(intake)
    intake.semantic.action = "create_from_shared_guide"
    content = "北京一日游，参考刚才攻略"
    _, route, _ = intake.agent._route_conversation_turn(session=intake.agent._session(intake.session),
        content=content, payload=AgentMessageRequest(content=content, context={}))
    user_id = intake.agent._insert_turn(intake.session, "user", content, "active", agent_request_json={
        "capabilityIngress": ConversationCapabilityCarrier(intake.db).capture(intake.session, response.assistant_turn.id)})
    intake.agent._insert_turn(intake.session, "assistant", "已进入新的讨论", "active")
    before = _counts(intake.db)
    with pytest.raises(ValueError, match="shared_initial_source_binding_invalid"):
        SharedTravelSourceService(intake.db).bind_initial_request(intake.session, user_id, route.to_context())
    assert _counts(intake.db) == before and intake.reads == [URL]


def test_removing_admitted_binding_does_not_remove_initial_source_obligation(initial_bound):
    bound = initial_bound
    state = bound.state
    assistant_id, pipeline, initial, dates, _ = _prepare_initial(bound)
    request = _payload(state.db, bound.user_id, "agent_request_json")
    request["conversationIntent"]["semanticAction"] = {"name": "create"}
    _save_payload(state.db, bound.user_id, request, "agent_request_json")
    before = _counts(state.db)
    with pytest.raises(ValueError, match="shared_initial_source_binding_invalid"):
        state.agent._prepare_simple_open_direction_frontier(session_id=state.session, user_turn_id=bound.user_id,
            assistant_turn_id=assistant_id, city="北京", active_version_id=None, request_context=bound.context,
            pipeline_context=pipeline, initial_plan=initial, resolved_dates=dates, tool_events=[])
    with pytest.raises(ValueError, match="shared_initial_source_binding_invalid"):
        state.agent._simple_open_direction_proposal_response(session_id=state.session,
            user_turn_id=bound.user_id, assistant_turn_id=assistant_id, content=bound.content,
            request_context=bound.context, pipeline_context=pipeline, session_before=state.agent._session(state.session),
            initial_plan=initial, resolved_dates=dates, grounding_report={},
            snapshot=_snapshot(state.agent._session(state.session)["active_plan_id"], "无来源方案", "B000A"), tool_events=[])
    assert _counts(state.db) == before and state.reads == [URL]


@pytest.mark.parametrize("clock", ["12:40", "12：40"])
def test_clock_minute_does_not_become_a_place_name_prefix(clock):
    body = f"{clock}天安门广场。\n798艺术中心。"
    mentions = TravelGuideAdviceService._place_mentions_in_text(body, "landmark")
    assert "天安门广场" in mentions
    assert "40天安门广场" not in mentions
    assert "798艺术中心" in mentions
    document = {
        "refId": "ref-clock",
        "sourceFingerprint": "c" * 64,
        "status": "succeeded",
        "bodyText": body,
        "contentKind": "article",
        "contentFingerprint": hashlib.sha256(body.encode()).hexdigest(),
    }
    hints = GuideSourceRefreshService._document_hints([document], "e" * 64, ["landmark"])
    assert [hint["mentionText"] for hint in hints] == ["天安门广场"]
