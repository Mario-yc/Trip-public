import json

import pytest

from src.services.conversation_action_catalog import ConversationActionCatalog, parse_action_response
from src.services.conversation_intent_router import IntentRoutingSnapshotV1


def snapshot(**state):
    return IntentRoutingSnapshotV1("fingerprint", {"workflowPhase": "guide_advice"}, state)


def test_new_shared_link_is_read_before_any_planning_without_granting_authority():
    catalog = ConversationActionCatalog(
        snapshot(
            sharedLinkInput=True,
            hasPlanningRoot=True,
            guideRequirement={"evidenceFingerprint": "old"},
            capabilityMatches={"continue_plan_expansion": [{}]},
        )
    )
    assert "read_shared_guide" in catalog.names
    assert not {"create", "continue_directions", "continue_with_guide", "refine_request"} & catalog.names
    assert parse_action_response(response("read_shared_guide"), catalog)[0].intent == "inspect_or_explain"
    assert "read_shared_guide" not in ConversationActionCatalog(snapshot()).names


def response(name, args=None, finish="tool_calls"):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args or {})},
                        }
                    ]
                },
            }
        ]
    }


def test_guide_action_requires_current_valid_guide_requirement():
    state = dict(hasPlanningRoot=True, capabilityMatches={"continue_plan_expansion": [{}]})
    assert "continue_with_guide" not in ConversationActionCatalog(snapshot(**state)).names
    state["guideRequirement"] = {"evidenceFingerprint": "server-only"}
    catalog = ConversationActionCatalog(snapshot(**state))
    assert "continue_with_guide" in catalog.names
    assert "server-only" not in json.dumps(catalog.tools())


def test_action_response_is_strict_and_cannot_grant_authority():
    catalog = ConversationActionCatalog(snapshot())
    assert parse_action_response(response("explain"), catalog)[0].name == "explain"
    for body in [
        response("continue_with_guide"),
        response("explain", {"choiceId": "invented"}),
        response("explain", finish="length"),
        {"choices": []},
    ]:
        with pytest.raises(ValueError):
            parse_action_response(body, catalog)
    body = response("explain")
    body["choices"][0]["message"]["tool_calls"] *= 2
    with pytest.raises(ValueError):
        parse_action_response(body, catalog)


def test_snapshot_does_not_expose_mutable_authority():
    original = {"capabilityMatches": {"continue_plan_expansion": [{"choiceId": "a"}]}}
    frozen = snapshot(**original)
    original["capabilityMatches"]["continue_plan_expansion"].clear()
    exposed = frozen.server_state
    exposed["capabilityMatches"].clear()
    assert frozen.server_state["capabilityMatches"]["continue_plan_expansion"][0]["choiceId"] == "a"


def full_catalog():
    from src.services.conversation_action_catalog import ACTIONS

    return ConversationActionCatalog(
        snapshot(
            hasActiveVersion=True,
            hasPlanningRoot=True,
            guideRequirement={"evidenceFingerprint": "PRIVATE"},
            capabilityMatches={action.intent: [{}] for action in ACTIONS},
        )
    )


def test_fixed_actions_publish_no_arguments_and_restore_server_target():
    catalog = full_catalog()
    fixed = {
        "explain",
        "cancel",
        "refine_request",
        "regenerate",
        "continue_directions",
        "continue_with_guide",
        "search_guide",
        "retry",
        "answer_clarification",
    }
    tools = {item["function"]["name"]: item["function"] for item in catalog.tools()}
    for name in fixed:
        assert tools[name]["parameters"]["properties"] == {}
        assert tools[name]["parameters"]["additionalProperties"] is False
        action, args = parse_action_response(response(name), catalog)
        assert args.target.kind == action.target
    assert all("request" not in tool["parameters"]["properties"] for tool in tools.values())


@pytest.mark.parametrize(
    "name,fields",
    [
        ("modify", {"kind", "dayNumber", "timeBucket", "mentionText"}),
        ("fill_slot", {"kind", "dayNumber"}),
        ("search_candidate", {"kind", "dayNumber"}),
        ("request_adoption", {"kind", "ordinal"}),
    ],
)
def test_actions_publish_only_consumed_semantic_target_fields(name, fields):
    tool = next(item["function"] for item in full_catalog().tools() if item["function"]["name"] == name)
    assert set(tool["parameters"]["properties"]) == {"target"}
    assert set(tool["parameters"]["properties"]["target"]["properties"]) == fields


@pytest.mark.parametrize(
    "name,args",
    [
        ("explain", {"request": "repeat original user message"}),
        ("continue_with_guide", {"target": {"kind": "latest_guide"}}),
        ("modify", {"target": {"kind": "day", "ordinal": 1}}),
        ("fill_slot", {"target": {"kind": "pending_slot", "mentionText": "fake slot"}}),
        ("request_adoption", {"target": {"kind": "proposal_ordinal", "dayNumber": 1}}),
        ("modify", {"target": {"kind": "current_itinerary", "sourceAssistantTurnId": "forged"}}),
        ("modify", {"target": {"kind": "current_itinerary"}, "sharedSourceBinding": {"id": "forged"}}),
    ],
)
def test_parser_rejects_every_unpublished_argument_even_when_global_model_knows_it(name, args):
    with pytest.raises(ValueError):
        parse_action_response(response(name, args), full_catalog())


