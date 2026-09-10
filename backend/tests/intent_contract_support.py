from __future__ import annotations

import json
from typing import Any


def intent_contract_for_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return a strict intent response for legacy business-controller doubles."""

    message = str(context.get("message") or "")
    state = context.get("state") if isinstance(context.get("state"), dict) else {}
    if state.get("hasUnansweredClarification"):
        intent = "clarification_answer"
        requested_scope = "clarification"
    elif state.get("hasActiveVersion") and any(
        marker in message for marker in ("修改为", "改成", "换成", "调整为", "改到")
    ):
        intent = "modify_itinerary"
        requested_scope = "active_itinerary"
    elif any(marker in message for marker in ("重试", "再试", "重新来")):
        intent = "retry_current_stage"
        requested_scope = "current_stage"
    elif state.get("hasActiveVersion"):
        intent = "modify_itinerary"
        requested_scope = "active_itinerary"
    else:
        intent = "create_itinerary"
        requested_scope = "new_itinerary"
    return {
        "intent": intent,
        "confidence": 0.99,
        "requestedScope": requested_scope,
        "isQuestion": False,
        "isNegated": "不要" in message or "别" in message,
    }


class IntentContractProviderMixin:
    """Add the new intent seam without changing a double's business behavior."""

    def decide_autonomy_lite(
        self,
        context: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        if context.get("schemaVersion") == "conversation-intent-context-v1":
            assert timeout_seconds > 0
            return json.dumps(
                intent_contract_for_context(context),
                ensure_ascii=False,
            )
        inherited = getattr(super(), "decide_autonomy_lite", None)
        if callable(inherited):
            return inherited(context, timeout_seconds=timeout_seconds)
        raise RuntimeError("controller_provider_unavailable")


def install_intent_contract(provider: Any) -> Any:
    inherited = provider.decide_autonomy_lite

    def decide_autonomy_lite(
        context: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> Any:
        if context.get("schemaVersion") == "conversation-intent-context-v1":
            return intent_contract_for_context(context)
        return inherited(context, timeout_seconds=timeout_seconds)

    provider.decide_autonomy_lite = decide_autonomy_lite
    return provider
