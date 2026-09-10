"""Full instructions must describe the actual validator, not an example dialect."""

from copy import deepcopy
import json
import re

import pytest
from pydantic import Field, ValidationError

from src.services.agent_action_directive import DraftItineraryDirective
from src.services.agent_autonomy_service import ModelDecisionV3
from src.services.agent_decision_contract_service import AgentDecisionContractService


def test_full_draft_guide_is_schema_derived_and_bounded():
    contract = AgentDecisionContractService.full_draft_type_contract()
    schema = DraftItineraryDirective.model_json_schema(by_alias=True)
    assert contract["actionSchemaHash"] == AgentDecisionContractService.hash_schema(schema)
    assert len(contract["guide"].encode("utf-8")) <= 4600
    assert "DraftItineraryDirective=" in contract["guide"]
    assert "requiredGoalCounts?:map<string,int>" in contract["guide"]


class GuideDecoder:
    """Independent test reader for the documented type grammar."""

    def __init__(self, text):
        self.text = text
        self.position = 0

    def take(self, text):
        if self.text.startswith(text, self.position):
            self.position += len(text)
            return True
        return False

    def expect(self, text):
        assert self.take(text), (self.text[self.position:], text)

    def name(self):
        if self.text[self.position] == '"':
            value, end = json.JSONDecoder().raw_decode(self.text[self.position:])
            self.position += end
            return value
        match = re.match(r"\w+", self.text[self.position:])
        assert match
        self.position += len(match[0])
        return match[0]

    def node(self):
        if self.take("("):
            branches = [self.node()]
            while self.take("|"):
                branches.append(self.node())
            self.expect(")")
            value = {"anyOf": branches}
        elif self.take("["):
            value = {"type": "array", "items": self.node()}
            self.expect("]")
        elif self.take("map<string,"):
            value = {"type": "object", "additionalProperties": self.node()}
            self.expect(">")
        elif self.take("{"):
            properties, required = {}, []
            while not self.take("}"):
                name = self.name()
                if not self.take("?"):
                    required.append(name)
                self.expect(":")
                properties[name] = self.node()
                if self.take(","):
                    continue
                self.expect("}")
                break
            value = {"type": "object", "properties": properties}
            if required:
                value["required"] = required
            if self.take("!"):
                value["additionalProperties"] = False
            else:
                self.expect("+<")
                value["additionalProperties"] = self.node()
                self.expect(">")
        else:
            name = self.name()
            primitives = {"int": "integer", "bool": "boolean", "number": "number", "string": "string", "null": "null"}
            value = ({"type": primitives[name]} if name in primitives else
                     True if name == "any" else False if name == "never" else {"$ref": "#/$defs/" + name})
        if self.take("@"):
            constraints, end = json.JSONDecoder().raw_decode(self.text[self.position:])
            self.position += end
            if value is True:
                value = {}
            value.update(constraints)
        return value


