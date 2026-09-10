"""Provider-evidence gate for standalone experience occurrences."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


class ExperienceIndependenceService:
    SCHEMA_VERSION = "experience-independence-v1"
    CATEGORY_EVIDENCE_ASSET = Path(__file__).with_name("amap_category_evidence_v1.json")
    _AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")
    _FINGERPRINT_RE = re.compile(r"[0-9a-fA-F]{64}")
    _ATTACHED_FACILITY_RE = re.compile(r"(?:售票处|入口|出口|山门|服务中心|游客中心|停车场|卫生间|商店|码头)$")
    _NON_OPERATING_RE = re.compile(
        r"(?:暂停营业|停止营业|歇业|关闭|closed|suspended)",
        re.IGNORECASE,
    )

    @classmethod
    def evaluate(cls, candidate: Any, day_anchor: Any) -> dict[str, Any]:
        candidate_id = cls._amap_id(candidate, "id", "amapId", "amap_id")
        anchor_ids = {
            cls._amap_id(day_anchor, "amapId", "amap_id", "id"),
            cls._amap_id(day_anchor, "parentPoiId", "parent_poi_id"),
            cls._amap_id(day_anchor, "indoorParentPoiId", "indoor_parent_poi_id"),
        }
        anchor_ids.discard("")
        parent_id = cls._amap_id(candidate, "parentPoiId", "parent_poi_id")
        indoor_parent_id = cls._amap_id(candidate, "indoorParentPoiId", "indoor_parent_poi_id")
        physical_group = parent_id or indoor_parent_id or candidate_id
        candidate_address = cls._normalize_text(cls._value(candidate, "address"))
        anchor_name = cls._normalize_text(cls._value(day_anchor, "name"))
        address_containment = bool(anchor_name and len(anchor_name) >= 2 and anchor_name in candidate_address)
        provider_type = cls._value(candidate, "type", "providerRawType", "provider_raw_type")
        typecode = str(cls._value(candidate, "providerTypeCode", "provider_type_code", "typecode") or "").strip()
        name = cls._value(candidate, "name")
        business_status = cls._value(candidate, "businessStatus", "business_status")
        distance = cls._number(cls._value(candidate, "distanceMeters", "distance_meters"))
        provider_receipt = cls._provider_receipt(candidate)
        provider_queried_at = cls._provider_queried_at(candidate)
        category_decision = cls._category_decision(
            experience_role="standalone_park",
            typecode=typecode,
            provider_type=provider_type,
        )

        reason_codes: list[str] = []
        if provider_receipt is None:
            reason_codes.append("provider_query_receipt_missing_or_invalid")
        if provider_queried_at is None:
            reason_codes.append("provider_queried_at_missing_or_invalid")

        same_parent = bool(
            (parent_id and parent_id in anchor_ids) or (indoor_parent_id and indoor_parent_id in anchor_ids)
        )
        if same_parent:
            status = "embedded_in_day_anchor"
            reason_codes.append("provider_parent_matches_day_anchor")
        elif address_containment:
            status = "embedded_in_day_anchor"
            reason_codes.append("provider_address_inside_day_anchor")
        else:
            normalized_facility_name = re.sub(r"[（(].*$", "", str(name or "")).strip()
            attached_facility = bool(cls._ATTACHED_FACILITY_RE.search(normalized_facility_name))
            non_operating = bool(cls._NON_OPERATING_RE.search(str(business_status or "")))
            parent_conflict = bool(parent_id or indoor_parent_id)
            category_status = str(category_decision.get("status") or "unrecognized")
            standalone_category = category_status == "accepted"
            traceable_provider_evidence = provider_receipt is not None and provider_queried_at is not None

            if attached_facility:
                reason_codes.append("attached_facility_conflict")
            if non_operating:
                reason_codes.append("provider_business_status_not_open")
            if parent_conflict:
                reason_codes.append("provider_parent_requires_independence_proof")
            if category_status == "rejected":
                reason_codes.append("standalone_park_category_rejected")
            elif category_status == "source_unavailable":
                reason_codes.append("standalone_park_category_evidence_unavailable")
            elif not standalone_category:
                reason_codes.append("standalone_park_category_missing")

            if (
                standalone_category
                and traceable_provider_evidence
                and not attached_facility
                and not non_operating
                and not parent_conflict
            ):
                status = "standalone_verified"
            else:
                status = "independence_pending"
                if distance is not None:
                    reason_codes.append("distance_is_not_parent_evidence")

        category_material = {
            "experienceRole": "standalone_park",
            "providerTypeCode": typecode or None,
            "providerType": provider_type or None,
            "sourceFingerprint": category_decision.get("sourceFingerprint"),
            "decision": category_decision.get("status"),
        }
        category_fingerprint = (
            cls._fingerprint(category_material)
            if typecode and provider_type and category_decision.get("sourceFingerprint")
            else None
        )
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "candidateAmapId": candidate_id,
            "dayAnchorAmapId": cls._amap_id(day_anchor, "amapId", "amap_id", "id"),
            "physicalGroupId": physical_group,
            "status": status,
            "evidence": {
                "parentPoiId": parent_id or None,
                "indoorParentPoiId": indoor_parent_id or None,
                "typecode": typecode or None,
                "providerType": provider_type or None,
                "businessStatus": business_status or None,
                "addressContainmentMatched": address_containment,
                "distanceMeters": distance,
                "providerQueryReceiptFingerprint": provider_receipt,
                "providerQueriedAt": provider_queried_at,
                "providerCategorySourceFingerprint": category_decision.get("sourceFingerprint"),
                "providerCategorySourceArtifactFingerprint": category_decision.get("sourceArtifactFingerprint"),
                "providerCategorySourceVersion": category_decision.get("sourceVersion"),
                "providerCategorySourceUrl": category_decision.get("sourceUrl"),
                "providerCategoryDecision": category_decision.get("status"),
                "providerCategoryEvidenceFingerprint": category_fingerprint,
            },
            "reasonCodes": list(dict.fromkeys(reason_codes)),
        }

    @classmethod
    def physical_group_id(cls, candidate: Any) -> str:
        return (
            cls._amap_id(candidate, "parentPoiId", "parent_poi_id")
            or cls._amap_id(candidate, "indoorParentPoiId", "indoor_parent_poi_id")
            or cls._amap_id(candidate, "amapId", "amap_id", "id")
        )

    @classmethod
    def accepted_provider_typecodes(cls, experience_role: str) -> tuple[str, ...]:
        """Expose the exact Provider categories used by an admission role.

        Acquisition must not search a broader, incompatible category and then
        rely on Consumer Admission to reject every result.  Returning an empty
        tuple keeps callers fail-closed when the versioned evidence is missing
        or malformed.
        """

        payload = cls._category_evidence()
        role_policy = (
            (payload.get("experienceRoles") or {}).get(str(experience_role or ""))
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(role_policy, dict):
            return ()
        typecodes: list[str] = []
        for item in role_policy.get("acceptedCategories") or []:
            if not isinstance(item, dict):
                return ()
            typecode = str(item.get("typecode") or "").strip()
            provider_type = cls._normalize_type_path(item.get("providerType"))
            if not re.fullmatch(r"\d{6}", typecode) or not provider_type:
                return ()
            if typecode not in typecodes:
                typecodes.append(typecode)
        return tuple(typecodes)

    @classmethod
    @lru_cache(maxsize=1)
    def _category_evidence(cls) -> dict[str, Any] | None:
        try:
            raw = cls.CATEGORY_EVIDENCE_ASSET.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schemaVersion") != "provider-category-evidence-v1"
            or payload.get("provider") != "amap-place-search"
            or not isinstance(payload.get("experienceRoles"), dict)
        ):
            return None
        return {
            **payload,
            "contentSha256": hashlib.sha256(raw).hexdigest(),
            "assetName": cls.CATEGORY_EVIDENCE_ASSET.name,
        }

    @classmethod
    def _category_decision(
        cls,
        *,
        experience_role: str,
        typecode: str,
        provider_type: Any,
    ) -> dict[str, Any]:
        payload = cls._category_evidence()
        if not isinstance(payload, dict):
            return {"status": "source_unavailable"}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source_metadata = {
            "sourceFingerprint": str(payload.get("contentSha256") or "") or None,
            "sourceArtifactFingerprint": str(source.get("artifactSha256") or "") or None,
            "sourceVersion": str(source.get("contentVersion") or "") or None,
            "sourceUrl": str(source.get("url") or "") or None,
        }
        role_policy = (payload.get("experienceRoles") or {}).get(experience_role)
        if not isinstance(role_policy, dict):
            return {"status": "source_unavailable", **source_metadata}
        candidate_identity = (str(typecode or "").strip(), cls._normalize_type_path(provider_type))

        rejected = {
            (
                str(item.get("typecode") or "").strip(),
                cls._normalize_type_path(item.get("providerType")),
            )
            for item in role_policy.get("rejectedCategories") or []
            if isinstance(item, dict)
        }
        if candidate_identity in rejected:
            return {"status": "rejected", **source_metadata}

        accepted = {
            (
                str(item.get("typecode") or "").strip(),
                cls._normalize_type_path(item.get("providerType")),
            )
            for item in role_policy.get("acceptedCategories") or []
            if isinstance(item, dict)
        }
        return {
            "status": "accepted" if candidate_identity in accepted else "unrecognized",
            **source_metadata,
        }

    @staticmethod
    def _value(candidate: Any, *names: str) -> Any:
        for name in names:
            value = candidate.get(name) if isinstance(candidate, Mapping) else getattr(candidate, name, None)
            if value not in (None, ""):
                return value
        return ""

    @classmethod
    def _amap_id(cls, candidate: Any, *names: str) -> str:
        value = str(cls._value(candidate, *names) or "").strip().upper()
        return value if cls._AMAP_ID_RE.fullmatch(value) else ""

    @classmethod
    def _provider_receipt(cls, candidate: Any) -> str | None:
        value = str(
            cls._value(
                candidate,
                "providerQueryReceiptFingerprint",
                "provider_query_receipt_fingerprint",
            )
            or ""
        ).strip()
        return value if cls._FINGERPRINT_RE.fullmatch(value) else None

    @classmethod
    def _provider_queried_at(cls, candidate: Any) -> str | None:
        raw = cls._value(candidate, "providerQueriedAt", "provider_queried_at")
        if isinstance(raw, datetime):
            parsed = raw
            serialized = raw.isoformat()
        else:
            serialized = str(raw or "").strip()
            if not serialized:
                return None
            try:
                parsed = datetime.fromisoformat(serialized.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return serialized

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "")).casefold()

    @staticmethod
    def _normalize_type_path(value: Any) -> str:
        return ";".join(
            segment.strip().casefold() for segment in str(value or "").replace("；", ";").split(";") if segment.strip()
        )

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
