"""Semantic proposals only: this catalog never executes or grants a capability."""

from __future__ import annotations

import json
import copy
from dataclasses import dataclass
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from src.services.conversation_intent_router import IntentRoutingSnapshotV1, SemanticTargetReference


class SemanticActionArguments(BaseModel):
    target: SemanticTargetReference = Field(default_factory=SemanticTargetReference)
    requested_action: Optional[
        Literal[
            "create",
            "refine_request",
            "regenerate",
            "continue_directions",
            "continue_with_guide",
            "search_guide",
            "fill_slot",
            "search_candidate",
            "modify",
            "request_adoption",
            "retry",
            "answer_clarification",
        ]
    ] = Field(default=None, alias="requestedAction")

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class ConversationAction:
    name: str
    intent: str
    scope: str
    target: str
    description: str
    continuation_mode: str | None = None


ACTIONS = (
    ConversationAction(
        "create_from_shared_guide",
        "create_itinerary",
        "new_itinerary",
        "none",
        "用户在刚刚只读导入攻略后补充自己的旅行需求：基于这份已读取资料创建新方案，至少一处来源地点必须满足用户需求与真实地点核验。天数、预算等以本轮用户需求为准，不能采用作者的行程合同。",
    ),
    ConversationAction(
        "read_shared_guide",
        "inspect_or_explain",
        "none",
        "none",
        "用户提供小红书分享链接作为资料（纯链接或带分享文字），或要求根据这条新链接规划：先只读导入真实笔记正文，展示来源，再结合用户合同规划。不能将链接当作旅行需求、旧攻略或直接生成；引用讨论、否定读取则选择 explain/cancel。",
    ),
    ConversationAction(
        "explain", "inspect_or_explain", "none", "none", "只读问题、原因解释、讨论或引用一句话，不要求执行。"
    ),
    ConversationAction(
        "clarify",
        "inspect_or_explain",
        "none",
        "none",
        "无法确定用户要做哪个动作，或多个现有目标同样匹配，需询问。不是条件缺失：已理解动作但缺少上下文/证据/权限时选择 unavailable。",
    ),
    ConversationAction(
        "unavailable",
        "inspect_or_explain",
        "none",
        "none",
        "已理解用户希望执行的动作，但当前目录不提供该动作，或状态缺少所引用的攻略、方案、旅行根或权限。报告缺失条件；不得换成新建、普通续规划或语义澄清。",
    ),
    ConversationAction("cancel", "cancel_action", "current_action", "none", "用户明确取消或停止当前操作。"),
    ConversationAction(
        "create", "create_itinerary", "new_itinerary", "none", "开始新的旅行需求；不是在已有方案上继续探索。"
    ),
    ConversationAction(
        "refine_request",
        "create_itinerary",
        "planning_root",
        "planning_root",
        "用户明确改变或补充原旅行合同，需重新规划。不能用于沿用原需求生成其他方案。",
    ),
    ConversationAction(
        "regenerate", "regenerate_from_scratch", "full_task", "none", "用户明确要求从头重新规划，不是继续探索其他方向。"
    ),
    ConversationAction(
        "continue_directions",
        "continue_plan_expansion",
        "planning_root",
        "planning_root",
        "沿用原旅行需求继续探索其他方向，不要求采用攻略地点。",
    ),
    ConversationAction(
        "continue_with_guide",
        "continue_plan_expansion",
        "planning_root",
        "latest_guide",
        "沿用原旅行需求，参考刚才搜索/攻略/建议中的地点再给出方案；必须采用至少一个合格新攻略地点。礼貌问法也是请求。",
        "guide_grounded",
    ),
    ConversationAction(
        "search_guide",
        "search_travel_guide_advice",
        "planning_root",
        "planning_root",
        "搜索或重新搜索旅行攻略，提供只读建议，不生成方案。",
    ),
    ConversationAction(
        "fill_slot", "continue_pending_slot", "pending_slot", "pending_slot", "继续补齐已有待解决行程槽位。"
    ),
    ConversationAction(
        "search_candidate",
        "manual_candidate_search",
        "candidate_search",
        "pending_slot",
        "为待解决槽位按用户指定地点搜索候选。",
    ),
    ConversationAction("modify", "modify_itinerary", "active_itinerary", "current_itinerary", "修改已采用的当前行程。"),
    ConversationAction(
        "request_adoption",
        "adopt_plan",
        "portfolio",
        "portfolio",
        "请求采用已有方案；只定位并展示确认能力，不直接授权提交。",
    ),
    ConversationAction("retry", "retry_current_stage", "current_stage", "current_stage", "明确重试当前失败阶段。"),
    ConversationAction(
        "answer_clarification",
        "clarification_answer",
        "clarification",
        "clarification",
        "回答当前服务端待答的澄清问题，不适用于无关新请求。",
    ),
)


def allowed_targets(action: ConversationAction) -> set[str]:
    targets = {"none", action.target}
    if action.name == "modify":
        targets.update(("day", "time_window"))
    if action.name == "request_adoption":
        targets.add("proposal_ordinal")
    return targets


# Only these actions need model-selected semantic coordinates. All other
# targets come from the selected catalog action, never from model authority.
_TARGET_FIELDS = {
    "modify": {"kind", "dayNumber", "timeBucket", "mentionText"},
    "fill_slot": {"kind", "dayNumber"},
    "search_candidate": {"kind", "dayNumber"},
    "request_adoption": {"kind", "ordinal"},
}


