from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .portfolio_pending_slot_service import PortfolioPendingSlotError, PortfolioPendingSlotService


DENSITY_CANDIDATE_ACTION = "resume_density_candidate"
DENSITY_REFRESH_ACTION = "refresh_density_candidates"
DENSITY_NEARBY_ACTION = "expand_density_nearby"
DENSITY_MAP_ACTION = "open_density_map"
DENSITY_MANUAL_SEARCH_ACTION = "search_density_manual_candidates"
DENSITY_ACTIONS = {
    DENSITY_CANDIDATE_ACTION,
    DENSITY_REFRESH_ACTION,
    DENSITY_NEARBY_ACTION,
    DENSITY_MAP_ACTION,
    DENSITY_MANUAL_SEARCH_ACTION,
}

_RUNTIME_STARTED_AT = datetime.now(timezone.utc).isoformat()


class PortfolioDensityContinuationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PortfolioDensityCheckpoint:
    payload: dict[str, Any]
    fingerprint: str


class PortfolioDensityContinuationService:
    """Build and validate the durable pause/resume envelope for density choices."""

    TTL_MINUTES = 60

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @staticmethod
    def is_density_option(option: Any) -> bool:
        if not isinstance(option, dict):
            return False
        kind = str(option.get("kind") or "")
        action = str(option.get("action") or "")
        if kind in {"portfolio_density_candidate", "portfolio_density_retry", "portfolio_density_map"}:
            return True
        if PortfolioDensityContinuationService._is_scoped_manual_option(option):
            return True
        return action in DENSITY_ACTIONS

    @staticmethod
    def normalized_action(option: dict[str, Any]) -> str:
        action = str(option.get("action") or "")
        kind = str(option.get("kind") or "")
        if action in DENSITY_ACTIONS:
            return action
        if kind == "portfolio_density_candidate":
            return DENSITY_CANDIDATE_ACTION
        if kind == "portfolio_density_map":
            return DENSITY_MAP_ACTION
        if kind == "portfolio_density_retry":
            return (
                DENSITY_NEARBY_ACTION
                if str(option.get("retryMode") or "") == "expand_nearby"
                else DENSITY_REFRESH_ACTION
            )
        if PortfolioDensityContinuationService._is_scoped_manual_option(option):
            return DENSITY_MANUAL_SEARCH_ACTION
        return action

    @staticmethod
    def _is_scoped_manual_option(option: dict[str, Any]) -> bool:
        if (
            str(option.get("kind") or "") != "custom_input"
            or str(option.get("action") or "") != "manual_continuation"
        ):
            return False
        return all(
            option.get(key) not in (None, "")
            for key in (
                "briefId",
                "poolId",
                "planningSlotId",
                "dayNumber",
                "intentType",
            )
        )

    @classmethod
    def issue_reissued_checkpoint(
        cls,
        *,
        source_assistant_turn_id: str,
        option: dict[str, Any],
        active_snapshot: dict[str, Any],
        expected_base_version_id: str,
    ) -> dict[str, Any]:
        """Sign a remaining choice from the newly committed active snapshot."""
        selection = PortfolioPendingSlotService.selection_context(active_snapshot)
        slot = PortfolioPendingSlotService.find_exact_slot(
            active_snapshot,
            {**selection, **option},
        )
        payload = {
            "schemaVersion": "portfolio-density-checkpoint-v2",
            "sourceAssistantTurnId": source_assistant_turn_id,
            "choiceId": str(option.get("id") or ""),
            "normalizedAction": cls.normalized_action(option),
            "expectedBaseVersionId": expected_base_version_id,
            "planningSelectionRootTurnId": selection["planningSelectionRootTurnId"],
            "rootPortfolioId": selection["rootPortfolioId"],
            "requestContractFingerprint": selection["requestContractFingerprint"],
            "focusBriefId": selection["focusBriefId"],
            "briefId": str(slot.get("briefId") or ""),
            "poolId": str(slot.get("poolId") or ""),
            "planningSlotId": str(slot.get("planningSlotId") or ""),
            "dayNumber": int(slot.get("dayNumber") or 0),
            "intentType": str(slot.get("intentType") or ""),
            "candidateRecordId": str(option.get("candidateRecordId") or ""),
            "amapId": str(option.get("amapId") or ""),
            "expiresAt": str(option.get("expiresAt") or ""),
        }
        payload["checkpointFingerprint"] = hashlib.sha256(
            cls._dump(payload).encode("utf-8")
        ).hexdigest()
        return payload

    def load_checkpoint(
        self,
        *,
        session_id: str,
        source_assistant_turn_id: str,
        option: dict[str, Any],
    ) -> PortfolioDensityCheckpoint:
        row = self.db.execute(
            """
            SELECT id, turn_index, created_at, agent_request_json, agent_response_json, itinerary_version_id
            FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'
            """,
            (source_assistant_turn_id, session_id),
        ).fetchone()
        if row is None:
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少原规划上下文，请刷新缺失地点候选后重试。",
            )
        request = self._json_dict(row["agent_request_json"])
        response = self._json_dict(row["agent_response_json"])
        persisted_option = next(
            (
                item
                for item in response.get("choiceOptions") or []
                if isinstance(item, dict) and str(item.get("id") or "") == str(option.get("id") or "")
            ),
            None,
        )
        if persisted_option is None:
            raise PortfolioDensityContinuationError(
                "agent_choice_identity_mismatch",
                "该地点选择不属于指定规划暂停点，请刷新后重试。",
            )
        self._validate_scope(option, persisted_option)
        expected_base_version_id = str(
            persisted_option.get("expectedBaseVersionId")
            or row["itinerary_version_id"]
            or ""
        )
        session_row = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择所属会话已失效，请刷新后重试。",
            )
        current_base_version_id = str(session_row["active_version_id"] or "")
        if (
            expected_base_version_id
            and expected_base_version_id != current_base_version_id
        ):
            raise PortfolioDensityContinuationError(
                "agent_choice_target_stale",
                "该地点选择基于旧行程版本，请使用最新回复中的候选。",
            )
        expires_at = self._expires_at(persisted_option, str(row["created_at"] or ""))
        if expires_at <= datetime.now(timezone.utc):
            raise PortfolioDensityContinuationError(
                "plan_proposal_expired",
                "该地点确认已超过可选择时限，请刷新缺失地点候选。",
            )
        active_version = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
            (current_base_version_id, session_id),
        ).fetchone()
        if active_version is None:
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少当前行程快照，请刷新后重试。",
            )
        try:
            active_snapshot = PortfolioPendingSlotService.reconcile(
                self._json_dict(active_version["snapshot_json"])
            )
            selection_context = PortfolioPendingSlotService.selection_context(active_snapshot)
        except PortfolioPendingSlotError as error:
            raise PortfolioDensityContinuationError(error.code, error.message) from error
        directive = request.get("planningDirective")
        if not isinstance(directive, dict):
            decision = request.get("agentDecision")
            directive = decision.get("actionDirective") if isinstance(decision, dict) else None
        if not isinstance(directive, dict) or directive.get("type") != "draft_itinerary":
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少原规划 directive，请刷新缺失地点候选后重试。",
            )
        grounding = response.get("grounding") if isinstance(response.get("grounding"), dict) else {}
        initial_plan = response.get("initialPlan") or grounding.get("initialPlan")
        pipeline_context = response.get("pipelineContext") or grounding.get("pipelineContext")
        if not isinstance(initial_plan, dict) or not isinstance(pipeline_context, dict):
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少可续跑的 DaySlot/IntentPool，请刷新后重试。",
            )
        contract = request.get("requestIntentContract")
        dates = request.get("resolvedTripDates")
        effective_message = str(
            request.get("effectiveUserMessage")
            or request.get("sourceUserRequest")
            or request.get("latestUserMessage")
            or ""
        ).strip()
        if not isinstance(contract, dict) or not isinstance(dates, dict) or not effective_message:
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少原始需求合同，请刷新后重试。",
            )
        density_groups = [
            item for item in persisted_option.get("densityGroups") or [] if isinstance(item, dict)
        ]
        primary_group = density_groups[0] if density_groups else {}
        scope = {
            "briefId": persisted_option.get("briefId") or primary_group.get("briefId"),
            "poolId": persisted_option.get("poolId") or primary_group.get("poolId"),
            "planningSlotId": (
                persisted_option.get("planningSlotId")
                or primary_group.get("planningSlotId")
                or primary_group.get("slotId")
            ),
            "dayNumber": persisted_option.get("dayNumber") or primary_group.get("dayNumber"),
        }
        # A direct-scope legacy option has no densityGroups.  Its display and
        # request fields are never used to reconstruct a slot: only its
        # persisted opaque option id selects one exact member of the current
        # active snapshot.  The snapshot is also the source for DaySlot and
        # IntentPool reconstruction below.
        try:
            authoritative_slot = PortfolioPendingSlotService.find_exact_slot(
                active_snapshot,
                {
                    **selection_context,
                    **scope,
                },
            )
        except PortfolioPendingSlotError as error:
            raise PortfolioDensityContinuationError(error.code, error.message) from error
        authoritative_scope = {
            "briefId": authoritative_slot.get("briefId"),
            "poolId": authoritative_slot.get("poolId"),
            "planningSlotId": authoritative_slot.get("planningSlotId"),
            "dayNumber": authoritative_slot.get("dayNumber"),
        }
        if scope != authoritative_scope:
            raise PortfolioDensityContinuationError(
                "agent_choice_identity_mismatch",
                "该地点选择的暂停槽位与当前持久化方案身份不一致。",
            )
        if density_groups:
            # Newer options carry their complete checkpoint.  Every checkpoint
            # scope field must agree with the one current snapshot member.
            group_scope = {
                "briefId": primary_group.get("briefId"),
                "poolId": primary_group.get("poolId"),
                "planningSlotId": primary_group.get("planningSlotId") or primary_group.get("slotId"),
                "dayNumber": primary_group.get("dayNumber"),
            }
            if group_scope != authoritative_scope:
                raise PortfolioDensityContinuationError(
                    "agent_choice_identity_mismatch",
                    "该地点选择的 checkpoint 与当前持久化槽位不一致。",
                )
        primary_group = authoritative_slot
        scope = authoritative_scope
        signed_checkpoint = persisted_option.get("continuationCheckpoint")
        if (
            isinstance(signed_checkpoint, dict)
            and str(signed_checkpoint.get("schemaVersion") or "")
            == "portfolio-density-checkpoint-v2"
        ):
            supplied_fingerprint = str(signed_checkpoint.get("checkpointFingerprint") or "")
            unsigned = {key: value for key, value in signed_checkpoint.items() if key != "checkpointFingerprint"}
            if not supplied_fingerprint or supplied_fingerprint != hashlib.sha256(
                self._dump(unsigned).encode("utf-8")
            ).hexdigest():
                raise PortfolioDensityContinuationError(
                    "agent_choice_identity_mismatch", "该地点选择的 checkpoint 指纹无效。"
                )
            expected_checkpoint = {
                "sourceAssistantTurnId": source_assistant_turn_id,
                "choiceId": str(persisted_option.get("id") or ""),
                "expectedBaseVersionId": expected_base_version_id,
                **selection_context,
                **authoritative_scope,
                "intentType": str(authoritative_slot.get("intentType") or ""),
                "candidateRecordId": str(persisted_option.get("candidateRecordId") or ""),
                "amapId": str(persisted_option.get("amapId") or ""),
            }
            if any(str(signed_checkpoint.get(key) or "") != str(value or "") for key, value in expected_checkpoint.items()):
                raise PortfolioDensityContinuationError(
                    "agent_choice_identity_mismatch", "该地点选择的 checkpoint 与当前持久化身份不一致。"
                )
        if self._later_checkpoint_covers_scope(
            session_id=session_id,
            source_turn_index=int(row["turn_index"] or 0),
            scope=scope,
        ):
            raise PortfolioDensityContinuationError(
                "agent_choice_target_stale",
                "该缺失槽位已在后续确认中补齐，请使用最新方案或继续确认下一处地点。",
            )
        initial_plan = self._scope_initial_plan(
            initial_plan,
            creative_portfolio=(
                response.get("creativePortfolio")
                if isinstance(response.get("creativePortfolio"), dict)
                else {}
            ),
            scope=scope,
            primary_group=primary_group,
            city=str(active_snapshot.get("city") or request.get("city") or pipeline_context.get("city") or ""),
        )
        payload = {
            "schemaVersion": "portfolio-density-continuation-v1",
            "normalizedAction": self.normalized_action(persisted_option),
            "sourceAssistantTurnId": source_assistant_turn_id,
            "sourceUserTurnId": persisted_option.get("sourceUserTurnId") or request.get("currentUserTurnId"),
            "planningRunId": response.get("planningRunId"),
            "expectedBaseVersionId": expected_base_version_id or None,
            "choiceId": persisted_option.get("id"),
            "briefId": persisted_option.get("briefId") or primary_group.get("briefId"),
            "poolId": persisted_option.get("poolId") or primary_group.get("poolId"),
            "planningSlotId": (
                persisted_option.get("planningSlotId")
                or primary_group.get("planningSlotId")
                or primary_group.get("slotId")
            ),
            "dayNumber": persisted_option.get("dayNumber") or primary_group.get("dayNumber"),
            "intentType": persisted_option.get("intentType") or primary_group.get("intentType"),
            "candidateRecordId": persisted_option.get("candidateRecordId"),
            "amapId": persisted_option.get("amapId"),
            "retryMode": persisted_option.get("retryMode"),
            "densityGroups": density_groups,
            "expiresAt": expires_at.isoformat(),
            "effectiveUserMessage": effective_message,
            "sourceUserRequest": request.get("sourceUserRequest") or effective_message,
            "resolvedTripDates": dates,
            "requestIntentContract": contract,
            "planningDirective": directive,
            "initialPlan": initial_plan,
            "pipelineContext": pipeline_context,
            "planningSelectionRootTurnId": str(
                pipeline_context.get("planningSelectionRootTurnId")
                or request.get("planningSelectionRootTurnId")
                or source_assistant_turn_id
            ),
            "rootPortfolioId": str(
                persisted_option.get("rootPortfolioId")
                or pipeline_context.get("rootPortfolioId")
                or response.get("rootPortfolioId")
                or ""
            ),
            "focusBriefId": str(
                persisted_option.get("focusBriefId")
                or pipeline_context.get("portfolioGroundingFocusBriefId")
                or pipeline_context.get("focusBriefId")
                or persisted_option.get("briefId")
                or primary_group.get("briefId")
                or ""
            ),
            "requestContractFingerprint": str(
                persisted_option.get("requestContractFingerprint")
                or pipeline_context.get("requestContractFingerprint")
                or hashlib.sha256(self._dump(contract).encode("utf-8")).hexdigest()
            ),
            "constraintLedger": response.get("constraintLedger"),
            "creativePortfolio": response.get("creativePortfolio"),
            "groundingCheckpoint": response.get("groundingCheckpoint"),
            "runtime": self.runtime_info(),
        }
        fingerprint = hashlib.sha256(self._dump(payload).encode("utf-8")).hexdigest()
        payload["checkpointFingerprint"] = fingerprint
        return PortfolioDensityCheckpoint(payload=payload, fingerprint=fingerprint)

    @classmethod
    def _scope_initial_plan(
        cls,
        initial_plan: dict[str, Any],
        *,
        creative_portfolio: dict[str, Any] | None = None,
        scope: dict[str, Any],
        primary_group: dict[str, Any],
        city: str,
    ) -> dict[str, Any]:
        """Keep the source plan only when it owns the persisted choice scope.

        A partial comparison can be projected from a different creative brief
        than the source turn's first ``initialPlan``.  The opaque density
        choice remains authoritative, so rebuild the smallest exact
        DaySlot/IntentPool pair from its persisted group instead of retrying an
        unrelated brief or accepting client-supplied scope.
        """

        brief_id = str(scope.get("briefId") or "").strip()
        pool_id = str(scope.get("poolId") or "").strip()
        slot_id = str(scope.get("planningSlotId") or "").strip()
        day_number = int(scope.get("dayNumber") or 0)
        slots = [item for item in initial_plan.get("daySlots") or [] if isinstance(item, dict)]
        pools = [item for item in initial_plan.get("intentPools") or [] if isinstance(item, dict)]
        slot_matches = any(
            str(item.get("slotId") or "") == slot_id
            and int(item.get("dayNumber") or 0) == day_number
            for item in slots
        )
        pool_matches = any(
            str(item.get("briefId") or "") == brief_id
            and str(item.get("poolId") or "") == pool_id
            and slot_id in {str(value or "") for value in item.get("assignToSlots") or []}
            for item in pools
        )
        if slot_matches and pool_matches:
            return initial_plan

        # The persisted portfolio is the authoritative owner of every slot in
        # the focused creative brief. Restore that complete plan so a scoped
        # retry can add one candidate without making the root verifier forget
        # already grounded required occurrences from the other days.
        matching_proposals = [
            item
            for item in (creative_portfolio or {}).get("proposals") or []
            if isinstance(item, dict)
            and str((item.get("brief") or {}).get("briefId") or "").strip() == brief_id
        ]
        if len(matching_proposals) == 1:
            proposal = matching_proposals[0]
            proposal_slots = [
                item for item in proposal.get("daySlots") or [] if isinstance(item, dict)
            ]
            proposal_pools = [
                item for item in proposal.get("intentPools") or [] if isinstance(item, dict)
            ]
            proposal_slot_matches = any(
                str(item.get("slotId") or "") == slot_id
                and int(item.get("dayNumber") or 0) == day_number
                for item in proposal_slots
            )
            proposal_pool_matches = any(
                str(item.get("briefId") or "") == brief_id
                and str(item.get("poolId") or "") == pool_id
                and slot_id in {str(value or "") for value in item.get("assignToSlots") or []}
                for item in proposal_pools
            )
            if proposal_slot_matches and proposal_pool_matches:
                restored_slots: list[dict[str, Any]] = []
                default_start_times = {
                    "morning": "09:00",
                    "上午": "09:00",
                    "noon": "12:00",
                    "midday": "12:00",
                    "中午": "12:00",
                    "afternoon": "14:00",
                    "下午": "14:00",
                    "evening": "18:00",
                    "night": "18:00",
                    "傍晚": "18:00",
                    "晚上": "18:00",
                    "夜间": "18:00",
                }
                for item in proposal_slots:
                    restored = copy.deepcopy(item)
                    start_time = str(restored.get("startTime") or "").strip()
                    time_window = str(restored.get("timeWindow") or "").strip()
                    if not start_time:
                        window_token = time_window.lower()
                        start_time = default_start_times.get(window_token, "")
                        if not start_time and len(time_window) >= 5 and time_window[2:3] == ":":
                            start_time = time_window[:5]
                    # Creative proposal slots deliberately permit an unknown
                    # start time, while AgentInitialPlanOutput requires a
                    # string. Preserve the uncertainty as an empty string (or
                    # a deterministic window start) instead of failing before
                    # the scoped provider search can run.
                    restored["startTime"] = start_time
                    restored_slots.append(restored)
                return {
                    "reply": "从持久化 Portfolio 恢复目标方向的完整计划。",
                    "mode": "initial_plan",
                    "daySlots": restored_slots,
                    "intentPools": copy.deepcopy(proposal_pools),
                    "warnings": [],
                }

        persisted_scope = {
            "briefId": str(primary_group.get("briefId") or "").strip(),
            "poolId": str(primary_group.get("poolId") or "").strip(),
            "planningSlotId": str(
                primary_group.get("planningSlotId") or primary_group.get("slotId") or ""
            ).strip(),
            "dayNumber": int(primary_group.get("dayNumber") or 0),
        }
        if persisted_scope != {
            "briefId": brief_id,
            "poolId": pool_id,
            "planningSlotId": slot_id,
            "dayNumber": day_number,
        }:
            raise PortfolioDensityContinuationError(
                "agent_choice_identity_mismatch",
                "该地点选择的暂停槽位与持久化方案身份不一致。",
            )

        time_window = str(primary_group.get("timeWindow") or "")
        start_time = str(primary_group.get("startTime") or "")
        duration_minutes = int(primary_group.get("durationMinutes") or 0)
        requirement_level = str(primary_group.get("requirementLevel") or "").lower()
        goal_id = str(primary_group.get("sourceGoalId") or "").strip()
        intent_type = str(primary_group.get("intentType") or "visit")
        return {
            "reply": "从持久化部分方案恢复精确待补槽位。",
            "mode": "initial_plan",
            "daySlots": [
                {
                    "slotId": slot_id,
                    "dayNumber": day_number,
                    "date": primary_group.get("date"),
                    "timeWindow": time_window,
                    "startTime": start_time,
                    "durationMinutes": duration_minutes,
                    "kind": str(primary_group.get("kind") or intent_type),
                    "rawNeed": str(
                        primary_group.get("rawNeed")
                        or primary_group.get("displayNeed")
                        or intent_type
                    ),
                    "routeAnchor": True,
                    "priority": int(primary_group.get("priority") or 0),
                    "notes": "由持久化 opaque choice 的 exact pending slot 恢复。",
                }
            ],
            "intentPools": [
                {
                    "poolId": pool_id,
                    "briefId": brief_id,
                    "rawNeed": str(
                        primary_group.get("rawNeed")
                        or primary_group.get("displayNeed")
                        or intent_type
                    ),
                    "city": city,
                    "intentType": intent_type,
                    "targetCount": 1,
                    "requirementLevel": (
                        "required" if requirement_level in {"hard", "required"} else "optional"
                    ),
                    "hardGoalId": goal_id if requirement_level in {"hard", "required"} else None,
                    "softGoalId": goal_id if requirement_level not in {"hard", "required"} else None,
                    "assignToSlots": [slot_id],
                    "candidateHints": [],
                    "hintPolicy": "no_hint",
                    "entityBindingMode": "category",
                }
            ],
            "warnings": [],
        }

    @staticmethod
    def apply_to_request_context(
        request_context: dict[str, Any], checkpoint: PortfolioDensityCheckpoint
    ) -> None:
        payload = checkpoint.payload
        request_context.update(
            {
                "sourceUserRequest": payload["sourceUserRequest"],
                "effectiveUserMessage": payload["effectiveUserMessage"],
                "resolvedTripDates": payload["resolvedTripDates"],
                "requestIntentContract": payload["requestIntentContract"],
                "planningDirective": payload["planningDirective"],
                "resumePlanningAttempt": {
                    "enabled": True,
                    "sourceAssistantTurnId": payload["sourceAssistantTurnId"],
                    "sourceUserTurnId": payload.get("sourceUserTurnId"),
                    "reason": "portfolio_density_structured_resume",
                    "resumeFromStage": "collect_candidates",
                    "reuseInitialPlan": True,
                    "retryOnlyUnresolvedSlots": True,
                    "initialPlan": payload["initialPlan"],
                    "pipelineContext": payload["pipelineContext"],
                    "constraintLedger": payload.get("constraintLedger"),
                    "creativePortfolio": payload.get("creativePortfolio"),
                    "groundingCheckpoint": payload.get("groundingCheckpoint"),
                },
                "portfolioDensityContinuation": payload,
                "structuredPlanningChoiceResume": True,
                "skipPostObservationController": True,
                "syntheticAgentChoice": True,
            }
        )
        request_context["planningSelectionRootTurnId"] = payload[
            "planningSelectionRootTurnId"
        ]
        request_context["rootPortfolioId"] = payload["rootPortfolioId"]
        request_context["portfolioGroundingFocusBriefId"] = payload["focusBriefId"]
        request_context["requestContractFingerprint"] = payload[
            "requestContractFingerprint"
        ]
        # A density button is an opaque continuation command, not a new user
        # request. Record a synthetic decision so trace consumers can prove
        # that no root controller/model was called for this confirmation turn.
        decision_state = {
            "decisionId": f"density:{payload.get('choiceId') or checkpoint.fingerprint[:16]}",
            "source": "persisted_structured_choice",
            "controlOwner": "portfolio_density_continuation",
            "decisionPath": "structured_choice",
            "primaryAction": "draft_itinerary",
            "accepted": True,
            "decisionSummary": "按持久化的密度确认续跑原 Portfolio。",
            "actualExecutionRoute": "portfolio_density_resume",
            "proposedExecutionRoute": "portfolio_density_resume",
            "controllerCalled": False,
            "controllerSucceeded": False,
            "controllerFullCalled": False,
            "controllerFullSucceeded": False,
            "controllerLiteCalled": False,
            "controllerLiteSucceeded": False,
            "structuredPlanningChoiceResume": True,
            "checkpointFingerprint": checkpoint.fingerprint,
        }
        request_context["agentDecisionState"] = decision_state
        request_context["agentDecision"] = {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": "draft_itinerary",
            "actionDirective": payload["planningDirective"],
        }
        request_context["actualExecutionRoute"] = "portfolio_density_resume"

    @staticmethod
    def made_progress(response: Any, checkpoint: dict[str, Any]) -> bool:
        # Search/planning metadata is proposal evidence, not user confirmation.
        # A slot is covered only by a committed timeline mutation whose result
        # snapshot removes the exact SlotKey. That proof is evaluated by the
        # transaction path; refresh/nearby staging can never claim progress.
        return False

    @staticmethod
    def has_new_candidate_options(response: Any, checkpoint: dict[str, Any]) -> bool:
        """Return whether a retry exposed a new scoped AMap candidate.

        Candidate record ids are intentionally ignored because a refresh may
        persist the same physical POI under a new record. Progress is the
        appearance of a new brief/pool/slot/day/amap identity.
        """

        target_scope = (
            str(checkpoint.get("briefId") or ""),
            str(checkpoint.get("poolId") or ""),
            str(checkpoint.get("planningSlotId") or ""),
            str(checkpoint.get("dayNumber") or ""),
        )
        if not all(target_scope):
            return False

        previous: set[tuple[str, str, str, str, str]] = set()
        for group in checkpoint.get("densityGroups") or []:
            if not isinstance(group, dict):
                continue
            group_scope = (
                str(group.get("briefId") or ""),
                str(group.get("poolId") or ""),
                str(group.get("planningSlotId") or group.get("slotId") or ""),
                str(group.get("dayNumber") or ""),
            )
            if group_scope != target_scope:
                continue
            for candidate in group.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                amap_id = str(candidate.get("amapId") or candidate.get("id") or "")
                if amap_id:
                    previous.add((*group_scope, amap_id))

        current: set[tuple[str, str, str, str, str]] = set()
        assistant_turn = getattr(response, "assistant_turn", None)
        for option in list(getattr(assistant_turn, "choice_options", None) or []):
            if not isinstance(option, dict):
                continue
            if str(option.get("kind") or "") != "portfolio_density_candidate":
                continue
            option_scope = (
                str(option.get("briefId") or ""),
                str(option.get("poolId") or ""),
                str(option.get("planningSlotId") or ""),
                str(option.get("dayNumber") or ""),
            )
            amap_id = str(option.get("amapId") or "")
            if option_scope == target_scope and amap_id:
                current.add((*option_scope, amap_id))
        return bool(current - previous)

    def _later_checkpoint_covers_scope(
        self,
        *,
        session_id: str,
        source_turn_index: int,
        scope: dict[str, Any],
    ) -> bool:
        brief_id = str(scope.get("briefId") or "")
        pool_id = str(scope.get("poolId") or "")
        slot_id = str(scope.get("planningSlotId") or "")
        day_number = str(scope.get("dayNumber") or "")
        if not all((brief_id, pool_id, slot_id, day_number)):
            return False
        rows = self.db.execute(
            """
            SELECT agent_response_json
            FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status = 'active'
              AND turn_index > ? AND agent_response_json IS NOT NULL
            ORDER BY turn_index DESC
            """,
            (session_id, source_turn_index),
        ).fetchall()
        for row in rows:
            response = self._json_dict(row["agent_response_json"])
            grounding = (
                response.get("groundingCheckpoint")
                if isinstance(response.get("groundingCheckpoint"), dict)
                else {}
            )
            for report in grounding.get("poolReports") or []:
                if not isinstance(report, dict):
                    continue
                if str(report.get("briefId") or "") != brief_id:
                    continue
                if str(report.get("poolId") or "") != pool_id:
                    continue
                slot_days = report.get("slotDayNumbers")
                scoped_day = slot_days.get(slot_id) if isinstance(slot_days, dict) else None
                if str(scoped_day or "") != day_number:
                    continue
                if slot_id in {
                    str(item) for item in report.get("resolvedSlotIds") or [] if str(item)
                }:
                    return True
        return False

    @staticmethod
    def runtime_info() -> dict[str, str]:
        return {
            "runtimeBuildId": _RUNTIME_BUILD_ID,
            "runtimeStartedAt": _RUNTIME_STARTED_AT,
        }

    @classmethod
    def _expires_at(cls, option: dict[str, Any], created_at: str) -> datetime:
        raw = option.get("expiresAt")
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                pass
        try:
            created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            created = datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created + timedelta(minutes=cls.TTL_MINUTES)

    @staticmethod
    def _validate_scope(requested: dict[str, Any], persisted: dict[str, Any]) -> None:
        def scoped_identity(option: dict[str, Any]) -> dict[str, Any]:
            groups = [item for item in option.get("densityGroups") or [] if isinstance(item, dict)]
            primary = groups[0] if groups else {}
            return {
                **option,
                "briefId": option.get("briefId") or primary.get("briefId"),
                "poolId": option.get("poolId") or primary.get("poolId"),
                "planningSlotId": (
                    option.get("planningSlotId")
                    or primary.get("planningSlotId")
                    or primary.get("slotId")
                ),
                "dayNumber": option.get("dayNumber") or primary.get("dayNumber"),
                "intentType": option.get("intentType") or primary.get("intentType"),
            }

        requested_identity = scoped_identity(requested)
        persisted_identity = scoped_identity(persisted)
        missing = [
            key
            for key in ("briefId", "poolId", "planningSlotId", "dayNumber", "intentType")
            if persisted_identity.get(key) in (None, "")
        ]
        if missing:
            raise PortfolioDensityContinuationError(
                "agent_choice_source_context_missing",
                "该地点选择缺少完整的方案、日期或槽位身份，请刷新候选后重试。",
            )
        for key in (
            "id",
            "kind",
            "candidateRecordId",
            "amapId",
            "briefId",
            "poolId",
            "planningSlotId",
            "dayNumber",
            "intentType",
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "requestContractFingerprint",
            "expectedBaseVersionId",
            "focusBriefId",
            "retryMode",
        ):
            left = requested_identity.get(key)
            right = persisted_identity.get(key)
            if left not in (None, "") and right not in (None, "") and str(left) != str(right):
                raise PortfolioDensityContinuationError(
                    "agent_choice_identity_mismatch",
                    "该地点选择的规划身份与持久化暂停点不一致。",
                )

    @staticmethod
    def _json_dict(value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _runtime_build_id() -> str:
    configured = str(os.getenv("TRIP_BUILD_ID") or os.getenv("GIT_COMMIT_SHA") or "").strip()
    if configured:
        return configured
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(__file__).with_name("agent_service.py")):
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return f"dev-{digest.hexdigest()[:16]}"


_RUNTIME_BUILD_ID = _runtime_build_id()
