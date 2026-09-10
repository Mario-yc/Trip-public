from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


class EntityQualificationEvidenceService:
    """Generic resolver backed by versioned, attributable evidence assets."""

    ASSET_DIRECTORY = Path(__file__).resolve().parent
    BINDING_SCHEMA_VERSION = "entity-qualification-binding-v1"

    @classmethod
    @lru_cache(maxsize=16)
    def load(cls, scheme: str, value: str) -> dict[str, Any] | None:
        for path in sorted(cls.ASSET_DIRECTORY.glob("qualification_evidence_*.json")):
            raw = path.read_bytes()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                str(payload.get("qualificationScheme") or "") == scheme
                and str(payload.get("qualificationValue") or "") == value
            ):
                return {**payload, "contentSha256": hashlib.sha256(raw).hexdigest(), "assetName": path.name}
        return None

    @classmethod
    def canonical_hints(cls, *, locality: str, scheme: str, value: str) -> list[str]:
        payload = cls.load(scheme, value) or {}
        locality_key = cls._normalize_locality(locality)
        return [
            str(item.get("canonicalName") or "")
            for item in payload.get("entities") or []
            if isinstance(item, dict)
            and cls._normalize_locality(item.get("locality")) == locality_key
            and str(item.get("canonicalName") or "")
        ]

    @classmethod
    def qualified_entities(cls, *, locality: str, scheme: str, value: str) -> dict[str, Any] | None:
        """Return the attributable, ordered locality slice used to freeze a frontier."""

        payload = cls.load(scheme, value)
        if not isinstance(payload, dict):
            return None
        locality_key = cls._normalize_locality(locality)
        entities = [
            dict(item)
            for item in payload.get("entities") or []
            if isinstance(item, dict)
            and cls._normalize_locality(item.get("locality")) == locality_key
            and str(item.get("canonicalName") or "")
        ]
        return {
            "schemaVersion": str(payload.get("schemaVersion") or ""),
            "source": dict(payload.get("source") or {}),
            "qualificationScheme": str(payload.get("qualificationScheme") or ""),
            "qualificationValue": str(payload.get("qualificationValue") or ""),
            "contentSha256": str(payload.get("contentSha256") or ""),
            "assetName": str(payload.get("assetName") or ""),
            "entities": entities,
        }

    @classmethod
    def qualifies(cls, candidate: Any, *, scheme: str, value: str) -> bool:
        payload = cls.load(scheme, value) or {}
        name = cls._value(candidate, "parentInstitutionName", "parent_institution_name") or cls._value(
            candidate, "name"
        )
        normalized = cls._normalize_entity(name)
        candidate_locality = cls._normalize_locality(
            cls._value(candidate, "city", "locality", "province")
        )
        candidate_address = cls._normalize_locality(cls._value(candidate, "address"))
        for item in payload.get("entities") or []:
            if not isinstance(item, dict):
                continue
            accepted = [item.get("canonicalName"), *(item.get("aliases") or [])]
            evidence_locality = cls._normalize_locality(item.get("locality"))
            locality_matches = bool(
                evidence_locality
                and (
                    candidate_locality == evidence_locality
                    or (not candidate_locality and evidence_locality in candidate_address)
                )
            )
            if locality_matches and normalized and normalized in {
                cls._normalize_entity(raw) for raw in accepted if raw
            }:
                return True
        return False

    @classmethod
    def entity_fingerprint(cls, *, evidence_fingerprint: str, entity: Mapping[str, Any]) -> str:
        return cls._fingerprint(
            {
                "qualificationEvidenceFingerprint": str(evidence_fingerprint or ""),
                "canonicalName": str(entity.get("canonicalName") or "").strip(),
                "locality": str(entity.get("locality") or "").strip(),
                "aliases": sorted(str(value) for value in entity.get("aliases") or [] if str(value)),
            }
        )

    @classmethod
    def build_binding(
        cls,
        *,
        evidence: Mapping[str, Any],
        entity: Mapping[str, Any],
        planning_root_id: str,
        request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        evidence_fingerprint = str(evidence.get("contentSha256") or "")
        entity_fingerprint = cls.entity_fingerprint(
            evidence_fingerprint=evidence_fingerprint,
            entity=entity,
        )
        material = {
            "schemaVersion": cls.BINDING_SCHEMA_VERSION,
            "qualificationScheme": str(evidence.get("qualificationScheme") or ""),
            "qualificationValue": str(evidence.get("qualificationValue") or ""),
            "qualificationEvidenceFingerprint": evidence_fingerprint,
            "evidenceEntityFingerprint": entity_fingerprint,
            "canonicalName": str(entity.get("canonicalName") or "").strip(),
            "locality": str(entity.get("locality") or "").strip(),
            "planningRootId": str(planning_root_id or ""),
            "requestContractFingerprint": str(request_contract_fingerprint or ""),
        }
        material["bindingFingerprint"] = cls._fingerprint(material)
        return material

    @classmethod
    def validate_binding(
        cls,
        binding: Any,
        *,
        expected_planning_root_id: str = "",
        expected_request_contract_fingerprint: str = "",
        expected_entity_fingerprint: str = "",
        expected_canonical_name: str = "",
    ) -> str:
        if not isinstance(binding, Mapping):
            return "qualification_binding_missing"
        if str(binding.get("schemaVersion") or "") != cls.BINDING_SCHEMA_VERSION:
            return "qualification_binding_schema_invalid"
        material = {
            key: item_value
            for key, item_value in binding.items()
            if key != "bindingFingerprint"
        }
        if str(binding.get("bindingFingerprint") or "") != cls._fingerprint(material):
            return "qualification_binding_fingerprint_mismatch"
        if (
            expected_planning_root_id
            and str(binding.get("planningRootId") or "") != str(expected_planning_root_id)
        ) or (
            expected_request_contract_fingerprint
            and str(binding.get("requestContractFingerprint") or "")
            != str(expected_request_contract_fingerprint)
        ):
            return "qualification_binding_scope_mismatch"
        scheme = str(binding.get("qualificationScheme") or "")
        value = str(binding.get("qualificationValue") or "")
        evidence = cls.qualified_entities(
            locality=str(binding.get("locality") or ""),
            scheme=scheme,
            value=value,
        )
        if not isinstance(evidence, dict):
            return "qualification_binding_evidence_missing"
        if str(binding.get("qualificationEvidenceFingerprint") or "") != str(
            evidence.get("contentSha256") or ""
        ):
            return "qualification_binding_evidence_epoch_mismatch"
        canonical_name = str(binding.get("canonicalName") or "").strip()
        locality = cls._normalize_locality(binding.get("locality"))
        entity = next(
            (
                item
                for item in evidence.get("entities") or []
                if isinstance(item, dict)
                and str(item.get("canonicalName") or "").strip() == canonical_name
                and cls._normalize_locality(item.get("locality")) == locality
            ),
            None,
        )
        if entity is None:
            return "qualification_binding_entity_missing"
        entity_fingerprint = cls.entity_fingerprint(
            evidence_fingerprint=str(evidence.get("contentSha256") or ""),
            entity=entity,
        )
        if (
            str(binding.get("evidenceEntityFingerprint") or "") != entity_fingerprint
            or (expected_entity_fingerprint and entity_fingerprint != str(expected_entity_fingerprint))
            or (
                expected_canonical_name
                and cls._normalize_entity(expected_canonical_name) != cls._normalize_entity(canonical_name)
            )
        ):
            return "qualification_binding_entity_mismatch"
        return ""

    @staticmethod
    def _value(candidate: Any, *names: str) -> str:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, None)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def _normalize_entity(value: Any) -> str:
        return re.sub(r"[\s·•・()（）\[\]【】]", "", str(value or "")).casefold()

    @staticmethod
    def _normalize_locality(value: Any) -> str:
        return re.sub(r"(?:特别行政区|自治区|自治州|地区|盟|市)$", "", str(value or "").strip()).casefold()

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
