from __future__ import annotations

import json
import re
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from typing import Any, Iterable, Iterator, Optional

from src.models.route_option import normalize_route_mode


_CANONICAL_AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")
_SUPPORTED_ROUTE_MODES = {"walking", "bicycling", "transit", "driving", "taxi"}


@dataclass
class AmapCallBudget:
    place_text_max: int = 6
    place_around_max: int = 8
    route_refresh_max: int = 8
    total_external_max: int = 16
    source: str = "agent_run"
    used_place_text: int = 0
    used_place_detail: int = 0
    used_place_around: int = 0
    used_route: int = 0
    cache_hit_count: int = 0
    skipped_because_budget: int = 0
    rate_limited: bool = False
    cooldown_remaining_seconds: Optional[int] = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    query_ledger: set[tuple[str, str, str, str, str, str, str]] = field(default_factory=set)
    authorized_route_work: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    authorized_route_unscoped_work: set[tuple[str, str, str]] = field(default_factory=set)
    authorized_route_scope_leases: dict[
        tuple[str, str, str], dict[str, dict[str, Any]]
    ] = field(default_factory=dict)
    authorized_place_around_work: dict[tuple[str, str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    duplicate_external_query_count: int = 0
    reused_query_count: int = 0
    new_query_count: int = 0
    last_denial_reason: Optional[str] = None
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    @classmethod
    def for_creative_portfolio_candidates(cls) -> "AmapCallBudget":
        """Return the single authoritative Portfolio candidate envelope.

        Route preflight owns a separate budget.  Keeping route capacity at zero
        here prevents candidate discovery workers from consuming route calls
        through the wrong accounting scope.
        """

        return cls(
            place_text_max=12,
            place_around_max=8,
            route_refresh_max=0,
            total_external_max=24,
            source="creative_portfolio_candidate_grounding",
        )

    @classmethod
    def for_creative_portfolio_route_preflight(cls) -> "AmapCallBudget":
        """Return an empty brief-local ledger populated from materialized work.

        Stage B cannot know its exact Provider envelope until the proposal's
        canonical route anchors exist.  ``PortfolioRouteFeasibilityService``
        therefore authorizes unique pair/mode and nearby-query keys immediately
        before their bounded execution.  Starting at zero prevents a fixed
        allowance from becoming permission for requests that the snapshot did
        not actually derive.
        """

        return cls(
            place_text_max=0,
            place_around_max=0,
            route_refresh_max=0,
            total_external_max=0,
            source="creative_portfolio_route_preflight",
        )

    def authorize_route_work(
        self,
        requests: Iterable[dict[str, Any]],
    ) -> bool:
        """Add exact canonical route work to the Portfolio envelope once."""

        if self.source != "creative_portfolio_route_preflight":
            return True
        normalized: list[
            tuple[
                tuple[str, str, str],
                dict[str, Any],
                Optional[dict[str, Any]],
            ]
        ] = []
        for raw in requests:
            if not isinstance(raw, dict):
                self.last_denial_reason = "invalid_route_work"
                return False
            key, denial_reason = self._normalized_route_work_key(
                from_amap_id=str(raw.get("fromPhysicalId") or raw.get("fromAmapId") or ""),
                to_amap_id=str(raw.get("toPhysicalId") or raw.get("toAmapId") or ""),
                mode=str(raw.get("mode") or ""),
            )
            if key is None:
                self.last_denial_reason = denial_reason or "invalid_route_work"
                return False
            from_id, to_id, mode = key
            candidate_id = str(
                raw.get("candidatePhysicalId") or raw.get("candidateAmapId") or ""
            ).strip().upper()
            if candidate_id and not _CANONICAL_AMAP_ID_RE.fullmatch(candidate_id):
                self.last_denial_reason = "invalid_route_identity"
                return False
            raw_certificate = raw.get("repairScopeCertificate")
            if raw_certificate is None and any(
                field_name in raw
                for field_name in (
                    "scopeFingerprint",
                    "continuationMode",
                    "adjacentAnchorIds",
                    "adjacentRouteLedgerKeys",
                )
            ):
                raw_certificate = raw
            reason = str(raw.get("reason") or "route_work").strip()
            if raw_certificate is None and (
                reason == "replacement_adjacent" or bool(candidate_id)
            ):
                self.last_denial_reason = "route_scope_missing"
                return False
            certificate: Optional[dict[str, Any]] = None
            if raw_certificate is not None:
                certificate, scope_role, denial_reason = self._validated_repair_scope_certificate(
                    raw_certificate,
                    route_key=key,
                )
                if certificate is None or not scope_role:
                    self.last_denial_reason = denial_reason or "invalid_route_scope_certificate"
                    return False
                if candidate_id and candidate_id != certificate["candidatePhysicalId"]:
                    self.last_denial_reason = "route_scope_candidate_mismatch"
                    return False
            normalized.append(
                (
                    key,
                    {
                        "fromPhysicalId": from_id,
                        "toPhysicalId": to_id,
                        "mode": mode,
                        "reason": reason,
                        "condition": str(raw.get("condition") or "always").strip(),
                        **({"candidatePhysicalId": candidate_id} if candidate_id else {}),
                    },
                    certificate,
                )
            )
        with self._lock:
            for key, evidence, certificate in normalized:
                if key not in self.authorized_route_work:
                    self.authorized_route_work[key] = evidence
                if certificate is None:
                    self.authorized_route_unscoped_work.add(key)
                else:
                    fingerprint = str(certificate["scopeFingerprint"])
                    self.authorized_route_scope_leases.setdefault(key, {})[
                        fingerprint
                    ] = certificate
            self._recalculate_creative_portfolio_limits()
            self.last_denial_reason = None
            return True

    def authorize_place_around_work(
        self,
        requests: Iterator[dict[str, Any]] | list[dict[str, Any]],
    ) -> None:
        """Add exact nearby-query work to the Portfolio envelope once."""

        if self.source != "creative_portfolio_route_preflight":
            return
        with self._lock:
            for raw in requests:
                if not isinstance(raw, dict):
                    continue
                center = str(raw.get("center") or "").strip()
                keyword = str(raw.get("keyword") or "").strip()
                category = str(raw.get("category") or "").strip()
                radius = str(raw.get("radius") or "").strip()
                if not center or not keyword or not radius:
                    continue
                key = (center, keyword.casefold(), category.casefold(), radius)
                if key not in self.authorized_place_around_work:
                    self.authorized_place_around_work[key] = {
                        "center": center,
                        "keyword": keyword,
                        "category": category,
                        "radius": radius,
                        "reason": str(raw.get("reason") or "route_repair_nearby").strip(),
                    }
            self._recalculate_creative_portfolio_limits()

    def _recalculate_creative_portfolio_limits(self) -> None:
        self.place_text_max = 0
        self.place_around_max = len(self.authorized_place_around_work)
        self.route_refresh_max = len(self.authorized_route_unscoped_work) + sum(
            len(scopes) for scopes in self.authorized_route_scope_leases.values()
        )
        self.total_external_max = self.place_around_max + self.route_refresh_max

    def _creative_portfolio_derivation(self) -> dict[str, Any]:
        route_requests = sorted(
            (dict(item) for item in self.authorized_route_work.values()),
            key=lambda item: (
                str(item.get("fromPhysicalId") or ""),
                str(item.get("toPhysicalId") or ""),
                str(item.get("mode") or ""),
            ),
        )
        route_work_leases = [
            {
                "fromPhysicalId": key[0],
                "toPhysicalId": key[1],
                "mode": key[2],
                "leaseKind": "unscoped",
            }
            for key in sorted(self.authorized_route_unscoped_work)
        ]
        route_work_leases.extend(
            {
                "fromPhysicalId": key[0],
                "toPhysicalId": key[1],
                "mode": key[2],
                "leaseKind": "exact_repair_scope",
                "scopeFingerprint": scope_fingerprint,
                "repairScopeCertificate": deepcopy(certificate),
            }
            for key in sorted(self.authorized_route_scope_leases)
            for scope_fingerprint, certificate in sorted(
                self.authorized_route_scope_leases[key].items()
            )
        )
        around_requests = sorted(
            (dict(item) for item in self.authorized_place_around_work.values()),
            key=lambda item: (
                str(item.get("center") or ""),
                str(item.get("keyword") or ""),
                str(item.get("category") or ""),
                str(item.get("radius") or ""),
            ),
        )
        preferred_modes = [
            str(item.get("mode") or "")
            for item in route_requests
            if str(item.get("condition") or "") == "always"
            and str(item.get("mode") or "") != "walking"
        ]
        return {
            "schemaVersion": "creative-portfolio-route-budget-v1",
            "preferredMode": preferred_modes[0] if preferred_modes else "",
            "baselineAdjacentPairCount": sum(
                str(item.get("reason") or "") == "baseline_adjacent" for item in route_requests
            ),
            "insertionBypassPairCount": sum(
                str(item.get("reason") or "") == "insertion_bypass" for item in route_requests
            ),
            "replacementCandidateCount": len(
                {
                    str(item.get("candidatePhysicalId") or "")
                    for item in route_requests
                    if str(item.get("candidatePhysicalId") or "")
                }
            ),
            "replacementPreferredPairCount": sum(
                str(item.get("reason") or "") == "replacement_adjacent" for item in route_requests
            ),
            "conditionalWalkingPairCount": sum(
                str(item.get("condition") or "") == "preferred_mode_unavailable"
                for item in route_requests
            ),
            "themeWalkingPairCount": sum(
                str(item.get("reason") or "") == "theme_walking" for item in route_requests
            ),
            "nearbySearchMax": len(around_requests),
            "routeRequests": route_requests,
            "routeWorkLeaseCount": len(route_work_leases),
            "routeWorkLeases": route_work_leases,
            **({"placeAroundRequests": around_requests} if around_requests else {}),
        }

    def try_acquire(
        self,
        *,
        endpoint: str,
        keyword: str = "",
        category: str = "",
        center: str = "",
        radius: str = "",
        source: str = "",
        from_amap_id: str = "",
        to_amap_id: str = "",
        mode: str = "",
        query_scope_fingerprint: str = "",
        query_variant_fingerprint: str = "",
        repair_scope_certificate: Optional[dict[str, Any]] = None,
    ) -> bool:
        with self._lock:
            route_key: Optional[tuple[str, str, str]] = None
            route_match: Optional[
                tuple[tuple[str, str, str], Optional[dict[str, Any]], str]
            ] = None
            if self.source == "creative_portfolio_route_preflight" and self._endpoint_key(endpoint) == "route":
                route_match = self._validate_route_work_locked(
                    from_amap_id=from_amap_id,
                    to_amap_id=to_amap_id,
                    mode=mode or category,
                    endpoint=endpoint,
                    source=source,
                    record_denial=True,
                    repair_scope_certificate=repair_scope_certificate,
                )
                if route_match is None:
                    return False
                route_key = route_match[0]
            query_key = (
                endpoint,
                (
                    f"{route_key[0]}->{route_key[1]}"
                    if route_key is not None
                    else keyword.strip().casefold()
                ),
                route_key[2] if route_key is not None else category.strip().casefold(),
                center.strip(),
                radius.strip(),
                str(
                    ((route_match or (None, None, ""))[1] or {}).get(
                        "scopeFingerprint"
                    )
                    or query_scope_fingerprint
                    or ""
                ),
                str(query_variant_fingerprint or ""),
            )
            if query_key in self.query_ledger:
                self.duplicate_external_query_count += 1
                self.reused_query_count += 1
                self.last_denial_reason = "duplicate_query_suppressed"
                self.skipped.append(
                    {
                        "endpoint": endpoint,
                        "keyword": keyword,
                        "category": category,
                        "center": center,
                        "radius": radius,
                        "queryVariantFingerprint": str(query_variant_fingerprint or "") or None,
                        "source": source or self.source,
                        "reason": self.last_denial_reason,
                        **(
                            self._route_work_evidence(route_match)
                            if route_match is not None
                            else {}
                        ),
                    }
                )
                return False
            endpoint_key = self._endpoint_key(endpoint)
            if not self._has_capacity(endpoint_key):
                self.skipped_because_budget += 1
                self.last_denial_reason = "budget_exceeded"
                self.skipped.append(
                    {
                        "endpoint": endpoint,
                        "keyword": keyword,
                        "category": category,
                        "source": source or self.source,
                        "reason": "budget_exceeded",
                        **(
                            self._route_work_evidence(route_match)
                            if route_match is not None
                            else {}
                        ),
                    }
                )
                return False
            self.query_ledger.add(query_key)
            self.new_query_count += 1
            self.last_denial_reason = None
            if endpoint_key == "place_text":
                self.used_place_text += 1
            elif endpoint_key == "place_detail":
                self.used_place_detail += 1
            elif endpoint_key == "place_around":
                self.used_place_around += 1
            elif endpoint_key == "route":
                self.used_route += 1
            self.calls.append(
                {
                    "endpoint": endpoint,
                    "keyword": keyword,
                    "category": category,
                    "source": source or self.source,
                    "queryVariantFingerprint": str(query_variant_fingerprint or "") or None,
                    "cacheHit": False,
                    "skipped": False,
                    **(
                        self._route_work_evidence(route_match)
                        if route_match is not None
                        else {}
                    ),
                }
            )
            return True

    def validate_route_work(
        self,
        *,
        from_amap_id: str,
        to_amap_id: str,
        mode: str,
        endpoint: str = "route",
        source: str = "",
        repair_scope_certificate: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Validate one exact Portfolio route lease without consuming it."""

        if self.source != "creative_portfolio_route_preflight":
            return True
        with self._lock:
            return (
                self._validate_route_work_locked(
                    from_amap_id=from_amap_id,
                    to_amap_id=to_amap_id,
                    mode=mode,
                    endpoint=endpoint,
                    source=source,
                    record_denial=True,
                    repair_scope_certificate=repair_scope_certificate,
                )
                is not None
            )

    def record_cache_hit(
        self,
        *,
        endpoint: str,
        keyword: str = "",
        category: str = "",
        center: str = "",
        radius: str = "",
        source: str = "",
        from_amap_id: str = "",
        to_amap_id: str = "",
        mode: str = "",
        repair_scope_certificate: Optional[dict[str, Any]] = None,
    ) -> bool:
        with self._lock:
            route_key: Optional[tuple[str, str, str]] = None
            route_match: Optional[
                tuple[tuple[str, str, str], Optional[dict[str, Any]], str]
            ] = None
            if self.source == "creative_portfolio_route_preflight" and self._endpoint_key(endpoint) == "route":
                route_match = self._validate_route_work_locked(
                    from_amap_id=from_amap_id,
                    to_amap_id=to_amap_id,
                    mode=mode or category,
                    endpoint=endpoint,
                    source=source,
                    record_denial=True,
                    repair_scope_certificate=repair_scope_certificate,
                )
                if route_match is None:
                    return False
                route_key = route_match[0]
            self.cache_hit_count += 1
            self.reused_query_count += 1
            self.calls.append(
                {
                    "endpoint": endpoint,
                    "keyword": keyword,
                    "category": category,
                    "center": center,
                    "radius": radius,
                    "source": source or self.source,
                    "cacheHit": True,
                    "skipped": False,
                    **(
                        self._route_work_evidence(route_match)
                        if route_match is not None
                        else {}
                    ),
                }
            )
            self.last_denial_reason = None
            return True

    @staticmethod
    def _normalized_route_work_key(
        *,
        from_amap_id: str,
        to_amap_id: str,
        mode: str,
    ) -> tuple[Optional[tuple[str, str, str]], Optional[str]]:
        from_id = str(from_amap_id or "").strip().upper()
        to_id = str(to_amap_id or "").strip().upper()
        if not _CANONICAL_AMAP_ID_RE.fullmatch(from_id) or not _CANONICAL_AMAP_ID_RE.fullmatch(to_id):
            return None, "invalid_route_identity"
        if from_id == to_id:
            return None, "same_route_endpoint"
        normalized_mode = normalize_route_mode(str(mode or "").strip().casefold())
        if normalized_mode not in _SUPPORTED_ROUTE_MODES:
            return None, "invalid_route_mode"
        return (from_id, to_id, normalized_mode), None

    @staticmethod
    def _route_work_evidence(
        route_match: tuple[
            tuple[str, str, str], Optional[dict[str, Any]], str
        ],
    ) -> dict[str, Any]:
        route_key, certificate, scope_role = route_match
        evidence: dict[str, Any] = {
            "fromAmapId": route_key[0],
            "toAmapId": route_key[1],
            "mode": route_key[2],
            "authorizationMatched": True,
        }
        if certificate is not None:
            evidence.update(
                {
                    "planningRoot": str(
                        certificate.get("planningSelectionRootTurnId")
                        or certificate["rootPortfolioId"]
                    ),
                    "rootPortfolioId": certificate["rootPortfolioId"],
                    "briefId": certificate["briefId"],
                    "dayNumber": certificate["dayNumber"],
                    "planningSlotId": certificate["planningSlotId"],
                    "candidatePhysicalId": certificate["candidatePhysicalId"],
                    "adjacentAnchorIds": list(certificate["adjacentAnchorIds"]),
                    "adjacentRouteLedgerKeys": deepcopy(
                        certificate["adjacentRouteLedgerKeys"]
                    ),
                    "routeContractFingerprint": certificate[
                        "routeContractFingerprint"
                    ],
                    "repairScopeFingerprint": certificate["scopeFingerprint"],
                    "routeScopeRole": scope_role,
                    "repairScopeCertificate": deepcopy(certificate),
                }
            )
        return evidence

    @classmethod
    def _validated_repair_scope_certificate(
        cls,
        raw_certificate: Any,
        *,
        route_key: tuple[str, str, str],
    ) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
        if not isinstance(raw_certificate, dict):
            return None, None, "invalid_route_scope_certificate"
        material = {
            "continuationMode": raw_certificate.get("continuationMode"),
            "rootPortfolioId": raw_certificate.get("rootPortfolioId"),
            "briefId": raw_certificate.get("briefId"),
            "poolId": raw_certificate.get("poolId"),
            "dayNumber": raw_certificate.get("dayNumber"),
            "planningSlotId": raw_certificate.get("planningSlotId"),
            "candidatePhysicalId": raw_certificate.get("candidatePhysicalId"),
            "adjacentAnchorIds": raw_certificate.get("adjacentAnchorIds"),
            "adjacentRouteLedgerKeys": raw_certificate.get("adjacentRouteLedgerKeys"),
            "routeContractFingerprint": raw_certificate.get("routeContractFingerprint"),
        }
        if "planningSelectionRootTurnId" in raw_certificate:
            material["planningSelectionRootTurnId"] = raw_certificate.get(
                "planningSelectionRootTurnId"
            )
        fingerprint = str(raw_certificate.get("scopeFingerprint") or "").strip()
        expected_fingerprint = sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if not fingerprint or fingerprint != expected_fingerprint:
            return None, None, "route_scope_fingerprint_mismatch"
        if material["continuationMode"] != "repair_exact_slot":
            return None, None, "invalid_route_scope_certificate"
        text_fields = (
            "rootPortfolioId",
            "briefId",
            "planningSlotId",
            "routeContractFingerprint",
        )
        if any(
            not isinstance(material[field_name], str)
            or not material[field_name]
            or material[field_name] != material[field_name].strip()
            for field_name in text_fields
        ):
            return None, None, "invalid_route_scope_certificate"
        if not re.fullmatch(r"[0-9a-f]{64}", str(material["routeContractFingerprint"])):
            return None, None, "invalid_route_scope_certificate"
        planning_root = material.get("planningSelectionRootTurnId")
        if (
            not isinstance(planning_root, str)
            or not planning_root
            or planning_root != planning_root.strip()
        ):
            return None, None, "invalid_route_scope_certificate"
        day_number = material["dayNumber"]
        if isinstance(day_number, bool) or not isinstance(day_number, int) or day_number <= 0:
            return None, None, "invalid_route_scope_certificate"
        candidate_id = str(material["candidatePhysicalId"] or "")
        if (
            candidate_id != candidate_id.strip().upper()
            or not _CANONICAL_AMAP_ID_RE.fullmatch(candidate_id)
        ):
            return None, None, "invalid_route_scope_certificate"
        adjacent_anchor_ids = material["adjacentAnchorIds"]
        if (
            not isinstance(adjacent_anchor_ids, list)
            or not adjacent_anchor_ids
            or any(
                not isinstance(item, str) or not item or item != item.strip()
                for item in adjacent_anchor_ids
            )
            or len(set(adjacent_anchor_ids)) != len(adjacent_anchor_ids)
        ):
            return None, None, "invalid_route_scope_certificate"
        raw_ledger_keys = material["adjacentRouteLedgerKeys"]
        if (
            not isinstance(raw_ledger_keys, list)
            or not raw_ledger_keys
            or len(raw_ledger_keys) != len(adjacent_anchor_ids)
            or len(raw_ledger_keys) > 2
        ):
            return None, None, "invalid_route_scope_certificate"
        ledger_keys: list[tuple[str, str, str]] = []
        for raw_key in raw_ledger_keys:
            if not isinstance(raw_key, dict):
                return None, None, "invalid_route_scope_certificate"
            ledger_key, denial_reason = cls._normalized_route_work_key(
                from_amap_id=str(raw_key.get("fromPhysicalId") or ""),
                to_amap_id=str(raw_key.get("toPhysicalId") or ""),
                mode=str(raw_key.get("mode") or ""),
            )
            if ledger_key is None:
                return None, None, denial_reason or "invalid_route_scope_certificate"
            if (
                raw_key.get("fromPhysicalId") != ledger_key[0]
                or raw_key.get("toPhysicalId") != ledger_key[1]
                or raw_key.get("mode") != ledger_key[2]
            ):
                return None, None, "invalid_route_scope_certificate"
            ledger_keys.append(ledger_key)
        if len(set(ledger_keys)) != len(ledger_keys):
            return None, None, "invalid_route_scope_certificate"
        if len(ledger_keys) == 1:
            if candidate_id not in ledger_keys[0][:2]:
                return None, None, "invalid_route_scope_certificate"
        elif not (
            ledger_keys[0][1] == candidate_id
            and ledger_keys[1][0] == candidate_id
            and ledger_keys[0][2] == ledger_keys[1][2]
        ):
            return None, None, "invalid_route_scope_certificate"
        scope_role: Optional[str] = None
        if route_key in ledger_keys:
            scope_role = "adjacent"
        elif len(ledger_keys) == 2 and route_key == (
            ledger_keys[0][0],
            ledger_keys[1][1],
            ledger_keys[0][2],
        ):
            scope_role = "bypass"
        if scope_role is None:
            return None, None, "route_scope_pair_mismatch"
        certificate = deepcopy(material)
        certificate["scopeFingerprint"] = fingerprint
        return certificate, scope_role, None

    def _validate_route_work_locked(
        self,
        *,
        from_amap_id: str,
        to_amap_id: str,
        mode: str,
        endpoint: str,
        source: str,
        record_denial: bool,
        repair_scope_certificate: Optional[dict[str, Any]],
    ) -> Optional[
        tuple[tuple[str, str, str], Optional[dict[str, Any]], str]
    ]:
        key, denial_reason = self._normalized_route_work_key(
            from_amap_id=from_amap_id,
            to_amap_id=to_amap_id,
            mode=mode,
        )
        if key is not None and endpoint not in {"route", f"route/{key[2]}"}:
            key = None
            denial_reason = "invalid_route_mode"
        if key is not None and key not in self.authorized_route_work:
            key = None
            denial_reason = "route_work_unauthorized"
        matched_certificate: Optional[dict[str, Any]] = None
        scope_role = ""
        if key is not None:
            scoped_leases = self.authorized_route_scope_leases.get(key, {})
            has_unscoped_lease = key in self.authorized_route_unscoped_work
            if repair_scope_certificate is not None:
                candidate_certificate, candidate_role, scope_denial = (
                    self._validated_repair_scope_certificate(
                        repair_scope_certificate,
                        route_key=key,
                    )
                )
                candidate_fingerprint = str(
                    (candidate_certificate or {}).get("scopeFingerprint") or ""
                )
                stored_certificate = scoped_leases.get(candidate_fingerprint)
                if candidate_certificate is None or stored_certificate is None:
                    key = None
                    denial_reason = scope_denial or "route_scope_unauthorized"
                elif stored_certificate != candidate_certificate:
                    key = None
                    denial_reason = "route_scope_certificate_mismatch"
                else:
                    matched_certificate = stored_certificate
                    scope_role = str(candidate_role or "")
            elif not scoped_leases and has_unscoped_lease:
                scope_role = "unscoped"
            else:
                key = None
                denial_reason = (
                    "route_scope_missing" if scoped_leases else "route_work_unauthorized"
                )
        if key is not None:
            self.last_denial_reason = None
            return key, matched_certificate, scope_role
        self.last_denial_reason = denial_reason or "route_work_unauthorized"
        if record_denial:
            self.skipped.append(
                {
                    "endpoint": endpoint,
                    "source": source or self.source,
                    "fromAmapId": str(from_amap_id or "").strip().upper(),
                    "toAmapId": str(to_amap_id or "").strip().upper(),
                    "mode": normalize_route_mode(str(mode or "").strip().casefold()),
                    "reason": self.last_denial_reason,
                    "authorizationMatched": False,
                }
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            used_total = self.used_total_external
            retain_full_route_ledger = self.source == "creative_portfolio_route_preflight"
            visible_calls = self.calls if retain_full_route_ledger else self.calls[-20:]
            visible_skipped = self.skipped if retain_full_route_ledger else self.skipped[-20:]
            snapshot = {
                "budget": {
                    "amapPoiTextSearchMax": self.place_text_max,
                    "amapPoiTextAndDetailMax": self.place_text_max,
                    "amapPoiAroundSearchMax": self.place_around_max,
                    "amapRouteRefreshMax": self.route_refresh_max,
                    "amapTotalExternalCallsMax": self.total_external_max,
                },
                "used": {
                    "usedPlaceText": self.used_place_text,
                    "usedPlaceDetail": self.used_place_detail,
                    "usedPlaceAround": self.used_place_around,
                    "usedRoute": self.used_route,
                    "usedTotalExternal": used_total,
                },
                "usedPlaceText": self.used_place_text,
                "usedPlaceDetail": self.used_place_detail,
                "usedPlaceAround": self.used_place_around,
                "usedRoute": self.used_route,
                "usedTotalExternal": used_total,
                "cacheHitCount": self.cache_hit_count,
                "duplicateExternalQueryCount": self.duplicate_external_query_count,
                "reusedQueryCount": self.reused_query_count,
                "newQueryCount": self.new_query_count,
                "skippedBecauseBudget": self.skipped_because_budget,
                "rateLimited": self.rate_limited,
                "cooldownRemainingSeconds": self.cooldown_remaining_seconds,
                "calls": deepcopy(visible_calls),
                "skipped": deepcopy(visible_skipped),
                "source": self.source,
            }
            if self.source == "creative_portfolio_route_preflight":
                snapshot["derivation"] = self._creative_portfolio_derivation()
            return snapshot

    @property
    def used_total_external(self) -> int:
        return (
            self.used_place_text
            + self.used_place_detail
            + self.used_place_around
            + self.used_route
        )

    def _has_capacity(self, endpoint_key: str) -> bool:
        if self.used_total_external >= self.total_external_max:
            return False
        if endpoint_key == "place_text":
            return self.used_place_text + self.used_place_detail < self.place_text_max
        if endpoint_key == "place_detail":
            return self.used_place_text + self.used_place_detail < self.place_text_max
        if endpoint_key == "place_around":
            return self.used_place_around < self.place_around_max
        if endpoint_key == "route":
            return self.used_route < self.route_refresh_max
        return self.used_total_external < self.total_external_max

    def _endpoint_key(self, endpoint: str) -> str:
        if endpoint == "place/text":
            return "place_text"
        if endpoint == "place/detail":
            return "place_detail"
        if endpoint in {"config/district", "assistant/coordinate/convert"}:
            return "place_detail"
        if endpoint == "place/around":
            return "place_around"
        if endpoint.startswith("route/") or endpoint == "route":
            return "route"
        return "other"


_CURRENT_AMAP_CALL_BUDGET: ContextVar[Optional[AmapCallBudget]] = ContextVar(
    "trip_current_amap_call_budget",
    default=None,
)
_CURRENT_AMAP_ROUTE_REPAIR_SCOPE: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "trip_current_amap_route_repair_scope",
    default=None,
)


def current_amap_call_budget() -> Optional[AmapCallBudget]:
    return _CURRENT_AMAP_CALL_BUDGET.get()


def current_amap_route_repair_scope() -> Optional[dict[str, Any]]:
    certificate = _CURRENT_AMAP_ROUTE_REPAIR_SCOPE.get()
    return deepcopy(certificate) if certificate is not None else None


@contextmanager
def amap_route_repair_scope(
    certificate: Optional[dict[str, Any]],
) -> Iterator[Optional[dict[str, Any]]]:
    isolated = deepcopy(certificate) if certificate is not None else None
    token = _CURRENT_AMAP_ROUTE_REPAIR_SCOPE.set(isolated)
    try:
        yield deepcopy(isolated) if isolated is not None else None
    finally:
        _CURRENT_AMAP_ROUTE_REPAIR_SCOPE.reset(token)


@contextmanager
def amap_call_budget_scope(budget: Optional[AmapCallBudget]) -> Iterator[Optional[AmapCallBudget]]:
    token = _CURRENT_AMAP_CALL_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_AMAP_CALL_BUDGET.reset(token)
