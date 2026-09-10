import math
import re
from datetime import datetime, timezone
from typing import Any, Optional


NIGHT_VIEW_WEAK_RE = re.compile(
    r"(图书馆|文化中心|文化馆|文创店|售票|票务|门票|入口|出入口|停车场|地铁站|公交站|服务中心|游客中心|"
    r"办公室|办公|公司|酒店|宾馆|公寓|住宅|小区|打卡|航拍|剪影|雕塑|中塔公园|商店|专卖店|水站|卫生间|拍照|摄影|"
    r"管理中心|管理处|休闲健身广场|小区广场|内部道路|道路|路口|闸机|检票|售票处|"
    r"主题公园|游乐园|欢乐谷|amusement\s*park)",
    re.IGNORECASE,
)
NIGHT_VIEW_DINING_RE = re.compile(
    r"(餐厅|餐馆|饭店|酒楼|餐饮|小吃|火锅|烤鱼|烧烤|麻辣烫|咖啡|茶馆|美食|food|restaurant|cafe)", re.IGNORECASE
)
NIGHT_VIEW_EXPLICITLY_UNAVAILABLE_RE = re.compile(
    r"(暂停开放|暂停营业|临时关闭|停止开放|停止入场|已关闭|闭馆|歇业|不开放|不可入场|暂停接待|施工关闭|"
    r"\bclosed\b|\bsuspended\b|\bunavailable\b|\bnot[_ -]?(?:available|open)\b)",
    re.IGNORECASE,
)
NIGHT_VIEW_CONTROLLED_ACCESS_RE = re.compile(
    r"(观景台|观景平台|电视塔|摩天大楼|屋顶|天台|塔顶|tower|observation\s*deck|rooftop)",
    re.IGNORECASE,
)
NIGHT_VIEW_VERIFIED_AVAILABLE_RE = re.compile(
    r"(verified[_ -]?open|open[_ -]?at[_ -]?requested[_ -]?time|available|正常开放|开放中|可入场|"
    r"(?:[01]?\d|2[0-3]):[0-5]\d\s*[-~—至]\s*(?:[01]?\d|2[0-3]):[0-5]\d)",
    re.IGNORECASE,
)
NIGHT_VIEW_PUBLIC_OUTDOOR_RE = re.compile(
    r"(公共空间|公共观景|城市阳台|城市公园|公园广场|滨水|水岸|江边|河岸|步道|步行街|街区|广场|桥|"
    r"waterfront|promenade|public\s*(?:space|park|plaza)|park|riverside)",
    re.IGNORECASE,
)
ACCESS_EVIDENCE_RE = re.compile(
    r"(开放|营业|入场|准入|预约|门票|时段|open|opening|hours|access|admission|entry|availability)",
    re.IGNORECASE,
)
SUPPORTED_ACCESS_POLICIES = {
    "public_outdoor",
    "verified_controlled_access",
    "public_outdoor_or_verified_controlled_access",
}
ACCESS_TIMESTAMP_FIELDS = (
    "providerEvidenceQueriedAt",
    "provider_evidence_queried_at",
    "accessEvidenceObservedAt",
    "access_evidence_observed_at",
    "evidenceObservedAt",
    "evidence_observed_at",
    "observedAt",
    "observed_at",
    "queriedAt",
    "queried_at",
    "fetchedAt",
    "fetched_at",
    "publishedAt",
    "published_at",
    "freshness",
)
NIGHT_VIEW_SIGNAL_RE = re.compile(
    r"(夜景|夜游|观景|观景台|观景平台|地标建筑|地标建筑群|天际线|亮化|灯光|塔|电视塔|CBD|"
    r"商圈|滨水夜景|滨水|水岸|江边|河岸|夜游步道|城市阳台|桥)"
)
PUBLIC_CITY_VIEW_EVIDENCE_RE = re.compile(
    r"(城市阳台|天际线|CBD|地标建筑|观景台|观景平台|跨江桥|跨河桥|城市夜景)",
    re.IGNORECASE,
)
WATERFRONT_EXPERIENCE_FAMILIES = frozenset(
    {"waterfront", "waterfront_evening", "waterfront_night_walk", "riverside", "riverfront"}
)
PUBLIC_OUTDOOR_EVENING_FAMILIES = WATERFRONT_EXPERIENCE_FAMILIES | {"park_relax"}
GENERIC_NIGHT_VIEW_RE = re.compile(
    r"^(夜景观景点|夜景点|观景点|观景台|观景平台|城市观景点|地标观景点|城市夜景|夜景地点|"
    r"夜景地标|地标景点|地标|夜景|观景|晚上看夜景)$"
)
RANGE_NIGHT_VIEW_RE = re.compile(r"(附近范围|附近区域|附近一带|一带|周边|范围定位|周边范围)")
NIGHT_VIEW_ACTIVITY_SUFFIX_RE = re.compile(
    r"(夜游步道|观景平台|观景台|观景点|灯光秀|夜景|夜游|观景|亮化|灯光|夜间|晚上)$"
)
MATCHED_HINT_NOTE_RE = re.compile(r"matchedCandidateHint[：:]([^；;]+)")
MATCHED_HINT_BINDING_NOTE_RE = re.compile(r"matchedCandidateHintBinding[：:](search_query|discovery_query)")
GENERIC_NIGHT_VIEW_HINT_CORES = {
    "地标",
    "景点",
    "夜景",
    "夜游",
    "观景",
    "观景点",
    "公园",
    "广场",
    "商圈",
}
HINT_PROVENANCE_PUBLIC_TYPE_RE = re.compile(
    r"(scenic|风景名胜|旅游景点|观景台|观景平台|地标景观|水域景观|城市公园|公园广场|步行街|滨水步道)"
)


