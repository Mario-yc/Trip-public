"""Durable at-most-once message admission, using existing turn identity/JSON.

An unfinished journal is never permission to rerun a model or a writer.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import HTTPException


class AgentIngressJournal:
    KEY = "ingressJournal"

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @staticmethod
    def identity(session_id: str, request_id: str) -> str:
        return "turn_req_" + sha256(json.dumps([session_id, request_id]).encode()).hexdigest()

    @staticmethod
    def payload_hash(payload: dict) -> str:
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def existing(self, session_id: str, request_id: str, payload: dict) -> dict | None:
        if self.db.execute("SELECT 1 FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone() is None:
            raise HTTPException(404, "Agent session not found")
        row = self.db.execute("SELECT status, agent_request_json FROM conversation_turns WHERE id = ? AND session_id = ?",
                              (self.identity(session_id, request_id), session_id)).fetchone()
        if row is None:
            return None
        journal = json.loads(row["agent_request_json"] or "{}").get(self.KEY) or {}
        if journal.get("payloadHash") != self.payload_hash(payload):
            raise HTTPException(409, {"code": "request_id_payload_conflict", "message": "该请求编号已用于不同内容，未再次执行。"})
        if row["status"] == "superseded":
            raise HTTPException(409, {"code": "request_superseded", "message": "该历史请求已被编辑替代，未再次执行。"})
        return journal

    def claim(self, session_id: str, request_id: str, payload: dict) -> tuple[str, bool]:
        turn_id = self.identity(session_id, request_id)
        if self.db.in_transaction:
            raise RuntimeError("ingress_claim_requires_short_transaction")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.existing(session_id, request_id, payload)
            if existing is not None:
                self.db.commit()
                return turn_id, False
            now = datetime.now(timezone.utc).isoformat()
            index = self.db.execute("SELECT COALESCE(MAX(turn_index), 0) + 1 FROM conversation_turns WHERE session_id = ?", (session_id,)).fetchone()[0]
            journal = {"schemaVersion": "agent-ingress-journal-v1", "requestId": request_id,
                       "payloadHash": self.payload_hash(payload), "state": "claimed", "stage": "before_semantics"}
            self.db.execute("""INSERT INTO conversation_turns
                (id, session_id, role, content, turn_index, status, agent_request_json, created_at, updated_at)
                VALUES (?, ?, 'user', ?, ?, 'active', ?, ?, ?)""",
                (turn_id, session_id, str(payload.get("content") or "").strip(), index,
                 json.dumps({self.KEY: journal}, ensure_ascii=False), now, now))
            self.db.execute("UPDATE conversation_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self.db.commit()
            return turn_id, True
        except Exception:
            self.db.rollback()
            raise

    def merge_context(self, turn_id: str, context: dict) -> dict:
        # Caller/client context never owns the journal, even when extra fields
        # are accepted by the legacy message context schema.
        server_fields = {self.KEY, "capabilityIngress", "sharedSourceRead", "sharedGuideIdentityPolicy"}
        result = {key: value for key, value in context.items() if key not in server_fields}
        row = self.db.execute("SELECT agent_request_json FROM conversation_turns WHERE id = ?", (turn_id,)).fetchone()
        persisted = json.loads(row["agent_request_json"] or "{}") if row else {}
        for key in server_fields:
            if key in persisted:
                result[key] = persisted[key]
        return result

    def update(self, turn_id: str, **changes: Any) -> None:
        row = self.db.execute("SELECT agent_request_json FROM conversation_turns WHERE id = ?", (turn_id,)).fetchone()
        context = json.loads(row["agent_request_json"] or "{}") if row else {}
        if self.KEY not in context:
            return
        if changes.get("state") == "completed":
            response = changes.get("response") or {}
            assistant = response.get("assistantTurn") or {}
            current = self.db.execute("""SELECT status, agent_response_json FROM conversation_turns
                WHERE id = ? AND role = 'assistant' AND session_id =
                (SELECT session_id FROM conversation_turns WHERE id = ?)""", (assistant.get("id"), turn_id)).fetchone()
            # Bind the cached result to the response revision, not merely its
            # ID. Also reject a concurrent reissue that preceded completion:
            # an old outcome must never be sealed with a newer row's digest.
            changes["assistantResponseFingerprint"] = (
                self.response_fingerprint(current)
                if current and self.authority_material(json.loads(current["agent_response_json"] or "{}"))
                == self.authority_material(assistant) else None
            )
        context[self.KEY].update(changes)
        self.db.execute("UPDATE conversation_turns SET agent_request_json = ? WHERE id = ?",
                        (json.dumps(context, ensure_ascii=False, default=str), turn_id))

    @staticmethod
    def authority_material(response: dict) -> dict:
        fields = ("id", "choiceId", "action", "kind", "sourceAssistantTurnId", "planningSelectionRootTurnId",
                  "rootPortfolioId", "requestContractFingerprint", "expectedBaseVersionId", "proposalId",
                  "materialFingerprint", "repairChoiceId", "operationOrigin", "guideEvidenceFingerprint")
        # Visit facts and lifecycle are read-time projections; opaque authority
        # and comparison identity must nevertheless match the persisted row.
        return {key: [{field: item.get(field) for field in fields}
                      for item in response.get(key) or [] if isinstance(item, dict)]
                for key in ("choiceOptions", "comparisonProjections")}

    @staticmethod
    def response_fingerprint(row: sqlite3.Row) -> str:
        return AgentIngressJournal.payload_hash({"status": row["status"], "response": row["agent_response_json"]})

    def replay(self, session_id: str, journal: dict) -> dict:
        if journal.get("state") == "failed":
            raise HTTPException(int(journal.get("httpStatus") or 409), journal.get("error") or "request_failed")
        outcome = journal.get("response")
        if journal.get("state") != "completed" or not isinstance(outcome, dict):
            raise HTTPException(409, {"code": "request_outcome_pending", "message": "原请求仍在处理或执行结果尚待核对，未重复执行。"})
        latest = self.db.execute("""SELECT id, status, agent_response_json FROM conversation_turns WHERE session_id = ? AND role = 'assistant'
            AND status != 'superseded' ORDER BY turn_index DESC LIMIT 1""", (session_id,)).fetchone()
        session = self.db.execute("SELECT active_version_id FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        historical = (not latest or latest["id"] != outcome["assistantTurn"]["id"]
                      or str(session["active_version_id"] or "") != str(journal.get("activeVersionId") or "")
                      or not journal.get("assistantResponseFingerprint")
                      or journal["assistantResponseFingerprint"] != self.response_fingerprint(latest))
        # Deserialize a fresh response; no session hydration/reconciliation,
        # carrier rotation, Provider, timing finalizer or write is permitted.
        result = json.loads(json.dumps(outcome))
        result["requestReplay"] = {"replayed": True, "isHistorical": historical}
        if historical:
            result["itinerary"] = None
            result["version"] = None
            result["pendingPoiCandidates"] = []
            for key in ("userTurn", "assistantTurn"):
                result[key]["choiceOptions"] = []
                result[key]["comparisonProjections"] = []
        return result
