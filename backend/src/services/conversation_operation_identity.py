"""Resolve presentation choices to server-owned, direct operation identities.

Reissue preserves an origin; an executor issuing the next direction does not.
Guide continuation is a separate business operation bound to its evidence.
"""
from __future__ import annotations

import json
import sqlite3
import copy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class OperationIdentity:
    source_turn_id: str
    choice_id: str
    operation_id: str
    variant: str


class ConversationOperationIdentity:
    ACTIONS = {"continue_plan_expansion", "select_plan_proposal", "search_travel_guide_advice"}
    SCOPE_FIELDS = ("action", "kind", "proposalId", "planningSelectionRootTurnId", "rootPortfolioId",
                    "requestContractFingerprint", "expectedBaseVersionId", "materialFingerprint")

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @classmethod
    def scope(cls, option: dict) -> str:
        projection = option.get("comparisonProjection") or {}
        material = {key: option.get(key, projection.get(key)) for key in cls.SCOPE_FIELDS}
        return sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def canonical_option(self, session_id: str, source_id: str, choice_id: str) -> dict:
        row = self.db.execute("SELECT agent_response_json, status FROM conversation_turns WHERE id = ? AND session_id = ? AND role = 'assistant'", (source_id, session_id)).fetchone()
        payload = json.loads(row["agent_response_json"] or "{}") if row else {}
        matches = [item for item in payload.get("choiceOptions") or [] if isinstance(item, dict) and item.get("id") == choice_id]
        if not row or row["status"] == "superseded" or len(matches) != 1:
            raise ValueError("operation_presentation_identity_invalid")
        if row["status"] == "failed":
            marker = payload.get("capabilityCarryForward") or payload.get("guideContinuationRecovery") or {}
            if not (isinstance(marker, dict) and matches[0].get("operationOrigin")
                    and marker.get("schemaVersion") in {"capability-carry-forward-v1", "guide-continuation-recovery-v1"}
                    and marker.get("targetAssistantTurnId") == source_id
                    and marker.get("status") == "reissued" and marker.get("businessExecutionStarted") is False):
                raise ValueError("operation_failed_carrier_not_reissued")
        return matches[0]

    def origin(self, session_id: str, source_id: str, choice_id: str) -> dict:
        option = self.canonical_option(session_id, source_id, choice_id)
        origin = option.get("operationOrigin")
        if origin is None:
            return {"schemaVersion": "conversation-operation-origin-v1", "sourceAssistantTurnId": source_id,
                    "choiceId": choice_id, "scopeFingerprint": self.scope(option)}
        if not isinstance(origin, dict) or origin.get("schemaVersion") != "conversation-operation-origin-v1":
            raise ValueError("operation_origin_invalid")
        original = self.canonical_option(session_id, str(origin.get("sourceAssistantTurnId") or ""), str(origin.get("choiceId") or ""))
        if original.get("operationOrigin") is not None or origin.get("scopeFingerprint") != self.scope(original) or self.scope(option) != self.scope(original):
            raise ValueError("operation_origin_scope_invalid")
        return dict(origin)

    def resolve(self, session_id: str, source_id: str, choice_id: str, *, guide_evidence: str | None = None) -> OperationIdentity:
        option = self.canonical_option(session_id, source_id, choice_id)
        origin = self.origin(session_id, source_id, choice_id)
        variant = "guide_grounded" if guide_evidence else str(option.get("action") or "")
        canonical_choice = str(origin["choiceId"])
        if guide_evidence:
            if option.get("action") != "continue_plan_expansion":
                raise ValueError("operation_guide_action_invalid")
            # Pre-upgrade executions used the unsuffixed choice key. Read that
            # exact row before admitting a new evidence-specific operation;
            # missing lineage is uncertainty, not permission to execute again.
            legacy = self.db.execute(
                "SELECT continuation_json FROM agent_choice_executions WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?",
                (session_id, origin["sourceAssistantTurnId"], canonical_choice),
            ).fetchone()
            use_legacy_key = False
            if legacy is not None:
                continuation = json.loads(legacy["continuation_json"] or "{}")
                requirement = continuation.get("guideContinuationRequirement") or {}
                journal = continuation.get("operationIdentity")
                if journal is None or journal.get("guideEvidenceFingerprint"):
                    if requirement.get("evidenceFingerprint") != guide_evidence:
                        raise ValueError("operation_legacy_execution_lineage_unknown")
                    use_legacy_key = True
                elif (journal.get("schemaVersion") != "execution-operation-identity-v1"
                      or journal.get("sourceAssistantTurnId") != origin["sourceAssistantTurnId"]
                      or journal.get("choiceId") != origin["choiceId"]):
                    raise ValueError("operation_legacy_execution_lineage_unknown")
            if not use_legacy_key:
                canonical_choice += ":guide:" + sha256(guide_evidence.encode()).hexdigest()[:32]
        operation_id = "operation_" + sha256(json.dumps([origin["sourceAssistantTurnId"], canonical_choice, origin["scopeFingerprint"], variant]).encode()).hexdigest()
        return OperationIdentity(str(origin["sourceAssistantTurnId"]), canonical_choice, operation_id, variant)

    def execution(self, session_id: str, source_id: str, choice_id: str, *, guide_evidence: str | None = None) -> dict | None:
        identity = self.resolve(session_id, source_id, choice_id, guide_evidence=guide_evidence)
        row = self.db.execute("SELECT * FROM agent_choice_executions WHERE session_id = ? AND source_turn_id = ? AND choice_id = ?",
                              (session_id, identity.source_turn_id, identity.choice_id)).fetchone()
        return dict(row) if row else None

    def source_option_for_execution(self, execution: dict) -> dict:
        journal = json.loads(execution.get("continuation_json") or "{}")
        identity = journal.get("operationIdentity")
        if identity is None:
            return self.canonical_option(execution["session_id"], execution["source_turn_id"], execution["choice_id"])
        if not isinstance(identity, dict) or identity.get("schemaVersion") != "execution-operation-identity-v1":
            raise ValueError("operation_execution_identity_invalid")
        resolved = self.resolve(execution["session_id"], identity["sourceAssistantTurnId"], identity["choiceId"],
                                guide_evidence=identity.get("guideEvidenceFingerprint"))
        if (resolved.source_turn_id != execution["source_turn_id"] or resolved.choice_id != execution["choice_id"]
                or resolved.operation_id != identity.get("operationId")):
            raise ValueError("operation_execution_identity_invalid")
        return self.canonical_option(execution["session_id"], identity["sourceAssistantTurnId"], identity["choiceId"])

    def offered_execution(self, session_id: str, source_id: str, choice_id: str) -> dict | None:
        option = self.canonical_option(session_id, source_id, choice_id)
        if option.get("action") != "continue_plan_expansion":
            return self.execution(session_id, source_id, choice_id)
        # A response can contain guide advice and an ordinary continuation
        # button. Response-level evidence is not that button's operation
        # variant. Guide semantic binding passes its evidence explicitly;
        # dedicated/reissued evidence-bound options carry it on the option.
        evidence = option.get("guideEvidenceFingerprint")
        return self.execution(session_id, source_id, choice_id, guide_evidence=evidence)

    def assert_latest(self, session_id: str, source_id: str) -> None:
        row = self.db.execute("SELECT id FROM conversation_turns WHERE session_id = ? AND role = 'assistant' AND status != 'superseded' ORDER BY turn_index DESC LIMIT 1", (session_id,)).fetchone()
        if row is None or row["id"] != source_id:
            raise ValueError("operation_carrier_stale")

    def reissue_material(self, session_id: str, source_id: str, material: dict) -> dict:
        """Intersect refreshed legality with the exact unconsumed source grant.

        Caller owns the transaction/CAS. No historical walk or new operation
        is allowed here; executor-issued next operations use a different path.
        """
        row = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE session_id = ? AND id = ? AND role = 'assistant' AND status != 'superseded'", (session_id, source_id)).fetchone()
        if row is None:
            raise ValueError("operation_presentation_identity_invalid")
        response = json.loads(row[0] or "{}")
        consumed = set(response.get("consumedChoiceIds") or [])
        eligible = []
        for old in response.get("choiceOptions") or []:
            if not isinstance(old, dict) or old.get("id") in consumed or old.get("action") not in self.ACTIONS:
                continue
            execution = self.offered_execution(session_id, source_id, old["id"])
            if execution is not None and not self.explicitly_unstarted(execution):
                continue
            eligible.append(old)
        refreshed = copy.deepcopy(material)
        choices = []
        for option in refreshed.get("choiceOptions") or []:
            matches = [old for old in eligible if self.scope(old) == self.scope(option)]
            if len(matches) != 1:
                continue
            old = matches[0]
            old_display = option["id"]
            option["id"] = option["choiceId"] = "choice_" + uuid4().hex[:24]
            option["operationOrigin"] = self.origin(session_id, source_id, old["id"])
            if option.get("action") == "select_plan_proposal":
                updated = self.db.execute("UPDATE agent_plan_proposals SET choice_id = ? WHERE id = ? AND portfolio_id = ? AND choice_id = ?", (option["id"], option["proposalId"], option["rootPortfolioId"], old_display))
                if updated.rowcount != 1:
                    raise ValueError("capability_proposal_choice_stale")
                option["comparisonProjection"]["choiceId"] = option["id"]
                for projection in refreshed.get("comparisonProjections") or []:
                    if projection.get("proposalId") == option["proposalId"]:
                        projection["choiceId"] = option["id"]
            choices.append(option)
        refreshed["choiceOptions"] = choices
        return refreshed

    def lifecycle_status(self, session_id: str, source_id: str, option: dict) -> str | None:
        try:
            execution = self.offered_execution(session_id, source_id, str(option.get("id") or ""))
        except ValueError:
            return "stale"
        return str(execution["status"]) if execution is not None else None

    @staticmethod
    def explicitly_unstarted(execution: dict) -> bool:
        try:
            outcome = json.loads(execution.get("outcome_json") or "{}")
        except (TypeError, ValueError):
            return False
        return (isinstance(outcome, dict) and execution.get("status") == "failed_retryable"
                and outcome.get("businessExecutionStarted") is False and outcome.get("boundedAttemptConsumed") is False
                and outcome.get("plannerCalled") is False
                and all(outcome.get(field) == 0 for field in ("proposalDelta", "versionDelta", "patchDelta", "routeWriteDelta")))
