from __future__ import annotations

import copy
from typing import Any


class PortfolioPendingSlotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class PortfolioPendingSlotService:
    """Reconcile and bind metadata-only Portfolio slots from one active snapshot.

    The active version snapshot is authoritative. Grounding reports and labels
    can offer candidates, but they cannot cover or retarget a slot.
    """

    CONTEXT_KEYS = (
        "planningSelectionRootTurnId",
        "rootPortfolioId",
        "focusBriefId",
        "requestContractFingerprint",
    )
    SLOT_KEYS = ("briefId", "poolId", "planningSlotId", "dayNumber")

    @classmethod
    def reconcile(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(snapshot)
        context = cls.selection_context(result)
        focus_brief = context["focusBriefId"]
        cls._validate_snapshot_scope(result, focus_brief)
        existing_day_numbers = {
            int(day.get("dayNumber") or 0)
            for day in result.get("days") or []
            if isinstance(day, dict) and int(day.get("dayNumber") or 0) > 0
        }
        covered = {
            (
                str(metadata.get("creativeBriefId") or ""),
                str(metadata.get("poolId") or ""),
                str(metadata.get("planningSlotId") or ""),
                int(day.get("dayNumber") or 0),
            )
            for day in result.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            for metadata in [
                segment.get("semanticMetadata")
                if isinstance(segment.get("semanticMetadata"), dict)
                else {}
            ]
            if str(metadata.get("planningSlotId") or "")
        }
        pending: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, int]] = set()
        for raw in result.get("portfolioPendingSlots") or []:
            if not isinstance(raw, dict):
                raise PortfolioPendingSlotError(
                    "portfolio_pending_slot_invalid", "缺失槽位必须使用结构化对象。"
                )
            slot = copy.deepcopy(raw)
            key = cls.slot_key(slot)
            if key[0] != focus_brief:
                raise PortfolioPendingSlotError(
                    "portfolio_pending_slot_cross_brief",
                    "缺失槽位不属于当前冻结方案。",
                )
            if key[3] not in existing_day_numbers:
                raise PortfolioPendingSlotError(
                    "portfolio_pending_slot_day_missing",
                    "缺失槽位不属于当前时间轴中的任何一天。",
                )
            if key in seen:
                raise PortfolioPendingSlotError(
                    "portfolio_pending_slot_duplicate", "缺失槽位身份重复。"
                )
            seen.add(key)
            if key in covered:
                continue
            slot["state"] = "pending"
            pending.append(slot)
        pending.sort(
            key=lambda item: (
                int(item.get("dayNumber") or 0),
                str(item.get("startTime") or item.get("timeWindow") or ""),
                str(item.get("planningSlotId") or ""),
            )
        )
        result["portfolioPendingSlots"] = pending
        partial = (
            copy.deepcopy(result.get("portfolioPartialTimeline"))
            if isinstance(result.get("portfolioPartialTimeline"), dict)
            else {}
        )
        partial["pendingSlotCount"] = len(pending)
        if pending:
            partial["pendingSlotsStatus"] = "pending"
            partial["status"] = "partial"
            result["status"] = "partial"
        else:
            partial["pendingSlotsStatus"] = "completed"
            if partial.get("strictProposalVerifierPassed") is True:
                partial["status"] = "completed"
                result["status"] = "completed"
            else:
                partial["status"] = "partial"
                result["status"] = "partial"
        result["portfolioPartialTimeline"] = partial
        return result

    @classmethod
    def _validate_snapshot_scope(cls, snapshot: dict[str, Any], focus_brief: str) -> None:
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                metadata = (
                    segment.get("semanticMetadata")
                    if isinstance(segment.get("semanticMetadata"), dict)
                    else {}
                )
                segment_brief = str(metadata.get("creativeBriefId") or "").strip()
                if segment_brief and segment_brief != focus_brief:
                    raise PortfolioPendingSlotError(
                        "portfolio_segment_cross_brief",
                        "部分时间轴包含其他方案的地点谱系。",
                    )
        for binding in snapshot.get("portfolioRequiredCandidateBindings") or []:
            if not isinstance(binding, dict):
                continue
            binding_brief = str(binding.get("briefId") or "").strip()
            if binding_brief and binding_brief != focus_brief:
                raise PortfolioPendingSlotError(
                    "portfolio_required_binding_cross_brief",
                    "部分时间轴包含其他方案的必选地点谱系。",
                )
    @classmethod
    def selection_context(cls, snapshot: dict[str, Any]) -> dict[str, str]:
        raw = (
            snapshot.get("portfolioSelectionContext")
            if isinstance(snapshot.get("portfolioSelectionContext"), dict)
            else {}
        )
        context = {key: str(raw.get(key) or "").strip() for key in cls.CONTEXT_KEYS}
        creative = (
            snapshot.get("creativeBrief")
            if isinstance(snapshot.get("creativeBrief"), dict)
            else {}
        )
        creative_brief = str(creative.get("briefId") or creative.get("brief_id") or "").strip()
        if not all(context.values()):
            raise PortfolioPendingSlotError(
                "portfolio_selection_context_missing",
                "部分时间轴缺少冻结的规划选择身份。",
            )
        if creative_brief and creative_brief != context["focusBriefId"]:
            raise PortfolioPendingSlotError(
                "portfolio_selection_focus_mismatch",
                "部分时间轴与冻结方案身份不一致。",
            )
        return context

    @classmethod
    def slot_key(cls, value: dict[str, Any]) -> tuple[str, str, str, int]:
        brief_id = str(value.get("briefId") or value.get("focusBriefId") or "").strip()
        pool_id = str(value.get("poolId") or "").strip()
        planning_slot_id = str(value.get("planningSlotId") or value.get("slotId") or "").strip()
        day_number = int(value.get("dayNumber") or 0)
        if not brief_id or not pool_id or not planning_slot_id or day_number <= 0:
            raise PortfolioPendingSlotError(
                "portfolio_pending_slot_identity_missing",
                "缺失槽位缺少 brief、pool、slot 或 Day 身份。",
            )
        return brief_id, pool_id, planning_slot_id, day_number

    @classmethod
    def find_exact_slot(
        cls,
        snapshot: dict[str, Any],
        command: dict[str, Any],
    ) -> dict[str, Any]:
        reconciled = cls.reconcile(snapshot)
        context = cls.selection_context(reconciled)
        for key in cls.CONTEXT_KEYS:
            if str(command.get(key) or "").strip() != context[key]:
                raise PortfolioPendingSlotError(
                    "portfolio_selection_root_mismatch",
                    "补槽请求与当前冻结规划身份不一致。",
                )
        command_key = cls.slot_key(command)
        matches = [
            item
            for item in reconciled.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and cls.slot_key(item) == command_key
        ]
        if len(matches) != 1:
            raise PortfolioPendingSlotError(
                "portfolio_pending_slot_stale",
                "目标缺失槽位已失效或身份不唯一。",
            )
        return copy.deepcopy(matches[0])
