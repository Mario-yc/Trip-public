from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable, get_args, get_origin

from pydantic import BaseModel

from src.services.agent_action_directive import (
    AskUserDirective,
    DraftItineraryDirective,
    FinishDirective,
    OptimizeRouteDirective,
    PatchDirective,
    ReadItineraryDirective,
    ResolvePoiDirective,
    VerifyExternalFactsDirective,
)


ACTION_MODELS: dict[str, type[BaseModel]] = {
    "ask_user": AskUserDirective,
    "draft_itinerary": DraftItineraryDirective,
    "finish": FinishDirective,
    "optimize_route": OptimizeRouteDirective,
    "patch_itinerary": PatchDirective,
    "read_itinerary": ReadItineraryDirective,
    "resolve_poi": ResolvePoiDirective,
    "verify_external_facts": VerifyExternalFactsDirective,
}


class AgentDecisionContractService:
    """Builds the model, validator and repair contract from Pydantic models."""

    CONTRACT_VERSION = "agent-decision-contract-v3"
    NORMALIZATION_GUIDANCE = {
        "resolve_poi": {
            "segmentId": "targetSegmentIds",
            "targetSegmentId": "targetSegmentIds",
            "searchText": "searchIntent",
            "searchQuery": "searchIntent",
            "query": "searchIntent",
            "dayNumber": "validationEvidenceOnly",
        }
    }

    def __init__(self, *, decision_model: type[BaseModel], allowed_actions: Iterable[str]):
        self.decision_model = decision_model
        self.allowed_actions = tuple(sorted(set(allowed_actions)))

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def hash_schema(cls, value: Any) -> str:
        return sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    @classmethod
    def full_draft_type_contract(cls) -> dict[str, str]:
        """Describe Full's exact draft validation types without a second schema.

        Only JSON-schema annotations (not validation rules) are omitted. New
        schema mechanisms fail explicitly until the renderer supports them.
        The server continues validating with the original Pydantic model.
        """
        schema = DraftItineraryDirective.model_json_schema(by_alias=True)
        return {
            "actionSchemaHash": cls.hash_schema(schema),
            "guide": cls._schema_type_guide(schema, DraftItineraryDirective.__name__),
        }

    @classmethod
    def full_draft_compact_output_profile(cls) -> dict[str, Any]:
        """Describe safe producer omissions; never transform a model response.

        JSON Schema does not serialize Pydantic default factories, so collect
        defaults from the same model fields that generate the exact schema.
        Optional fields with contextual business obligations are deliberately
        excluded from the omission table, including their nullable defaults.
        """
        schema = DraftItineraryDirective.model_json_schema(by_alias=True)
        business_required = {
            "actionDirective.requestCoverage": "every supplied clause while coverage is pending",
            "actionDirective.dayStrategies[].requiredGoalIds": "every required goal assigned to that day",
            "actionDirective.occurrenceScheduleHints": "exactly one hint per scheduled goal/day",
            "actionDirective.occurrenceScheduleHints[].preferredStartTime": "non-evening/night hints",
            "actionDirective.routePlanningPolicy": "when the route policy requirement requires it",
            "actionDirective.routePlanningPolicy.mobilityProfile": "when the route policy requires mobility",
            "actionDirective.routePlanningPolicy.detourEnvelope": "when the route policy requires detour bounds",
        }
        defaults: dict[str, Any] = {}

        def nested_models(annotation: Any, suffix: str = "") -> Iterable[tuple[type[BaseModel], str]]:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                yield annotation, suffix
                return
            for argument in get_args(annotation):
                yield from nested_models(argument, suffix + ("[]" if get_origin(annotation) is list else ""))

        def visit(model: type[BaseModel], path: str, ancestors: tuple[type[BaseModel], ...]) -> None:
            if model in ancestors:
                raise ValueError("full_draft_omission_recursive_model")
            for name, field in model.model_fields.items():
                field_path = path + "." + str(field.alias or name)
                if not field.is_required() and field_path not in business_required:
                    value = field.get_default(call_default_factory=True)
                    if isinstance(value, BaseModel):
                        value = value.model_dump(mode="json", by_alias=True)
                    defaults[field_path] = json.loads(cls._canonical_json(value))
                for child, suffix in nested_models(field.annotation):
                    visit(child, field_path + suffix, (*ancestors, model))

        visit(DraftItineraryDirective, "actionDirective", ())
        empty_fields = sorted({path.rsplit(".", 1)[-1] for path, value in defaults.items() if value in ([], {})})
        scalar_defaults = [
            path.rsplit(".", 1)[-1] + "=" + cls._canonical_json(value)
            for path, value in defaults.items()
            if value is not None and not isinstance(value, (dict, list, bool))
        ]
        guide = (
            "Compact draft: omit optional values only when equal to their existing defaults; keep non-default decisions. "
            "Empty fields may be omitted: " + ",".join(empty_fields) + ". "
            "Scalar defaults: " + ",".join(scalar_defaults) + ". "
            "Do not restate unchanged candidateSelectionPolicy/schedulePolicy. "
            "Business obligations override optional status: retain every pending requestCoverage clause, requiredGoalIds, "
            "every scheduled goal/day hint, required start times, and required route policy/children. "
            "Preserve sourceText, IDs, counts, days and time semantics. This is output guidance, never permission to repair truncated JSON."
        )
        return {
            "actionSchemaHash": cls.hash_schema(schema),
            "omissionDefaults": defaults,
            "businessRequiredPaths": business_required,
            "guide": guide,
        }

    @classmethod
    def _schema_type_guide(cls, schema: dict[str, Any], root_name: str) -> str:
        annotations = {"title", "description", "default", "examples"}
        constraints = {
            "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
            "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems",
            "uniqueItems", "minProperties", "maxProperties",
        }
        structure = {"$ref", "type", "properties", "required", "additionalProperties", "items", "anyOf"}
        primitives = {"integer": "int", "number": "number", "string": "string", "boolean": "bool", "null": "null"}
        definitions = schema.get("$defs", {})
        if not isinstance(definitions, dict) or root_name in definitions:
            raise ValueError("full_draft_type_schema_invalid_definitions")

        def fail(path: str, reason: str) -> None:
            raise ValueError(f"full_draft_type_schema_unsupported:{path}:{reason}")

        def render(node: Any, path: str) -> str:
            if isinstance(node, bool):
                return "any" if node else "never"
            if not isinstance(node, dict):
                fail(path, "non_schema")
            unknown = set(node) - annotations - constraints - structure
            if unknown:
                fail(path, ",".join(sorted(unknown)))
            core = set(node) & structure
            if "$ref" in node:
                reference = node["$ref"]
                if (core != {"$ref"} or not isinstance(reference, str)
                        or not reference.startswith("#/$defs/") or reference[8:] not in definitions):
                    fail(path, "reference")
                value = reference[8:]
            elif "anyOf" in node:
                branches = node["anyOf"]
                if core != {"anyOf"} or not isinstance(branches, list) or not branches:
                    fail(path, "anyOf")
                value = "(" + "|".join(render(branch, f"{path}/anyOf/{index}") for index, branch in enumerate(branches)) + ")"
            elif node.get("type") == "object":
                if core - {"type", "properties", "required", "additionalProperties"}:
                    fail(path, "object")
                properties = node.get("properties", {})
                required = node.get("required", [])
                if (not isinstance(properties, dict) or not isinstance(required, list)
                        or len(set(required)) != len(required) or not set(required).issubset(properties)):
                    fail(path, "required_properties")
                extra = node.get("additionalProperties", True)
                if not properties and extra is not False:
                    value = "map<string," + render(extra, path + "/additionalProperties") + ">"
                else:
                    fields = []
                    for name, definition in properties.items():
                        label = name if name.isidentifier() else cls._canonical_json(name)
                        fields.append(label + ("" if name in required else "?") + ":" + render(definition, path + "/properties/" + name))
                    value = "{" + ",".join(fields) + "}"
                    value += "!" if extra is False else "+<" + render(extra, path + "/additionalProperties") + ">"
            elif node.get("type") == "array":
                if core - {"type", "items"}:
                    fail(path, "array")
                value = "[" + render(node.get("items", True), path + "/items") + "]"
            elif core.issubset({"type"}):
                kind = node.get("type")
                if kind is not None and (not isinstance(kind, str) or kind not in primitives):
                    fail(path, "type")
                value = primitives.get(kind, "any")
            else:
                fail(path, "combined_schema")
            rules = {key: node[key] for key in sorted(constraints) if key in node}
            return value + ("@" + cls._canonical_json(rules) if rules else "")

        root = {key: value for key, value in schema.items() if key != "$defs"}
        guide = [
            "JSON types: field? may be omitted; null is separate. [T]=array; (A|B)=anyOf; "
            "map<string,T>=arbitrary keys; {...}!=only listed keys; {...}+<T>=extra keys of T. "
            "@{...}=JSON Schema constraints; int=integer; bool=boolean; any/never=unrestricted/impossible. "
            "Names reference definitions below. Defaults/descriptions omitted. Output JSON, not this grammar.",
            root_name + "=" + render(root, root_name),
        ]
        for name, definition in sorted(definitions.items()):
            if not name.isidentifier():
                fail("$defs", "definition_name")
            guide.append(name + "=" + render(definition, "$defs/" + name))
        return "\n".join(guide)

    def build(self) -> dict[str, Any]:
        decision_schema = self.decision_model.model_json_schema(by_alias=True)
        action_schemas = {
            action: ACTION_MODELS[action].model_json_schema(by_alias=True)
            for action in self.allowed_actions
            if action in ACTION_MODELS
        }
        allowed_actions_hash = self.hash_schema(list(self.allowed_actions))
        action_schema_hashes = {action: self.hash_schema(schema) for action, schema in action_schemas.items()}
        contract_core = {
            "contractVersion": self.CONTRACT_VERSION,
            "decisionSchema": decision_schema,
            "allowedActions": list(self.allowed_actions),
            "allowedActionsHash": allowed_actions_hash,
            "actionSchemas": action_schemas,
            "actionSchemaHashes": action_schema_hashes,
            "normalizationGuidance": self.NORMALIZATION_GUIDANCE,
        }
        return {**contract_core, "contractHash": self.hash_schema(contract_core)}

    @classmethod
    def deterministic_draft_directive(
        cls,
        goal_requirements: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compile the server-authored cardinality example into a draft directive.

        This is intentionally identity-free: it distributes ledger goals across
        allowed days but never invents a POI, candidate, route, or write result.
        """

        example = cls._minimal_example("draft_itinerary", {}, list(goal_requirements or []))
        directive = example.get("actionDirective")
        if not isinstance(directive, dict) or not directive.get("dayStrategies"):
            raise ValueError("deterministic_draft_directive_unavailable")
        return directive

    def repair_payload(
        self,
        *,
        action: str,
        invalid_paths: list[str],
        allowed_ids: dict[str, list[str]],
        aliases_applied: list[str],
        goal_requirements: list[dict[str, Any]] | None = None,
        route_policy_requirement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract = self.build()
        exact_action_schema = contract["actionSchemas"].get(action, {})
        is_draft_repair = action == "draft_itinerary"
        # Goal cardinality, scheduling rules, and route estimates are authority
        # only for draft_itinerary. Including them in ask_user repair duplicated
        # a large unrelated contract and could exhaust the fixed Controller
        # request budget before a valid clarification repair reached the model.
        repair_goal_requirements = (
            self._repair_goal_requirements(goal_requirements or [])
            if is_draft_repair
            else []
        )
        repair_route_policy_requirement = (
            self._repair_route_policy_requirement(route_policy_requirement)
            if is_draft_repair
            else None
        )
        hard_required_goals = [
            item
            for item in repair_goal_requirements
            if int(item.get("requiredMin") or 0) > 0
            and item.get("requirementLevel") not in {"soft_experience", "optional"}
        ]
        optional_goals = [
            item for item in repair_goal_requirements if item.get("requirementLevel") in {"soft_experience", "optional"}
        ]
        action_schema = self._repair_action_schema(
            action,
            exact_action_schema,
            route_policy_required=bool(repair_route_policy_requirement),
        )
        required_field_checklist = self._repair_required_field_checklist(
            action,
            exact_action_schema,
        )
        if repair_route_policy_requirement:
            required_field_checklist = {
                **required_field_checklist,
                "actionDirective.routePlanningPolicy": [
                    "source",
                    "objective",
                    "allowExperienceDetour",
                    *(
                        ["mobilityProfile"]
                        if repair_route_policy_requirement["mobilityProfile"]["required"]
                        else []
                    ),
                    *(
                        ["detourEnvelope"]
                        if repair_route_policy_requirement["detourEnvelope"]["required"]
                        else []
                    ),
                ],
                **(
                    {
                        "actionDirective.routePlanningPolicy.mobilityProfile": [
                            "transportMode",
                            "paceClass",
                        ]
                    }
                    if repair_route_policy_requirement["mobilityProfile"]["required"]
                    else {}
                ),
                **(
                    {
                        "actionDirective.routePlanningPolicy.detourEnvelope": [
                            "maxGeneralizedCostDelta",
                            "maxDetourRatio",
                        ]
                    }
                    if repair_route_policy_requirement["detourEnvelope"]["required"]
                    else {}
                ),
            }
        payload = {
            "contractVersion": contract["contractVersion"],
            "contractHash": contract["contractHash"],
            "allowedActions": contract["allowedActions"],
            "action": action,
            # The exact schema remains bound by actionSchemaHash and every
            # repaired object is revalidated with the full Pydantic model.
            # DraftItineraryDirective's expanded JSON schema repeats nested
            # definitions already represented by the authoritative goal
            # contract and minimal example, pushing the one bounded repair
            # request over the unchanged Controller byte limit.  Send a
            # lossless field-level projection for that action only.
            "actionSchema": action_schema,
            "actionSchemaHash": contract["actionSchemaHashes"].get(action),
            # The byte-bounded action schema projection intentionally omits
            # repeated nested definitions. Preserve their required-field
            # contract separately so repair never has to infer required fields
            # from an example or from Pydantic defaults.
            "requiredFieldChecklist": required_field_checklist,
            # Pydantic reports the same structural failure once per list index.
            # Repair needs the contract path, not every occurrence index; stable
            # canonicalization keeps that evidence complete without letting a
            # live list cardinality consume the fixed request budget.
            "invalidPaths": self._repair_invalid_paths(invalid_paths),
            "allowedIds": allowed_ids,
            "aliasesApplied": sorted(set(aliases_applied)),
            "normalizationGuidance": contract["normalizationGuidance"].get(action, {}),
            "minimalExample": self._minimal_example(
                action,
                allowed_ids,
                repair_goal_requirements,
                route_policy_requirement=repair_route_policy_requirement,
            ),
        }
        if is_draft_repair:
            payload.update(
                {
                    # The source ledger carries search/access/freshness metadata
                    # irrelevant to repair. Keep only typed draft authority.
                    "goalRequirements": repair_goal_requirements,
                    "requiredGoalCounts": {
                        str(item["goalId"]): int(item.get("requiredMin") or 1)
                        for item in hard_required_goals
                        if item.get("goalId")
                    },
                    "goalCardinality": {
                        str(item["goalId"]): {
                            "minCount": int(item.get("requiredMin") or 0),
                            "preferredCount": int(
                                item.get("preferredCount") or item.get("requiredMin") or 0
                            ),
                            "maxCount": item.get("maxCount"),
                            "cardinalitySource": item.get("cardinalitySource"),
                            "distributionPolicy": item.get("distributionPolicy"),
                            "allowedDayNumbers": list(item.get("allowedDayNumbers") or []),
                        }
                        for item in repair_goal_requirements
                        if item.get("goalId")
                    },
                    "optionalGoalIds": [
                        str(item["goalId"]) for item in optional_goals if item.get("goalId")
                    ],
                    "draftSchedulingRules": {
                        "goalPriorityIsOrderingOnly": True,
                        "goalPriorityMayContainKnownHardOrSoftGoals": True,
                        "requiredGoalCountTotalsMustMeetRequiredGoalCounts": True,
                        "requiredGoalCountPerDayMustBeOneOrOmitted": True,
                        "requiredGoalCountTotalsMustNotExceedMaxCount": True,
                        "optionalGoalOccurrencesMustFitOptionalExperienceBudget": True,
                        "requiredGoalsMustUseAllowedDayNumbers": True,
                        "repeatedGoalsMustBeDistributedAcrossDistinctDays": True,
                        "softExperienceGoalsMayBeOptionalOrUnscheduled": True,
                        "eachScheduledGoalDayRequiresExactlyOneOccurrenceScheduleHint": True,
                        "occurrenceScheduleHintDuplicatesOrExtrasAllowed": False,
                        "nonEveningNightHintRequiresPreferredStartTime": True,
                        "eveningNightHintMayUseDateAndCoordinatesInsteadOfPreferredStartTime": True,
                        "everyOccurrenceScheduleHintRequiresPositiveDurationEstimate": True,
                        "everyDayStrategyRequiresNonEmptyTheme": True,
                        "everyOccurrenceScheduleHintRequiresEstimateSource": True,
                        "occurrenceScheduleHintEstimateSourceAllowedValues": self._draft_schedule_hint_estimate_sources(
                            exact_action_schema
                        ),
                    },
                }
            )
        if repair_route_policy_requirement:
            payload["routePlanningPolicyRequirement"] = repair_route_policy_requirement
        return payload

    @staticmethod
    def _repair_route_policy_requirement(
        requirement: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Project the contextual route requirement without copying request state."""

        if not isinstance(requirement, dict) or requirement.get("required") is not True:
            return None
        missing = sorted(
            {
                str(item)
                for item in requirement.get("missingFields") or []
                if str(item) in {"mobilityProfile", "detourTolerance"}
            }
        )
        if not missing:
            return None
        return {
            "required": True,
            "source": "controller_estimate",
            "missingFields": missing,
            "mobilityProfile": {
                "required": "mobilityProfile" in missing,
                "fields": ["transportMode", "paceClass"],
            },
            "detourEnvelope": {
                "required": "detourTolerance" in missing,
                "fields": ["maxGeneralizedCostDelta", "maxDetourRatio"],
            },
        }

    @staticmethod
    def _repair_invalid_paths(invalid_paths: Iterable[str]) -> list[str]:
        """Collapse repeated list indices while retaining every distinct contract path."""

        result: list[str] = []
        for value in invalid_paths:
            parts: list[str] = []
            for part in str(value or "").split("."):
                if not part:
                    continue
                if part.isdigit() and parts:
                    parts[-1] = f"{parts[-1]}[]"
                else:
                    parts.append(part)
            path = ".".join(parts)
            if path and path not in result:
                result.append(path)
        return result

    @staticmethod
    def _repair_goal_requirements(
        goal_requirements: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project the authoritative goal ledger to repair-relevant typed fields."""

        result: list[dict[str, Any]] = []
        for source in goal_requirements:
            if not isinstance(source, dict) or not source.get("goalId"):
                continue
            allowed_fields = (
                "goalId",
                "intentType",
                "requirementLevel",
                "requiredMin",
                "preferredCount",
                "maxCount",
                "cardinalitySource",
                "distributionPolicy",
                "allowedDayNumbers",
            )
            projected = {key: source[key] for key in allowed_fields if key in source}
            if "requiredMin" not in projected and "minCount" in source:
                projected["requiredMin"] = source.get("minCount")
            if "allowedDayNumbers" in projected:
                projected["allowedDayNumbers"] = list(projected.get("allowedDayNumbers") or [])
            result.append(projected)
        return result

    @staticmethod
    def _repair_action_schema(
        action: str,
        exact_schema: dict[str, Any],
        *,
        route_policy_required: bool = False,
    ) -> dict[str, Any]:
        if action != "draft_itinerary":
            return exact_schema
        properties = exact_schema.get("properties") if isinstance(exact_schema, dict) else {}
        if not isinstance(properties, dict):
            properties = {}
        projected_properties: dict[str, Any] = {}
        for field_name in (
            "type",
            "goalPriority",
            "dayStrategies",
            "optionalExperienceBudget",
            "searchPriority",
            "candidateSelectionPolicy",
            "schedulePolicy",
            "routePlanningPolicy",
            "occurrenceScheduleHints",
            "routeGapSupplementHints",
        ):
            field = properties.get(field_name)
            if not isinstance(field, dict):
                continue
            compact = {
                key: value
                for key, value in field.items()
                if key in {"type", "const", "enum", "minimum", "maximum", "minItems", "maxItems"}
            }
            if field_name in {
                "goalPriority",
                "dayStrategies",
                "searchPriority",
                "occurrenceScheduleHints",
                "routeGapSupplementHints",
            }:
                compact["type"] = "array"
            elif field_name in {"candidateSelectionPolicy", "schedulePolicy", "routePlanningPolicy"}:
                compact["type"] = (
                    "object"
                    if field_name != "routePlanningPolicy" or route_policy_required
                    else ["object", "null"]
                )
            projected_properties[field_name] = compact
        required_fields = list(exact_schema.get("required") or [])
        if route_policy_required and "routePlanningPolicy" not in required_fields:
            required_fields.append("routePlanningPolicy")
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required_fields,
            "properties": projected_properties,
            "validation": "full_server_pydantic_schema_after_repair",
        }

    @classmethod
    def _repair_required_field_checklist(
        cls,
        action: str,
        exact_schema: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Project nested required paths without copying the expanded schema."""

        if action != "draft_itinerary" or not isinstance(exact_schema, dict):
            return {}
        result: dict[str, list[str]] = {}

        def visit(node: Any, path: str, *, depth: int) -> None:
            if depth > 4 or not isinstance(node, dict):
                return
            resolved = cls._resolve_local_schema_ref(exact_schema, node)
            if not isinstance(resolved, dict):
                return
            required = [str(item) for item in resolved.get("required") or [] if str(item)]
            if required:
                result[path] = required
            properties = resolved.get("properties")
            if not isinstance(properties, dict):
                return
            for field_name, field_schema in properties.items():
                if not isinstance(field_schema, dict):
                    continue
                if isinstance(field_schema.get("items"), dict):
                    visit(field_schema["items"], f"{path}.{field_name}[]", depth=depth + 1)
                    continue
                if "$ref" in field_schema or isinstance(field_schema.get("properties"), dict):
                    visit(field_schema, f"{path}.{field_name}", depth=depth + 1)

        visit(exact_schema, "actionDirective", depth=0)
        return result

    @classmethod
    def _draft_schedule_hint_estimate_sources(cls, exact_schema: dict[str, Any]) -> list[str]:
        properties = exact_schema.get("properties") if isinstance(exact_schema, dict) else {}
        if not isinstance(properties, dict):
            return []
        hints = properties.get("occurrenceScheduleHints")
        if not isinstance(hints, dict) or not isinstance(hints.get("items"), dict):
            return []
        hint_schema = cls._resolve_local_schema_ref(exact_schema, hints["items"])
        hint_properties = hint_schema.get("properties") if isinstance(hint_schema, dict) else {}
        if not isinstance(hint_properties, dict):
            return []
        estimate_source = hint_properties.get("estimateSource")
        if not isinstance(estimate_source, dict):
            return []
        return [str(item) for item in estimate_source.get("enum") or [] if str(item)]

    @staticmethod
    def _resolve_local_schema_ref(root_schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        prefix = "#/$defs/"
        if not isinstance(ref, str) or not ref.startswith(prefix):
            return node
        definitions = root_schema.get("$defs")
        if not isinstance(definitions, dict):
            return {}
        target = definitions.get(ref[len(prefix) :])
        return target if isinstance(target, dict) else {}

    @staticmethod
    def _minimal_example(
        action: str,
        allowed_ids: dict[str, list[str]],
        goal_requirements: list[dict[str, Any]],
        *,
        route_policy_requirement: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        segment_id = next(iter(allowed_ids.get("segmentIds") or []), "<observed-segment-id>")
        version_id = next(iter(allowed_ids.get("versionIds") or []), None)
        required_goals = [
            item
            for item in goal_requirements
            if int(item.get("requiredMin") or 0) > 0
            and item.get("requirementLevel") not in {"soft_experience", "optional"}
        ]
        optional_goals = [
            item for item in goal_requirements if item.get("requirementLevel") in {"soft_experience", "optional"}
        ]
        required_goal_ids = [str(item.get("goalId")) for item in required_goals if item.get("goalId")]
        placements: dict[int, list[str]] = {}
        for item in required_goals:
            goal_id = str(item.get("goalId") or "")
            if not goal_id:
                continue
            allowed_days = [int(day) for day in item.get("allowedDayNumbers") or [1] if isinstance(day, int)]
            count = min(int(item.get("requiredMin") or 1), len(allowed_days))
            for day in allowed_days[:count]:
                placements.setdefault(day, []).append(goal_id)
        first_day = min(placements) if placements else 1
        optional_by_day: dict[int, list[str]] = {}
        optional_occurrence_count = 0
        optional_occurrence_limit = 3
        for item in required_goals:
            goal_id = str(item.get("goalId") or "")
            required = int(item.get("requiredMin") or 0)
            preferred = int(item.get("preferredCount") or required)
            maximum = item.get("maxCount")
            if maximum is not None:
                preferred = min(preferred, int(maximum))
            allowed_days = [int(day) for day in item.get("allowedDayNumbers") or [first_day] if isinstance(day, int)]
            required_days = {day for day, goals in placements.items() if goal_id in goals}
            for day in [value for value in allowed_days if value not in required_days][: max(0, preferred - required)]:
                if optional_occurrence_count >= optional_occurrence_limit:
                    break
                optional_by_day.setdefault(day, []).append(goal_id)
                optional_occurrence_count += 1
        for item in optional_goals:
            goal_id = str(item.get("goalId") or "")
            if not goal_id:
                continue
            allowed_days = [int(day) for day in item.get("allowedDayNumbers") or [first_day] if isinstance(day, int)]
            target_count = (
                len(allowed_days)
                if item.get("distributionPolicy") == "every_allowed_day"
                else max(1, int(item.get("requiredMin") or 1))
            )
            if item.get("maxCount") is not None:
                target_count = min(target_count, int(item["maxCount"]))
            target_days = allowed_days[:target_count]
            for day in target_days:
                if optional_occurrence_count >= optional_occurrence_limit:
                    break
                optional_by_day.setdefault(day, []).append(goal_id)
                optional_occurrence_count += 1
        strategy_days = sorted(set(placements) | set(optional_by_day)) or [first_day]
        day_strategies = [
            {
                "dayNumber": day,
                "theme": "满足必选目标",
                "requiredGoalIds": placements.get(day, []),
                "requiredGoalCounts": {goal_id: 1 for goal_id in placements.get(day, [])},
                "optionalGoalIds": optional_by_day.get(day, []),
                "pace": "standard",
                "maxRouteAnchors": 4,
            }
            for day in strategy_days
        ]
        intent_type_by_goal = {
            str(item.get("goalId") or ""): str(item.get("intentType") or "")
            for item in goal_requirements
            if item.get("goalId")
        }
        occurrence_schedule_hints: list[dict[str, Any]] = []
        for day in strategy_days:
            day_goal_ids = [*placements.get(day, []), *optional_by_day.get(day, [])]
            for sequence, goal_id in enumerate(day_goal_ids, start=1):
                intent_type = intent_type_by_goal.get(goal_id, "")
                day_part = (
                    "night"
                    if intent_type == "night_view"
                    else "noon"
                    if intent_type == "meal"
                    else "morning"
                    if sequence == 1
                    else "afternoon"
                )
                preferred_hour = min(21, 9 + (sequence - 1) * 3)
                occurrence_schedule_hints.append(
                    {
                        "goalId": goal_id,
                        "dayNumber": day,
                        "dayPart": day_part,
                        "sequence": sequence,
                        "preferredStartTime": f"{preferred_hour:02d}:00",
                        "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                        "estimateSource": "controller_estimate",
                        "confidence": 0.7,
                    }
                )
        examples: dict[str, dict[str, Any]] = {
            "draft_itinerary": {
                "type": "draft_itinerary",
                "goalPriority": [
                    *required_goal_ids,
                    *[str(item["goalId"]) for item in optional_goals if item.get("goalId")],
                ],
                "dayStrategies": day_strategies,
                "optionalExperienceBudget": optional_occurrence_count,
                "searchPriority": required_goal_ids,
                "candidateSelectionPolicy": {
                    "autoSelectWhenDominant": True,
                    "askWhenMaterialTradeoff": True,
                    "preferLowDetour": True,
                    "avoidRecentEntities": True,
                },
                "schedulePolicy": {
                    "respectOpeningWindowsWhenKnown": True,
                    "allowProvisionalWhenUnknown": True,
                },
                "occurrenceScheduleHints": occurrence_schedule_hints,
            },
            "resolve_poi": {
                "type": "resolve_poi",
                "targetSegmentIds": [segment_id],
                "searchIntent": "<user requested place intent>",
                "searchMode": "text",
                "maxCandidates": 4,
                "autoSelectPolicy": "dominant_safe_candidate_only",
                "askUserPolicy": "material_tradeoff_only",
            },
            "ask_user": {"type": "ask_user", "question": "<material choice>", "choiceIds": []},
            "finish": {"type": "finish", "assistantReply": "<snapshot-backed result>"},
        }
        if action == "draft_itinerary" and route_policy_requirement:
            route_policy: dict[str, Any] = {
                "objective": "least_generalized_cost",
                "source": "controller_estimate",
                "allowExperienceDetour": True,
            }
            if route_policy_requirement["mobilityProfile"]["required"]:
                route_policy["mobilityProfile"] = {
                    "transportMode": "transit",
                    "paceClass": "standard",
                }
            if route_policy_requirement["detourEnvelope"]["required"]:
                route_policy["detourEnvelope"] = {
                    "maxGeneralizedCostDelta": 30,
                    "maxDetourRatio": 0.3,
                }
            examples["draft_itinerary"]["routePlanningPolicy"] = route_policy
        if version_id and segment_id != "<observed-segment-id>":
            examples["patch_itinerary"] = {
                "type": "patch_itinerary",
                "operationIntent": "replace_segment_start_time",
                "baseVersionId": version_id,
                "targetSegmentIds": [segment_id],
                "requestedOutcome": "将目标行程开始时间调整为 10:00",
                "preserve": ["其余行程内容与顺序"],
                "maxChangedSegmentCount": 1,
                "startTime": "10:00",
            }
        return {
            "schemaVersion": "agent-decision-v3",
            "primaryAction": action,
            "actionDirective": examples.get(action, {"type": action}),
        }
