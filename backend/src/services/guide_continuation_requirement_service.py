"""Server-owned evidence overlay for guide-grounded plan continuations.

Travel-guide snippets are untrusted discovery hints.  This service binds one
opaque continuation capability to the exact persisted guide result that
produced it, re-verifies that result's evidence fingerprint, and returns only
source-backed place hints.  The returned object grants no POI identity or
write authority; downstream AMap grounding and proposal verification remain
mandatory.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from src.services.travel_guide_advice_service import TravelGuideAdviceService


@dataclass(frozen=True)
class GuideContinuationRequirementError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


class GuideContinuationRequirementService:
    SCHEMA_VERSION = "guide-continuation-requirement-v1"
    PLACE_HINT_SCHEMA_VERSION = "guide-place-hint-v1"
    IDENTITY_POLICY = "guide-poi-identity-v1"

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @staticmethod
    def evidence_fingerprint(*, query_fingerprint: str, source_fingerprints: list[str]) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "queryFingerprint": str(query_fingerprint),
                    "sourceFingerprints": [str(item) for item in source_fingerprints],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def requirement_fingerprint(requirement: dict[str, Any]) -> str:
        """Hash the complete server-issued overlay, excluding its own hash."""

        material = {
            str(key): copy.deepcopy(value)
            for key, value in requirement.items()
            if str(key) != "requirementFingerprint"
        }
        return hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def build(
        self,
        *,
        session_id: str,
        selected_choice: dict[str, Any],
        active_version_id: Optional[str],
        require_place_hints: bool = True,
    ) -> dict[str, Any]:
        option = selected_choice.get("option") if isinstance(selected_choice.get("option"), dict) else {}
        capability_source_turn_id = str(selected_choice.get("sourceAssistantTurnId") or "")
        choice_id = str(selected_choice.get("choiceId") or "")
        if (
            str(option.get("kind") or "") != "simple_direction_more_plans"
            or str(option.get("action") or "") != "continue_plan_expansion"
            or not capability_source_turn_id
            or not choice_id
        ):
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        latest = self.db.execute(
            """SELECT id, agent_response_json FROM conversation_turns
            WHERE session_id = ? AND role = 'assistant' AND status != 'superseded'
            ORDER BY turn_index DESC LIMIT 1""",
            (session_id,),
        ).fetchone()
        if latest is None or str(latest["id"] or "") != capability_source_turn_id:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        latest_payload = self._json(latest["agent_response_json"])
        canonical_options = [
            item
            for item in latest_payload.get("choiceOptions") or []
            if isinstance(item, dict)
            and str(item.get("id") or item.get("choiceId") or "") == choice_id
            and str(item.get("kind") or "") == "simple_direction_more_plans"
            and str(item.get("action") or "") == "continue_plan_expansion"
        ]
        if len(canonical_options) != 1:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        canonical_option = canonical_options[0]
        for field in (
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "requestContractFingerprint",
            "expectedBaseVersionId",
            "guideEvidenceSourceAssistantTurnId",
            "guideChoiceExecutionId",
            "guideQueryFingerprint",
            "guideEvidenceFingerprint",
        ):
            if str(canonical_option.get(field) or "") != str(option.get(field) or ""):
                raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        requirement = self.validate_original_evidence(session_id=session_id, option=canonical_option,
            capability_source_turn_id=capability_source_turn_id, active_version_id=active_version_id,
            require_place_hints=require_place_hints)
        # Only a new operation adopts the current policy. Replay uses the exact
        # persisted operation's binding; pure source/carrier validation below
        # must never rewrite a legacy overlay or change its fingerprint.
        from src.services.conversation_operation_identity import ConversationOperationIdentity
        try:
            execution = ConversationOperationIdentity(self.db).execution(
                session_id, capability_source_turn_id, choice_id,
                guide_evidence=requirement["evidenceFingerprint"],
            )
        except ValueError as error:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid") from error
        if execution is None:
            requirement["identityPolicy"] = self.IDENTITY_POLICY
        else:
            journal = self._json(execution.get("continuation_json"))
            stored = journal.get("guideSourceBindingRequirement") or journal.get("guideContinuationRequirement")
            if not isinstance(stored, dict) or not stored:
                raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
            if "identityPolicy" in stored:
                if stored["identityPolicy"] != self.IDENTITY_POLICY:
                    raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
                requirement["identityPolicy"] = stored["identityPolicy"]
        requirement["requirementFingerprint"] = self.requirement_fingerprint(requirement)
        return requirement

    def validate_original_evidence(self, *, session_id: str, option: dict[str, Any],
                                  capability_source_turn_id: str, active_version_id: Optional[str],
                                  require_place_hints: bool = True) -> dict[str, Any]:
        """Internal read-only lineage check; does not grant latest-carrier authority."""
        canonical_option = option
        guide_source_turn_id = str(
            canonical_option.get("guideEvidenceSourceAssistantTurnId")
            or capability_source_turn_id
        )
        guide_row = self.db.execute(
            """SELECT id, agent_response_json FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
            (guide_source_turn_id, session_id),
        ).fetchone()
        if guide_row is None:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        guide_payload = self._json(guide_row["agent_response_json"])
        if str(guide_payload.get("mode") or "") not in {"travel_guide_advice", "shared_travel_source"}:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        advice = guide_payload.get("guideAdvice") if isinstance(guide_payload.get("guideAdvice"), dict) else {}
        if str(advice.get("status") or "") != "completed":
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        planning_root_id = str(option.get("planningSelectionRootTurnId") or "")
        portfolio_id = str(option.get("rootPortfolioId") or "")
        request_fingerprint = str(option.get("requestContractFingerprint") or "")
        expected_base_version_id = str(option.get("expectedBaseVersionId") or "")
        root = self.db.execute(
            """SELECT id, source_user_turn_id, request_contract_fingerprint,
                      expected_base_version_id, source_assistant_turn_id, summary_json
            FROM agent_plan_portfolios WHERE id = ? AND session_id = ?""",
            (portfolio_id, session_id),
        ).fetchone()
        if (
            root is None
            or not all((planning_root_id, portfolio_id, request_fingerprint, guide_source_turn_id))
            or str(root["source_user_turn_id"] or "") != planning_root_id
            or str(root["request_contract_fingerprint"] or "") != request_fingerprint
            or str(root["expected_base_version_id"] or "") != expected_base_version_id
            or str(root["source_assistant_turn_id"] or "") != capability_source_turn_id
            or expected_base_version_id != str(active_version_id or "")
        ):
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        if guide_payload.get("mode") == "shared_travel_source":
            # An explicit public-note read is NOT a fabricated web-search choice
            # execution. Bind its own direct, persisted intake journal instead.
            from src.services.shared_travel_source_service import SharedTravelSourceService
            from src.services.guide_source_refresh_service import GuideSourceRefreshService
            source = guide_payload.get("sharedSource") or {}
            try:
                SharedTravelSourceService(self.db).validate(session_id, guide_source_turn_id, source)
            except ValueError as error:
                raise GuideContinuationRequirementError("guide_evidence_lineage_invalid") from error
            expected_advice = SharedTravelSourceService.advice(source)
            initial_requirement = None
            if source.get("planningBinding") is None:
                # The source was admitted before any root existed. The exact
                # initial request journal, not copied source JSON, binds it now.
                from src.services.simple_open_direction_service import SimpleOpenDirectionService
                try:
                    initial_requirement = SharedTravelSourceService(self.db).initial_requirement(
                        session_id, planning_root_id,
                        SimpleOpenDirectionService(self.db).latest_root(session_id=session_id) or {})
                except ValueError as error:
                    raise GuideContinuationRequirementError("guide_evidence_lineage_invalid") from error
                if not initial_requirement or initial_requirement.get("sourceAssistantTurnId") != guide_source_turn_id:
                    raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
            if (advice != expected_advice
                    or (initial_requirement is None and source.get("planningBinding") != {
                        "planningSelectionRootTurnId": planning_root_id, "rootPortfolioId": portfolio_id,
                        "requestContractFingerprint": request_fingerprint,
                        "expectedBaseVersionId": option.get("expectedBaseVersionId")})
                    or option.get("guideChoiceExecutionId") != source.get("executionId")
                    or option.get("guideQueryFingerprint") != advice["queryFingerprint"]
                    or option.get("guideEvidenceFingerprint") != advice["evidenceFingerprint"]):
                raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
            contract = self._json(root["summary_json"]).get("requestIntentContract") or {}
            allowed = sorted({str(item.get("intentType")) for item in contract.get("requiredIntents") or []
                              if isinstance(item, dict) and item.get("intentType")})
            hints = GuideSourceRefreshService._document_hints(
                SharedTravelSourceService.documents(source, advice), advice["evidenceFingerprint"], allowed)
            if require_place_hints and not hints:
                raise GuideContinuationRequirementError("guide_place_hints_missing")
            requirement = {"schemaVersion": self.SCHEMA_VERSION, "evidenceKind": "shared_public_note",
                "sourceAssistantTurnId": guide_source_turn_id, "capabilitySourceAssistantTurnId": capability_source_turn_id,
                "guideChoiceExecutionId": source["executionId"], "sourceMaterialId": source["sourceMaterialId"],
                "planningSelectionRootTurnId": planning_root_id, "rootPortfolioId": portfolio_id,
                "requestContractFingerprint": request_fingerprint, "expectedBaseVersionId": option.get("expectedBaseVersionId"),
                "queryFingerprint": advice["queryFingerprint"], "evidenceFingerprint": advice["evidenceFingerprint"],
                "minimumNovelGroundedPlaceCount": 1, "allowedSourceIntentTypes": allowed, "placeHints": hints}
            requirement["requirementFingerprint"] = self.requirement_fingerprint(requirement)
            return requirement

        execution = self.db.execute(
            """SELECT * FROM agent_choice_executions
            WHERE session_id = ? AND action = 'search_travel_guide_advice'
              AND status = 'succeeded' AND execution_turn_id = ?
            ORDER BY updated_at DESC LIMIT 1""",
            (session_id, guide_source_turn_id),
        ).fetchone()
        if execution is None:
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        expected_guide_execution_id = str(canonical_option.get("guideChoiceExecutionId") or "")
        if expected_guide_execution_id and expected_guide_execution_id != str(execution["id"] or ""):
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")
        execution_outcome = self._json(execution["outcome_json"])
        outcome_advice = (
            execution_outcome.get("guideAdvice")
            if isinstance(execution_outcome.get("guideAdvice"), dict)
            else {}
        )
        if (
            str(execution_outcome.get("mode") or "") != "travel_guide_advice"
            or str(outcome_advice.get("queryFingerprint") or "")
            != str(advice.get("queryFingerprint") or "")
            or str(outcome_advice.get("evidenceFingerprint") or "")
            != str(advice.get("evidenceFingerprint") or "")
            or [
                str(item.get("sourceFingerprint") or "")
                for item in outcome_advice.get("sourceRefs") or []
                if isinstance(item, dict)
            ]
            != [
                str(item.get("sourceFingerprint") or "")
                for item in advice.get("sourceRefs") or []
                if isinstance(item, dict)
            ]
            or any(
                int(execution_outcome.get(field) or 0) != 0
                for field in (
                    "newProposalDelta",
                    "proposalDelta",
                    "proposalWriteDelta",
                    "versionDelta",
                    "patchDelta",
                    "routeWriteDelta",
                )
            )
        ):
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        source_refs = [item for item in advice.get("sourceRefs") or [] if isinstance(item, dict)]
        ordered_source_fingerprints = [str(item.get("sourceFingerprint") or "") for item in source_refs]
        query_fingerprint = str(advice.get("queryFingerprint") or "")
        evidence_fingerprint = str(advice.get("evidenceFingerprint") or "")
        if (
            not query_fingerprint
            or not evidence_fingerprint
            or not source_refs
            or any(not item for item in ordered_source_fingerprints)
            or self.evidence_fingerprint(
                query_fingerprint=query_fingerprint,
                source_fingerprints=ordered_source_fingerprints,
            )
            != evidence_fingerprint
            or str(option.get("guideEvidenceFingerprint") or evidence_fingerprint) != evidence_fingerprint
            or str(canonical_option.get("guideQueryFingerprint") or query_fingerprint) != query_fingerprint
        ):
            raise GuideContinuationRequirementError("guide_evidence_lineage_invalid")

        raw_hints = TravelGuideAdviceService.extract_place_hints(advice, allow_legacy_source_derivation=True)
        place_hints = self._validated_place_hints(
            raw_hints,
            advice=advice,
            evidence_fingerprint=evidence_fingerprint,
        )
        if not place_hints and require_place_hints:
            raise GuideContinuationRequirementError("guide_place_hints_missing")

        requirement = {
            "schemaVersion": self.SCHEMA_VERSION,
            "sourceAssistantTurnId": guide_source_turn_id,
            "capabilitySourceAssistantTurnId": capability_source_turn_id,
            "guideChoiceExecutionId": str(execution["id"] or ""),
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": request_fingerprint,
            "expectedBaseVersionId": option.get("expectedBaseVersionId"),
            "queryFingerprint": query_fingerprint,
            "evidenceFingerprint": evidence_fingerprint,
            "minimumNovelGroundedPlaceCount": 1,
            "placeHints": place_hints,
        }
        requirement["requirementFingerprint"] = self.requirement_fingerprint(requirement)
        return requirement

    @classmethod
    def _validated_place_hints(
        cls,
        raw_hints: Any,
        *,
        advice: dict[str, Any],
        evidence_fingerprint: str,
    ) -> list[dict[str, Any]]:
        source_refs = {
            str(item.get("refId") or ""): item
            for item in advice.get("sourceRefs") or []
            if isinstance(item, dict) and str(item.get("refId") or "")
        }
        recommendations = {
            str(item.get("refId") or ""): item
            for item in advice.get("recommendations") or []
            if isinstance(item, dict) and str(item.get("refId") or "")
        }
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_hints or []:
            if not isinstance(raw, dict):
                continue
            mention = str(raw.get("mentionText") or "").strip()
            intent_type = str(raw.get("intentType") or "").strip()
            ref_ids = [str(item) for item in raw.get("sourceRefIds") or [] if str(item)]
            if (
                str(raw.get("schemaVersion") or cls.PLACE_HINT_SCHEMA_VERSION)
                != cls.PLACE_HINT_SCHEMA_VERSION
                or not mention
                or not intent_type
                or not ref_ids
                or any(ref_id not in source_refs for ref_id in ref_ids)
                or str(raw.get("guideEvidenceFingerprint") or evidence_fingerprint) != evidence_fingerprint
            ):
                continue
            supporting_refs = [
                ref_id
                for ref_id in ref_ids
                if mention in str(source_refs[ref_id].get("title") or "")
                or mention in str((recommendations.get(ref_id) or {}).get("title") or "")
                or mention in str((recommendations.get(ref_id) or {}).get("text") or "")
            ]
            if not supporting_refs:
                continue
            key = (intent_type.casefold(), mention.casefold())
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "schemaVersion": cls.PLACE_HINT_SCHEMA_VERSION,
                    "mentionText": mention,
                    "intentType": intent_type,
                    "sourceRefIds": supporting_refs,
                    "sourceFingerprints": [
                        str(source_refs[ref_id].get("sourceFingerprint") or "") for ref_id in supporting_refs
                    ],
                    "guideEvidenceFingerprint": evidence_fingerprint,
                    "verificationStatus": "unresolved_amap_grounding",
                }
            )
            if len(normalized) >= len(source_refs):
                break
        return copy.deepcopy(normalized)

    @staticmethod
    def _json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
