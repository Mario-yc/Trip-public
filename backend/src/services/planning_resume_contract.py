"""Shared fail-closed contract for persisted planning checkpoints."""

from __future__ import annotations

from typing import Any


RESUMABLE_PLANNING_STATES = {
    "draft_needs_completion",
    "quality_contract_failed",
    "no_minimum_viable_day",
    "provider_rate_limited",
    "required_intent_unresolved",
    "waiting_for_poi_grounding",
    "waiting_for_required_intent",
    "semantic_candidate_hint_missing",
}


def has_resumable_checkpoint_material(payload: dict[str, Any]) -> bool:
    """Return true only when a checkpoint has an initial plan and resume state.

    A prior complete request is not a checkpoint.  In particular, an
    ``initialPlan`` paired with an empty ``pipelineContext`` and no directive,
    preview, or unresolved slot must never be advertised as reusable.
    """

    if not isinstance(payload, dict):
        return False
    grounding = payload.get("grounding")
    grounding = grounding if isinstance(grounding, dict) else {}
    initial_plan = payload.get("initialPlan") or grounding.get("initialPlan")
    if not isinstance(initial_plan, dict):
        return False
    pipeline_context = payload.get("pipelineContext") or grounding.get(
        "pipelineContext"
    )
    pipeline_context = (
        pipeline_context if isinstance(pipeline_context, dict) else {}
    )
    planning_directive = (
        payload.get("planningDirective")
        or grounding.get("planningDirective")
        or pipeline_context.get("planningDirective")
    )
    return any(
        isinstance(value, (dict, list)) and bool(value)
        for value in (
            pipeline_context,
            planning_directive,
            payload.get("planningPreview"),
            grounding.get("planningPreview"),
            payload.get("unresolvedSlots"),
            grounding.get("unresolvedSlots"),
        )
    )


def resumable_planning_checkpoint(
    payload: dict[str, Any],
    *,
    itinerary_version_id: object = None,
) -> bool:
    """Apply the single persisted-checkpoint eligibility contract."""

    if itinerary_version_id:
        return False
    if payload.get("resultVersionId") not in {None, ""}:
        return False
    if str(payload.get("terminalStatus") or "") not in {
        "",
        "failed",
        "needs_confirmation",
    }:
        return False
    grounding = payload.get("grounding")
    grounding = grounding if isinstance(grounding, dict) else {}
    finalization = grounding.get("finalization")
    finalization = finalization if isinstance(finalization, dict) else {}
    mode = str(payload.get("mode") or "")
    state = str(
        payload.get("resultState")
        or payload.get("planningStatus")
        or grounding.get("resultState")
        or ""
    )
    reason = str(
        payload.get("noVersionReason")
        or payload.get("reason")
        or grounding.get("noVersionReason")
        or grounding.get("reason")
        or finalization.get("unresolvedPolicy")
        or ""
    )
    if reason == "complete_itinerary_quality_contract_blocked":
        reason = "quality_contract_failed"
    if (
        mode not in {"staged_initial_pipeline_waiting", "staged_initial_pipeline_failed"}
        and state not in RESUMABLE_PLANNING_STATES
        and reason not in RESUMABLE_PLANNING_STATES
    ):
        return False
    if (
        state not in RESUMABLE_PLANNING_STATES
        and reason not in RESUMABLE_PLANNING_STATES
    ):
        return False
    return has_resumable_checkpoint_material(payload)
