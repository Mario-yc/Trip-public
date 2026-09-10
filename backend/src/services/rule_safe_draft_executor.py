from __future__ import annotations

from typing import Any, Callable, TypeVar

from fastapi import HTTPException


T = TypeVar("T")


class RuleSafeDraftExecutor:
    """Authorizes and invokes the deterministic draft route from a claimed persisted choice."""

    def execute(
        self,
        *,
        request_context: dict[str, Any],
        selected_choice: dict[str, Any],
        execution: dict[str, Any],
        invoke: Callable[[dict[str, Any]], T],
    ) -> T:
        option = selected_choice.get("option") if isinstance(selected_choice.get("option"), dict) else {}
        choice_id = str(selected_choice.get("persistedChoiceId") or "")
        action = str(selected_choice.get("persistedChoiceAction") or "")
        if (
            not choice_id
            or action != "confirm_rule_safe_draft"
            or choice_id != str(execution.get("choice_id") or "")
            or action != str(execution.get("action") or "")
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_choice_identity_mismatch", "message": "规则安全草稿授权身份不一致。"},
            )
        request_context.update(
            {
                "explicitRuleSafeDraft": True,
                "skipController": True,
                "controlOwner": "user_confirmed_safe_fallback",
                "fallbackReason": "controller_unavailable_user_confirmed_rule_safe_draft",
                "choiceExecutionId": execution.get("id"),
                "sourceDecisionId": option.get("sourceDecisionId"),
            }
        )
        return invoke(request_context)
