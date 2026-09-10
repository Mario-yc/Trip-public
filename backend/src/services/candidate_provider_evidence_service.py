"""Canonical, bounded provider evidence shared by admission and materialization."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


class CandidateProviderEvidenceService:
    """Project the exact provider facts that may authorize candidate admission.

    The projection is deliberately bounded so it can be copied into proposal
    snapshots and compared again at the final verifier without depending on a
    transient provider object.
    """

    _TEXT_FIELDS = {
        "name": ("name",),
        "address": ("address",),
        "district": ("district",),
        "sourceNote": ("sourceNote", "source_note"),
        "source": ("source",),
        "city": ("city",),
        "providerTypeCode": ("providerTypeCode", "provider_type_code"),
        "providerType": ("providerType", "provider_type"),
        "type": ("type",),
        "category": ("category",),
        "businessArea": ("businessArea", "business_area"),
        "openTimeToday": ("openTimeToday", "open_time_today"),
        "openTimeWeek": ("openTimeWeek", "open_time_week"),
        "parentPoiId": ("parentPoiId", "parent_poi_id"),
        "indoorParentPoiId": ("indoorParentPoiId", "indoor_parent_poi_id"),
        "businessStatus": ("businessStatus", "business_status"),
    }
    _NUMBER_FIELDS = {
        "longitude": ("longitude",),
        "latitude": ("latitude",),
        "rating": ("rating",),
        "cost": ("cost",),
        "confidence": ("confidence",),
        "routeDetourMinutes": ("routeDetourMinutes", "detourMinutes"),
    }
    _CLAIM_FIELDS = (
        "claimKey",
        "claimType",
        "stance",
        "sourceType",
        "sourceName",
        "sourceUrlHash",
        "freshness",
        "confidence",
        "summary",
        "supportedSignals",
    )
    _HEX_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

    @classmethod
    def project(
        cls,
        value: Any,
        *,
        include_missing: bool = True,
    ) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(by_alias=True, exclude_none=True)
        if not isinstance(value, dict) or not value:
            return {}

        amap_id = str(value.get("amapId") or value.get("id") or "").strip().upper()
        result: dict[str, Any] = {}
        if include_missing or "amapId" in value or "id" in value:
            normalized_amap_id = amap_id or None
            if include_missing or normalized_amap_id is not None:
                result["amapId"] = normalized_amap_id
        for target, aliases in cls._TEXT_FIELDS.items():
            if not include_missing and not any(alias in value for alias in aliases):
                continue
            raw = cls._first(value, aliases)
            normalized = str(raw).strip()[:500] if raw is not None else ""
            normalized_text = normalized or None
            if include_missing or normalized_text is not None:
                result[target] = normalized_text
        for target, aliases in cls._NUMBER_FIELDS.items():
            if not include_missing and not any(alias in value for alias in aliases):
                continue
            raw = cls._first(value, aliases)
            try:
                normalized_number = float(raw) if raw is not None and str(raw).strip() else None
            except (TypeError, ValueError):
                normalized_number = None
            # POI persistence uses 0.0 when provider confidence is absent. Keep
            # that storage default equivalent to missing provider evidence.
            if target == "confidence" and normalized_number == 0.0:
                normalized_number = None
            if include_missing or normalized_number is not None:
                result[target] = normalized_number

        for target, aliases, normalizer in (
            (
                "providerQueryReceiptFingerprint",
                ("providerQueryReceiptFingerprint", "provider_query_receipt_fingerprint"),
                cls._provider_receipt_fingerprint,
            ),
            (
                "providerQueriedAt",
                ("providerQueriedAt", "provider_queried_at"),
                cls._provider_queried_at,
            ),
        ):
            if not include_missing and not any(alias in value for alias in aliases):
                continue
            normalized = normalizer(cls._first(value, aliases))
            if include_missing or normalized is not None:
                result[target] = normalized

        for key, projector in (
            ("aliases", lambda raw: cls._string_list(raw, limit=12)),
            ("tags", lambda raw: cls._string_list(raw, limit=12)),
            ("children", cls._children),
            ("photos", cls._photos),
        ):
            if include_missing or key in value:
                result[key] = projector(value.get(key))
        if include_missing or "sourceClaims" in value or "source_claims" in value:
            result["sourceClaims"] = cls._claims(
                value.get("sourceClaims") if isinstance(value.get("sourceClaims"), list) else value.get("source_claims")
            )
        return result

    @classmethod
    def materialize_poi(
        cls,
        value: Any,
        *,
        local_id: str,
        fallback_name: str = "已核验地点",
    ) -> dict[str, Any]:
        """Create a DB-safe POI while preserving the exact provider facts.

        SQLite requires several text/numeric fields to be non-null. Empty
        storage defaults are normalized back to missing evidence by
        :meth:`project`, so final report comparison remains exact without
        allowing ``None`` to overwrite persistence-safe values.
        """

        evidence = cls.project(value)
        payload: dict[str, Any] = {
            "id": str(local_id),
            "amapId": str(evidence.get("amapId") or ""),
            "name": str(evidence.get("name") or fallback_name),
            "city": str(evidence.get("city") or ""),
            "category": str(evidence.get("category") or ""),
            "latitude": evidence.get("latitude"),
            "longitude": evidence.get("longitude"),
            "source": str(evidence.get("source") or ""),
            "confidence": float(evidence.get("confidence") or 0.0),
            "type": str(evidence.get("type") or ""),
            "district": str(evidence.get("district") or ""),
            "address": str(evidence.get("address") or ""),
            "sourceNote": str(evidence.get("sourceNote") or ""),
            "photos": list(evidence.get("photos") or []),
        }
        payload.update({key: item for key, item in evidence.items() if item is not None})
        return payload

    @staticmethod
    def _first(value: dict[str, Any], aliases: tuple[str, ...]) -> Any:
        return next((value.get(alias) for alias in aliases if value.get(alias) is not None), None)

    @staticmethod
    def _string_list(value: Any, *, limit: int) -> list[str]:
        if isinstance(value, str):
            value = [item for item in re.split(r"[;,；，]", value) if item.strip()]
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:120] for item in value[:limit] if str(item).strip()]

    @classmethod
    def _provider_receipt_fingerprint(cls, value: Any) -> str | None:
        normalized = str(value or "").strip().lower()
        return normalized if cls._HEX_FINGERPRINT_RE.fullmatch(normalized) else None

    @staticmethod
    def _provider_queried_at(value: Any) -> str | None:
        if isinstance(value, datetime):
            value = value.isoformat()
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 64 or any(ord(character) < 32 for character in normalized):
            return None
        return normalized

    @staticmethod
    def _children(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for child in value[:8]:
            if not isinstance(child, dict):
                continue
            safe = {
                key: str(child.get(key) or "").strip()[:160]
                for key in ("id", "name", "type", "typecode")
                if str(child.get(key) or "").strip()
            }
            if safe:
                result.append(safe)
        return result

    @staticmethod
    def _photos(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for photo in value[:8]:
            if not isinstance(photo, dict):
                continue
            safe = {
                key: str(photo.get(key) or "").strip()[:500]
                for key in ("title", "url")
                if str(photo.get(key) or "").strip()
            }
            if safe:
                result.append(safe)
        return result

    @classmethod
    def _claims(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for claim in value[:8]:
            if not isinstance(claim, dict):
                continue
            safe: dict[str, Any] = {}
            for key in cls._CLAIM_FIELDS:
                if key not in claim:
                    continue
                raw = claim[key]
                if key == "supportedSignals":
                    safe[key] = cls._string_list(raw, limit=8)
                elif isinstance(raw, str):
                    safe[key] = raw.strip()[:500]
                elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    safe[key] = raw
            if safe:
                result.append(safe)
        return result
