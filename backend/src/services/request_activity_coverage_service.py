"""Compile one Full semantic coverage proposal before the first request freeze.

Clause splitting is punctuation-only, not a travel intent classifier. The model
classifies every server supplied clause; the host owns spans, goal identities,
supported activity types and the resulting occurrence contract.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CoveredActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    goal_id: str = Field(alias="goalId", min_length=1)
    source_text: str = Field(alias="sourceText", min_length=1, max_length=1200)
    intent_type: Literal[
        "campus_visit", "museum", "landmark", "park", "area_walk",
        "local_culture", "meal", "shopping", "night_view", "rest",
    ] = Field(alias="intentType")
    polarity: Literal["required", "excluded"]
    allowed_day_numbers: list[int] = Field(alias="allowedDayNumbers", min_length=1, max_length=14)
    min_count: int = Field(alias="minCount", ge=1, le=14)
    day_part: Literal["morning", "noon", "afternoon", "evening", "night", "flexible"] = Field(alias="dayPart")
    exact_entity: Optional[str] = Field(default=None, alias="exactEntity", min_length=1, max_length=200)


class RequestClauseCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    clause_id: str = Field(alias="clauseId")
    classification: Literal["activity", "constraint", "context", "instruction", "mixed", "unresolved"]
    activities: list[CoveredActivity] = Field(default_factory=list, max_length=2)


class RequestActivityCoverageError(ValueError):
    pass


class RequiredGoalOptionalConflictError(RequestActivityCoverageError):
    """A model cannot downgrade an authoritative obligation by moving buckets."""

    def __init__(self, goal_id: str):
        self.goal_id = goal_id
        super().__init__("draft_required_goal_misclassified_as_optional:" + goal_id)


class RequestActivityCoverageService:
    KEY = "requestActivityCoverage"

    @classmethod
    def prepare(cls, contract: dict, message: str) -> dict:
        if contract.get("planningRequestEnvelope") or contract.get(cls.KEY):
            raise RequestActivityCoverageError("request_activity_coverage_frozen")
        if not message.strip() or len(message) > 1200:
            raise RequestActivityCoverageError("request_activity_coverage_source_size_invalid")
        clauses = []
        for match in re.finditer(r"[^，,。；;！？!?\n]+", message):
            raw = match.group()
            value = raw.strip()
            if not value:
                continue
            index = len(clauses) + 1
            start = match.start() + len(raw) - len(raw.lstrip())
            clauses.append({
                "clauseId": f"request_clause_{index}", "text": value,
                "start": start, "end": start + len(value),
                "goalIds": [f"goal_request_{index}_{activity}" for activity in (1, 2)],
            })
        if not clauses or len(clauses) > 32:
            raise RequestActivityCoverageError("request_activity_coverage_clause_limit")
        result = deepcopy(contract)
        result[cls.KEY] = {
            "schemaVersion": "request-activity-coverage-v1", "status": "pending",
            "sourceText": message, "sourceFingerprint": sha256(message.encode("utf-8")).hexdigest(),
            "clauses": clauses,
        }
        return result

    @classmethod
    def compile(cls, contract: dict, proposal: object) -> dict:
        coverage = contract.get(cls.KEY) or {}
        if contract.get("planningRequestEnvelope") or coverage.get("status") != "pending":
            raise RequestActivityCoverageError("request_activity_coverage_frozen")
        clauses = coverage.get("clauses") or []
        if not isinstance(proposal, list) or len(proposal) != len(clauses):
            raise RequestActivityCoverageError("request_activity_coverage_clause_missing")
        try:
            entries = [RequestClauseCoverage.model_validate(item) for item in proposal]
        except (ValidationError, TypeError) as error:
            raise RequestActivityCoverageError("request_activity_coverage_schema_invalid") from error
        if [entry.clause_id for entry in entries] != [clause["clauseId"] for clause in clauses]:
            raise RequestActivityCoverageError("request_activity_coverage_clause_identity_invalid")
        source = str(coverage.get("sourceText") or "")
        if sha256(source.encode("utf-8")).hexdigest() != coverage.get("sourceFingerprint"):
            raise RequestActivityCoverageError("request_activity_coverage_source_fingerprint_invalid")
        day_count = int(contract.get("dayCount") or 1)
        mapped, validated_activities = [], []
        seen_goals = set()
        for clause, entry in zip(clauses, entries):
            if source[clause["start"]:clause["end"]] != clause["text"]:
                raise RequestActivityCoverageError("request_activity_coverage_source_span_invalid")
            if entry.classification == "unresolved":
                raise RequestActivityCoverageError("request_activity_coverage_unresolved")
            if bool(entry.activities) != (entry.classification in {"activity", "mixed"}):
                raise RequestActivityCoverageError("request_activity_coverage_activity_mapping_invalid")
            for activity in entry.activities:
                if activity.goal_id not in clause["goalIds"] or activity.goal_id in seen_goals:
                    raise RequestActivityCoverageError("request_activity_coverage_goal_identity_invalid")
                seen_goals.add(activity.goal_id)
                if activity.source_text not in clause["text"]:
                    raise RequestActivityCoverageError("request_activity_coverage_source_span_invalid")
                if activity.exact_entity is not None and (
                    not activity.exact_entity.strip()
                    or activity.exact_entity != activity.exact_entity.strip()
                    or activity.exact_entity not in activity.source_text
                    or activity.polarity != "required"
                ):
                    # A name is source-bound intent, never a canonical POI or
                    # permission to exclude an entire activity category.
                    raise RequestActivityCoverageError("request_activity_coverage_exact_entity_invalid")
                days = activity.allowed_day_numbers
                if len(days) != len(set(days)) or any(day < 1 or day > day_count for day in days):
                    raise RequestActivityCoverageError("request_activity_coverage_day_invalid")
                if activity.min_count > len(days):
                    raise RequestActivityCoverageError("request_activity_coverage_occurrence_unsupported")
                span_start = clause["start"] + clause["text"].index(activity.source_text)
                mapped.append({
                    **activity.model_dump(by_alias=True), "clauseId": entry.clause_id,
                    "start": span_start, "end": span_start + len(activity.source_text),
                })
                validated_activities.append((entry.clause_id, activity))
        # Validate every supplied identity/span first. This compares only exact
        # structured evidence, not semantic similarity, and never merges goals.
        activity_signatures = {}
        for activity in mapped:
            signature = (
                activity["clauseId"], activity["start"], activity["end"], activity["intentType"],
                activity["polarity"], tuple(sorted(activity["allowedDayNumbers"])),
                activity["minCount"], activity["dayPart"],
            )
            if signature in activity_signatures:
                raise RequestActivityCoverageError(
                    "request_activity_coverage_duplicate_activity:"
                    + activity["clauseId"] + ":" + activity_signatures[signature] + "," + activity["goalId"]
                )
            activity_signatures[signature] = activity["goalId"]
        old_by_intent = {
            item["intentType"]: item for item in contract.get("requiredIntents") or []
            if isinstance(item, dict) and item.get("intentType")
        }
        old_goals = [item for item in contract.get("requiredIntents") or [] if isinstance(item, dict) and item.get("intentType")]
        if len(old_by_intent) != len(old_goals):
            raise RequestActivityCoverageError("request_activity_coverage_legacy_goal_mapping_ambiguous")
        required, excluded = [], []
        for clause_id, activity in validated_activities:
            if activity.polarity == "excluded":
                excluded.append(activity.intent_type)
                continue
            days = activity.allowed_day_numbers
            goal = deepcopy(old_by_intent.get(activity.intent_type) or {})
            existing_entity = str(goal.get("exactEntity") or "").strip()
            if existing_entity and (
                existing_entity not in activity.source_text
                or (activity.exact_entity is not None and activity.exact_entity != existing_entity)
            ):
                raise RequestActivityCoverageError("request_activity_coverage_exact_entity_conflict")
            exact_entity = activity.exact_entity or existing_entity
            if exact_entity:
                goal.update(exactEntity=exact_entity, entityBindingMode="exact_entity", explicitlyNamed=True)
            goal.update({
                "goalId": activity.goal_id, "intentType": activity.intent_type,
                "target": activity.min_count, "requiredMin": activity.min_count,
                "minCount": activity.min_count, "preferredCount": activity.min_count,
                "maxCount": activity.min_count, "requirementLevel": "required",
                "priorityTier": "hard", "userExplicit": True,
                "source": "full_source_clause_coverage", "cardinalitySource": "full_source_clause_coverage",
                "distributionPolicy": "every_allowed_day" if activity.min_count == len(days) else "one_of_allowed_days",
                "allowedDayNumbers": sorted(days), "rawNeed": activity.source_text,
                "sourceClauseId": clause_id,
                "schedulePreference": {
                    "dayPart": activity.day_part, "userExplicit": True, "priority": "hard",
                    "sourceGoalId": activity.goal_id, "preferredDayNumbers": sorted(days),
                },
            })
            required.append(goal)
        if not required:
            raise RequestActivityCoverageError("request_activity_coverage_no_supported_activity")
        if set(excluded).intersection(item["intentType"] for item in required):
            raise RequestActivityCoverageError("request_activity_coverage_polarity_conflict")
        goal_rebinding = {}
        for old in old_goals:
            intent = old["intentType"]
            matches = [item for item in required if item["intentType"] == intent]
            old_min = int(old.get("requiredMin") or old.get("minCount") or old.get("target") or 0)
            is_hard = old_min > 0 and old.get("requirementLevel") not in {"soft_experience", "optional"}
            # Coverage may add missing obligations, but it has no authority to
            # delete a pre-existing hard goal. Genuine corrections must use the
            # existing semantic-edit authorization, not an incidental negation.
            if is_hard and not matches and intent in excluded:
                raise RequestActivityCoverageError("request_activity_coverage_hard_goal_exclusion_requires_edit:" + str(old.get("goalId") or intent))
            if is_hard and (not matches or sum(item["requiredMin"] for item in matches) < old_min):
                raise RequestActivityCoverageError("request_activity_coverage_hard_goal_omitted:" + str(old.get("goalId") or intent))
            old_days = set(old.get("allowedDayNumbers") or [])
            old_part = (old.get("schedulePreference") or {}).get("dayPart")
            if is_hard and any(
                (old_days and not set(item["allowedDayNumbers"]).issubset(old_days))
                or (old_part and old_part != "flexible" and item["schedulePreference"]["dayPart"] != old_part)
                for item in matches
            ):
                raise RequestActivityCoverageError("request_activity_coverage_hard_goal_scope_changed:" + str(old.get("goalId") or intent))
            if old.get("goalId") and matches:
                if len(matches) != 1:
                    raise RequestActivityCoverageError("request_activity_coverage_legacy_goal_mapping_ambiguous")
                goal_rebinding[old["goalId"]] = matches[0]["goalId"]
        result = deepcopy(contract)
        result.update({
            "requiredIntents": required, "optionalIntents": [],
            "lockedEntities": list(dict.fromkeys([
                *(contract.get("lockedEntities") or []),
                *(item["exactEntity"] for item in required if item.get("exactEntity")),
            ])),
            "negativeConstraints": list(dict.fromkeys([*(contract.get("negativeConstraints") or []), *excluded])),
            "userRequestedThemes": list(dict.fromkeys(item["intentType"] for item in required)),
        })
        result.pop("provisionalGoalOccurrenceProjection", None)
        def rebind(value):
            if isinstance(value, dict):
                return {key: goal_rebinding.get(item, item) if key in {"goalId", "sourceGoalId"} and isinstance(item, str) else rebind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rebind(item) for item in value]
            return value
        result = rebind(result)
        result[cls.KEY] = {**deepcopy(coverage), "status": "complete", "classifiedClauses": deepcopy(proposal), "activities": mapped, "goalRebinding": goal_rebinding}
        return result

    @classmethod
    def validate_directive(cls, contract: dict, directive: dict) -> None:
        if (contract.get(cls.KEY) or {}).get("status") != "complete":
            raise RequestActivityCoverageError("request_activity_coverage_not_complete")
        hints = {
            (item.get("goalId"), item.get("dayNumber")): item
            for item in directive.get("occurrenceScheduleHints") or [] if isinstance(item, dict)
        }
        placements = {}
        hard_goal_ids = {goal["goalId"] for goal in contract["requiredIntents"]
                         if int(goal.get("requiredMin") or 0) > 0
                         and goal.get("requirementLevel") not in {"soft_experience", "optional"}}
        for day in directive.get("dayStrategies") or []:
            conflicts = sorted(hard_goal_ids.intersection(day.get("optionalGoalIds") or []))
            if conflicts:
                raise RequiredGoalOptionalConflictError(conflicts[0])
            for goal in day.get("requiredGoalIds") or []:
                placements.setdefault(goal, []).append(day.get("dayNumber"))
        for goal in contract["requiredIntents"]:
            days = placements.get(goal["goalId"], [])
            if len(days) != goal["requiredMin"] or not set(days).issubset(goal["allowedDayNumbers"]):
                raise RequestActivityCoverageError("request_activity_coverage_directive_omitted")
            part = goal["schedulePreference"]["dayPart"]
            for day in days:
                hint = hints.get((goal["goalId"], day)) or {}
                if part != "flexible" and hint.get("dayPart") != part:
                    raise RequestActivityCoverageError("request_activity_coverage_directive_time_mismatch")
