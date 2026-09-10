"""Deduplicate already-grounded candidate pools before portfolio optimization."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True)
class SharedCandidateUniverse:
    pools: dict[str, list[dict[str, Any]]]
    unique_query_count: int
    deduped_query_count: int
    # Network queries may be shared, but supply remains owned by the planning
    # pool/slot that requested it. Collapsing this to intent-only caused a Day 2
    # slot to disappear when Day 1 reused the same query result.
    pool_candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    scoped_pool_candidates: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)

    def candidates_for_pool(self, pool_id: str, intent_type: str, brief_id: str = "") -> list[dict[str, Any]]:
        scoped_key = (str(brief_id or ""), pool_id)
        owned = self.scoped_pool_candidates.get(scoped_key, []) if brief_id else self.pool_candidates.get(pool_id, [])
        # Reuse grounded network evidence when this consumer's own query was
        # budget-limited, but never reuse its prior planning identity. The
        # staging layer rebinds every returned candidate to the target
        # brief/pool/slot/day and reruns the semantic gate.
        combined = [*owned, *self.pools.get(intent_type, [])]
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in combined:
            identity = str(item.get("amapId") or item.get("id") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            result.append(dict(item))
        return result


class SharedCandidateUniverseBuilder:
    def build(self, pool_reports: list[dict[str, Any]]) -> SharedCandidateUniverse:
        pools: dict[str, list[dict[str, Any]]] = {}
        pool_candidates: dict[str, list[dict[str, Any]]] = {}
        scoped_pool_candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
        seen_queries: set[str] = set()
        deduped = 0
        seen_entities: dict[str, set[str]] = {}
        for report in pool_reports:
            if not isinstance(report, dict):
                continue
            key = self._query_key(report)
            if key in seen_queries:
                deduped += 1
            else:
                seen_queries.add(key)
            intent = str(report.get("intentType") or "").strip()
            if not intent:
                continue
            pool = pools.setdefault(intent, [])
            entity_ids = seen_entities.setdefault(intent, set())
            pool_id = str(report.get("poolId") or "").strip()
            brief_id = str(report.get("briefId") or "").strip()
            slot_ids = [str(item) for item in report.get("requiredSlotIds") or [] if str(item)]
            if not slot_ids:
                slot_ids = [str(item) for item in report.get("unresolvedSlotIds") or [] if str(item)]
            slot_days = report.get("slotDayNumbers") if isinstance(report.get("slotDayNumbers"), dict) else {}
            local_entities: set[tuple[str, str, str]] = set()
            for raw_candidate in self._report_candidates(report):
                candidate = self._normalize_candidate(raw_candidate)
                if candidate is None or not self._grounded(candidate):
                    continue
                identity = str(candidate.get("amapId") or candidate.get("id") or "")
                if not identity:
                    continue
                if identity not in entity_ids:
                    entity_ids.add(identity)
                    pool.append(dict(candidate))
                if not pool_id:
                    continue
                supplied_slots = slot_ids or [str(candidate.get("planningSlotId") or "")]
                for slot_id in supplied_slots:
                    if not slot_id:
                        continue
                    supply_key = (identity, slot_id, brief_id)
                    if supply_key in local_entities:
                        continue
                    local_entities.add(supply_key)
                    supplied = {
                        **candidate,
                        "briefId": brief_id,
                        "poolId": pool_id,
                        "planningSlotId": slot_id,
                        "dayNumber": slot_days.get(slot_id, candidate.get("dayNumber")),
                        "candidateSupply": {
                            "briefId": brief_id,
                            "poolId": pool_id,
                            "planningSlotId": slot_id,
                            "dayNumber": slot_days.get(slot_id, candidate.get("dayNumber")),
                        },
                    }
                    pool_candidates.setdefault(pool_id, []).append(supplied)
                    scoped_pool_candidates.setdefault((brief_id, pool_id), []).append(supplied)
        return SharedCandidateUniverse(
            pools=pools,
            unique_query_count=len(seen_queries),
            deduped_query_count=deduped,
            pool_candidates=pool_candidates,
            scoped_pool_candidates=scoped_pool_candidates,
        )

    @staticmethod
    def _report_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
        """Accept the existing candidate-first evidence without weakening it.

        `safeCandidates` has passed a source-scope precheck. That is evidence
        quality, not target-consumer authorization.
        """
        safe = report.get("safeCandidates")
        source = report.get("sourceCandidates")
        result: list[dict[str, Any]] = []
        if isinstance(safe, list):
            result.extend(
                {
                    **item,
                    "sourcePrecheck": {
                        "passed": True,
                        "scope": "source_pool",
                    },
                }
                for item in safe
                if isinstance(item, dict)
            )
        if isinstance(source, list):
            result.extend(
                {
                    **item,
                    "sourcePrecheck": {
                        "passed": True,
                        "scope": "amap_identity_and_provider_facts",
                    },
                }
                for item in source
                if isinstance(item, dict) and item.get("sourceEvidenceEligible") is True
            )
        if result:
            return result
        legacy = report.get("candidates")
        return [item for item in legacy if isinstance(item, dict)] if isinstance(legacy, list) else []

    @staticmethod
    def _normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
        amap_id = str(candidate.get("amapId") or candidate.get("id") or "").strip()
        if not amap_id:
            return None
        result = dict(candidate)
        result["amapId"] = amap_id
        result["providerType"] = str(result.get("providerType") or result.get("type") or "")
        legacy_semantic_passed = result.pop("semanticPassed", None)
        result.pop("consumerAdmission", None)
        result.pop("consumerAdmissionReport", None)
        result.pop("scoreEligible", None)
        source_note = result.pop("sourceNote", result.pop("source_note", None))
        if source_note:
            result["sourceDiscoveryNote"] = str(source_note)[:320]
        if "sourcePrecheck" not in result and legacy_semantic_passed is True:
            result["sourcePrecheck"] = {"passed": True, "scope": "source_pool_legacy"}
        return result

    @staticmethod
    def _grounded(candidate: dict[str, Any]) -> bool:
        return bool(
            (candidate.get("amapId") or candidate.get("id"))
            and candidate.get("longitude") is not None
            and candidate.get("latitude") is not None
            and candidate.get("providerType")
            and isinstance(candidate.get("sourcePrecheck"), dict)
            and candidate["sourcePrecheck"].get("passed") is True
        )

    def _query_key(self, report: dict[str, Any]) -> str:
        hints = report.get("candidateHints") if isinstance(report.get("candidateHints"), list) else []
        fields = [
            report.get("city"),
            report.get("intentType"),
            report.get("rawNeed"),
            ",".join(sorted(str(item) for item in hints)),
            report.get("searchMode"),
            report.get("anchorIdentity"),
        ]
        return "|".join(self._normalize(value) for value in fields)

    @staticmethod
    def _normalize(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or "")).lower()