def validation_schema(schema):
    """Discard only non-validation annotations, never actual field names."""
    if isinstance(schema, list):
        return [validation_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in {"title", "description", "default", "examples"}:
            continue
        if key in {"properties", "$defs"}:
            result[key] = {name: validation_schema(item) for name, item in value.items()}
        else:
            result[key] = validation_schema(value)
    return result


def decode_guide(guide):
    decoded = {}
    for line in guide.splitlines()[1:]:
        name, body = line.split("=", 1)
        parser = GuideDecoder(body)
        decoded[name] = parser.node()
        assert parser.position == len(body)
    root_name = guide.splitlines()[1].split("=", 1)[0]
    root = decoded.pop(root_name)
    if decoded:
        root["$defs"] = decoded
    return root


def test_guide_round_trip_preserves_every_schema_validation_rule():
    schema = DraftItineraryDirective.model_json_schema(by_alias=True)
    decoded = decode_guide(AgentDecisionContractService.full_draft_type_contract()["guide"])
    assert decoded == validation_schema(schema)
    assert len(decoded["$defs"]) == len(schema["$defs"])


def test_added_schema_field_automatically_changes_guide_and_source_hash(monkeypatch):
    before = AgentDecisionContractService.full_draft_type_contract()
    schema = DraftItineraryDirective.model_json_schema(by_alias=True)
    schema["$defs"]["RoutePlanningPolicy"]["properties"]["futureRequiredField"] = {
        "anyOf": [{"type": "string", "minLength": 2, "maxLength": 9, "pattern": "^[a-z]+$"}, {"type": "null"}],
    }
    schema["$defs"]["RoutePlanningPolicy"]["required"].append("futureRequiredField")
    monkeypatch.setattr(DraftItineraryDirective, "model_json_schema", classmethod(lambda cls, **kwargs: deepcopy(schema)))
    after = AgentDecisionContractService.full_draft_type_contract()
    assert after["actionSchemaHash"] != before["actionSchemaHash"]
    assert decode_guide(after["guide"]) == validation_schema(schema)
    assert "futureRequiredField?:" not in after["guide"]
    assert "futureRequiredField:(string@" in after["guide"]


@pytest.mark.parametrize("keyword,value", [
    ("oneOf", [{"type": "string"}, {"type": "number"}]),
    ("format", "email"),
    ("if", {"required": ["future"]}),
    ("unevaluatedProperties", False),
])
def test_unknown_schema_mechanisms_fail_closed(keyword, value):
    schema = {"type": "string", keyword: value}
    with pytest.raises(ValueError, match="full_draft_type_schema_unsupported"):
        AgentDecisionContractService._schema_type_guide(schema, "FutureSchema")


@pytest.mark.parametrize("extra", [False, True, {"type": "integer"}])
def test_declared_objects_and_dynamic_maps_keep_distinct_extra_key_rules(extra):
    schema = {"type": "object", "additionalProperties": extra, "properties": {
        "requiredNullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "optionalNonNull": {"type": "integer", "minimum": 1},
    }, "required": ["requiredNullable"]}
    guide = AgentDecisionContractService._schema_type_guide(schema, "ObjectPolicy")
    assert decode_guide(guide) == schema
    assert "requiredNullable?:" not in guide
    assert "optionalNonNull?:int" in guide


def real_failed_full_decision():
    """Portable public decision fields from the one-shot 2026-09-06 gate.

    Retained source: turn_req_133f9ac0f7be87fecae35341e019a83567872809a651b22d48c0ebb7121c6166,
    agentDecisionState.providerRawDecisions[0]. No runtime DB or secrets needed.
    """
    coverage = [{"clauseId": f"request_clause_{i}", "classification": "constraint", "activities": []}
                for i in range(1, 12)]
    for index in (0, 9, 10):
        coverage[index]["classification"] = "context"
    for index, intent, part, text in [(4, "museum", "morning", "上午参观一处博物馆"),
                                      (5, "landmark", "afternoon", "下午在城市地标散步")]:
        coverage[index - 1].update(classification="activity", activities=[{
            "goalId": f"goal_request_{index}_1", "sourceText": text, "intentType": intent,
            "polarity": "required", "allowedDayNumbers": [1], "minCount": 1, "dayPart": part,
        }])
    return {"schemaVersion": "agent-decision-v3", "primaryAction": "draft_itinerary", "actionDirective": {
        "type": "draft_itinerary", "requestCoverage": coverage,
        "dayStrategies": [{"dayNumber": 1, "theme": "北京文化地标一日游",
                           "requiredGoalIds": ["goal_request_4_1", "goal_request_5_1"],
                           "requiredGoalCounts": {"goal_request_4_1": 1, "goal_request_5_1": 1},
                           "optionalGoalIds": [], "pace": "moderate", "maxRouteAnchors": 2}],
        "occurrenceScheduleHints": [{"goalId": goal, "dayNumber": 1, "dayPart": part,
                                     "sequence": sequence, "preferredStartTime": clock,
                                     "durationEstimate": {"min": 120, "preferred": 150, "max": 180},
                                     "estimateSource": "controller_estimate", "confidence": 0.7}
                                    for goal, part, sequence, clock in [
                                        ("goal_request_4_1", "morning", 1, "09:00"),
                                        ("goal_request_5_1", "afternoon", 2, "13:00")]],
        "requestCoverageNote": "draft for confirmation",
        "routePlanningPolicy": {"transportMode": "public_transit", "paceClass": "moderate",
                                "maxGeneralizedCostDelta": 30, "maxDetourRatio": 1.5},
    }}


def test_real_seven_full_schema_errors_stay_rejected():
    assert AgentDecisionContractService.hash_schema(real_failed_full_decision()) == "b1dec3e7d22ebf244b66584a5c514f89bda412a2a9ea5fc1c406ba26e57a9eeb"
    with pytest.raises(ValidationError) as caught:
        ModelDecisionV3.model_validate(real_failed_full_decision())
    errors = caught.value.errors(include_url=False)
    assert len(errors) == 7
    paths = {(tuple(item["loc"])[2:], item["type"]) for item in errors}
    assert paths == {
        (("dayStrategies", 0, "pace"), "literal_error"),
        (("routePlanningPolicy", "source"), "missing"),
        (("routePlanningPolicy", "transportMode"), "extra_forbidden"),
        (("routePlanningPolicy", "paceClass"), "extra_forbidden"),
        (("routePlanningPolicy", "maxGeneralizedCostDelta"), "extra_forbidden"),
        (("routePlanningPolicy", "maxDetourRatio"), "extra_forbidden"),
        (("requestCoverageNote",), "extra_forbidden"),
    }


def test_correct_schema_nested_policy_is_accepted_without_alias_normalization():
    raw = real_failed_full_decision()
    directive = raw["actionDirective"]
    directive["dayStrategies"][0]["pace"] = "standard"
    directive.pop("requestCoverageNote")
    directive["routePlanningPolicy"] = {
        "source": "controller_estimate",
        "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
        "detourEnvelope": {"maxGeneralizedCostDelta": 30, "maxDetourRatio": 1.5},
    }
    assert ModelDecisionV3.model_validate(raw).primary_action == "draft_itinerary"


def complete_draft_with_explicit_defaults():
    """Independent complete fixture, never reconstructed from truncated output."""
    activities = [
        ("goal_request_1_1", "museum", "morning", "上午参观博物馆", "09:00"),
        ("goal_request_2_1", "landmark", "afternoon", "下午在城市地标散步", "14:00"),
    ]
    coverage = [
        {
            "clauseId": f"request_clause_{index}", "classification": "activity",
            "activities": [{
                "goalId": goal, "sourceText": source, "intentType": intent,
                "polarity": "required", "allowedDayNumbers": [1], "minCount": 1, "dayPart": part,
            }],
        }
        for index, (goal, intent, part, source, _) in enumerate(activities, 1)
    ]
    coverage.extend({"clauseId": f"request_clause_{index}", "classification": "constraint", "activities": []}
                    for index in range(3, 12))
    return {
        "schemaVersion": "agent-decision-v3", "primaryAction": "draft_itinerary",
        "actionDirective": {
            "type": "draft_itinerary", "requestCoverage": coverage,
            "goalPriority": [], "searchPriority": [], "optionalExperienceBudget": 0,
            "candidateSelectionPolicy": {
                "autoSelectWhenDominant": True, "askWhenMaterialTradeoff": True,
                "preferLowDetour": True, "avoidRecentEntities": True,
            },
            "schedulePolicy": {"respectOpeningWindowsWhenKnown": True, "allowProvisionalWhenUnknown": True},
            "dayStrategies": [{
                "dayNumber": 1, "theme": "文化漫步", "requiredGoalIds": [row[0] for row in activities],
                "requiredGoalCounts": {}, "optionalGoalIds": [], "pace": "standard", "maxRouteAnchors": 4,
            }],
            "routePlanningPolicy": {
                "source": "controller_estimate", "objective": "least_generalized_cost", "allowExperienceDetour": False,
                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                "detourEnvelope": {"maxGeneralizedCostDelta": 35, "maxDetourRatio": 0.35},
            },
            "occurrenceScheduleHints": [{
                "goalId": goal, "dayNumber": 1, "dayPart": part, "sequence": index,
                "preferredStartTime": clock, "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
                "estimateSource": "controller_estimate", "confidence": 0.8,
            } for index, (goal, _, part, _, clock) in enumerate(activities, 1)],
            "routeGapSupplementHints": [],
        },
    }


def test_compact_profile_is_schema_bound_and_keeps_business_required_fields():
    profile = AgentDecisionContractService.full_draft_compact_output_profile()
    defaults = profile["omissionDefaults"]
    assert profile["actionSchemaHash"] == AgentDecisionContractService.full_draft_type_contract()["actionSchemaHash"]
    assert len(profile["guide"].encode("utf-8")) <= 850
    assert defaults["actionDirective.dayStrategies[].pace"] == "standard"
    assert defaults["actionDirective.dayStrategies[].maxRouteAnchors"] == 4
    assert defaults["actionDirective.dayStrategies[].requiredGoalCounts"] == {}
    assert defaults["actionDirective.requestCoverage[].activities"] == []
    assert defaults["actionDirective.routePlanningPolicy.objective"] == "least_generalized_cost"
    assert defaults["actionDirective.routePlanningPolicy.allowExperienceDetour"] is False
    assert defaults["actionDirective.candidateSelectionPolicy"] == {
        "autoSelectWhenDominant": True, "askWhenMaterialTradeoff": True,
        "preferLowDetour": True, "avoidRecentEntities": True,
    }
    protected = profile["businessRequiredPaths"]
    assert {
        "actionDirective.requestCoverage", "actionDirective.dayStrategies[].requiredGoalIds",
        "actionDirective.occurrenceScheduleHints", "actionDirective.occurrenceScheduleHints[].preferredStartTime",
        "actionDirective.routePlanningPolicy", "actionDirective.routePlanningPolicy.mobilityProfile",
        "actionDirective.routePlanningPolicy.detourEnvelope",
    } == set(protected)
    assert set(defaults).isdisjoint(protected)
    assert "actionDirective.type" not in defaults
    assert "actionDirective.dayStrategies[].theme" not in defaults


def test_profile_follows_new_model_defaults_without_a_second_field_allowlist(monkeypatch):
    import src.services.agent_decision_contract_service as contract_module

    class ExtendedDraft(DraftItineraryDirective):
        compact_flag: bool = Field(default=False, alias="compactFlag")
        compact_list: list[int] = Field(default_factory=list, alias="compactList")

    previous = AgentDecisionContractService.full_draft_compact_output_profile()
    monkeypatch.setattr(contract_module, "DraftItineraryDirective", ExtendedDraft)
    current = AgentDecisionContractService.full_draft_compact_output_profile()
    assert current["actionSchemaHash"] != previous["actionSchemaHash"]
    assert current["omissionDefaults"]["actionDirective.compactFlag"] is False
    assert current["omissionDefaults"]["actionDirective.compactList"] == []
    assert "compactList" in current["guide"]


def test_complete_canonical_default_omission_preserves_strict_domain_values_and_saves_bytes():
    explicit = complete_draft_with_explicit_defaults()
    compact = deepcopy(explicit)
    directive = compact["actionDirective"]
    for key in ("goalPriority", "searchPriority", "optionalExperienceBudget", "candidateSelectionPolicy",
                "schedulePolicy", "routeGapSupplementHints"):
        directive.pop(key)
    for key in ("requiredGoalCounts", "optionalGoalIds", "pace", "maxRouteAnchors"):
        directive["dayStrategies"][0].pop(key)
    for key in ("objective", "allowExperienceDetour"):
        directive["routePlanningPolicy"].pop(key)
    for clause in directive["requestCoverage"]:
        if clause["classification"] == "constraint":
            clause.pop("activities")

    explicit_model = ModelDecisionV3.model_validate(explicit, strict=True)
    compact_model = ModelDecisionV3.model_validate(compact, strict=True)
    assert compact_model.model_dump(by_alias=True) == explicit_model.model_dump(by_alias=True)
    assert len(directive["requestCoverage"]) == 11
    assert directive["occurrenceScheduleHints"] == explicit["actionDirective"]["occurrenceScheduleHints"]
    assert directive["dayStrategies"][0]["requiredGoalIds"] == ["goal_request_1_1", "goal_request_2_1"]
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Both sides already use identical whitespace-free JSON: this measures
    # default-field omission, in UTF-8 bytes, not a claimed token budget.
    assert len(encode(explicit)) - len(encode(compact)) >= 500
    assert len(encode(compact)) < len(encode(explicit))


def test_nondefault_decisions_are_not_semantically_equivalent_to_omission():
    explicit = complete_draft_with_explicit_defaults()
    explicit["actionDirective"]["dayStrategies"][0]["pace"] = "relaxed"
    omitted = deepcopy(explicit)
    omitted["actionDirective"]["dayStrategies"][0].pop("pace")
    assert ModelDecisionV3.model_validate(explicit, strict=True) != ModelDecisionV3.model_validate(omitted, strict=True)
    assert AgentDecisionContractService.full_draft_compact_output_profile()["omissionDefaults"][
        "actionDirective.dayStrategies[].pace"
    ] == "standard"
