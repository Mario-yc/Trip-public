from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


class DecisionNormalizationError(ValueError):
    def __init__(self, reason_code: str, path: str, detail: str = ""):
        super().__init__(f"{reason_code}:{path}:{detail}".rstrip(":"))
        self.reason_code = reason_code
        self.path = path
        self.detail = detail


@dataclass(frozen=True)
class DecisionNormalizationResult:
    normalized: dict[str, Any]
    aliases_applied: list[str]


class AgentDecisionNormalizer:
    """Normalizes only proven-safe provider aliases before strict validation."""

    RESOLVE_CANONICAL_FIELDS = {
        "type",
        "targetGoalId",
        "targetSegmentIds",
        "searchIntent",
        "searchMode",
        "maxCandidates",
        "autoSelectPolicy",
        "askUserPolicy",
    }
    RESOLVE_ALIAS_FIELDS = {"segmentId", "targetSegmentId", "searchText", "searchQuery", "query", "dayNumber"}

    def normalize(self, payload: dict[str, Any], *, context: dict[str, Any]) -> DecisionNormalizationResult:
        if not isinstance(payload, dict):
            raise DecisionNormalizationError("invalid_payload", "$", "expected object")
        normalized = deepcopy(payload)
        directive = normalized.get("actionDirective")
        if not isinstance(directive, dict):
            return DecisionNormalizationResult(normalized=normalized, aliases_applied=[])
        action = str(normalized.get("primaryAction") or "")
        directive_type = str(directive.get("type") or "")
        aliases_applied: list[str] = []
        if action == "ask_user" and not directive_type:
            # ``type`` only discriminates the non-writing AskUserDirective and
            # duplicates the already model-authored primaryAction.  Copying it
            # does not author a question, option, semantic patch, or capability;
            # every business field still passes the strict clarification gates.
            directive["type"] = action
            directive_type = action
            aliases_applied.append("actionDirective.type:copied_from_primaryAction")
        if action != directive_type:
            raise DecisionNormalizationError("action_directive_mismatch", "actionDirective.type")
        if action == "ask_user":
            aliases_applied.extend(self._normalize_ask_user_semantic_aliases(directive, context))
        assumptions = normalized.get("assumptions")
        if isinstance(assumptions, list):
            retained = [
                item for item in assumptions if not (isinstance(item, dict) and item.get("source") == "user_message")
            ]
            if len(retained) != len(assumptions):
                normalized["assumptions"] = retained
                aliases_applied.append("userMessageFactsRemovedFromAssumptions")
                if not retained and normalized.get("assumptionPolicy") == "allow_reversible":
                    normalized["assumptionPolicy"] = "none"
        if action == "resolve_poi":
            aliases_applied.extend(self._normalize_resolve_poi(directive, context))
        aliases_applied.extend(self._normalize_candidate_identity_alias(directive, context))
        if (
            action == "patch_itinerary"
            and directive.get("operationIntent") == "replace_segment_poi_from_candidate"
            and directive.get("candidateId")
            and directive.get("amapPoiId")
            and not str(directive.get("requestedOutcome") or "").strip()
        ):
            directive["requestedOutcome"] = "replace segment POI with persisted candidate"
            aliases_applied.append("requestedOutcome:derived_from_candidate_patch")
        self._validate_version_and_identity(directive, context)
        return DecisionNormalizationResult(normalized=normalized, aliases_applied=sorted(set(aliases_applied)))

    def _normalize_ask_user_semantic_aliases(
        self,
        directive: dict[str, Any],
        context: dict[str, Any],
    ) -> list[str]:
        """Rename proven string aliases without authoring spatial meaning.

        The Controller occasionally uses natural JSON property aliases even
        when the projected contract contains the canonical field names.  Only
        two text-property aliases are safe to normalize: neither operation
        chooses a place, radius, Provider identity, or coordinate.  Abstract
        objects (for example ``{type: city_center}``) remain untouched so the
        strict spatial validator rejects them.
        """

        dimensions = {
            str(item.get("dimensionId") or ""): item
            for item in context.get("clarificationDimensions") or []
            if isinstance(item, dict) and str(item.get("dimensionId") or "")
        }
        supplied_batch = directive.get("questions")
        if isinstance(supplied_batch, list) and supplied_batch:
            questions = [(index, item) for index, item in enumerate(supplied_batch)]
        else:
            questions = [(None, directive)]

        aliases: list[str] = []
        for question_index, question in questions:
            if not isinstance(question, dict):
                continue
            dimension_id = str(question.get("dimensionId") or "")
            dimension = dimensions.get(dimension_id)
            if not isinstance(dimension, dict) or str(dimension.get("status") or "unresolved") != "unresolved":
                continue
            allowed_fields = {str(item) for item in dimension.get("allowedSemanticFields") or []}
            semantic_options = [
                item for item in dimension.get("semanticOptions") or [] if isinstance(item, dict)
            ]
            if semantic_options:
                question_path = (
                    f"actionDirective.questions[{question_index}]" if question_index is not None else "actionDirective"
                )
                supplied_options = question.get("options")
                expected_ids = [str(item.get("id") or "") for item in semantic_options]
                supplied_ids = [
                    str(item.get("id") or "") for item in supplied_options or [] if isinstance(item, dict)
                ]
                if not isinstance(supplied_options, list) or supplied_ids != expected_ids:
                    raise DecisionNormalizationError(
                        "server_semantic_option_identity_mismatch",
                        f"{question_path}.options",
                    )
                semantic_by_id = {str(item["id"]): deepcopy(item.get("semanticValue")) for item in semantic_options}
                for option_index, option in enumerate(supplied_options):
                    if "semanticValue" in option:
                        raise DecisionNormalizationError(
                            "controller_semantic_value_not_allowed",
                            f"{question_path}.options[{option_index}].semanticValue",
                        )
                    option["semanticValue"] = semantic_by_id[str(option["id"])]
                expected_allow_free_text = bool(dimension.get("allowFreeText"))
                supplied_allow_free_text = question.get("allowFreeText")
                if supplied_allow_free_text is not None and bool(supplied_allow_free_text) != expected_allow_free_text:
                    raise DecisionNormalizationError(
                        "controller_allow_free_text_mismatch",
                        f"{question_path}.allowFreeText",
                    )
                question["allowFreeText"] = expected_allow_free_text
                aliases.append(f"{question_path}.options:server_semantics_injected")
            if "spatialResolutionInput" not in allowed_fields:
                continue
            options = question.get("options")
            if not isinstance(options, list):
                continue
            question_path = (
                f"actionDirective.questions[{question_index}]" if question_index is not None else "actionDirective"
            )
            for option_index, option in enumerate(options):
                if not isinstance(option, dict):
                    continue
                semantic_value = option.get("semanticValue")
                if not isinstance(semantic_value, dict):
                    continue
                spatial_input = semantic_value.get("spatialResolutionInput")
                if not isinstance(spatial_input, dict):
                    continue
                spatial_path = f"{question_path}.options[{option_index}].semanticValue.spatialResolutionInput"
                kind = str(spatial_input.get("kind") or "")
                if kind == "reference_point_radius":
                    aliases.extend(
                        self._normalize_nonempty_text_alias(
                            spatial_input,
                            alias="referencePoint",
                            canonical="referenceText",
                            path=spatial_path,
                        )
                    )
                elif kind == "administrative_area":
                    aliases.extend(
                        self._normalize_nonempty_text_alias(
                            spatial_input,
                            alias="area",
                            canonical="administrativeAreaText",
                            path=spatial_path,
                        )
                    )
        return aliases

    @staticmethod
    def _normalize_nonempty_text_alias(
        value: dict[str, Any],
        *,
        alias: str,
        canonical: str,
        path: str,
    ) -> list[str]:
        if alias not in value:
            return []
        alias_value = value.get(alias)
        if not isinstance(alias_value, str) or not alias_value.strip():
            return []
        normalized_alias = alias_value.strip()
        if canonical in value:
            canonical_value = value.get(canonical)
            if not isinstance(canonical_value, str) or canonical_value.strip() != normalized_alias:
                raise DecisionNormalizationError("conflicting_alias", f"{path}.{canonical}")
        value[canonical] = normalized_alias
        value.pop(alias, None)
        return [f"{path}.{alias}:{canonical}"]

    def _normalize_resolve_poi(self, directive: dict[str, Any], context: dict[str, Any]) -> list[str]:
        unknown = set(directive) - self.RESOLVE_CANONICAL_FIELDS - self.RESOLVE_ALIAS_FIELDS
        if unknown:
            field = sorted(unknown)[0]
            raise DecisionNormalizationError("unknown_dangerous_field", f"actionDirective.{field}")

        aliases: list[str] = []
        canonical_segments = self._segment_values(directive.get("targetSegmentIds"))
        alias_segments: list[tuple[str, str]] = []
        for key in ("segmentId", "targetSegmentId"):
            if key in directive:
                aliases.append(key)
                value = str(directive.get(key) or "").strip()
                if value:
                    alias_segments.append((key, value))
        distinct_alias_segments = {value for _, value in alias_segments}
        if len(distinct_alias_segments) > 1 or (
            canonical_segments and distinct_alias_segments and set(canonical_segments) != distinct_alias_segments
        ):
            raise DecisionNormalizationError("conflicting_alias", "actionDirective.targetSegmentIds")
        segments = canonical_segments or sorted(distinct_alias_segments)
        if not segments:
            segments = []

        canonical_search = str(directive.get("searchIntent") or "").strip()
        alias_search: list[tuple[str, str]] = []
        for key in ("searchText", "searchQuery", "query"):
            if key in directive:
                aliases.append(key)
                value = str(directive.get(key) or "").strip()
                if value:
                    alias_search.append((key, value))
        distinct_search = {value for _, value in alias_search}
        if len(distinct_search) > 1 or (canonical_search and distinct_search and {canonical_search} != distinct_search):
            raise DecisionNormalizationError("conflicting_alias", "actionDirective.searchIntent")
        search_intent = canonical_search or next(iter(distinct_search), "")
        if not search_intent:
            raise DecisionNormalizationError("invalid_canonical_value", "actionDirective.searchIntent")
        valid_segments = self._segment_day_map(context)
        requested_day_number = context.get("requestedDayNumber")
        for index, segment_id in enumerate(segments):
            if segment_id not in valid_segments:
                raise DecisionNormalizationError(
                    "invented_or_stale_target", f"actionDirective.targetSegmentIds[{index}]"
                )
            if requested_day_number is not None and valid_segments.get(segment_id) != requested_day_number:
                raise DecisionNormalizationError(
                    "explicit_user_day_mismatch",
                    f"actionDirective.targetSegmentIds[{index}]",
                )
        if "dayNumber" in directive:
            aliases.append("dayNumber")
            day_number = directive.get("dayNumber")
            if segments and any(valid_segments.get(segment_id) != day_number for segment_id in segments):
                raise DecisionNormalizationError("day_segment_mismatch", "actionDirective.dayNumber")

        directive["targetSegmentIds"] = segments
        directive["searchIntent"] = search_intent
        for key in self.RESOLVE_ALIAS_FIELDS:
            directive.pop(key, None)
        return aliases

    def _normalize_candidate_identity_alias(self, directive: dict[str, Any], context: dict[str, Any]) -> list[str]:
        candidate_id = str(directive.get("candidateId") or "")
        amap_poi_id = str(directive.get("amapPoiId") or "")
        if not candidate_id or not amap_poi_id or candidate_id != amap_poi_id:
            return []
        pairs = self._candidate_pairs(context)
        matches = {(str(group_id), str(amap_id)) for group_id, amap_id in pairs if str(amap_id) == amap_poi_id}
        if len(matches) != 1:
            return []
        group_id, _ = next(iter(matches))
        if not group_id or group_id == candidate_id:
            return []
        directive["candidateId"] = group_id
        return [f"candidateId:{candidate_id}->{group_id}"]

    def _validate_version_and_identity(self, directive: dict[str, Any], context: dict[str, Any]) -> None:
        base_version = directive.get("baseVersionId")
        active_version = context.get("activeVersionId")
        if base_version and active_version and base_version != active_version:
            raise DecisionNormalizationError("stale_base_version", "actionDirective.baseVersionId")
        candidate_id = directive.get("candidateId")
        amap_poi_id = directive.get("amapPoiId")
        if not candidate_id and not amap_poi_id:
            return
        valid_pairs = self._candidate_pairs(context)
        if (candidate_id, amap_poi_id) not in valid_pairs:
            raise DecisionNormalizationError("candidate_identity_not_in_safe_group", "actionDirective.candidateId")

    @staticmethod
    def _segment_values(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    @staticmethod
    def _segment_day_map(context: dict[str, Any]) -> dict[str, Any]:
        refs = context.get("segmentRefs") or context.get("segment_refs") or []
        result: dict[str, Any] = {}
        for item in refs:
            if not isinstance(item, dict):
                continue
            segment_id = item.get("segmentId") or item.get("id")
            if segment_id:
                result[str(segment_id)] = item.get("dayNumber")
        return result

    @staticmethod
    def _candidate_pairs(context: dict[str, Any]) -> set[tuple[Any, Any]]:
        observation = context.get("agentObservation") if isinstance(context.get("agentObservation"), dict) else {}
        candidate_state = (
            observation.get("candidateState") if isinstance(observation.get("candidateState"), dict) else {}
        )
        groups = (
            context.get("candidateGroups")
            or context.get("pendingCandidateGroups")
            or candidate_state.get("pendingGroups")
            or context.get("pendingAmapPoiCandidates")
            or []
        )
        pairs: set[tuple[Any, Any]] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = group.get("id") or group.get("candidateId")
            candidate_ids = group.get("candidateIds") or []
            amap_ids = group.get("amapPoiIds") or []
            if len(candidate_ids) == len(amap_ids):
                pairs.update(zip(candidate_ids, amap_ids))
            candidates = group.get("candidates") or group.get("safeCandidates") or []
            for candidate in candidates:
                if isinstance(candidate, dict):
                    amap_id = candidate.get("amapPoiId") or candidate.get("id") or candidate.get("amapId")
                    pairs.add((candidate.get("candidateId") or group_id, amap_id))
        return pairs
