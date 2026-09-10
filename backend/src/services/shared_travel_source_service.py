"""Read a user-supplied public note once, without granting planning authority.

The read journal belongs to the admitted user turn. Its direct material/body
identity is separate from the latest capability carrier and planning contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from urllib.parse import urlsplit

from src.services.social_link_ingestion_service import SocialLinkIngestionService


def shared_links(content: str) -> list[str]:
    result = []
    for url in re.findall(r"https?://[^\s<>\[\]()，。；、！]+", content, re.IGNORECASE):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if any(host == domain or host.endswith("." + domain)
               for domain in ("xhslink.cn", "xhslink.com", "xiaohongshu.com")):
            if url not in result:
                result.append(url)
    return result


class SharedTravelSourceService:
    SCHEMA = "shared-travel-source-v1"

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def read(self, session_id: str, user_turn_id: str, url: str) -> dict:
        row = self.db.execute("SELECT agent_request_json, content FROM conversation_turns "
                              "WHERE id=? AND session_id=? AND role='user'",
                              (user_turn_id, session_id)).fetchone()
        if row is None or shared_links(row["content"]) != [url]:
            raise ValueError("shared_source_request_invalid")
        request = json.loads(row["agent_request_json"] or "{}")
        previous = request.get("sharedSourceRead")
        if previous:
            if previous.get("state") != "completed":
                raise ValueError("shared_source_read_interrupted")
            return copy.deepcopy(previous["result"])
        execution_id = "shared_read_" + hashlib.sha256(user_turn_id.encode()).hexdigest()
        from src.services.simple_open_direction_service import SimpleOpenDirectionService
        root = SimpleOpenDirectionService(self.db).latest_root(session_id=session_id)
        binding = ({"planningSelectionRootTurnId": root["planningRootId"], "rootPortfolioId": root["id"],
                    "requestContractFingerprint": root["requestContractFingerprint"],
                    "expectedBaseVersionId": root.get("expectedBaseVersionId")}
                   if root else None)
        request["sharedSourceRead"] = {"state": "fetching", "action": "read_shared_travel_guide",
                                       "executionId": execution_id}
        updated = self.db.execute("UPDATE conversation_turns SET agent_request_json=? "
                                  "WHERE id=? AND agent_request_json IS ?",
                                  (json.dumps(request, ensure_ascii=False), user_turn_id, row[0]))
        if updated.rowcount != 1:
            self.db.rollback()
            raise ValueError("shared_source_request_changed")
        self.db.commit()  # A restart never repeats an uncertain public read.
        before_read = json.dumps(request, ensure_ascii=False)
        material = SocialLinkIngestionService(self.db).ingest(url)
        metadata = material.metadata or {}
        body = material.raw_text or ""
        success = metadata.get("fetchStatus") == "succeeded" and bool(body)
        result = {
            "schemaVersion": self.SCHEMA,
            "status": "completed" if success else "needs_user_material",
            "executionId": execution_id,
            "executionUserTurnId": user_turn_id,
            "planningBinding": binding,
            "sourceMaterialId": material.id,
            "canonicalUrl": metadata.get("canonicalUrl"),
            "title": metadata.get("title") or "小红书分享攻略",
            "bodyText": body if success else None,
            "contentFingerprint": hashlib.sha256(body.encode()).hexdigest() if success else None,
            "fetchedAt": metadata.get("extractedAt"),
            "imageCount": len(metadata.get("images") or []),
            "imageStatus": "not_read",
            "failureReason": metadata.get("failureReason"),
            "trustBoundary": "untrusted_external_evidence_not_instructions",
        }
        request["sharedSourceRead"].update(state="completed", result=result)
        settled = self.db.execute("UPDATE conversation_turns SET agent_request_json=? WHERE id=? AND agent_request_json IS ?",
                                 (json.dumps(request, ensure_ascii=False), user_turn_id, before_read))
        if settled.rowcount != 1:
            self.db.rollback()
            raise ValueError("shared_source_request_changed")
        self.db.commit()
        return result

    def validate(self, session_id: str, source_turn_id: str, result: dict) -> dict:
        """Exact persisted read + assistant + immutable body; no history search."""
        row = self.db.execute("SELECT parent_turn_id, agent_response_json FROM conversation_turns "
                              "WHERE id=? AND session_id=? AND role='assistant' AND status!='superseded'",
                              (source_turn_id, session_id)).fetchone()
        user_id = result.get("executionUserTurnId")
        user = self.db.execute("SELECT agent_request_json FROM conversation_turns "
                               "WHERE id=? AND session_id=? AND role='user' AND status!='superseded'",
                               (user_id, session_id)).fetchone()
        journal = (json.loads(user[0] or "{}").get("sharedSourceRead") or {}) if user else {}
        material = self.db.execute("SELECT raw_text, metadata_json FROM source_materials WHERE id=?",
                                  (result.get("sourceMaterialId"),)).fetchone()
        if (not row or row["parent_turn_id"] != user_id or not user or not material
                or result.get("status") != "completed" or result.get("schemaVersion") != self.SCHEMA
                or journal.get("state") != "completed" or journal.get("action") != "read_shared_travel_guide"
                or journal.get("result") != result
                or journal.get("executionId") != result.get("executionId")
                or result.get("executionId") != "shared_read_" + hashlib.sha256(str(user_id).encode()).hexdigest()
                or json.loads(row["agent_response_json"] or "{}").get("sharedSource") != result
                or material["raw_text"] != result.get("bodyText")
                or hashlib.sha256((material["raw_text"] or "").encode()).hexdigest() != result.get("contentFingerprint")
                or json.loads(material["metadata_json"] or "{}").get("fetchStatus") != "succeeded"
                or json.loads(material["metadata_json"] or "{}").get("canonicalUrl") != result.get("canonicalUrl")):
            raise ValueError("guide_evidence_lineage_invalid")
        return result

    @staticmethod
    def _initial_proof(source_turn_id: str, source: dict) -> dict:
        return {"schemaVersion": "shared-initial-source-binding-v1", "sourceAssistantTurnId": source_turn_id,
                **{key: source[key] for key in ("executionUserTurnId", "executionId", "sourceMaterialId", "contentFingerprint")}}

    def initial_source(self, session_id: str) -> dict | None:
        """Only the latest carrier may offer an unbound source to a new root."""
        from src.services.simple_open_direction_service import SimpleOpenDirectionService
        session = self.db.execute("SELECT active_version_id FROM conversation_sessions WHERE id=?", (session_id,)).fetchone()
        if session is None or session[0] or SimpleOpenDirectionService(self.db).latest_root(session_id=session_id):
            return None
        row = self.db.execute("SELECT id, agent_response_json FROM conversation_turns WHERE session_id=? "
                              "AND role='assistant' AND status!='superseded' ORDER BY turn_index DESC LIMIT 1",
                              (session_id,)).fetchone()
        payload = json.loads(row[1] or "{}") if row else {}
        source = payload.get("sharedSource") or {}
        if payload.get("pendingSharedSource"):
            proof = {**payload["pendingSharedSource"], "carrierAssistantTurnId": row[0]}
            self.validate_initial_source(session_id, proof)
            return proof
        if payload.get("mode") != "shared_travel_source" or source.get("status") != "completed":
            return None
        self.validate(session_id, row[0], source)
        if source.get("planningBinding") is not None:
            return None
        return {**self._initial_proof(row[0], source), "carrierAssistantTurnId": row[0]}

    def validate_initial_source(self, session_id: str, proof: dict) -> dict:
        row = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id=? AND session_id=? "
                              "AND role='assistant' AND status!='superseded'",
                              (proof.get("sourceAssistantTurnId"), session_id)).fetchone()
        source = json.loads(row[0] or "{}").get("sharedSource") or {} if row else {}
        self.validate(session_id, str(proof.get("sourceAssistantTurnId") or ""), source)
        base_proof = self._initial_proof(proof["sourceAssistantTurnId"], source)
        carrier_id = proof.get("carrierAssistantTurnId")
        carrier = self.db.execute("SELECT agent_response_json FROM conversation_turns WHERE id=? AND session_id=? "
            "AND role='assistant' AND status!='superseded'", (carrier_id, session_id)).fetchone()
        carrier_payload = json.loads(carrier[0] or "{}") if carrier else {}
        if (source.get("planningBinding") is not None
                or {key: value for key, value in proof.items() if key != "carrierAssistantTurnId"} != base_proof
                or not carrier or (carrier_id != proof["sourceAssistantTurnId"] and carrier_payload.get("pendingSharedSource") != base_proof)):
            raise ValueError("shared_initial_source_binding_invalid")
        return source

    def bind_initial_request(self, session_id: str, user_turn_id: str, route: dict) -> None:
        from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
        proof = (route.get("semanticAction") or {}).get("sharedSourceBinding") or {}
        if proof != self.initial_source(session_id):
            raise ValueError("shared_initial_source_binding_invalid")
        row = self.db.execute("SELECT agent_request_json FROM conversation_turns WHERE id=? AND session_id=? "
                              "AND role='user' AND status!='superseded'", (user_turn_id, session_id)).fetchone()
        request = json.loads(row[0] or "{}") if row else {}
        capture = request.get("capabilityIngress") or {}
        if not row or capture.get("sourceAssistantTurnId") != proof["carrierAssistantTurnId"]:
            raise ValueError("shared_initial_source_binding_invalid")
        request["conversationIntent"] = copy.deepcopy(route)
        request["sharedGuideIdentityPolicy"] = GuideContinuationRequirementService.IDENTITY_POLICY
        self.db.execute("UPDATE conversation_turns SET agent_request_json=? WHERE id=?",
                        (json.dumps(request, ensure_ascii=False), user_turn_id))
        self.db.commit()

    def initial_requirement(self, session_id: str, user_turn_id: str, root: dict, *, expected_proof: dict | None = None) -> dict | None:
        """Rebind only the admitted request's source to its new frozen contract."""
        from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
        from src.services.guide_source_refresh_service import GuideSourceRefreshService
        from src.services.simple_open_direction_service import SimpleOpenDirectionService
        row = self.db.execute("SELECT agent_request_json FROM conversation_turns WHERE id=? AND session_id=? "
                              "AND role='user' AND status!='superseded'", (user_turn_id, session_id)).fetchone()
        request = json.loads(row[0] or "{}") if row else {}
        action = (request.get("conversationIntent") or {}).get("semanticAction") or {}
        if ("sharedGuideIdentityPolicy" in request
                and request["sharedGuideIdentityPolicy"] != GuideContinuationRequirementService.IDENTITY_POLICY):
            raise ValueError("shared_initial_source_binding_invalid")
        if action.get("name") not in {"create_from_shared_guide", "answer_clarification"}:
            if expected_proof is not None:
                raise ValueError("shared_initial_source_binding_invalid")
            return None
        proof = action.get("sharedSourceBinding") or {}
        if expected_proof is not None and proof != expected_proof:
            raise ValueError("shared_initial_source_binding_invalid")
        source = self.validate_initial_source(session_id, proof)
        current = SimpleOpenDirectionService(self.db).latest_root(session_id=session_id)
        contract = root.get("requestIntentContract") or {}
        if (not current or current["id"] != root.get("id") or current != root
                or root.get("planningRootId") != user_turn_id
                or (request.get("capabilityIngress") or {}).get("sourceAssistantTurnId") != proof["carrierAssistantTurnId"]
                or root.get("requestContractFingerprint") != GuideSourceRefreshService.fingerprint(contract)
                or root.get("expectedBaseVersionId") is not None):
            raise ValueError("shared_initial_source_binding_invalid")
        advice = self.advice(source)
        documents = self.documents(source, advice)
        allowed = sorted({str(item["intentType"]) for item in contract.get("requiredIntents") or []
                          if isinstance(item, dict) and item.get("intentType")})
        hints = GuideSourceRefreshService._document_hints(documents, advice["evidenceFingerprint"], allowed)
        if not hints:
            raise ValueError("guide_source_places_missing")
        requirement = {"schemaVersion": GuideContinuationRequirementService.SCHEMA_VERSION,
            "evidenceKind": "shared_public_note", "initialSourceBinding": copy.deepcopy(proof),
            "initialUserTurnId": user_turn_id, "sourceAssistantTurnId": proof["sourceAssistantTurnId"],
            "capabilitySourceAssistantTurnId": root["sourceAssistantTurnId"],
            "guideChoiceExecutionId": source["executionId"], "sourceMaterialId": source["sourceMaterialId"],
            "planningSelectionRootTurnId": user_turn_id, "rootPortfolioId": root["id"],
            "requestContractFingerprint": root["requestContractFingerprint"], "expectedBaseVersionId": None,
            "queryFingerprint": advice["queryFingerprint"], "evidenceFingerprint": advice["evidenceFingerprint"],
            "minimumNovelGroundedPlaceCount": 1, "allowedSourceIntentTypes": allowed, "placeHints": hints,
            "sourceDocumentEvidence": {"schemaVersion": GuideSourceRefreshService.SCHEMA,
                "documents": documents, "fingerprint": GuideSourceRefreshService.fingerprint(documents)}}
        if "sharedGuideIdentityPolicy" in request:
            requirement["identityPolicy"] = request["sharedGuideIdentityPolicy"]
        requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(requirement)
        if not GuideSourceRefreshService.validate_documents(requirement):
            raise ValueError("guide_source_evidence_invalid")
        return requirement

    @staticmethod
    def advice(result: dict) -> dict:
        from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService

        fingerprint = result.get("contentFingerprint") or ""
        query = hashlib.sha256(json.dumps(["shared_public_note", result.get("sourceMaterialId"), fingerprint]).encode()).hexdigest()
        evidence = GuideContinuationRequirementService.evidence_fingerprint(
            query_fingerprint=query, source_fingerprints=[fingerprint])
        return {"status": "completed", "evidenceKind": "shared_public_note",
                "queryFingerprint": query, "evidenceFingerprint": evidence,
                "sourceRefs": [{"refId": "shared-ref-" + fingerprint[:24],
                                "url": result["canonicalUrl"], "title": result["title"],
                                "snippet": "", "sourceFingerprint": fingerprint}], "placeHints": []}

    @staticmethod
    def documents(result: dict, advice: dict) -> list[dict]:
        source = advice["sourceRefs"][0]
        return [{"refId": source["refId"], "sourceFingerprint": source["sourceFingerprint"],
                 "sourceUrl": source["url"], "readAttempted": False, "status": "succeeded",
                 "reason": None, "canonicalUrl": result["canonicalUrl"], "title": result["title"],
                 "bodyText": result["bodyText"], "contentFingerprint": result["contentFingerprint"],
                 "fetchedAt": result["fetchedAt"], "contentKind": "article"}]
