"""Bounded admitted-candidate inventory for dynamic Portfolio directions."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


class CreativeCandidateInventoryService:
    SCHEMA_VERSION = "creative-candidate-inventory-v1"
    MAX_ENTRIES = 96

    @classmethod
    def capture(
        cls,
        candidate: dict[str, Any],
        *,
        family: str,
        intent_type: str,
        day_number: int,
        time_window: str,
        brief_id: str,
        pool_id: str,
        planning_slot_id: str,
    ) -> dict[str, Any] | None:
        report = (
            candidate.get("consumerAdmissionReport")
            if isinstance(candidate.get("consumerAdmissionReport"), dict)
            else {}
        )
        candidate_id = str(candidate.get("amapId") or candidate.get("id") or "").strip()
        if report.get("scoreEligible") is not True or not candidate_id:
            return None
        if candidate.get("longitude") is None or candidate.get("latitude") is None:
            return None
        area_key = str(
            candidate.get("businessArea") or candidate.get("business_area") or candidate.get("district") or ""
        ).strip()
        if not area_key:
            area_key = f"grid:{float(candidate['longitude']):.2f},{float(candidate['latitude']):.2f}"
        return {
            "candidateId": candidate_id,
            "name": str(candidate.get("name") or "")[:120],
            "family": str(family or intent_type),
            "intentType": str(intent_type),
            "dayNumber": int(day_number),
            "timeWindow": str(time_window),
            "areaKey": area_key[:120],
            "longitude": float(candidate["longitude"]),
            "latitude": float(candidate["latitude"]),
            "briefId": str(brief_id),
            "poolId": str(pool_id),
            "planningSlotId": str(planning_slot_id),
            "scoreEligible": True,
            "admissionFingerprint": str(report.get("evidenceFingerprint") or report.get("consumerFingerprint") or "")[
                :128
            ],
        }

    @classmethod
    def build(cls, entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int, str, str]] = set()
        for raw in entries:
            if not isinstance(raw, dict) or raw.get("scoreEligible") is not True:
                continue
            key = (
                str(raw.get("candidateId") or ""),
                str(raw.get("family") or ""),
                int(raw.get("dayNumber") or 0),
                str(raw.get("timeWindow") or ""),
                str(raw.get("areaKey") or ""),
            )
            if not all((key[0], key[1], key[4])) or key in seen:
                continue
            seen.add(key)
            normalized.append(dict(raw))
            if len(normalized) >= cls.MAX_ENTRIES:
                break
        indexes: dict[str, dict[str, list[str]]] = {}
        for index_name, field in (
            ("byIntent", "intentType"),
            ("byDay", "dayNumber"),
            ("byTime", "timeWindow"),
            ("byArea", "areaKey"),
            ("byThemeFamily", "family"),
        ):
            grouped: dict[str, list[str]] = defaultdict(list)
            for item in normalized:
                candidate_id = str(item.get("candidateId") or "")
                bucket = str(item.get(field) or "")
                if candidate_id and bucket and candidate_id not in grouped[bucket]:
                    grouped[bucket].append(candidate_id)
            indexes[index_name] = dict(grouped)
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "entries": normalized,
            "indexes": indexes,
            "scoreEligibleCount": len(normalized),
            "areaClusterCount": len(indexes["byArea"]),
            "themeFamilyCount": len(indexes["byThemeFamily"]),
        }

    @classmethod
    def merge(cls, *inventories: dict[str, Any] | None) -> dict[str, Any]:
        entries = [
            item
            for inventory in inventories
            if isinstance(inventory, dict)
            for item in inventory.get("entries") or []
            if isinstance(item, dict)
        ]
        return cls.build(entries)