def _closed_arguments(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("semantic_action_contract_invalid")
        result[key] = value
    return result


class ConversationActionCatalog:
    def __init__(self, snapshot: IntentRoutingSnapshotV1):
        state = snapshot.server_state
        matches = state.get("capabilityMatches") or {}
        available = {"explain", "clarify", "unavailable", "cancel"}
        if not state.get("hasActiveVersion") and not state.get("pendingClarification"):
            available.add("create")
        if state.get("hasPlanningRoot"):
            available.update(("refine_request", "regenerate"))
        if state.get("hasActiveVersion"):
            available.update(("modify", "regenerate"))
        if state.get("pendingClarification"):
            available.add("answer_clarification")
        for action in ACTIONS:
            if matches.get(action.intent):
                available.add(action.name)
        if not state.get("guideRequirement"):
            available.discard("continue_with_guide")
        if state.get("sharedInitialSource"):
            available.discard("create")
            available.add("create_from_shared_guide")
        elif state.get("sharedInitialSourceInvalid"):
            available.discard("create")
        if state.get("sharedLinkInput"):
            # Intake a new evidence object first. It is not permission to use
            # a previous guide, create a root, or mutate the user's contract.
            available = {"read_shared_guide", "explain", "clarify", "unavailable", "cancel"}
        self.actions = tuple(action for action in ACTIONS if action.name in available)
        self.names = frozenset(action.name for action in self.actions)

    def tools(self) -> list[dict[str, Any]]:
        # A small shared semantic schema: no opaque authority in arguments.
        schema = SemanticActionArguments.model_json_schema(by_alias=True)
        definitions = schema.pop("$defs", {})

        def inline(node: Any) -> Any:
            if isinstance(node, list):
                return [inline(item) for item in node]
            if not isinstance(node, dict):
                return node
            if "$ref" in node:
                reference = str(node["$ref"])
                if not reference.startswith("#/$defs/"):
                    raise ValueError("semantic_action_schema_reference_invalid")
                return inline(
                    {
                        **definitions[reference.removeprefix("#/$defs/")],
                        **{key: value for key, value in node.items() if key != "$ref"},
                    }
                )
            return {key: inline(value) for key, value in node.items()}

        schema = inline(schema)
        tools = []
        for item in self.actions:
            parameters = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
            target_fields = _TARGET_FIELDS.get(item.name)
            if target_fields:
                target = copy.deepcopy(schema["properties"]["target"])
                target["properties"] = {
                    key: value for key, value in target["properties"].items() if key in target_fields
                }
                target["properties"]["kind"]["enum"] = sorted(allowed_targets(item))
                parameters["properties"]["target"] = target
            if item.name == "unavailable":
                parameters["required"].append("requestedAction")
                parameters["properties"]["requestedAction"] = {
                    **schema["properties"]["requestedAction"]["anyOf"][0],
                    "description": "已经理解但当前无法执行的语义动作，仅作缺失条件说明，不授予权限。",
                }
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": parameters,
                    },
                }
            )
        return tools


def parse_action_response(
    body: Any, catalog: ConversationActionCatalog
) -> tuple[ConversationAction, SemanticActionArguments]:
    try:
        choices = body["choices"]
        if len(choices) != 1 or choices[0]["finish_reason"] != "tool_calls":
            raise ValueError("semantic_action_response_incomplete")
        message = choices[0]["message"]
        calls = message["tool_calls"]
        if message.get("refusal") or len(calls) != 1 or calls[0]["type"] != "function":
            raise ValueError("semantic_action_call_count_invalid")
        function = calls[0]["function"]
        action = next((item for item in catalog.actions if item.name == function["name"]), None)
        if action is None:
            raise ValueError("semantic_action_unavailable")
        raw = function["arguments"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
            raise ValueError("semantic_action_arguments_invalid")
        parsed = json.loads(raw, object_pairs_hook=_closed_arguments)
        if not isinstance(parsed, dict):
            raise ValueError("semantic_action_contract_invalid")
        if (action.name == "unavailable" and parsed.get("requestedAction") is None) or (
            action.name != "unavailable" and "requestedAction" in parsed
        ):
            raise ValueError("semantic_action_requested_action_invalid")
        published = (
            {"requestedAction"}
            if action.name == "unavailable"
            else {"target"}
            if action.name in _TARGET_FIELDS
            else set()
        )
        if set(parsed) - published:
            raise ValueError("semantic_action_contract_invalid")
        if "target" in parsed:
            target = parsed["target"]
            if not isinstance(target, dict) or set(target) - _TARGET_FIELDS[action.name]:
                raise ValueError("semantic_action_contract_invalid")
        arguments = SemanticActionArguments.model_validate(parsed)
        if (action.name == "unavailable" and arguments.requested_action is None) or (
            action.name != "unavailable" and "requested_action" in arguments.model_fields_set
        ):
            raise ValueError("semantic_action_requested_action_invalid")
        if arguments.target.kind not in allowed_targets(action):
            raise ValueError("semantic_action_target_mismatch")
        if arguments.target.kind == "none":
            arguments.target.kind = action.target
        return action, arguments
    except (KeyError, IndexError, TypeError, ValidationError, json.JSONDecodeError) as error:
        raise ValueError("semantic_action_contract_invalid") from error
