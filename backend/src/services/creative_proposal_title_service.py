"""Agent-authored, evidence-bound titles for complete Portfolio proposals."""

from __future__ import annotations

import copy
import json
import math
import re
from hashlib import sha256
from typing import Any, Callable, Iterable

from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


class CreativeProposalTitleService:
    SCHEMA_VERSION = "creative-proposal-title-v3"
    GENERATION_SOURCE = "agent_grounded_factual_summary"
    AGENT_SCHEMA_VERSION = "creative-proposal-title-candidates-v1"
    AGENT_GENERATION_SOURCE = "agent_generated_title_candidates"
    AGENT_GENERATION_STATUS_SCHEMA = "creative-proposal-title-generation-v1"
    SERVER_FALLBACK_SOURCE = "server_sealed_intent_summary"
    _BANNED_TITLE_PATTERNS = (
        r"[｜|、,]",
        r"(?:一|二|三|四|五|六|七|八|九|十|\d+)日游$",
        r"方向",
        r"(?:草案|骨架)",
        r"围绕.{0,24}(?:排成|串成)",
        "顺路" + r"的.{0,24}" + "漫游",
    )

    _OPTIONAL_FAMILY_ROLES = {
        "heritage_walk": "寻旧",
        "market_walk": "逛集",
        "art_walk": "艺游",
        "local_life": "慢行",
        "area_walk": "漫步",
        "park_relax": "游园",
    }
    _INTENT_ROLES = {
        "campus_visit": "访校",
        "meal": "寻味",
        "local_food": "寻味",
        "food": "寻味",
        "night_view": "入夜",
        "museum": "看展",
        "art_museum": "看展",
        "park": "游园",
    }

    @classmethod
    def project(
        cls,
        snapshot: dict[str, Any],
        *,
        city: str = "",
        max_beats: int | None = None,
        compact_names: bool = True,
        style: int = 0,
    ) -> dict[str, Any]:
        del cls, snapshot, city, max_beats, compact_names, style
        # Legacy deterministic prose/POI concatenation is intentionally closed.
        return {}

    @classmethod
    def title_variants(
        cls,
        snapshot: dict[str, Any],
        *,
        city: str = "",
    ) -> list[dict[str, Any]]:
        del cls, snapshot, city
        return []

    @staticmethod
    def incomplete_status_title(
        snapshot: dict[str, Any],
        verifier: dict[str, Any] | None = None,
    ) -> str:
        """Return status copy only; incomplete material never gets prose."""

        pending = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        pending_intents = {
            str(item.get("intentType") or item.get("sourceIntentType") or "").casefold() for item in pending
        }
        if "night_view" in pending_intents:
            return "夜景待确认"
        report = verifier if isinstance(verifier, dict) else {}
        route_failures = [
            *list(report.get("routeCoverageFailures") or []),
            *list(report.get("routeQualityFailures") or []),
        ]
        if route_failures:
            return "路线待核验"
        return "方案待补全"

    @classmethod
    def server_fallback_title(
        cls,
        snapshot: dict[str, Any],
        *,
        reserved_titles: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Create a readable fallback from admitted, sealed intent facts only.

        Agent naming remains the primary path.  This projection is used only
        when that one bounded call is unavailable or invalid; it deliberately
        excludes POI names so a title cannot degrade into a place-name list or
        attach evening semantics to the wrong segment.
        """

        evidence = cls._server_fallback_intent_evidence(snapshot)
        materialized_evidence = [item for item in evidence if item.get("state") == "materialized"]
        intents = {str(item.get("intentType") or "") for item in materialized_evidence}
        evening_intents = {
            str(item.get("intentType") or "")
            for item in materialized_evidence
            if str(item.get("dayPart") or "") in {"evening", "night"}
        }
        if "campus_visit" in intents and "park" in intents:
            titles = (
                [
                    "学府漫游与晚间游园",
                    "双日访校与入夜游园",
                    "校园漫步与城市入夜",
                    "书香街区与暮色绿意",
                    "学府行旅与夜色公园",
                    "校园风物与入夜漫步",
                ]
                if "park" in evening_intents
                else [
                    "学府漫游与城市游园",
                    "双日访校与公园漫步",
                    "校园漫步与城市绿意",
                    "书香街区与公园慢行",
                    "学府行旅与城市绿洲",
                    "校园风物与绿意漫步",
                ]
            )
        elif "campus_visit" in intents and "night_view" in intents:
            titles = [
                "学府漫游与城市夜色",
                "双日访校与夜色漫步",
                "校园漫步与城市入夜",
                "书香街区与暮色行旅",
                "学府风物与灯影慢行",
                "校园人文与城市华灯",
            ]
        elif "campus_visit" in intents and intents.intersection({"meal", "local_food", "food"}):
            titles = [
                "学府漫游与风味寻访",
                "双日访校与城市寻味",
                "校园漫步与在地风味",
                "书香街区与烟火餐桌",
                "学府行旅与京味慢寻",
                "校园风物与城市烟火",
            ]
        elif "park" in intents and intents.intersection({"meal", "local_food", "food"}):
            titles = (
                [
                    "城市寻味与晚间游园",
                    "在地风味与入夜漫步",
                    "城市餐桌与夜间绿意",
                    "烟火风味与暮色公园",
                    "街巷餐桌与夜色慢行",
                    "城市滋味与入夜绿洲",
                ]
                if "park" in evening_intents
                else [
                    "城市寻味与公园漫游",
                    "在地风味与城市绿意",
                    "城市餐桌与公园漫步",
                    "烟火风味与绿洲慢行",
                    "街巷餐桌与城市游园",
                    "城市滋味与公园小憩",
                ]
            )
        elif "night_view" in intents:
            titles = [
                "城市夜色从容漫游",
                "入夜之后的城市漫步",
                "城市灯影悠然行",
                "暮色街区慢行记",
                "城市华灯自在漫步",
                "夜色之中的从容行旅",
            ]
        elif "park" in intents:
            titles = (
                [
                    "晚间游园从容漫步",
                    "入夜之后的城市绿意",
                    "城市夜间悠然行",
                    "暮色公园自在慢行",
                    "城市绿洲入夜漫步",
                    "夜色园林从容行旅",
                ]
                if "park" in evening_intents
                else [
                    "城市公园从容漫游",
                    "城市绿意悠然行",
                    "公园与街区慢游",
                    "城市绿洲自在漫步",
                    "园林街巷从容行旅",
                    "绿意之间的城市慢行",
                ]
            )
        elif "campus_visit" in intents:
            titles = [
                "学府风物从容漫游",
                "双日校园悠然行",
                "校园人文漫步",
                "书香街区自在行",
                "学府之间的城市慢旅",
                "校园风物寻访记",
            ]
        elif intents.intersection({"meal", "local_food", "food"}):
            titles = [
                "城市风味从容寻访",
                "在地餐桌悠然行",
                "城市寻味慢游",
                "街巷烟火自在行",
                "城市餐桌风味记",
                "在地滋味从容漫步",
            ]
        else:
            titles = [
                "城市主题从容漫游",
                "城市日常悠然行",
                "城市节奏慢游",
                "街区风物自在行",
                "城市脉络漫步记",
                "从容探索城市一隅",
            ]
        title_signals = cls.required_title_signals(snapshot)
        if title_signals:
            titles = []
            for title_signal in title_signals:
                for candidate in cls._signal_bound_fallback_candidates(
                    title_signal,
                    intents=intents,
                    evening_intents=evening_intents,
                ):
                    if candidate not in titles:
                        titles.append(candidate)
        else:
            titles = ["城市方案待核验"]
        fingerprint = cls._fallback_evidence_fingerprint(evidence)
        reserved = {str(item).strip() for item in reserved_titles if str(item).strip()}
        admissible = [
            item
            for item in titles
            if cls._valid_agent_title_text(item)
            and cls._is_morphologically_distinct(item, reserved)
        ]
        if admissible:
            indexed = {title: index for index, title in enumerate(titles)}
            title = min(
                admissible,
                key=lambda item: (
                    cls._maximum_title_overlap(item, reserved),
                    indexed[item],
                ),
            )
        else:
            title = "城市方案待核验"
        signal = next((item for item in title_signals if item in title), "")
        return {
            "title": title,
            "fallbackTitle": title,
            "fallbackTitleCandidates": titles,
            "fallbackSource": cls.SERVER_FALLBACK_SOURCE,
            "fallbackEvidenceFingerprint": fingerprint,
            "fallbackEvidence": evidence,
            "titleDecisionSource": (
                "route_fact_bound_server_fallback" if signal else "neutral_unverified_server_fallback"
            ),
            "requiredTitleSignals": title_signals,
            "usedTitleSignal": signal or None,
        }

    @classmethod
    def required_title_signals(cls, snapshot: dict[str, Any]) -> list[str]:
        """Return bounded display signals sourced only from accepted AMap POIs."""

        accepted_signals = cls._accepted_title_signals(snapshot)
        proposal_specific = [
            str(item)
            for item in snapshot.get("proposalSpecificTitleSignals") or []
            if str(item) in accepted_signals
        ]
        if "proposalSpecificTitleSignals" in snapshot:
            return proposal_specific
        return accepted_signals

    @classmethod
    def proposal_specific_title_signals(
        cls,
        snapshot: dict[str, Any],
        *,
        sibling_snapshots: Iterable[dict[str, Any]],
    ) -> list[str]:
        """Return accepted facts that do not occur in any sibling proposal.

        The comparison happens before the provider prompt is built, so generic
        area/theme words shared by every route cannot masquerade as a
        proposal-specific naming signal.  Non-standalone POIs are already
        excluded by ``_records`` and therefore cannot re-enter through this
        inventory.
        """

        current = cls._accepted_title_signals(snapshot)
        shared: set[str] = set()
        for sibling in sibling_snapshots:
            if isinstance(sibling, dict):
                shared.update(cls._accepted_title_signals(sibling))
        return [signal for signal in current if signal not in shared]

    @classmethod
    def _accepted_title_signals(cls, snapshot: dict[str, Any]) -> list[str]:
        records = cls._records(snapshot, city=cls._resolved_city(snapshot, requested_city=""))
        signals: list[str] = []
        for record in records:
            district = re.sub(r"(?:市|区|县)$", "", str(record.get("district") or "").strip())
            name = cls._compact_name(str(record.get("name") or ""), city=str(record.get("city") or ""), compact=True)
            name = re.sub(
                r"(?:历史文化街区|文化街区|菜市场|市场|大学|学院|校区|公园|景区)$",
                "",
                name,
            ).strip()
            for value in (district, name):
                compact = re.sub(r"[^\u4e00-\u9fff]", "", value)
                if 2 <= len(compact) <= 8 and compact not in signals:
                    signals.append(compact)
        return signals[:8]

    @classmethod
    def _signal_bound_fallback_candidates(
        cls,
        signal: str,
        *,
        intents: set[str],
        evening_intents: set[str],
    ) -> list[str]:
        has_meal = bool(intents.intersection({"meal", "local_food", "food"}))
        has_campus = "campus_visit" in intents
        has_park = "park" in intents
        has_verified_evening = bool(evening_intents)
        if has_campus and has_meal:
            themes = ["学府寻味", "书香烟火", "访校食光", "校园风物", "学府慢行", "书香漫游"]
        elif has_campus and has_park:
            themes = ["学府游园", "书香绿意", "校园漫步", "学府风物", "访校慢行", "书香行旅"]
        elif has_campus:
            themes = ["学府漫游", "书香行旅", "校园风物", "访校慢行", "学府寻踪", "书香漫步"]
        elif has_meal:
            themes = ["街巷寻味", "烟火食光", "在地风味", "城市餐桌", "寻味慢游", "风物小食"]
        elif has_park:
            themes = ["绿意漫游", "游园慢行", "城市绿洲", "公园漫步", "园林风物", "绿意行旅"]
        else:
            themes = ["城市漫游", "街区风物", "在地慢行", "城市寻踪", "从容行旅", "街巷漫步"]
        if has_verified_evening:
            themes = ["暮色" + item for item in themes]
        candidates: list[str] = []
        for theme in themes:
            for candidate in (
                f"{signal}{theme}",
                f"{theme}{signal}",
                f"{theme[:2]}{signal}{theme[2:]}",
            ):
                if candidate not in candidates:
                    candidates.append(candidate)
        return [item for item in candidates if cls._valid_agent_title_text(item)] or ["城市主题从容漫游"]

    @classmethod
    def with_server_fallback_title(
        cls,
        snapshot: dict[str, Any],
        *,
        reason_code: str,
        failure_type: str = "",
        reserved_titles: Iterable[str] = (),
    ) -> dict[str, Any]:
        projection = cls.server_fallback_title(snapshot, reserved_titles=reserved_titles)
        material = copy.deepcopy(snapshot)
        material.pop("portfolioTitleEvidence", None)
        material["title"] = projection["title"]
        material["portfolioTitleGeneration"] = {
            "schemaVersion": cls.AGENT_GENERATION_STATUS_SCHEMA,
            "status": "failed_non_blocking",
            "retryable": False,
            "candidateCount": 0,
            "evidenceFingerprint": None,
            "reasonCode": str(reason_code or "title_generation_failed"),
            "failureType": str(failure_type or "") or None,
            **projection,
        }
        if isinstance(material.get("creativeBrief"), dict):
            material["creativeBrief"] = {
                **copy.deepcopy(material["creativeBrief"]),
                "title": projection["title"],
            }
        if isinstance(material.get("portfolioOutputQuality"), dict):
            quality = copy.deepcopy(material["portfolioOutputQuality"])
            quality.pop("originalCreativeTitle", None)
            quality["displayTitle"] = projection["title"]
            material["portfolioOutputQuality"] = quality
        return material

    @classmethod
    def sealed_server_fallback_title(cls, snapshot: dict[str, Any]) -> str:
        """Return a validated fallback title even when storage shows status copy.

        Proposal storage deliberately replaces ``snapshot.title`` with a
        truthful status such as ``方案待补全`` until strict route verification.
        The separately sealed fallback title remains presentation evidence and
        is the collision key for later directions, so validate it against the
        current immutable material before exposing it.
        """

        generation = (
            snapshot.get("portfolioTitleGeneration")
            if isinstance(snapshot.get("portfolioTitleGeneration"), dict)
            else {}
        )
        expected = cls.server_fallback_title(snapshot)
        stored_title = str(generation.get("fallbackTitle") or "").strip()
        stored_evidence = [
            copy.deepcopy(item)
            for item in generation.get("fallbackEvidence") or []
            if isinstance(item, dict)
        ]
        stored_candidates = [
            str(item).strip()
            for item in generation.get("fallbackTitleCandidates") or []
            if str(item).strip()
        ]
        expected_candidates = [str(item) for item in expected.get("fallbackTitleCandidates") or []]
        if not (
            generation.get("schemaVersion") == cls.AGENT_GENERATION_STATUS_SCHEMA
            and generation.get("status") == "failed_non_blocking"
            and generation.get("retryable") is False
            and generation.get("fallbackSource") == cls.SERVER_FALLBACK_SOURCE
            and stored_title
            and stored_title in expected_candidates
            and stored_candidates == expected_candidates
            and generation.get("fallbackEvidenceFingerprint")
            == cls._fallback_evidence_fingerprint(stored_evidence)
            and cls._fallback_evidence_matches_current_snapshot(
                snapshot,
                stored_evidence=stored_evidence,
                current_evidence=expected["fallbackEvidence"],
            )
        ):
            return ""
        return stored_title

    @classmethod
    def _fallback_evidence_matches_current_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        stored_evidence: list[dict[str, Any]],
        current_evidence: list[dict[str, Any]],
    ) -> bool:
        """Allow only controller-sealed pending rows added after legacy naming.

        Older Simple Open runs named the snapshot immediately before unresolved
        skeleton segments were converted to pending metadata.  The grounded POI
        evidence remains immutable; the only legitimate later additions are
        provenance-complete, provider-exhausted pending occurrences.
        """

        stored_index = 0
        for current_item in current_evidence:
            if stored_index < len(stored_evidence) and current_item == stored_evidence[stored_index]:
                stored_index += 1
                continue
            if not cls._is_controller_materialized_pending_evidence(snapshot, current_item):
                return False
        return stored_index == len(stored_evidence)

    @staticmethod
    def _is_controller_materialized_pending_evidence(
        snapshot: dict[str, Any],
        evidence: dict[str, Any],
    ) -> bool:
        if evidence.get("state") != "pending":
            return False
        for slot in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(slot, dict):
                continue
            preference = (
                slot.get("schedulePreference")
                if isinstance(slot.get("schedulePreference"), dict)
                else {}
            )
            day_number = int(slot.get("dayNumber") or 0)
            goal_id = str(slot.get("goalId") or "")
            source_goal_id = str(slot.get("sourceGoalId") or "")
            occurrence_id = str(slot.get("occurrenceId") or "")
            planning_slot_id = str(slot.get("planningSlotId") or "")
            if (
                slot.get("state") == "pending"
                and slot.get("groundingStatus") == "unresolved"
                and slot.get("simpleDirectionProviderExhausted") is True
                and slot.get("simpleDirectionRequirementLineageConflict") is False
                and slot.get("timingBasis") == "simple_direction_provider_exhausted_slot"
                and slot.get("reasonCode") == "provider_candidates_exhausted_or_semantically_rejected"
                and slot.get("requirementEvidenceSource") == "request_intent_contract"
                and "poi" not in slot
                and planning_slot_id
                and planning_slot_id == str(slot.get("slotId") or "")
                and str(slot.get("poolId") or "")
                and goal_id
                and goal_id == source_goal_id
                and occurrence_id == f"occ:{source_goal_id}:day:{day_number}"
                and str(slot.get("lineageAuthority") or "") == "goal_occurrence_compiler"
                and str(preference.get("sourceGoalId") or "") == source_goal_id
                and str(preference.get("occurrenceId") or "") == occurrence_id
                and day_number == int(evidence.get("dayNumber") or 0)
                and str(slot.get("intentType") or "") == str(evidence.get("intentType") or "")
                and occurrence_id == str(evidence.get("occurrenceId") or "")
                and str(preference.get("dayPart") or "") == str(evidence.get("dayPart") or "")
            ):
                return True
        return False

    @staticmethod
    def _fallback_evidence_fingerprint(evidence: list[dict[str, Any]]) -> str:
        return sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def is_valid_server_fallback_title(
        cls,
        snapshot: dict[str, Any],
        *,
        reserved_titles: Iterable[str] = (),
    ) -> bool:
        stored_title = str(snapshot.get("title") or "").strip()
        sealed_title = cls.sealed_server_fallback_title(snapshot)
        reserved = {str(item).strip() for item in reserved_titles if str(item).strip()}
        return bool(
            sealed_title
            and sealed_title == stored_title
            and cls._is_morphologically_distinct(sealed_title, reserved)
        )

    @classmethod
    def _server_fallback_intent_evidence(cls, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict) or not cls._is_canonical_amap_poi(segment.get("poi")):
                    continue
                if cls._has_non_standalone_experience_evidence(segment):
                    continue
                metadata = (
                    segment.get("semanticMetadata")
                    if isinstance(segment.get("semanticMetadata"), dict)
                    else {}
                )
                intent_type = str(metadata.get("intentType") or segment.get("kind") or "").strip()
                if not intent_type:
                    continue
                preference = (
                    metadata.get("schedulePreference")
                    if isinstance(metadata.get("schedulePreference"), dict)
                    else {}
                )
                evidence.append(
                    {
                        "state": "materialized",
                        "dayNumber": day_number,
                        "intentType": intent_type,
                        "dayPart": str(preference.get("dayPart") or ""),
                        "occurrenceId": str(metadata.get("occurrenceId") or ""),
                        "canonicalAmapId": PoiPhysicalIdentityService.canonical_amap_id(segment.get("poi")),
                        "latitude": round(float(segment["poi"]["latitude"]), 5),
                        "longitude": round(float(segment["poi"]["longitude"]), 5),
                    }
                )
        for pending in snapshot.get("portfolioPendingSlots") or []:
            if not isinstance(pending, dict):
                continue
            intent_type = str(pending.get("intentType") or pending.get("sourceIntentType") or "").strip()
            if not intent_type or not str(pending.get("occurrenceId") or "").strip():
                continue
            preference = (
                pending.get("schedulePreference")
                if isinstance(pending.get("schedulePreference"), dict)
                else {}
            )
            evidence.append(
                {
                    "state": "pending",
                    "dayNumber": int(pending.get("dayNumber") or 0),
                    "intentType": intent_type,
                    "dayPart": str(preference.get("dayPart") or ""),
                    "occurrenceId": str(pending.get("occurrenceId") or ""),
                }
            )
        evidence.sort(
            key=lambda item: (
                int(item.get("dayNumber") or 0),
                str(item.get("intentType") or ""),
                str(item.get("occurrenceId") or ""),
                str(item.get("state") or ""),
            )
        )
        return evidence

    @classmethod
    def select_agent_candidate(
        cls,
        snapshot: dict[str, Any],
        raw_candidates: str | dict[str, Any],
        *,
        reserved_titles: set[str] | None = None,
    ) -> dict[str, Any]:
        """Validate and select one model-authored title after proposal verification.

        The model may write the prose, but it cannot introduce itinerary facts:
        every candidate must bind to the complete canonical AMap evidence set.
        """

        try:
            payload = json.loads(raw_candidates) if isinstance(raw_candidates, str) else copy.deepcopy(raw_candidates)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schemaVersion") != cls.AGENT_SCHEMA_VERSION:
            return {}
        records = cls._records(snapshot, city=cls._resolved_city(snapshot, requested_city=""))
        evidence_ids = [str(item.get("amapId") or "").strip().upper() for item in records]
        if not evidence_ids:
            return {}
        expected_ids = set(evidence_ids)
        reserved = {str(item).strip() for item in reserved_titles or set() if str(item).strip()}
        reserved_normalized = {cls._normalized_title(item) for item in reserved}
        required_signals = cls.required_title_signals(snapshot)
        if not required_signals:
            # A provider-authored poetic title cannot be proposal-specific when
            # no accepted place/region signal can safely distinguish it.
            return {}
        time_semantics_allowed = cls._verified_evening_semantics(snapshot)
        valid: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        if len(candidates) < 3:
            return {}
        for item in candidates[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            normalized_title = cls._normalized_title(title)
            bound_ids = {
                str(value).strip().upper() for value in item.get("evidenceAmapIds") or [] if str(value).strip()
            }
            if (
                normalized_title in seen
                or normalized_title in reserved_normalized
                or not cls._valid_agent_title_text(title)
                or cls._is_simple_poi_name_compilation(title, records)
                or bound_ids != expected_ids
                or (required_signals and not any(signal in title for signal in required_signals))
                or (not time_semantics_allowed and re.search(r"(?:晚间|入夜|夜游|夜色|日落|暮色|华灯)", title))
                or not cls._is_morphologically_distinct(title, reserved)
            ):
                continue
            seen.add(normalized_title)
            used_signal = next((signal for signal in required_signals if signal in title), None)
            valid.append(
                {
                    "title": title,
                    "evidenceAmapIds": sorted(bound_ids),
                    "usedTitleSignal": used_signal,
                }
            )
        # A single acceptable line hidden among invalid model output is not a
        # three-candidate creative pass.  Requiring three independently valid
        # candidates keeps selection meaningful and makes provider degradation
        # fail closed instead of silently weakening the title contract.
        if len(valid) < 3:
            return {}
        selected = valid[0]
        return {
            "schemaVersion": cls.AGENT_SCHEMA_VERSION,
            "generationSource": cls.AGENT_GENERATION_SOURCE,
            "title": selected["title"],
            "candidateCount": len(candidates),
            "validCandidates": valid,
            "selectedAmapIds": evidence_ids,
            "evidenceFingerprint": sha256("|".join(evidence_ids).encode("utf-8")).hexdigest(),
            "evidence": records,
            "requiredTitleSignals": required_signals,
            "usedTitleSignal": selected.get("usedTitleSignal"),
            "titleDecisionSource": "provider_route_fact_bound",
            "timeSemanticsAllowed": time_semantics_allowed,
        }

    @classmethod
    def _valid_agent_title_text(cls, title: str) -> bool:
        compact = re.sub(r"\s+", "", str(title or ""))
        return bool(
            re.fullmatch(r"[\u4e00-\u9fff]{6,18}", compact)
            and not any(re.search(pattern, compact) for pattern in cls._BANNED_TITLE_PATTERNS)
            and not re.search(r"(?:待确认|待补全|待核验|草案|方案)[：:]?", compact)
            and not re.search(r"(?:百年|千年|皇家|帝王|第一|唯一|免费|免票|永久开放)", compact)
        )

    @staticmethod
    def _normalized_title(title: str) -> str:
        return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(title or "")).casefold()

    @classmethod
    def _is_morphologically_distinct(
        cls,
        title: str,
        sibling_titles: Iterable[str],
    ) -> bool:
        """Require route siblings to differ in both shape and wording.

        Exact-title uniqueness is insufficient for names such as
        ``海淀暮色学府寻味`` and ``海淀暮色学府游园``.  The comparison is
        deliberately deterministic and evidence-free: it only checks the
        display strings after the factual title validator has already passed.
        """

        candidate = cls._normalized_title(title)
        if not candidate:
            return False
        for sibling_title in sibling_titles:
            sibling = cls._normalized_title(str(sibling_title or ""))
            if not sibling:
                continue
            if candidate == sibling:
                return False
            if cls._common_prefix_length(candidate, sibling) >= 2:
                return False
            if cls._common_suffix_length(candidate, sibling) >= 2:
                return False
            if cls._title_bigram_jaccard(candidate, sibling) >= 0.5:
                return False
        return True

    @staticmethod
    def _common_prefix_length(left: str, right: str) -> int:
        count = 0
        for left_char, right_char in zip(left, right):
            if left_char != right_char:
                break
            count += 1
        return count

    @staticmethod
    def _common_suffix_length(left: str, right: str) -> int:
        count = 0
        for left_char, right_char in zip(reversed(left), reversed(right)):
            if left_char != right_char:
                break
            count += 1
        return count

    @staticmethod
    def _title_bigram_jaccard(left: str, right: str) -> float:
        left_pairs = {left[index : index + 2] for index in range(max(len(left) - 1, 0))}
        right_pairs = {right[index : index + 2] for index in range(max(len(right) - 1, 0))}
        union = left_pairs | right_pairs
        if not union:
            return 0.0
        return len(left_pairs & right_pairs) / len(union)

    @classmethod
    def _maximum_title_overlap(
        cls,
        title: str,
        sibling_titles: Iterable[str],
    ) -> float:
        candidate = cls._normalized_title(title)
        scores: list[float] = []
        for sibling_title in sibling_titles:
            sibling = cls._normalized_title(str(sibling_title or ""))
            if not sibling:
                continue
            scores.append(cls._title_bigram_jaccard(candidate, sibling))
        return max(scores, default=0.0)

    @classmethod
    def _verified_evening_semantics(cls, snapshot: dict[str, Any]) -> bool:
        for record in cls._records(snapshot, city=cls._resolved_city(snapshot, requested_city="")):
            raw = str(record.get("startTime") or "")
            match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
            if match and int(match.group(1)) >= 18:
                return True
        return False

    @classmethod
    def agent_generation_context(
        cls,
        snapshot: dict[str, Any],
        *,
        city: str = "",
        primary_axis: str = "",
        secondary_axes: Iterable[str] = (),
        optional_experiences: Iterable[str] = (),
        reserved_titles: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Build the only fact set a title provider is allowed to see.

        Callers invoke this after route preflight and proposal verification.
        The provider receives finalized canonical places plus a compact proof
        that the snapshot passed; it never receives pending or rejected POIs.
        """

        resolved_city = city or str(snapshot.get("city") or "")
        records = cls._records(snapshot, city=resolved_city)
        verifier = snapshot.get("portfolioVerifier") if isinstance(snapshot.get("portfolioVerifier"), dict) else {}
        route_rows = [
            item
            for key in ("portfolioRouteEvidence", "routeEvidence", "routeOptions")
            for item in (snapshot.get(key) or [])
            if isinstance(item, dict)
        ]
        verified_route_count = len(
            {
                (
                    str(item.get("fromSegmentId") or ""),
                    str(item.get("toSegmentId") or ""),
                )
                for item in route_rows
                if str(item.get("fromSegmentId") or "")
                and str(item.get("toSegmentId") or "")
                and cls._positive_number(item.get("durationSeconds") or item.get("duration_seconds"))
                and cls._positive_number(item.get("distanceMeters") or item.get("distance_meters"))
            }
        )
        required_title_signals = cls.required_title_signals(snapshot)
        return {
            "city": resolved_city,
            "theme": {
                "primaryAxis": str(primary_axis or ""),
                "secondaryAxes": [str(item) for item in secondary_axes if str(item)],
                "optionalExperiences": [str(item) for item in optional_experiences if str(item)],
            },
            "tone": "自然、克制、有文化感",
            "days": [
                {
                    "dayNumber": day.get("dayNumber"),
                    "places": [
                        {
                            "name": item.get("name"),
                            "amapId": item.get("amapId"),
                            "role": item.get("role"),
                        }
                        for item in records
                        if item.get("dayNumber") == day.get("dayNumber")
                    ],
                }
                for day in snapshot.get("days") or []
                if isinstance(day, dict)
            ],
            "evidenceAmapIds": [item.get("amapId") for item in records],
            "requiredTitleSignals": required_title_signals,
            "timeSemanticsAllowed": cls._verified_evening_semantics(snapshot),
            "reservedTitles": sorted({str(item).strip() for item in reserved_titles if str(item).strip()}),
            "verification": {
                "passed": verifier.get("passed") is True,
                "placeEvidencePassed": bool(records),
                "readinessState": (
                    "complete"
                    if verifier.get("passed") is True
                    else "partial_with_verified_places"
                    if records
                    else "unverified"
                ),
                "verifiedRouteCount": verified_route_count,
            },
        }

    @classmethod
    def generate_and_apply_agent_title(
        cls,
        snapshot: dict[str, Any],
        *,
        generator: Callable[[dict[str, Any]], str],
        context: dict[str, Any],
        reserved_titles: set[str] | None = None,
    ) -> dict[str, Any]:
        """Try title generation twice against one frozen evidence contract.

        The retry is title-only: both attempts receive byte-equivalent factual
        context and use the same validator. No caller hook can trigger place
        search, route verification, proposal mutation, or readiness changes.
        """

        frozen_context = copy.deepcopy(context)
        context_fingerprint = sha256(
            json.dumps(frozen_context, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        last_reason = "title_provider_failed"
        last_failure_type = ""
        rejected_attempts: list[dict[str, Any]] = []
        for attempt in range(1, 3):
            try:
                raw = generator(copy.deepcopy(frozen_context))
            except Exception as error:
                last_reason = "title_provider_failed"
                last_failure_type = type(error).__name__
                rejected_attempts.append(
                    {
                        "attempt": attempt,
                        "reasonCode": last_reason,
                        "failureType": last_failure_type,
                        "contextFingerprint": context_fingerprint,
                    }
                )
                continue
            projection = cls.select_agent_candidate(
                snapshot,
                raw,
                reserved_titles=reserved_titles,
            )
            if not projection:
                last_reason = "title_candidates_invalid"
                last_failure_type = ""
                rejected_attempts.append(
                    {
                        "attempt": attempt,
                        "reasonCode": last_reason,
                        "failureType": None,
                        "contextFingerprint": context_fingerprint,
                    }
                )
                continue
            material = cls.apply_agent_projection(snapshot, projection)
            if not cls.is_valid_agent_projection(material, projection):
                last_reason = "title_projection_invalid"
                last_failure_type = ""
                rejected_attempts.append(
                    {
                        "attempt": attempt,
                        "reasonCode": last_reason,
                        "failureType": None,
                        "contextFingerprint": context_fingerprint,
                    }
                )
                continue
            material["portfolioTitleGeneration"] = {
                "schemaVersion": cls.AGENT_GENERATION_STATUS_SCHEMA,
                "status": "succeeded",
                "retryable": False,
                "attemptCount": attempt,
                "maxAttempts": 2,
                "contextFingerprint": context_fingerprint,
                "rejectedAttempts": rejected_attempts,
                "candidateCount": len(projection.get("validCandidates") or []),
                "evidenceFingerprint": projection.get("evidenceFingerprint"),
                "reasonCode": None,
                "titleDecisionSource": projection.get("titleDecisionSource"),
                "requiredTitleSignals": copy.deepcopy(projection.get("requiredTitleSignals") or []),
                "usedTitleSignal": projection.get("usedTitleSignal"),
            }
            return material
        fallback = cls.with_server_fallback_title(
            snapshot,
            reason_code=last_reason,
            failure_type=last_failure_type,
            reserved_titles=reserved_titles or set(),
        )
        generation = fallback.get("portfolioTitleGeneration")
        if isinstance(generation, dict):
            generation.update(
                {
                    "attemptCount": 2,
                    "maxAttempts": 2,
                    "contextFingerprint": context_fingerprint,
                    "rejectedAttempts": rejected_attempts,
                }
            )
        return fallback

    @classmethod
    def with_agent_generation_failure(
        cls,
        snapshot: dict[str, Any],
        *,
        reason_code: str,
        failure_type: str = "",
    ) -> dict[str, Any]:
        """Keep a verified itinerary visible but non-adoptable and retryable."""

        material = copy.deepcopy(snapshot)
        title = "标题生成待重试"
        material.pop("portfolioTitleEvidence", None)
        material["title"] = title
        material["portfolioTitleGeneration"] = {
            "schemaVersion": cls.AGENT_GENERATION_STATUS_SCHEMA,
            "status": "failed_retryable",
            "retryable": True,
            "candidateCount": 0,
            "evidenceFingerprint": None,
            "reasonCode": str(reason_code or "title_generation_failed"),
            "failureType": str(failure_type or "") or None,
        }
        if isinstance(material.get("creativeBrief"), dict):
            material["creativeBrief"] = {
                **copy.deepcopy(material["creativeBrief"]),
                "title": title,
            }
        if isinstance(material.get("portfolioOutputQuality"), dict):
            quality = copy.deepcopy(material["portfolioOutputQuality"])
            quality.pop("originalCreativeTitle", None)
            quality["displayTitle"] = title
            material["portfolioOutputQuality"] = quality
        return material

    @classmethod
    def _is_simple_poi_name_compilation(
        cls,
        title: str,
        records: list[dict[str, Any]],
    ) -> bool:
        """Reject a title that is only two or more itinerary names joined up."""

        compact = re.sub(r"\s+", "", str(title or ""))
        names = sorted(
            {
                re.sub(r"\s+", "", str(item.get("name") or ""))
                for item in records
                if len(re.sub(r"\s+", "", str(item.get("name") or ""))) >= 2
            },
            key=len,
            reverse=True,
        )
        matched = [name for name in names if name in compact]
        if len(matched) < 2:
            return False
        remainder = compact
        for name in matched:
            remainder = remainder.replace(name, "")
        remainder = re.sub(r"[，、,与和及从到沿入走访逛看游]+", "", remainder)
        return not remainder

    @classmethod
    def apply_agent_projection(
        cls,
        snapshot: dict[str, Any],
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        if not cls.is_valid_agent_projection(snapshot, projection):
            return snapshot
        material = copy.deepcopy(snapshot)
        title = str(projection["title"]).strip()
        material["title"] = title
        material["portfolioTitleEvidence"] = copy.deepcopy(projection)
        if isinstance(material.get("creativeBrief"), dict):
            material["creativeBrief"] = {**copy.deepcopy(material["creativeBrief"]), "title": title}
        if isinstance(material.get("portfolioOutputQuality"), dict):
            material["portfolioOutputQuality"] = {
                **copy.deepcopy(material["portfolioOutputQuality"]),
                "originalCreativeTitle": title,
                "displayTitle": title,
            }
        return material

    @classmethod
    def is_valid_agent_projection(
        cls,
        snapshot: dict[str, Any],
        projection: Any,
    ) -> bool:
        """Revalidate a selected Agent title against the current snapshot.

        The projection can cross process and persistence boundaries before it
        is rendered.  Recomputing the complete evidence set here prevents a
        stale or forged projection from changing a proposal title.
        """

        if not isinstance(projection, dict):
            return False
        if (
            projection.get("schemaVersion") != cls.AGENT_SCHEMA_VERSION
            or projection.get("generationSource") != cls.AGENT_GENERATION_SOURCE
        ):
            return False
        title = str(projection.get("title") or "").strip()
        records = cls._records(
            snapshot,
            city=cls._resolved_city(snapshot, requested_city=""),
        )
        evidence_ids = [str(item.get("amapId") or "").strip().upper() for item in records]
        if not evidence_ids:
            return False
        selected_ids = [
            str(item).strip().upper() for item in projection.get("selectedAmapIds") or [] if str(item).strip()
        ]
        expected_fingerprint = sha256("|".join(evidence_ids).encode("utf-8")).hexdigest()
        expected_signals = cls.required_title_signals(snapshot)
        time_semantics_allowed = cls._verified_evening_semantics(snapshot)
        valid_candidates = (
            projection.get("validCandidates") if isinstance(projection.get("validCandidates"), list) else []
        )
        expected_set = set(evidence_ids)
        validated_titles: list[str] = []
        for candidate in valid_candidates:
            if not isinstance(candidate, dict):
                return False
            candidate_title = str(candidate.get("title") or "").strip()
            candidate_ids = {
                str(item).strip().upper() for item in candidate.get("evidenceAmapIds") or [] if str(item).strip()
            }
            if (
                not cls._valid_agent_title_text(candidate_title)
                or cls._is_simple_poi_name_compilation(candidate_title, records)
                or candidate_ids != expected_set
                or candidate_title in validated_titles
                or (expected_signals and not any(signal in candidate_title for signal in expected_signals))
                or (
                    not time_semantics_allowed
                    and re.search(r"(?:晚间|入夜|夜游|夜色|日落|暮色|华灯)", candidate_title)
                )
            ):
                return False
            validated_titles.append(candidate_title)
        return bool(
            cls._valid_agent_title_text(title)
            and len(validated_titles) >= 3
            and title in validated_titles
            and selected_ids == evidence_ids
            and projection.get("evidenceFingerprint") == expected_fingerprint
            and int(projection.get("candidateCount") or 0) >= 3
            and list(projection.get("requiredTitleSignals") or []) == expected_signals
            and (
                not expected_signals
                or str(projection.get("usedTitleSignal") or "") in expected_signals
            )
        )

    @classmethod
    def apply(
        cls,
        snapshot: dict[str, Any],
        *,
        city: str = "",
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del cls, city, projection
        # Retained as a fail-closed compatibility shim for older callers.
        return copy.deepcopy(snapshot)

    @classmethod
    def is_valid_projection(
        cls,
        snapshot: dict[str, Any],
        projection: Any,
    ) -> bool:
        del cls, snapshot, projection
        return False

    @classmethod
    def _resolved_city(
        cls,
        snapshot: dict[str, Any],
        *,
        requested_city: str,
    ) -> str:
        snapshot_city = str(snapshot.get("city") or "").strip()
        if snapshot_city:
            return snapshot_city
        poi_cities: list[str] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                if not cls._is_canonical_amap_poi(poi):
                    continue
                poi_city = str(poi.get("city") or "").strip()
                if poi_city and poi_city not in poi_cities:
                    poi_cities.append(poi_city)
        if len(poi_cities) == 1:
            return poi_cities[0]
        return str(requested_city or "").strip()

    @classmethod
    def _records(
        cls,
        snapshot: dict[str, Any],
        *,
        city: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            segments = [item for item in day.get("segments") or [] if isinstance(item, dict)]
            segments.sort(key=cls._segment_sort_key)
            for segment in segments:
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                if not cls._is_canonical_amap_poi(poi):
                    continue
                if cls._has_non_standalone_experience_evidence(segment):
                    continue
                amap_id = str(poi.get("amapId") or "").strip().upper()
                physical_id = PoiPhysicalIdentityService.canonical_amap_id(poi)
                if not physical_id or physical_id in seen_ids:
                    continue
                seen_ids.add(physical_id)
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                role, priority = cls._role(semantic, poi)
                records.append(
                    {
                        "segmentId": str(segment.get("id") or ""),
                        "amapId": amap_id,
                        "name": str(poi.get("name") or "").strip(),
                        "district": str(poi.get("district") or "").strip() or None,
                        "dayNumber": day_number,
                        "startTime": str(segment.get("startTime") or "").strip() or None,
                        "role": role,
                        "priority": priority,
                        "optionalExperienceFamily": str(semantic.get("optionalExperienceFamily") or "") or None,
                        "intentType": str(semantic.get("intentType") or semantic.get("sourceIntentType") or "") or None,
                        "city": city or str(poi.get("city") or ""),
                    }
                )
        return records

    @staticmethod
    def _has_non_standalone_experience_evidence(segment: dict[str, Any]) -> bool:
        semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
        schedule = (
            semantic.get("scheduleConstraints")
            if isinstance(semantic.get("scheduleConstraints"), dict)
            else {}
        )
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        candidates = (
            semantic.get("experienceIndependenceEvidence"),
            schedule.get("experienceIndependenceEvidence"),
            poi.get("experienceIndependenceEvidence"),
        )
        return any(
            isinstance(item, dict)
            and str(item.get("status") or "")
            in {"embedded_in_day_anchor", "independence_pending"}
            for item in candidates
        )

    @classmethod
    def _select_story_beats(
        cls,
        records: list[dict[str, Any]],
        *,
        max_beats: int | None,
    ) -> list[dict[str, Any]]:
        if max_beats is None or max_beats >= len(records):
            return list(records)
        limit = max(1, int(max_beats))
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()

        def add(items: Iterable[dict[str, Any]]) -> None:
            for item in items:
                if len(selected) >= limit:
                    return
                if item["amapId"] in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item["amapId"])

        optional = [item for item in records if int(item["priority"]) == 0]
        add(optional[:2])
        # Preserve the route's ending beat (commonly the verified night view),
        # then give meals and campuses a chance to make otherwise similar
        # proposals human-distinguishable.
        # Keep the final route anchor in the story, then reserve the remaining
        # beats for distinct semantic roles instead of letting the reversed
        # route order consume the whole title budget.
        add(records[-1:])
        for priority in (2, 3, 1, 4):
            add(item for item in records if int(item["priority"]) == priority)
        add(records)
        selected.sort(key=lambda item: records.index(item))
        return selected

    @classmethod
    def _render_title(
        cls,
        records: list[dict[str, Any]],
        *,
        city: str,
        day_count: int,
        compact_names: bool,
        style: int,
    ) -> str:
        del cls, records, city, day_count, compact_names, style
        return ""

    @staticmethod
    def _compact_name(name: str, *, city: str, compact: bool) -> str:
        value = str(name or "").strip()
        if compact:
            value = re.sub(rf"^{re.escape(city)}(?:市)?", "", value).strip()
            value = re.sub(r"[（(][^）)]{1,24}[）)]$", "", value).strip()
        return value

    @classmethod
    def _role(
        cls,
        semantic: dict[str, Any],
        poi: dict[str, Any],
    ) -> tuple[str, int]:
        family = str(semantic.get("optionalExperienceFamily") or semantic.get("family") or "").casefold()
        if family in cls._OPTIONAL_FAMILY_ROLES:
            return cls._OPTIONAL_FAMILY_ROLES[family], 0
        intent = str(semantic.get("intentType") or semantic.get("sourceIntentType") or "").casefold()
        coverage_roles = {str(item).casefold() for item in semantic.get("coverageRoles") or []}
        if "night_view" in coverage_roles:
            intent = "night_view"
        elif "meal" in coverage_roles or "local_food" in coverage_roles:
            intent = "meal"
        elif "campus_visit" in coverage_roles:
            intent = "campus_visit"
        if intent in cls._INTENT_ROLES:
            priority = (
                1
                if intent == "night_view"
                else 2
                if intent in {"meal", "local_food", "food"}
                else 3
                if intent == "campus_visit"
                else 4
            )
            return cls._INTENT_ROLES[intent], priority
        text = " ".join(str(poi.get(key) or "") for key in ("name", "type", "category", "providerType"))
        if re.search(r"大学|学院|高校", text):
            return "访校", 3
        if re.search(r"餐厅|餐馆|菜馆|小吃|烤鱼|烤鸭", text):
            return "寻味", 2
        if re.search(r"塔|观景|夜景", text):
            return "入夜", 1
        return "探访", 4

    @staticmethod
    def _segment_sort_key(segment: dict[str, Any]) -> tuple[int, str]:
        raw = str(segment.get("startTime") or "")
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        minutes = int(match.group(1)) * 60 + int(match.group(2)) if match else 24 * 60
        return minutes, str(segment.get("id") or "")

    @staticmethod
    def _positive_number(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    @staticmethod
    def _is_canonical_amap_poi(value: Any) -> bool:
        if not isinstance(value, dict) or str(value.get("source") or "") != "amap-place-search":
            return False
        amap_id = str(value.get("amapId") or "").strip().upper()
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id):
            return False
        if not str(value.get("name") or "").strip():
            return False
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90 <= latitude <= 90
            and -180 <= longitude <= 180
            and latitude != 0
            and longitude != 0
        )