def test_duplicate_json_keys_cannot_select_a_different_target():
    body = response("modify")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        '{"target":{"kind":"day","dayNumber":1,"dayNumber":2}}'
    )
    with pytest.raises(ValueError):
        parse_action_response(body, full_catalog())


@pytest.mark.parametrize(
    "name,target",
    [
        ("modify", {"kind": "day", "dayNumber": 2}),
        ("modify", {"kind": "time_window", "dayNumber": 1, "timeBucket": "afternoon", "mentionText": "国家博物馆"}),
        ("fill_slot", {"kind": "pending_slot", "dayNumber": 2}),
        ("search_candidate", {"kind": "pending_slot", "dayNumber": 1}),
        ("request_adoption", {"kind": "proposal_ordinal", "ordinal": 2}),
    ],
)
def test_trimmed_protocol_preserves_real_semantic_coordinates(name, target):
    action, args = parse_action_response(response(name, {"target": target}), full_catalog())
    assert action.name == name
    assert args.target.model_dump(by_alias=True, exclude_none=True) == target


@pytest.mark.parametrize(
    "name,target",
    [
        ("modify", {"kind": "proposal_ordinal", "dayNumber": 1}),
        ("modify", {"kind": "day", "dayNumber": 0}),
        ("modify", {"kind": "time_window", "timeBucket": "midnight"}),
        ("fill_slot", {"kind": "pending_slot", "dayNumber": 32}),
        ("request_adoption", {"kind": "proposal_ordinal", "ordinal": 13}),
    ],
)
def test_trimmed_protocol_retains_target_kinds_and_value_bounds(name, target):
    with pytest.raises(ValueError):
        parse_action_response(response(name, {"target": target}), full_catalog())


@pytest.mark.parametrize(
    "name,state",
    [
        ("create", {}),
        ("read_shared_guide", {"sharedLinkInput": True}),
        ("create_from_shared_guide", {"sharedInitialSource": {"sourceAssistantTurnId": "PRIVATE"}}),
    ],
)
def test_intake_and_new_root_actions_do_not_expose_argument_or_authority(name, state):
    catalog = ConversationActionCatalog(snapshot(**state))
    tool = next(item["function"] for item in catalog.tools() if item["function"]["name"] == name)
    assert tool["parameters"]["properties"] == {}
    assert "PRIVATE" not in json.dumps(tool)
    assert parse_action_response(response(name), catalog)[1].target.kind == "none"


def test_native_action_tools_use_existing_strict_schema_compiler():
    from src.services.tool_schema_compiler import compile_deepseek_tools

    catalog = ConversationActionCatalog(snapshot())
    tools, hashes = compile_deepseek_tools(catalog.tools(), strict=True)
    assert set(hashes) == catalog.names
    assert "$ref" not in json.dumps(tools)
    assert all(item["function"]["strict"] is True for item in tools)
    create = next(item for item in tools if item["function"]["name"] == "create")
    assert "parameters" not in create["function"]  # Existing strict compiler omits closed zero-argument parameters.
    assert parse_action_response(response("create"), catalog)[1].target.kind == "none"
    assert "requestedAction" not in json.dumps(create["function"])


def test_unavailable_reason_is_bound_to_requested_semantic_action_not_always_guide():
    from src.services.conversation_intent_router import ConversationIntentRouter

    state = snapshot(guideUnavailableReason="guide_evidence_missing")
    for wanted in ("modify", "continue_with_guide"):
        router = ConversationIntentRouter(
            routing_mode="active-all",
            lite_classifier=lambda _context: response("unavailable", {"requestedAction": wanted}),
        )
        result = router.classify("请执行请求", routing_snapshot=state)
        assert result.semantic_action["requestedAction"] == wanted
        assert result.semantic_action["guideUnavailableReason"] == (
            "guide_evidence_missing" if wanted == "continue_with_guide" else None
        )
        assert result.requires_clarification
    with pytest.raises(ValueError, match="requested_action_invalid"):
        parse_action_response(response("unavailable"), ConversationActionCatalog(state))


@pytest.mark.parametrize(
    "message",
    [
        "结合我的需求，参考网络攻略制定一份方案",
        "参考攻略建议的地点，生成新的方案",
        "参考攻略里面的景点，按照我的需求给出个方案",
        "基于普通攻略的建议，生成个方案",
        "基于搜索到的建议，给我生成一个方案出来",
        "按刚才推荐的地方再排一版",
        "能不能按这些建议排个方案？",
    ],
)
def test_active_selector_keeps_authority_server_side_and_accepts_polite_request(message):
    from src.services.conversation_intent_router import ConversationIntentRouter

    calls = []

    def classifier(context):
        calls.append(context)
        return response("continue_with_guide")

    state = snapshot(
        hasPlanningRoot=True,
        guideRequirement={"evidenceFingerprint": "PRIVATE-EVIDENCE"},
        capabilityMatches={"continue_plan_expansion": [{"choiceId": "PRIVATE-CHOICE"}]},
    )
    router = ConversationIntentRouter(lite_classifier=classifier, routing_mode="active-all")
    result = router.classify(message, routing_snapshot=state)
    assert result.continuation_mode == "guide_grounded"
    assert result.requires_clarification is False
    assert result.classification.intent == "continue_plan_expansion"
    assert len(calls) == 1
    assert "PRIVATE" not in json.dumps(calls)
    assert result.invocation_ledger["semanticActionCallCount"] == 1
    assert result.invocation_ledger["controllerLiteCallCount"] == 0