class NightViewCandidatePolicy:
    def evaluate(
        self,
        candidate: Any,
        *,
        amap_identity: Optional[object] = None,
        structured_hint: Optional[str] = None,
        experience_family: Optional[object] = None,
        enforce_legacy_availability: bool = True,
    ) -> dict[str, Any]:
        """Return structured, attributable night-view evidence.

        Search queries and source notes are provenance only. They cannot turn
        an unrelated AMap entity into a night-view anchor.
        """

        intrinsic_text = self._candidate_text(candidate)
        resolved_hint, hint_from_note = self._structured_hint(
            candidate,
            structured_hint=structured_hint,
        )
        hint_is_authoritative = self._hint_is_authoritative(
            candidate,
            explicit_hint=structured_hint,
            hint_from_note=hint_from_note,
        )
        hint_core = self._hint_core(resolved_hint, candidate)
        candidate_cores = self._candidate_entity_cores(candidate)
        candidate_core = candidate_cores[0] if candidate_cores else ""
        hint_entity_matched = bool(
            hint_core and any(hint_core in core or core in hint_core for core in candidate_cores if len(core) >= 3)
        )
        public_type_passed = bool(
            HINT_PROVENANCE_PUBLIC_TYPE_RE.search(
                " ".join(str(self._attribute(candidate, field_name) or "") for field_name in ("type", "category"))
            )
        )
        intrinsic_signals = list(dict.fromkeys(NIGHT_VIEW_SIGNAL_RE.findall(intrinsic_text)))
        normalized_family = str(experience_family or "").strip().casefold()
        reject_reason = self._identity_reject_reason(
            candidate,
            amap_identity=amap_identity,
        )
        if not reject_reason:
            reject_reason = self.placeholder_reject_reason(
                self._attribute(candidate, "name"),
                intrinsic_text,
            )
        if not reject_reason and enforce_legacy_availability:
            reject_reason = self._availability_reject_reason(candidate, intrinsic_text)
        if not reject_reason and NIGHT_VIEW_DINING_RE.search(intrinsic_text):
            reject_reason = "night_view_dining_or_non_view"
        if not reject_reason and NIGHT_VIEW_WEAK_RE.search(intrinsic_text):
            reject_reason = "weak_night_view_entity"
        if not reject_reason:
            reject_reason = self._subpoint_reason(candidate)
        concrete_hint = bool(
            hint_is_authoritative
            and resolved_hint
            and len(hint_core) >= 3
            and hint_core not in GENERIC_NIGHT_VIEW_HINT_CORES
        )
        if not reject_reason and concrete_hint and not hint_entity_matched:
            reject_reason = "night_view_hint_entity_mismatch"
        if not reject_reason and not intrinsic_signals:
            if not (
                hint_entity_matched
                and public_type_passed
                or normalized_family in PUBLIC_OUTDOOR_EVENING_FAMILIES
                and public_type_passed
            ):
                reject_reason = "night_view_signal_missing"
        if not reject_reason and not public_type_passed:
            reject_reason = "night_view_public_access_type_incompatible"
        if (
            not reject_reason
            and normalized_family == "public_city_view"
            and normalized_family not in WATERFRONT_EXPERIENCE_FAMILIES
            and not PUBLIC_CITY_VIEW_EVIDENCE_RE.search(intrinsic_text)
        ):
            # Water, wetland and ordinary park metadata can establish public
            # access but not the requested city-view experience.  A distinct
            # waterfront family is the explicit exception, never a query-text
            # inference.
            reject_reason = "public_city_view_evidence_missing"
        return {
            "decision": "accepted" if not reject_reason else "rejected",
            "rejectReason": reject_reason or None,
            "intrinsicSignals": intrinsic_signals,
            "structuredHint": resolved_hint or None,
            "structuredHintBinding": (
                "entity_constraint"
                if resolved_hint and hint_is_authoritative
                else "search_query"
                if resolved_hint
                else None
            ),
            "structuredHintCore": hint_core or None,
            "candidateEntityCore": candidate_core or None,
            "hintEntityMatched": hint_entity_matched,
            "publicAccessTypePassed": public_type_passed,
            "experienceFamily": normalized_family or None,
            "nightAvailabilityStatus": str(
                self._attribute(
                    candidate,
                    "night_availability_status",
                    "nightAvailabilityStatus",
                )
                or "unknown"
            ),
        }

    def evaluate_access_policy(
        self,
        candidate: Any,
        *,
        access_policy: object,
        evidence_freshness: Any,
        now: Optional[datetime] = None,
        spec_fingerprint: object = "",
        contract_error: object = "",
    ) -> dict[str, Any]:
        """Apply the ExperienceSpec access contract to one provider entity.

        Opening text without an attributable observation time is not current
        evidence. Controlled entrances always require a fresh proof; public
        outdoor places may omit it only when the policy explicitly allows the
        no-closure case.
        """

        policy = str(access_policy or "").strip()
        fingerprint = str(spec_fingerprint or "").strip()
        evaluated_at = self._utc_datetime(now) or datetime.now(timezone.utc)
        evidence = self.access_evidence_projection(candidate)
        access_class = self.access_class(candidate)
        result: dict[str, Any] = {
            "schemaVersion": "experience-access-policy-v1",
            "decision": "rejected",
            "reasonCode": "",
            "accessPolicy": policy,
            "accessClass": access_class,
            "evidenceFreshness": (
                dict(evidence_freshness) if isinstance(evidence_freshness, dict) else evidence_freshness
            ),
            "specFingerprint": fingerprint,
            "evaluatedAt": evaluated_at.isoformat(),
            "evidenceRequired": True,
            "evidenceTimestamp": None,
            "evidenceAgeHours": None,
            "policyExemption": None,
            "contractError": str(contract_error or "") or None,
        }

        def finish(decision: str, reason: str = "") -> dict[str, Any]:
            result["decision"] = decision
            result["reasonCode"] = reason
            return result

        if contract_error:
            return finish("rejected", str(contract_error))
        if policy not in SUPPORTED_ACCESS_POLICIES:
            return finish("rejected", "experience_access_policy_unknown")
        if not fingerprint:
            return finish("rejected", "experience_spec_fingerprint_missing")
        freshness = self._normalized_evidence_freshness(evidence_freshness)
        if freshness is None:
            return finish("rejected", "experience_evidence_freshness_invalid")
        result["evidenceFreshness"] = freshness
        if access_class == "unknown":
            return finish("rejected", "experience_access_classification_unknown")
        if (policy == "public_outdoor" and access_class != "public_outdoor") or (
            policy == "verified_controlled_access" and access_class != "controlled_access"
        ):
            return finish("rejected", "experience_access_policy_mismatch")

        closure_text = self._access_evidence_text(evidence)
        if NIGHT_VIEW_EXPLICITLY_UNAVAILABLE_RE.search(closure_text):
            return finish("rejected", "experience_access_explicitly_closed")

        evidence_required = access_class == "controlled_access" or bool(freshness.get("requiredForPublicOutdoor"))
        if access_class == "public_outdoor" and not bool(freshness.get("allowExplicitNoClosure")):
            evidence_required = True
        result["evidenceRequired"] = evidence_required
        if not evidence_required:
            result["policyExemption"] = "public_outdoor_no_explicit_closure"
            return finish("accepted")

        has_access_signal, timestamps = self._supporting_access_evidence(evidence)
        if not has_access_signal:
            return finish("pending_evidence", "experience_access_evidence_missing")
        if not timestamps:
            return finish(
                "pending_evidence",
                (
                    "experience_access_evidence_timestamp_invalid"
                    if self._has_access_timestamp_value(evidence)
                    else "experience_access_evidence_timestamp_missing"
                ),
            )
        current_timestamps = [item for item in timestamps if (evaluated_at - item).total_seconds() >= -60]
        if not current_timestamps:
            return finish(
                "pending_evidence",
                "experience_access_evidence_timestamp_invalid",
            )
        newest = max(current_timestamps)
        age_hours = max(
            0.0,
            (evaluated_at - newest).total_seconds() / 3600,
        )
        result["evidenceTimestamp"] = newest.isoformat()
        result["evidenceAgeHours"] = round(age_hours, 6)
        if age_hours > float(freshness["maxAgeHours"]):
            return finish("pending_evidence", "experience_access_evidence_stale")
        return finish("accepted")

    @classmethod
    def access_class(cls, candidate: Any) -> str:
        text = cls._candidate_text_static(candidate)
        if NIGHT_VIEW_CONTROLLED_ACCESS_RE.search(text):
            return "controlled_access"
        if NIGHT_VIEW_PUBLIC_OUTDOOR_RE.search(text):
            return "public_outdoor"
        return "unknown"

    @classmethod
    def access_evidence_projection(cls, candidate: Any) -> dict[str, Any]:
        claims = cls._attribute(candidate, "sourceClaims", "source_claims")
        safe_claims: list[dict[str, Any]] = []
        for raw in claims[:8] if isinstance(claims, list) else []:
            if not isinstance(raw, dict):
                continue
            safe: dict[str, Any] = {}
            for key in (
                "claimKey",
                "claimType",
                "stance",
                "summary",
                "supportedSignals",
                *ACCESS_TIMESTAMP_FIELDS,
            ):
                if key not in raw:
                    continue
                value = raw[key]
                if isinstance(value, str):
                    safe[key] = value.strip()[:500]
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    safe[key] = value
                elif isinstance(value, list):
                    safe[key] = [str(item).strip()[:120] for item in value[:8]]
            if safe:
                safe_claims.append(safe)
        return {
            "accessClass": cls.access_class(candidate),
            "nightAvailabilityStatus": str(
                cls._attribute(
                    candidate,
                    "nightAvailabilityStatus",
                    "night_availability_status",
                )
                or ""
            ).strip()[:160],
            "openTimeToday": str(cls._attribute(candidate, "openTimeToday", "open_time_today") or "").strip()[:500],
            "openTimeWeek": str(cls._attribute(candidate, "openTimeWeek", "open_time_week") or "").strip()[:500],
            "timestamps": {
                field: str(cls._attribute(candidate, field) or "").strip()[:80]
                for field in ACCESS_TIMESTAMP_FIELDS
                if cls._attribute(candidate, field) not in (None, "")
            },
            "sourceClaims": safe_claims,
        }

    def reject_reason(
        self,
        candidate: Any,
        *,
        amap_identity: Optional[object] = None,
        structured_hint: Optional[str] = None,
    ) -> str:
        return str(
            self.evaluate(
                candidate,
                amap_identity=amap_identity,
                structured_hint=structured_hint,
            ).get("rejectReason")
            or ""
        )

    def canonical_parent_name(self, candidate: Any) -> str:
        name = str(self._attribute(candidate, "name") or "")
        parent = re.split(r"[-－—]", name, maxsplit=1)[0].strip()
        return parent or name

    def placeholder_reject_reason(self, name: object, text: object = "") -> str:
        compact_name = "".join(str(name or "").strip().split())
        combined = " ".join(part for part in (compact_name, str(text or "")) if part)
        if GENERIC_NIGHT_VIEW_RE.match(compact_name):
            return "generic_night_view_placeholder"
        if RANGE_NIGHT_VIEW_RE.search(combined):
            return "range_night_view_anchor"
        return ""

    def _candidate_text(self, candidate: Any) -> str:
        return self._candidate_text_static(candidate)

    @classmethod
    def _candidate_text_static(cls, candidate: Any) -> str:
        parts = [str(cls._attribute(candidate, field_name) or "") for field_name in ("name", "type", "category")]
        for field_name in ("aliases", "verified_parent_name", "_trip_verified_parent"):
            value = cls._attribute(candidate, field_name)
            if isinstance(value, (list, tuple, set)):
                parts.extend(str(item or "") for item in value)
            else:
                parts.append(str(value or ""))
        return " ".join(parts)

    @staticmethod
    def _normalized_evidence_freshness(value: Any) -> Optional[dict[str, Any]]:
        if not isinstance(value, dict) or not value:
            return None
        allowed = {
            "maxAgeHours",
            "requiredForControlledAccess",
            "requiredForPublicOutdoor",
            "allowExplicitNoClosure",
        }
        if not set(value).issubset(allowed):
            return None
        max_age = value.get("maxAgeHours")
        if (
            not isinstance(max_age, (int, float))
            or isinstance(max_age, bool)
            or not math.isfinite(float(max_age))
            or float(max_age) <= 0
        ):
            return None
        if any(key in value and not isinstance(value[key], bool) for key in allowed - {"maxAgeHours"}):
            return None
        required_for_public = bool(value.get("requiredForPublicOutdoor", False))
        return {
            "maxAgeHours": float(max_age),
            "requiredForControlledAccess": bool(value.get("requiredForControlledAccess", True)),
            "requiredForPublicOutdoor": required_for_public,
            # If the confirmed contract requires fresh proof only for
            # controlled access, an outdoor public place is usable unless its
            # provider evidence explicitly says it is closed.  Callers may
            # still opt into stricter public-space freshness by setting
            # requiredForPublicOutdoor=true or allowExplicitNoClosure=false.
            "allowExplicitNoClosure": bool(
                value.get("allowExplicitNoClosure", not required_for_public)
            ),
        }

    @staticmethod
    def _access_evidence_text(evidence: dict[str, Any]) -> str:
        claim_text = " ".join(
            " ".join(
                str(claim.get(key) or "")
                for key in (
                    "claimKey",
                    "claimType",
                    "stance",
                    "summary",
                    "supportedSignals",
                )
            )
            for claim in evidence.get("sourceClaims") or []
            if isinstance(claim, dict)
        )
        return " ".join(
            (
                str(evidence.get("nightAvailabilityStatus") or ""),
                str(evidence.get("openTimeToday") or ""),
                str(evidence.get("openTimeWeek") or ""),
                claim_text,
            )
        )

    @classmethod
    def _supporting_access_evidence(
        cls,
        evidence: dict[str, Any],
    ) -> tuple[bool, list[datetime]]:
        top_level_text = " ".join(
            (
                str(evidence.get("nightAvailabilityStatus") or ""),
                str(evidence.get("openTimeToday") or ""),
                str(evidence.get("openTimeWeek") or ""),
            )
        )
        has_signal = bool(NIGHT_VIEW_VERIFIED_AVAILABLE_RE.search(top_level_text))
        timestamps = (
            [
                parsed
                for raw in (evidence.get("timestamps") or {}).values()
                if (parsed := cls._utc_datetime(raw)) is not None
            ]
            if has_signal
            else []
        )
        for claim in evidence.get("sourceClaims") or []:
            if not isinstance(claim, dict):
                continue
            if str(claim.get("stance") or "").casefold() != "support":
                continue
            claim_text = " ".join(
                str(claim.get(key) or "")
                for key in (
                    "claimKey",
                    "claimType",
                    "summary",
                    "supportedSignals",
                )
            )
            if not (ACCESS_EVIDENCE_RE.search(claim_text) and NIGHT_VIEW_VERIFIED_AVAILABLE_RE.search(claim_text)):
                continue
            has_signal = True
            timestamps.extend(
                parsed
                for field in ACCESS_TIMESTAMP_FIELDS
                if (parsed := cls._utc_datetime(claim.get(field))) is not None
            )
        return has_signal, list(dict.fromkeys(timestamps))

    @staticmethod
    def _has_access_timestamp_value(evidence: dict[str, Any]) -> bool:
        if any(str(value or "").strip() for value in (evidence.get("timestamps") or {}).values()):
            return True
        return any(
            str(claim.get(field) or "").strip()
            for claim in evidence.get("sourceClaims") or []
            if isinstance(claim, dict)
            for field in ACCESS_TIMESTAMP_FIELDS
        )

    @staticmethod
    def _utc_datetime(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value or "").strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _availability_reject_reason(self, candidate: Any, intrinsic_text: str) -> str:
        availability_status = str(
            self._attribute(
                candidate,
                "night_availability_status",
                "nightAvailabilityStatus",
            )
            or "unknown"
        )
        opening_evidence = " ".join(
            str(
                self._attribute(
                    candidate,
                    field_name,
                    camel_name,
                )
                or ""
            )
            for field_name, camel_name in (
                ("open_time_today", "openTimeToday"),
                ("open_time_week", "openTimeWeek"),
            )
        )
        availability_text = " ".join(item for item in (intrinsic_text, availability_status, opening_evidence) if item)
        if NIGHT_VIEW_EXPLICITLY_UNAVAILABLE_RE.search(availability_text):
            return "night_view_explicitly_unavailable"
        if NIGHT_VIEW_CONTROLLED_ACCESS_RE.search(intrinsic_text) and not NIGHT_VIEW_VERIFIED_AVAILABLE_RE.search(
            f"{availability_status} {opening_evidence}"
        ):
            return "night_view_availability_unverified"
        return ""

    @staticmethod
    def _attribute(candidate: Any, *names: str) -> Any:
        if isinstance(candidate, dict):
            for name in names:
                if candidate.get(name) not in (None, ""):
                    return candidate.get(name)
            return None
        for name in names:
            value = getattr(candidate, name, None)
            if value not in (None, ""):
                return value
        return None

    def _subpoint_reason(self, candidate: Any) -> str:
        name = str(self._attribute(candidate, "name") or "")
        parts = re.split(r"[-－—]", name, maxsplit=1)
        if len(parts) < 2:
            return ""
        suffix = parts[1].strip()
        if not suffix:
            return ""
        if NIGHT_VIEW_WEAK_RE.search(suffix):
            return "night_view_subpoi_requires_parent"
        if not NIGHT_VIEW_SIGNAL_RE.search(suffix):
            return "night_view_subpoi_requires_parent"
        return ""

    def _matches_structured_hint_provenance(self, candidate: Any) -> bool:
        """Admit only a night-specific hint whose concrete entity matches AMap."""

        matched_hint, _ = self._structured_hint(candidate)
        if not matched_hint or not NIGHT_VIEW_SIGNAL_RE.search(matched_hint):
            return False
        hint_core = self._hint_core(matched_hint, candidate)
        if len(hint_core) < 3 or hint_core in GENERIC_NIGHT_VIEW_HINT_CORES:
            return False
        candidate_public_type = " ".join(
            str(self._attribute(candidate, field_name) or "") for field_name in ("type", "category")
        )
        return bool(
            any(
                hint_core in core or core in hint_core
                for core in self._candidate_entity_cores(candidate)
                if len(core) >= 3
            )
            and HINT_PROVENANCE_PUBLIC_TYPE_RE.search(candidate_public_type)
        )

    def _identity_reject_reason(
        self,
        candidate: Any,
        *,
        amap_identity: Optional[object] = None,
    ) -> str:
        amap_id = str(amap_identity or self._attribute(candidate, "amap_id", "amapId", "id") or "").strip().upper()
        if (
            str(self._attribute(candidate, "source") or "") != "amap-place-search"
            or re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id) is None
        ):
            return "night_view_amap_identity_missing"
        if not str(self._attribute(candidate, "city") or "").strip():
            return "night_view_city_missing"
        try:
            longitude = float(self._attribute(candidate, "longitude"))
            latitude = float(self._attribute(candidate, "latitude"))
        except (TypeError, ValueError):
            return "night_view_amap_identity_missing"
        if not (
            math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and longitude != 0
            and latitude != 0
        ):
            return "night_view_amap_identity_missing"
        return ""

    @classmethod
    def _structured_hint(
        cls,
        candidate: Any,
        *,
        structured_hint: Optional[str] = None,
    ) -> tuple[str, bool]:
        if str(structured_hint or "").strip():
            return str(structured_hint or "").strip(), False
        matched_hint = str(cls._attribute(candidate, "_trip_matched_hint") or "").strip()
        if matched_hint:
            return matched_hint, False
        source_note = str(cls._attribute(candidate, "source_note", "sourceNote") or "")
        match = MATCHED_HINT_NOTE_RE.search(source_note)
        return (match.group(1).strip(), True) if match else ("", False)

    @classmethod
    def _hint_is_authoritative(
        cls,
        candidate: Any,
        *,
        explicit_hint: Optional[str],
        hint_from_note: bool,
    ) -> bool:
        if str(explicit_hint or "").strip():
            return True
        binding = str(cls._attribute(candidate, "_trip_matched_hint_binding") or "").strip()
        if binding in {"search_query", "discovery_query"}:
            return False
        if hint_from_note and MATCHED_HINT_BINDING_NOTE_RE.search(
            str(cls._attribute(candidate, "source_note", "sourceNote") or "")
        ):
            return False
        return True

    def _hint_core(self, matched_hint: str, candidate: Any) -> str:
        hint_core = self._normalize_entity_text(matched_hint)
        while hint_core:
            stripped = NIGHT_VIEW_ACTIVITY_SUFFIX_RE.sub("", hint_core)
            if stripped == hint_core:
                break
            hint_core = stripped
        city = self._normalize_entity_text(self._attribute(candidate, "city"))
        if city:
            for prefix in (city, city.removesuffix("市")):
                if prefix and hint_core.startswith(prefix):
                    hint_core = hint_core[len(prefix) :]
                    break
        return hint_core

    def _candidate_entity_cores(self, candidate: Any) -> list[str]:
        values: list[Any] = [self._attribute(candidate, "name")]
        for field_name in ("aliases", "verified_parent_name", "_trip_verified_parent"):
            value = self._attribute(candidate, field_name)
            if isinstance(value, (list, tuple, set)):
                values.extend(value)
            else:
                values.append(value)
        return list(
            dict.fromkeys(
                normalized for normalized in (self._normalize_entity_text(value) for value in values) if normalized
            )
        )

    @staticmethod
    def _normalize_entity_text(value: object) -> str:
        return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(value or "")).casefold()
