"""Carry still-valid capabilities across a proven non-executing response.

No historical scan, Provider or itinerary writer is reachable from this service.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from uuid import uuid4

from src.services.conversation_operation_identity import ConversationOperationIdentity
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService, GuideContinuationRequirementError
from src.services.simple_open_direction_service import SimpleOpenDirectionService


class ConversationCapabilityCarrier:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.identities = ConversationOperationIdentity(db)

    def capture(self, session_id: str, source_id: str) -> dict:
        row = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?", (source_id, session_id)).fetchone()
        if row is None:
            return {}
        self.identities.assert_latest(session_id, source_id)
        response = json.loads(row["agent_response_json"] or "{}")
        options = [item for item in response.get("choiceOptions") or []
                   if isinstance(item, dict) and item.get("action") in self.identities.ACTIONS]
        # A durable unfinished request cannot be treated as zero execution just
        # because a process-local lease no longer exists.
        uncertain = self.db.execute("""SELECT 1 FROM conversation_turns WHERE session_id = ? AND role = 'user'
            AND json_extract(agent_request_json, '$.ingressJournal.state') = 'claimed'
            AND json_extract(agent_request_json, '$.ingressJournal.stage') != 'before_semantics' LIMIT 1""", (session_id,)).fetchone()
        if uncertain:
            return {}
        return {"schemaVersion": "capability-carrier-ingress-v1", "sourceAssistantTurnId": source_id,
                "sourceResponse": row["agent_response_json"], "choiceIds": [item["id"] for item in options],
                "executionIds": [item[0] for item in self.db.execute("SELECT id FROM agent_choice_executions WHERE session_id = ?", (session_id,))],
                "counts": self.counts(session_id)}

    def counts(self, session_id: str) -> dict:
        counts = {}
        for table in ("itinerary_versions", "itinerary_patches", "agent_choice_executions"):
            counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)).fetchone()[0]
        counts["agent_plan_proposals"] = self.db.execute("SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id IN (SELECT id FROM agent_plan_portfolios WHERE session_id = ?)", (session_id,)).fetchone()[0]
        counts["route_options"] = self.db.execute("SELECT COUNT(*) FROM route_options WHERE plan_id IN (SELECT active_plan_id FROM conversation_sessions WHERE id = ?)", (session_id,)).fetchone()[0]
        return counts

    def unchanged_business_state(self, session_id: str, capture: dict) -> bool:
        before = capture.get("counts") or {}
        after = self.counts(session_id)
        if any(before.get(key) != value for key, value in after.items() if key != "agent_choice_executions"):
            return False
        if after["agent_choice_executions"] == before.get("agent_choice_executions"):
            return True
        # A newly persisted claim is not itself a consumed business attempt.
        # Accept it only with affirmative settlement evidence, never by absence.
        if not isinstance(capture.get("executionIds"), list):
            return False
        previous = set(capture["executionIds"])
        rows = [dict(row) for row in self.db.execute("SELECT * FROM agent_choice_executions WHERE session_id = ?", (session_id,))]
        return (previous.issubset({row["id"] for row in rows})
                and all(self.identities.explicitly_unstarted(row) for row in rows if row["id"] not in previous))

    def carry(self, session_id: str, next_id: str, capture: dict, *, nonexecuting: bool) -> bool:
        if not nonexecuting or capture.get("schemaVersion") != "capability-carrier-ingress-v1":
            return False
        if not self.unchanged_business_state(session_id, capture):
            return False
        source_id = str(capture.get("sourceAssistantTurnId") or "")
        self.identities.assert_latest(session_id, next_id)
        source = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?", (source_id, session_id)).fetchone()
        target = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?", (next_id, session_id)).fetchone()
        session = self.db.execute("SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        if not source or not target or source[0] != capture.get("sourceResponse"):
            return False
        target_payload = json.loads(target[0] or "{}")
        if target_payload.get("capabilityCarryForward", {}).get("sourceAssistantTurnId") == source_id:
            return False
        source_payload = json.loads(source[0] or "{}")
        if (not session[0] and SimpleOpenDirectionService(self.db).latest_root(session_id=session_id) is None
                and target_payload.get("mode") != "shared_travel_source"):
            # No planning capability is invented here. Preserve just one direct
            # validated source pointer across a proven non-executing exchange.
            from src.services.shared_travel_source_service import SharedTravelSourceService
            reader = SharedTravelSourceService(self.db)
            source_material = source_payload.get("sharedSource") or {}
            pending = source_payload.get("pendingSharedSource")
            if source_material.get("status") == "completed" and source_material.get("planningBinding") is None:
                pending = reader._initial_proof(source_id, source_material)
            if pending:
                reader.validate_initial_source(session_id, {**pending, "carrierAssistantTurnId": source_id})
                target_payload["pendingSharedSource"] = copy.deepcopy(pending)
                updated = self.db.execute("UPDATE conversation_turns SET agent_response_json=? WHERE id=? AND agent_response_json IS ?",
                    (json.dumps(target_payload, ensure_ascii=False), next_id, target[0]))
                if updated.rowcount != 1:
                    raise ValueError("shared_initial_source_binding_invalid")
                return True
        consumed = set(source_payload.get("consumedChoiceIds") or [])
        eligible = []
        for option in source_payload.get("choiceOptions") or []:
            if not isinstance(option, dict) or option.get("id") not in capture.get("choiceIds", []) or option.get("id") in consumed:
                continue
            if str(option.get("expectedBaseVersionId") or "") != str(session[0] or ""):
                continue
            execution = self.identities.offered_execution(session_id, source_id, option["id"])
            if execution is not None and not self.identities.explicitly_unstarted(execution):
                continue
            eligible.append(option)
        portfolios = {str(option.get("rootPortfolioId") or "") for option in eligible}
        if len(portfolios) != 1 or not next(iter(portfolios)):
            return False
        portfolio_id = next(iter(portfolios))
        requirement = None
        continuation = next((item for item in eligible if item.get("action") == "continue_plan_expansion"), None)
        if continuation and (source_payload.get("mode") == "travel_guide_advice" or continuation.get("guideEvidenceSourceAssistantTurnId")):
            try:
                requirement = GuideContinuationRequirementService(self.db).validate_original_evidence(
                    session_id=session_id, option=continuation, capability_source_turn_id=source_id, active_version_id=session[0],
                    require_place_hints=False)
            except GuideContinuationRequirementError:
                # Invalid guide evidence cannot be propagated as valid. Ordinary
                # expansion is still a separate independent capability.
                requirement = None
        self.db.execute("SAVEPOINT capability_carry")
        try:
            self.db.execute("UPDATE conversation_sessions SET id = id WHERE id = ?", (session_id,))
            self.identities.assert_latest(session_id, next_id)
            current_source = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id = ? AND session_id = ?", (source_id, session_id)).fetchone()
            current_base = self.db.execute("SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
            if (not current_source or current_source[0] != source[0] or not current_base or current_base[0] != session[0]
                    or not self.unchanged_business_state(session_id, capture)):
                raise ValueError("capability_carry_stale")
            material = SimpleOpenDirectionService(self.db).rotate_capability_carrier(
                session_id=session_id, portfolio_id=portfolio_id, expected_source_assistant_turn_id=source_id,
                next_source_assistant_turn_id=next_id, request_contract_fingerprint=str(eligible[0].get("requestContractFingerprint") or ""))
            choices = []
            for current in material.get("choiceOptions") or []:
                matches = [old for old in eligible if self.identities.scope(old) == self.identities.scope(current)]
                if len(matches) != 1:
                    continue
                old = matches[0]
                choice = copy.deepcopy(current)
                choice["operationOrigin"] = self.identities.origin(session_id, source_id, old["id"])
                choice["id"] = choice["choiceId"] = "choice_" + uuid4().hex[:24]
                if choice.get("action") == "continue_plan_expansion" and requirement:
                    choice.update(guideEvidenceSourceAssistantTurnId=requirement["sourceAssistantTurnId"],
                        guideChoiceExecutionId=requirement["guideChoiceExecutionId"], guideQueryFingerprint=requirement["queryFingerprint"],
                        guideEvidenceFingerprint=requirement["evidenceFingerprint"])
                if choice.get("action") == "select_plan_proposal":
                    updated_proposal = self.db.execute("UPDATE agent_plan_proposals SET choice_id = ? WHERE id = ? AND portfolio_id = ? AND choice_id = ?",
                                    (choice["id"], choice["proposalId"], portfolio_id, current["id"]))
                    if updated_proposal.rowcount != 1:
                        raise ValueError("capability_proposal_choice_stale")
                    choice["comparisonProjection"]["choiceId"] = choice["id"]
                    for projection in material.get("comparisonProjections") or []:
                        if projection.get("proposalId") == choice["proposalId"]:
                            projection["choiceId"] = choice["id"]
                choices.append(choice)
            if not choices:
                raise ValueError("capability_carry_empty")
            # Preserve unrelated retry/clarification options on the new reply.
            own = [item for item in target_payload.get("choiceOptions") or [] if item.get("action") not in self.identities.ACTIONS]
            target_payload.update(material)
            target_payload["choiceOptions"] = [*own, *choices]
            target_payload["capabilityCarryForward"] = {"schemaVersion": "capability-carry-forward-v1", "sourceAssistantTurnId": source_id,
                "targetAssistantTurnId": next_id, "status": "reissued", "businessExecutionStarted": False}
            updated = self.db.execute("UPDATE conversation_turns SET agent_response_json = ? WHERE id = ? AND agent_response_json IS ?",
                                      (json.dumps(target_payload, ensure_ascii=False), next_id, target[0]))
            if updated.rowcount != 1:
                raise ValueError("capability_carry_stale")
            self.db.execute("RELEASE SAVEPOINT capability_carry")
            return True
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT capability_carry")
            self.db.execute("RELEASE SAVEPOINT capability_carry")
            raise
