"""Once-per-operation public document evidence, separate from guide authority."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
from typing import Any

from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
from src.services.public_source_reader import PublicSourceReader
from src.services.travel_guide_advice_service import TravelGuideAdviceService


class GuideSourceRefreshError(ValueError):
    pass


class GuideSourceRefreshService:
    SCHEMA = "guide-source-refresh-v1"
    MAX_SOURCES = 5
    TOTAL_READ_SECONDS = 8.0

    def __init__(self, db: sqlite3.Connection, *, reader: Any = None, clock=time.monotonic):
        self.db = db
        self.reader = reader or PublicSourceReader()
        self.clock = clock

    @staticmethod
    def _json(raw: Any) -> dict:
        return raw if isinstance(raw, dict) else json.loads(raw or "{}")

    @staticmethod
    def fingerprint(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def same_origin(left: dict, right: dict) -> bool:
        ignored = {"capabilitySourceAssistantTurnId", "requirementFingerprint"}
        return {k: v for k, v in left.items() if k not in ignored} == {
            k: v for k, v in right.items() if k not in ignored
        }

    def refresh(
        self,
        *,
        session_id: str,
        execution_id: str,
        requirement: dict,
        selected_choice: dict,
        active_version_id: str | None,
        deadline: float | None = None,
    ) -> dict:
        read_deadline = min(self.clock() + self.TOTAL_READ_SECONDS, deadline if deadline is not None else float("inf"))
        row = self.db.execute(
            "SELECT * FROM agent_choice_executions WHERE id=? AND session_id=?", (execution_id, session_id)
        ).fetchone()
        if row is None or row["status"] != "executing":
            raise GuideSourceRefreshError("guide_source_execution_invalid")
        journal = self._json(row["continuation_json"])
        refresh = journal.get("guideSourceRefresh") or {}
        if refresh:
            if refresh.get("state") != "completed":
                raise GuideSourceRefreshError("guide_source_read_interrupted")
            base = journal.get("guideSourceBindingRequirement") or {}
            saved = journal.get("guideContinuationRequirement") or {}
            if not self.same_origin(base, requirement) or not self.validate_documents(saved):
                raise GuideSourceRefreshError("guide_evidence_lineage_invalid")
            return copy.deepcopy(saved)
        if (journal.get("guideBindingDispatch") or {}).get("state") != "dispatching":
            raise GuideSourceRefreshError("guide_source_execution_invalid")
        advice_row = self.db.execute(
            "SELECT agent_response_json FROM conversation_turns WHERE id=? AND session_id=? AND role='assistant'",
            (requirement["sourceAssistantTurnId"], session_id),
        ).fetchone()
        source_payload = self._json(advice_row[0]) if advice_row else {}
        advice = source_payload.get("guideAdvice") or {}
        shared_documents = None
        if requirement.get("evidenceKind") == "shared_public_note":
            from src.services.shared_travel_source_service import SharedTravelSourceService
            source = source_payload.get("sharedSource") or {}
            SharedTravelSourceService(self.db).validate(session_id, requirement["sourceAssistantTurnId"], source)
            if advice != SharedTravelSourceService.advice(source):
                raise GuideSourceRefreshError("guide_evidence_lineage_invalid")
            shared_documents = SharedTravelSourceService.documents(source, advice)
        evidence = TravelGuideAdviceService._guide_payload_evidence(advice)
        if ((shared_documents is None and (evidence is None or evidence[1] != requirement.get("evidenceFingerprint")))
                or advice.get("evidenceFingerprint") != requirement.get("evidenceFingerprint")):
            raise GuideSourceRefreshError("guide_evidence_lineage_invalid")
        sources = copy.deepcopy(advice["sourceRefs"][: self.MAX_SOURCES])
        journal["guideSourceBindingRequirement"] = copy.deepcopy(requirement)
        journal["guideSourceRefresh"] = {
            "schemaVersion": self.SCHEMA,
            "state": "fetching",
            "sourceRefIds": [s["refId"] for s in sources],
        }
        current_json = self._save(session_id, execution_id, row["continuation_json"], journal)
        # The durable dispatch fence is closed BEFORE any external read. A
        # crash cannot be recovered as an unstarted binding or refetched.
        if self.db.in_transaction:
            raise GuideSourceRefreshError("guide_source_transaction_open")
        documents = []
        for source in sources:
            if shared_documents is not None:
                documents.extend(shared_documents)
                break
            attempted = self.clock() < read_deadline
            try:
                result = (
                    self.reader.read(source["url"], deadline=read_deadline)
                    if attempted
                    else {"status": "not_attempted", "reason": "source_read_budget_exhausted"}
                )
            except Exception:
                result = {"status": "failed", "reason": "source_read_failed"}
            documents.append(
                {
                    "refId": source["refId"],
                    "sourceFingerprint": source["sourceFingerprint"],
                    "sourceUrl": source["url"],
                    "readAttempted": attempted,
                    **{
                        key: copy.deepcopy(result.get(key))
                        for key in (
                            "status",
                            "reason",
                            "canonicalUrl",
                            "title",
                            "bodyText",
                            "contentFingerprint",
                            "fetchedAt",
                            "contentKind",
                        )
                    },
                }
            )
        enriched = copy.deepcopy(requirement)
        enriched["sourceDocumentEvidence"] = {
            "schemaVersion": self.SCHEMA,
            "documents": documents,
            "fingerprint": self.fingerprint(documents),
        }
        enriched["placeHints"] = self._document_hints(documents, requirement["evidenceFingerprint"], requirement.get("allowedSourceIntentTypes"))
        enriched["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(enriched)
        # The source carrier/root/base must still be the one that authorized
        # this operation after network I/O. No target substitution is allowed.
        checked = GuideContinuationRequirementService(self.db).build(
            session_id=session_id,
            selected_choice=selected_choice,
            active_version_id=active_version_id,
            require_place_hints=False,
        )
        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if (
            not self.same_origin(checked, requirement)
            or session is None
            or str(session[0] or "") != str(active_version_id or "")
        ):
            raise GuideSourceRefreshError("guide_evidence_lineage_invalid")
        journal["guideContinuationRequirement"] = enriched
        journal["guideSourceRefresh"].update(state="completed", sourceDocumentFingerprint=self.fingerprint(documents))
        self._save(session_id, execution_id, current_json, journal)
        if not self.validate_documents(enriched):
            raise GuideSourceRefreshError("guide_source_evidence_invalid")
        return enriched

    def _save(self, session_id: str, execution_id: str, before: str, journal: dict) -> str:
        serialized = json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        updated = self.db.execute(
            "UPDATE agent_choice_executions SET continuation_json=? "
            "WHERE id=? AND session_id=? AND status='executing' AND continuation_json IS ?",
            (serialized, execution_id, session_id, before),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            raise GuideSourceRefreshError("guide_source_execution_changed")
        self.db.commit()
        return serialized

    @classmethod
    def _document_hints(cls, documents: list[dict], evidence_fingerprint: str, allowed_intents: list[str] | None = None) -> list[dict]:
        by_intent: dict[str, list[dict]] = {}
        seen: set[tuple[str, str]] = set()
        for document in documents:
            if not cls._valid_document(document):
                continue
            body = document["bodyText"]
            # Read the bounded body, not just its first search-sized prefix.
            # Chunk overlap protects names at a boundary; full-body boundary
            # verification below prevents a cut from inventing an entity end.
            for start in range(0, len(body), 1300):
                chunk = body[start : start + 1600]
                for intent in TravelGuideAdviceService._PLACE_SUFFIXES_BY_INTENT:
                    if allowed_intents is not None and intent not in allowed_intents:
                        continue
                    for mention in TravelGuideAdviceService._place_mentions_in_text(chunk, intent):
                        if (intent, mention) in seen:
                            continue
                        matches = list(re.finditer(re.escape(mention), body))
                        supported = next(
                            (
                                m
                                for m in matches
                                if TravelGuideAdviceService._safe_entity_span(
                                    mention, source_text=body, suffix_end=m.end()
                                )
                            ),
                            None,
                        )
                        if supported is None:
                            continue
                        seen.add((intent, mention))
                        excerpt = body[max(0, supported.start() - 100) : supported.end() + 160]
                        by_intent.setdefault(intent, []).append(
                            {
                                "schemaVersion": "guide-place-hint-v1",
                                "mentionText": mention,
                                "intentType": intent,
                                "sourceRefIds": [document["refId"]],
                                "sourceFingerprints": [document["sourceFingerprint"]],
                                "sourceDocumentFingerprints": [document["contentFingerprint"]],
                                "sourceExcerpt": excerpt,
                                "guideEvidenceFingerprint": evidence_fingerprint,
                                "verificationStatus": "unresolved_amap_grounding",
                            }
                        )
        result = []
        while by_intent and len(result) < min(len(documents), cls.MAX_SOURCES):
            for intent in list(by_intent):
                result.append(by_intent[intent].pop(0))
                if not by_intent[intent]:
                    del by_intent[intent]
                if len(result) >= min(len(documents), cls.MAX_SOURCES):
                    break
        return result

    @staticmethod
    def _valid_document(document: dict) -> bool:
        body = document.get("bodyText")
        return bool(
            document.get("status") == "succeeded"
            and isinstance(body, str)
            and body.strip()
            and len(body) <= 24_000
            and document.get("contentKind") in {"article", "main", "visible_text"}
            and hashlib.sha256(body.encode("utf-8")).hexdigest() == document.get("contentFingerprint")
        )

    @classmethod
    def validate_documents(cls, requirement: dict) -> bool:
        evidence = requirement.get("sourceDocumentEvidence")
        if evidence is None:
            return True  # Read-only compatibility for already persisted proposals.
        if not isinstance(evidence, dict) or evidence.get("schemaVersion") != cls.SCHEMA:
            return False
        documents = evidence.get("documents")
        if (
            not isinstance(documents, list)
            or not 1 <= len(documents) <= cls.MAX_SOURCES
            or any(not isinstance(d, dict) for d in documents)
            or evidence.get("fingerprint") != cls.fingerprint(documents)
        ):
            return False
        if any(d.get("status") == "succeeded" and not cls._valid_document(d) for d in documents):
            return False
        return requirement.get("placeHints") == cls._document_hints(
            documents, requirement.get("evidenceFingerprint", ""), requirement.get("allowedSourceIntentTypes")
        )

    @classmethod
    def controller_evidence(cls, requirement: dict) -> list[dict]:
        evidence = requirement.get("sourceDocumentEvidence") or {}
        result = []
        referenced_ids = {ref for hint in requirement.get("placeHints") or [] for ref in hint.get("sourceRefIds") or []}
        # The existing controller projection is bounded to two sources. Keep
        # actual selected-place support ahead of other readable documents.
        documents = sorted(
            evidence.get("documents") or [], key=lambda document: document.get("refId") not in referenced_ids
        )
        for document in documents:
            if not cls._valid_document(document):
                continue
            excerpts = [
                h["sourceExcerpt"]
                for h in requirement.get("placeHints") or []
                if document["refId"] in h.get("sourceRefIds", [])
            ]
            result.append(
                {
                    "sourceMaterialId": document["refId"],
                    "sourceUrl": document["canonicalUrl"],
                    "publicText": "\n".join(excerpts) if excerpts else document["bodyText"][:1400],
                    "metadata": {
                        "fetchStatus": "succeeded",
                        "contentFingerprint": document["contentFingerprint"],
                        "contentKind": document["contentKind"],
                    },
                }
            )
        return result
