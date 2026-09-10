"""Original request coverage is independent from POI availability and model transport."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from src.services.request_activity_coverage_service import RequestActivityCoverageError, RequestActivityCoverageService
from src.services.agent_autonomy_service import AgentAutonomyController, AutonomyContextProjector
from src.services.agent_observation_service import AgentObservationBuilder


REQUEST = (
    "请按刚才的小红书攻略规划北京2026年10月1日的一日游，1人，中等预算，"
    "上午参观一处博物馆，下午在城市地标散步，公交地铁优先。"
    "09:00开始20:00结束，不安排住宿，相邻地点交通不超过90分钟；"
    "生成可供确认的方案，不直接写入正式行程。"
)


def coverage_case(message=REQUEST, day_count=1):
    original = {
        "dayCount": day_count,
        "requiredPlanningDayNumbers": list(range(1, day_count + 1)),
        "requiredIntents": [{"goalId": "goal_museum", "intentType": "museum", "requiredMin": 1}],
        "negativeConstraints": [],
        "routeDecisionContract": {"adjacentLegConstraint": {"maxMinutes": 90}},
        "partySize": 1,
        "resolvedTripDates": {"dates": ["2026-10-01"]},
    }
    pending = RequestActivityCoverageService.prepare(original, message)
    clauses = pending["requestActivityCoverage"]["clauses"]
    proposal = []
    for clause in clauses:
        entry = {"clauseId": clause["clauseId"], "classification": "constraint", "activities": []}
        if "博物馆" in clause["text"] or "城市地标" in clause["text"]:
            intent = "museum" if "博物馆" in clause["text"] else "landmark"
            entry.update(classification="activity", activities=[{
                "goalId": clause["goalIds"][0], "sourceText": clause["text"], "intentType": intent,
                "polarity": "excluded" if "不去" in clause["text"] else "required",
                "allowedDayNumbers": list(range(1, day_count + 1)),
                "minCount": day_count, "dayPart": "morning" if intent == "museum" else "afternoon",
            }])
        proposal.append(entry)
    return original, pending, proposal


@pytest.mark.parametrize("message", [REQUEST, "北京两天，每日上午参观博物馆，每天下午在城市地标走走。"])
def test_every_original_activity_becomes_a_required_timed_obligation(message):
    original, pending, proposal = coverage_case(message, 2 if "两天" in message else 1)
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    goals = {item["intentType"]: item for item in compiled["requiredIntents"]}
    assert set(goals) == {"museum", "landmark"}
    for intent, part in [("museum", "morning"), ("landmark", "afternoon")]:
        assert goals[intent]["requiredMin"] == original["dayCount"]
        assert goals[intent]["schedulePreference"]["dayPart"] == part
        assert goals[intent]["userExplicit"] is True
    assert compiled["partySize"] == 1
    assert compiled["routeDecisionContract"] == original["routeDecisionContract"]
    assert compiled["resolvedTripDates"] == original["resolvedTripDates"]
    assert compiled["requestActivityCoverage"]["status"] == "complete"
    assert pending["requestActivityCoverage"]["status"] == "pending"


@pytest.mark.parametrize("clause", ["不去博物馆", "上午去博物馆但不要太远", "上午去博物馆，不限制馆藏年代"])
def test_full_exclusion_cannot_delete_an_existing_hard_obligation(clause):
    _, pending, proposal = coverage_case(f"北京一日游，{clause}，下午在城市地标散步。")
    museum = next(entry["activities"][0] for entry in proposal if entry["activities"] and entry["activities"][0]["intentType"] == "museum")
    museum["polarity"] = "excluded"
    before = deepcopy(pending)
    with pytest.raises(ValueError, match="request_activity_coverage.*hard_goal"):
        RequestActivityCoverageService.compile(pending, proposal)
    assert pending == before


def test_negative_activity_without_a_prior_hard_obligation_remains_an_exclusion():
    _, pending, proposal = coverage_case("北京一日游，不去博物馆，下午在城市地标散步。")
    pending["requiredIntents"] = []
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    assert [item["intentType"] for item in compiled["requiredIntents"]] == ["landmark"]
    assert "museum" in compiled["negativeConstraints"]


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "unsupported", "unresolved", "wrong_span", "wrong_goal", "wrong_day"])
def test_incomplete_or_unverifiable_coverage_is_rejected(mutation):
    _, pending, proposal = coverage_case()
    activity = next(entry for entry in proposal if entry["activities"])
    if mutation == "omit":
        proposal.remove(activity)
    elif mutation == "duplicate":
        proposal.append(deepcopy(activity))
    elif mutation == "unsupported":
        activity["activities"][0]["intentType"] = "unrecognized_activity"
    elif mutation == "unresolved":
        activity["classification"] = "unresolved"
    elif mutation == "wrong_span":
        activity["activities"][0]["sourceText"] = "作者推荐的故宫"
    elif mutation == "wrong_goal":
        activity["activities"][0]["goalId"] = "goal_museum"
    else:
        activity["activities"][0]["allowedDayNumbers"] = [2]
    before = deepcopy(pending)
    with pytest.raises(ValueError, match="request_activity_coverage"):
        RequestActivityCoverageService.compile(pending, proposal)
    assert pending == before


def test_complete_or_frozen_contract_cannot_be_recompiled():
    _, pending, proposal = coverage_case()
    complete = RequestActivityCoverageService.compile(pending, proposal)
    with pytest.raises(ValueError, match="request_activity_coverage.*frozen"):
        RequestActivityCoverageService.compile(complete, proposal)
    frozen = {**pending, "planningRequestEnvelope": {"sourcePlanningRootTurnId": "original_root"}}
    with pytest.raises(ValueError, match="request_activity_coverage.*frozen"):
        RequestActivityCoverageService.compile(frozen, proposal)


def test_all_clauses_preserve_original_offsets_including_dates_times_and_negations():
    _, pending, _ = coverage_case()
    clauses = pending["requestActivityCoverage"]["clauses"]
    assert len(clauses) > 8
    for clause in clauses:
        assert REQUEST[clause["start"]:clause["end"]] == clause["text"]
    assert any("09:00开始20:00结束" == item["text"] for item in clauses)
    assert any("不安排住宿" == item["text"] for item in clauses)


def test_known_hard_activity_cannot_be_relabelled_as_constraint():
    _, pending, proposal = coverage_case()
    entry = next(item for item in proposal if item["activities"] and item["activities"][0]["intentType"] == "museum")
    entry.update(classification="constraint", activities=[])
    with pytest.raises(ValueError, match="request_activity_coverage.*hard_goal"):
        RequestActivityCoverageService.compile(pending, proposal)


def test_instruction_clause_compiles_without_becoming_an_activity():
    _, pending, proposal = coverage_case()
    for index in (0, 9, 10):
        proposal[index]["classification"] = "instruction"
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    assert {goal["intentType"] for goal in compiled["requiredIntents"]} == {"museum", "landmark"}
    assert compiled["requestActivityCoverage"]["classifiedClauses"][0]["classification"] == "instruction"


def test_instruction_clause_cannot_carry_activity_evidence():
    _, pending, proposal = coverage_case()
    entry = next(item for item in proposal if item["activities"])
    entry["classification"] = "instruction"
    with pytest.raises(ValueError, match="request_activity_coverage_activity_mapping_invalid"):
        RequestActivityCoverageService.compile(pending, proposal)


def test_known_hard_count_or_day_part_cannot_be_weakened():
    _, pending, proposal = coverage_case("北京两天，每日上午参观博物馆，每天下午在城市地标走走。", 2)
    pending["requiredIntents"][0].update(requiredMin=2, allowedDayNumbers=[1, 2], schedulePreference={"dayPart": "morning"})
    museum = next(item["activities"][0] for item in proposal if item["activities"] and item["activities"][0]["intentType"] == "museum")
    museum["minCount"] = 1
    with pytest.raises(ValueError, match="request_activity_coverage.*hard_goal"):
        RequestActivityCoverageService.compile(pending, proposal)
    museum.update(minCount=2, dayPart="afternoon")
    with pytest.raises(ValueError, match="request_activity_coverage.*hard_goal"):
        RequestActivityCoverageService.compile(pending, proposal)


def test_existing_goal_linked_policies_are_rebound_to_the_covered_goal():
    _, pending, proposal = coverage_case()
    pending["experienceSpecs"] = [{"intentType": "museum", "sourceGoalId": "goal_museum", "evidencePolicy": "strict"}]
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    museum = next(item for item in compiled["requiredIntents"] if item["intentType"] == "museum")
    assert compiled["experienceSpecs"][0] == {"intentType": "museum", "sourceGoalId": museum["goalId"], "evidencePolicy": "strict"}


def test_same_type_legacy_goals_with_ambiguous_policies_fail_closed():
    _, pending, proposal = coverage_case()
    pending["requiredIntents"].append({"goalId": "second_museum", "intentType": "museum", "requiredMin": 1, "exactEntity": "一个不同的明确场馆"})
    with pytest.raises(ValueError, match="request_activity_coverage.*ambiguous"):
        RequestActivityCoverageService.compile(pending, proposal)


def recurring_activity_case():
    source = "每天上午参观985高校"
    contract = {
        "dayCount": 2,
        "requiredIntents": [{
            "goalId": "goal_campus", "intentType": "campus_visit", "requiredMin": 2,
            "allowedDayNumbers": [1, 2], "schedulePreference": {"dayPart": "morning"},
            "accessPolicy": "public_access_required", "distinctnessPolicy": "different_entities",
        }],
        "hardConstraints": ["campus_tier_985"],
        "entityQualificationConstraint": {
            "qualificationScheme": "moe_project_classification", "qualificationValue": "985",
            "evidencePolicy": "authoritative_source_required",
        },
        "routeDecisionContract": {"adjacentLegConstraint": {"maxMinutes": 90}},
    }
    pending = RequestActivityCoverageService.prepare(contract, source)
    activity = {
        "goalId": "goal_request_1_1", "sourceText": source, "intentType": "campus_visit",
        "polarity": "required", "allowedDayNumbers": [1, 2], "minCount": 2, "dayPart": "morning",
    }
    proposal = [{"clauseId": "request_clause_1", "classification": "activity", "activities": [activity]}]
    return pending, proposal


@pytest.mark.parametrize("legacy_count", [0, 1, 2])
@pytest.mark.parametrize("reverse_days", [False, True])
@pytest.mark.parametrize("polarity", ["required", "excluded"])
def test_duplicate_activity_is_rejected_before_legacy_mapping(legacy_count, reverse_days, polarity):
    pending, proposal = recurring_activity_case()
    old = pending["requiredIntents"][0]
    pending["requiredIntents"] = [{**deepcopy(old), "goalId": f"legacy_{index}"} for index in range(legacy_count)]
    activity = proposal[0]["activities"][0]
    activity["polarity"] = polarity
    duplicate = {**deepcopy(activity), "goalId": "goal_request_1_2"}
    if reverse_days:
        duplicate["allowedDayNumbers"] = [2, 1]
    proposal[0]["activities"].append(duplicate)
    before_contract, before_proposal = deepcopy(pending), deepcopy(proposal)
    with pytest.raises(RequestActivityCoverageError) as caught:
        RequestActivityCoverageService.compile(pending, proposal)
    assert str(caught.value) == (
        "request_activity_coverage_duplicate_activity:request_clause_1:goal_request_1_1,goal_request_1_2"
    )
    assert pending == before_contract
    assert proposal == before_proposal


@pytest.mark.parametrize("invalid", ["source", "goal_id"])
def test_duplicate_activity_detection_does_not_mask_invalid_source_or_identity(invalid):
    pending, proposal = recurring_activity_case()
    activity = proposal[0]["activities"][0]
    proposal[0]["activities"].append({**deepcopy(activity), "goalId": "goal_request_1_2"})
    # A later invalid clause must be checked before the first clause's duplicate.
    source = pending["requestActivityCoverage"]["sourceText"] + "，公交地铁优先"
    pending.pop("requestActivityCoverage")
    pending = RequestActivityCoverageService.prepare(pending, source)
    later = {**deepcopy(activity), "goalId": "goal_request_2_1", "sourceText": "公交地铁优先"}
    later["sourceText" if invalid == "source" else "goalId"] = "not_supplied"
    proposal.append({"clauseId": "request_clause_2", "classification": "activity", "activities": [later]})
    expected = "source_span_invalid" if invalid == "source" else "goal_identity_invalid"
    with pytest.raises(RequestActivityCoverageError, match="request_activity_coverage_" + expected):
        RequestActivityCoverageService.compile(pending, proposal)


@pytest.mark.parametrize("changed_field,changed_value", [
    ("sourceText", "985高校"), ("dayPart", "afternoon"), ("minCount", 1), ("intentType", "museum"),
])
def test_distinct_activity_signatures_are_not_reported_as_duplicates(changed_field, changed_value):
    pending, proposal = recurring_activity_case()
    pending["requiredIntents"] = []
    other = {**deepcopy(proposal[0]["activities"][0]), "goalId": "goal_request_1_2", changed_field: changed_value}
    proposal[0]["activities"].append(other)
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    assert [goal["goalId"] for goal in compiled["requiredIntents"]] == ["goal_request_1_1", "goal_request_1_2"]
    assert compiled["requestActivityCoverage"]["classifiedClauses"] == proposal


def test_different_source_activities_still_reject_ambiguous_legacy_mapping():
    pending, proposal = recurring_activity_case()
    other = {**deepcopy(proposal[0]["activities"][0]), "goalId": "goal_request_1_2", "sourceText": "985高校"}
    proposal[0]["activities"].append(other)
    with pytest.raises(RequestActivityCoverageError, match="request_activity_coverage_legacy_goal_mapping_ambiguous"):
        RequestActivityCoverageService.compile(pending, proposal)


def test_one_stable_recurring_activity_id_preserves_qualification_schedule_and_inputs():
    pending, proposal = recurring_activity_case()
    before_contract, before_proposal = deepcopy(pending), deepcopy(proposal)
    compiled = RequestActivityCoverageService.compile(pending, proposal)
    goal = compiled["requiredIntents"][0]
    directive = {
        "dayStrategies": [{"dayNumber": day, "requiredGoalIds": [goal["goalId"]]} for day in (1, 2)],
        "occurrenceScheduleHints": [{"goalId": goal["goalId"], "dayNumber": day, "dayPart": "morning"} for day in (1, 2)],
    }
    RequestActivityCoverageService.validate_directive(compiled, directive)
    assert len(compiled["requiredIntents"]) == 1
    assert goal["requiredMin"] == goal["minCount"] == goal["preferredCount"] == goal["maxCount"] == 2
    assert goal["allowedDayNumbers"] == [1, 2]
    assert goal["schedulePreference"]["dayPart"] == "morning"
    assert goal["accessPolicy"] == "public_access_required"
    assert goal["distinctnessPolicy"] == "different_entities"
    assert compiled["requestActivityCoverage"]["goalRebinding"] == {"goal_campus": goal["goalId"]}
    for key in ("hardConstraints", "entityQualificationConstraint", "routeDecisionContract"):
        assert compiled[key] == pending[key]
    assert pending == before_contract
    assert proposal == before_proposal


def full_case():
    _, pending, proposal = coverage_case()
    pending["routeDecisionContract"].update(status="ready", mobilityProfile={"transportMode": "transit", "paceClass": "standard"},
                                           detourTolerance={"maxGeneralizedCostDelta": 0, "maxDetourRatio": 0})
    positive = [activity for entry in proposal for activity in entry["activities"] if activity["polarity"] == "required"]
    goals = [item["goalId"] for item in positive]
    directive = {
        "type": "draft_itinerary", "requestCoverage": proposal, "goalPriority": goals,
        "dayStrategies": [{"dayNumber": 1, "theme": "馆藏与城市漫步", "requiredGoalIds": goals,
                           "requiredGoalCounts": {goal: 1 for goal in goals}, "optionalGoalIds": [], "pace": "relaxed", "maxRouteAnchors": 2}],
        "optionalExperienceBudget": 0,
        "routePlanningPolicy": {"source": "controller_estimate", "objective": "least_generalized_cost", "allowExperienceDetour": False,
                                "mobilityProfile": {"transportMode": "transit", "paceClass": "standard"},
                                "detourEnvelope": {"maxGeneralizedCostDelta": 0, "maxDetourRatio": 0}},
        "occurrenceScheduleHints": [
            {"goalId": item["goalId"], "dayNumber": 1, "dayPart": item["dayPart"], "sequence": index,
             "preferredStartTime": "09:00" if index == 1 else "14:00", "durationEstimate": {"min": 60, "preferred": 90, "max": 120},
             "estimateSource": "controller_estimate", "confidence": 0.9}
            for index, item in enumerate(positive, 1)
        ],
    }
    context = {
        "latestUserMessage": REQUEST, "effectiveUserMessage": REQUEST, "selectedCity": "北京",
        "serverExecutionProfile": "simple_open_v1", "requestIntentContract": pending,
        "resolvedTripDates": {"status": "resolved", "dates": ["2026-10-01"], "dayCount": 1},
    }
    return context, {"schemaVersion": "agent-decision-v3", "primaryAction": "draft_itinerary", "actionDirective": directive}


def run_full(context, output, on_context=None):
    calls = []
    def decide_autonomy(provider_context, **kwargs):
        calls.append(provider_context)
        if on_context:
            on_context(provider_context)
        return deepcopy(output)
    def forbidden(*args, **kwargs):
        pytest.fail("Coverage rejection must not call another model or a rule fallback")
    provider = SimpleNamespace(decide_autonomy=decide_autonomy, decide_autonomy_lite=forbidden)
    observation = AgentObservationBuilder().build(context)
    context["agentObservation"] = observation.model_dump(by_alias=True)
    result = AgentAutonomyController(provider=provider, fallback_decision_resolver=SimpleNamespace(resolve=forbidden)).decide(
        REQUEST, context, AutonomyContextProjector().project(context), available_tools={"resolve_poi", "patch_itinerary"},
        runtime_budget_tools={"resolve_poi", "patch_itinerary"}, observation=observation,
    )
    return result, calls


def test_one_existing_full_call_compiles_all_clauses_and_same_source_draft():
    context, output = full_case()
    result, calls = run_full(context, output)
    assert result.controller_full_succeeded, result.controller_error
    assert result.gated_decision.accepted, result.gated_decision.policy_reason_codes
    assert len(calls) == 1
    assert len(calls[0]["requestActivityClauses"]) == 11
    assert context["requestIntentContract"]["requestActivityCoverage"]["status"] == "complete"
    assert {item["intentType"] for item in context["agentObservation"]["requirementCoverage"]["required"]} == {"museum", "landmark"}


@pytest.mark.parametrize("failure", ["missing_coverage", "missing_clause", "missing_draft_goal", "wrong_time"])
def test_full_coverage_failure_never_repairs_or_falls_back_to_incomplete_success(failure):
    context, output = full_case()
    directive = output["actionDirective"]
    if failure == "missing_coverage":
        directive.pop("requestCoverage")
    elif failure == "missing_clause":
        directive["requestCoverage"].pop()
    elif failure == "missing_draft_goal":
        directive["dayStrategies"][0]["requiredGoalIds"].pop()
    else:
        directive["occurrenceScheduleHints"][0]["dayPart"] = "afternoon"
    result, calls = run_full(context, output)
    assert len(calls) == 1
    assert not result.controller_full_succeeded
    assert not result.controller_lite_called
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "ask_user"
    assert "request_activity_coverage_incomplete" in result.decision.reason_codes
    assert context["requestIntentContract"]["requestActivityCoverage"]["status"] == "pending"


@pytest.mark.parametrize("placement", ["only_optional", "both"])
def test_pending_hard_goal_in_optional_is_a_named_zero_execution_failure(placement):
    context, output = full_case()
    before = deepcopy(context["requestIntentContract"])
    directive = output["actionDirective"]
    strategy = directive["dayStrategies"][0]
    goal_id = strategy["requiredGoalIds"][0]
    strategy["optionalGoalIds"] = [goal_id]
    directive["optionalExperienceBudget"] = 1
    if placement == "only_optional":
        strategy["requiredGoalIds"].remove(goal_id)
        strategy["requiredGoalCounts"].pop(goal_id)
    original = deepcopy(output)
    result, calls = run_full(context, output)
    assert len(calls) == 1 and not result.controller_full_succeeded
    assert not result.controller_lite_called and result.schema_repair_attempts == 0
    assert result.decision.primary_action == "ask_user" and result.decision.required_tools == []
    assert "draft_required_goal_misclassified_as_optional" in result.decision.reason_codes
    assert result.decision.expected_outcome["conflictingGoalId"] == goal_id
    assert goal_id in result.controller_error
    assert result.decision.side_effects["itinerary"] is False
    assert context["requestIntentContract"] == before and output == original


@pytest.mark.parametrize("error", ["ControllerOutputTruncatedError:controller_output_truncated", "RequiredGoalOptionalConflictError:goal_museum"])
def test_model_contract_failure_preserves_request_without_blame_or_automatic_retry(error):
    decision = AgentAutonomyController._request_coverage_failure_decision(error)
    message = decision.user_visible_reason
    assert "原始需求已保留" in message
    assert "重新提交完整需求" not in message
    assert "原始需求完整转换" not in message
    if "Truncated" in error:
        assert "截断" in message
    assert decision.required_tools == []
    assert decision.action_directive.choice_ids == []


@pytest.mark.parametrize("error,expected", [
    ("ControllerOutputTruncatedError:controller_output_truncated:finish_reason=length", "输出被截断"),
    ("ControllerOutputIncompleteError:controller_output_incomplete", "模型服务已响应"),
    ("ValidationError:7 validation errors for ModelDecisionV3", "已返回内容"),
    ("DecisionNormalizationError:draft_goal_count_mismatch", "已返回内容"),
    ("JSONDecodeError:Expecting value: line 1 column 1 (char 0)", "已返回内容"),
    ("RequestActivityCoverageError:request_activity_coverage_directive_omitted", "已返回内容"),
    ("RequiredGoalOptionalConflictError:draft_required_goal_misclassified_as_optional:goal_museum", "已返回内容"),
    ("TimeoutError:controller_decision_timeout", "请求超时"),
    ("HTTPException:504: Provider response body read timed out", "请求超时"),
    ("HTTPException:408: request timeout", "请求超时"),
    ("RuntimeError:controller_provider_unavailable", "服务不可用"),
    ("HTTPException:400: DEEPSEEK_API_KEY is not configured", "服务不可用"),
    ("HTTPException:502: recorded provider failure", "HTTP 错误"),
    ("HTTPException:429: slow down", "HTTP 错误"),
    ("URLError:connection refused", "连接或 HTTP 错误"),
    ("ValueError:controller_full_payload_too_large", "调用前的容量限制"),
    ("RuntimeError:unexpected internal fault", "具体失败原因尚未确认"),
    (None, "具体失败原因尚未确认"),
])
def test_coverage_failure_copy_distinguishes_output_errors_from_no_complete_response(error, expected):
    decision = AgentAutonomyController._request_coverage_failure_decision(error)
    message = decision.user_visible_reason
    assert expected in message
    if expected != "已返回内容":
        assert "已返回内容" not in message and "合同校验未通过" not in message
    if expected in {"服务不可用", "具体失败原因尚未确认", "调用前的容量限制"}:
        assert "已调用" not in message
    assert "原始需求已保留" in message and "重新提交完整需求" not in message
    assert decision.reason_codes == ["request_activity_coverage_incomplete"]
    assert decision.expected_outcome["controllerError"] == error
    assert decision.required_tools == [] and decision.action_directive.choice_ids == []
    assert decision.side_effects["itinerary"] is False


@pytest.mark.parametrize("action,directive", [
    ("finish", {"type": "finish", "assistantReply": "方案已准备好"}),
    ("read_itinerary", {"type": "read_itinerary", "queryType": "timeline_summary"}),
    ("verify_external_facts", {"type": "verify_external_facts", "factTypes": ["weather"]}),
    ("ask_user", {"type": "ask_user", "question": "是否继续？", "choiceIds": []}),
])
def test_pending_coverage_cannot_escape_through_another_valid_full_action(action, directive):
    context, _ = full_case()
    original = deepcopy(context["requestIntentContract"])
    result, calls = run_full(context, {
        "schemaVersion": "agent-decision-v3", "primaryAction": action, "actionDirective": directive,
    })
    assert len(calls) == 1
    assert not result.controller_full_succeeded
    assert not result.controller_lite_called
    assert result.schema_repair_attempts == 0
    assert result.decision.primary_action == "ask_user"
    assert result.decision.required_tools == []
    assert result.decision.side_effects["itinerary"] is False
    assert result.decision.expected_outcome["timelineWriteDelta"] == 0
    assert "request_activity_coverage_incomplete" in result.decision.reason_codes
    assert "request_activity_coverage_non_draft_action" in result.controller_error
    assert context["requestIntentContract"] == original


@pytest.fixture
def rebound_time_contract():
    from src.services.agent_service import AgentService

    def build(day_part):
        message = "每晚都去逛公园，北京两日游。"
        goal = {"goalId": "goal_park", "intentType": "park", "requiredMin": 2,
                "allowedDayNumbers": [1, 2], "distributionPolicy": "every_allowed_day",
                "schedulePreference": {"dayPart": day_part, "userExplicit": True}}
        pending = RequestActivityCoverageService.prepare({
            "dayCount": 2, "requiredPlanningDayNumbers": [1, 2], "requiredIntents": [goal],
        }, message)
        clauses = pending["requestActivityCoverage"]["clauses"]
        proposal = [{"clauseId": item["clauseId"], "classification": "constraint", "activities": []} for item in clauses]
        proposal[0].update(classification="activity", activities=[{
            "goalId": clauses[0]["goalIds"][0], "sourceText": clauses[0]["text"], "intentType": "park",
            "polarity": "required", "allowedDayNumbers": [1, 2], "minCount": 2, "dayPart": day_part,
        }])
        contract = RequestActivityCoverageService.compile(pending, proposal)
        contract["planningRequestEnvelope"] = {"sourcePlanningRootTurnId": "original_root"}
        service = object.__new__(AgentService)
        # Source reading/fingerprinting has its own SQLite regression suite. This
        # fixture supplies its immutable output to exercise both supported parts.
        service._original_time_requirements = lambda **kwargs: (deepcopy([goal]), {})
        return service, {"summary": {"requestIntentContract": contract}}

    return build


@pytest.mark.parametrize("day_part", ["evening", "night"])
def test_continuation_preserves_completed_coverage_goal_rebinding(rebound_time_contract, day_part):
    service, portfolio = rebound_time_contract(day_part)
    before = deepcopy(portfolio)
    assert portfolio["summary"]["requestIntentContract"]["requestActivityCoverage"]["goalRebinding"] == {"goal_park": "goal_request_1_1"}
    service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)
    assert portfolio == before


@pytest.mark.parametrize("change", ["missing_target", "wrong_intent", "duplicate_target", "shared_target", "pending", "missing_evidence", "wrong_days"])
def test_continuation_rejects_tampered_coverage_rebinding(rebound_time_contract, change):
    from fastapi import HTTPException

    service, portfolio = rebound_time_contract("evening")
    contract = portfolio["summary"]["requestIntentContract"]
    coverage = contract["requestActivityCoverage"]
    goal = contract["requiredIntents"][0]
    if change == "missing_target":
        coverage["goalRebinding"]["goal_park"] = "missing"
    elif change == "wrong_intent":
        goal["intentType"] = "museum"
    elif change == "duplicate_target":
        contract["requiredIntents"].append(deepcopy(goal))
    elif change == "shared_target":
        coverage["goalRebinding"]["goal_other"] = goal["goalId"]
    elif change == "pending":
        coverage["status"] = "pending"
    elif change == "missing_evidence":
        coverage["activities"] = []
    else:
        goal["allowedDayNumbers"] = [1]
    before = deepcopy(portfolio)
    with pytest.raises(HTTPException) as error:
        service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)
    assert error.value.detail["code"] == "simple_direction_original_schedule_contract_outdated"
    assert error.value.detail["details"]["plannerCalled"] is False
    assert portfolio == before


@pytest.mark.parametrize("clause", ["每晚都去逛公园", "每夜都去逛公园"])
def test_source_root_continuation_reads_compiled_rebinding_without_writes(clause):
    import sqlite3
    from src.services.agent_service import AgentService
    from src.services.clarification_checkpoint_service import ClarificationCheckpointService

    message = f"{clause}，北京两日游，2026年10月1日到2日。"
    dates = {"dates": ["2026-10-01", "2026-10-02"], "dayCount": 2}
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE conversation_turns (id TEXT, session_id TEXT, role TEXT, status TEXT, content TEXT, agent_request_json TEXT)")
        db.execute("INSERT INTO conversation_turns VALUES (?, ?, 'user', 'active', ?, ?)", (
            "root", "session", message, json.dumps({"resolvedTripDates": dates}),
        ))
        db.commit()
        service = object.__new__(AgentService)
        service.db = db
        original = service._request_intent_contract(message, dates, None, city="北京", simple_open_profile=True)
        park = next(goal for goal in original["requiredIntents"] if goal["intentType"] == "park")
        assert park["requiredMin"] == 2 and park["schedulePreference"]["dayPart"] == "evening"
        pending = RequestActivityCoverageService.prepare(original, message)
        clauses = pending["requestActivityCoverage"]["clauses"]
        proposal = [{"clauseId": item["clauseId"], "classification": "constraint", "activities": []} for item in clauses]
        proposal[0].update(classification="activity", activities=[{
            "goalId": clauses[0]["goalIds"][0], "sourceText": clauses[0]["text"], "intentType": "park",
            "polarity": "required", "allowedDayNumbers": [1, 2], "minCount": 2, "dayPart": "evening",
        }])
        compiled = RequestActivityCoverageService.compile(pending, proposal)
        compiled["planningRequestEnvelope"] = {"city": "北京", "sourcePlanningRootTurnId": "root", "resolvedTripDates": dates}
        portfolio = {"source_user_turn_id": "root", "summary": {
            "requestIntentContract": compiled,
            "requestIntentContractMaterialFingerprint": ClarificationCheckpointService._fingerprint(compiled),
        }}
        before = deepcopy(portfolio)
        writes = db.total_changes
        service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)
        service._assert_frozen_original_time_requirements(session_id="session", portfolio=portfolio)
        assert portfolio == before and db.total_changes == writes
        assert not db.in_transaction
    finally:
        db.close()


def test_complete_full_http_envelope_keeps_existing_byte_limit_and_all_clauses(monkeypatch):
    from io import BytesIO
    from src.services.deepseek_agent_provider import DeepSeekAgentProvider
    from src.services.controller_context_projection_service import FULL_REQUEST_BYTE_LIMIT
    provider = DeepSeekAgentProvider()
    provider.api_key = "test-unused-key"
    captured = []
    def capture_request(request, **kwargs):
        captured.append(request.data)
        return BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())
    monkeypatch.setattr("src.services.deepseek_agent_provider.urlopen", capture_request)
    context, output = full_case()
    context["sourceMaterialEvidence"] = [{"sourceMaterialId": "source_1", "publicText": "这是攻略原文。" * 146,
                                          "sourceUrl": "https://example.test/note", "metadata": {"contentFingerprint": "a" * 64, "fetchStatus": "succeeded"}}]
    def inspect(provider_context):
        provider.decide_autonomy(provider_context, timeout_seconds=8)
    result, _ = run_full(context, output, inspect)
    assert result.controller_full_succeeded, result.controller_error
    assert len(captured) == 1
    size = len(captured[0])
    actual_payload = json.loads(captured[0])
    assert size <= FULL_REQUEST_BYTE_LIMIT == 15360
    assert len(json.loads(actual_payload["messages"][1]["content"])["requestActivityClauses"]) == 11
    from src.services.agent_decision_contract_service import AgentDecisionContractService
    typed = AgentDecisionContractService.full_draft_type_contract()
    assert typed["guide"] in actual_payload["messages"][0]["content"]
    assert typed["actionSchemaHash"] in actual_payload["messages"][0]["content"]
    assert actual_payload["max_tokens"] == 1200
    assert actual_payload["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("provider_succeeded", [True, False])
def test_day_slot_provider_and_timeout_fallback_preserve_every_covered_obligation(provider_succeeded):
    from src.services.agent_service import AgentService
    from src.api.schemas.agent import AgentInitialPlanOutput
    context, output = full_case()
    result, _ = run_full(context, output)
    assert result.controller_full_succeeded, result.controller_error
    directive = result.decision.action_directive.model_dump(by_alias=True)
    context.update(city="北京", planningDirective=directive, agentDecisionState={
        "source": "controller", "controlOwner": "model_controller", "decisionPath": "full",
        "accepted": True, "primaryAction": "draft_itinerary", "actionDirective": deepcopy(directive),
    })
    service = object.__new__(AgentService)
    service.initial_planning_mode = "simple_open_v1"
    service.settings = SimpleNamespace(simple_open_max_route_pairs=8, simple_open_max_search_slots=8)
    if provider_succeeded:
        initial = AgentInitialPlanOutput.model_validate({"mode": "initial_plan", "reply": "未覆盖所需槽位", "daySlots": [], "intentPools": []})
        projected = service._simple_open_project_initial_plan_to_authoritative_occurrences(initial, context)
    else:
        projected = service._validated_controller_draft_slot_fallback(context, TimeoutError("initial provider timeout"))
    assert projected is not None
    assert {(slot.kind, slot.start_time) for slot in projected.day_slots} == {("museum", "09:00"), ("landmark", "14:00")}
    assert len({row["sourceGoalId"] for row in context["simpleOpenAuthoritativeOccurrenceInventory"]}) == 2


def test_pending_coverage_cannot_freeze_a_root_or_materialize_a_proposal():
    from src.services.simple_open_direction_service import SimpleOpenDirectionService
    _, pending, _ = coverage_case()
    with pytest.raises(ValueError, match="request_activity_coverage_not_complete"):
        SimpleOpenDirectionService.prepare_direction_snapshot({}, request_contract=pending)
    service = SimpleOpenDirectionService(None)
    with pytest.raises(ValueError, match="request_activity_coverage_not_complete"):
        service.ensure_root(session_id="unused", planning_root_id="unused", source_assistant_turn_id="unused",
                            expected_base_version_id=None, source_observation_fingerprint="a" * 64,
                            request_contract_fingerprint="b" * 64, request_contract=pending,
                            locality="北京", max_pages_per_query=1)


def test_staged_context_rejects_pending_coverage_without_reextracting_or_writing():
    from backend.tests.unit.test_agent_service import open_db
    from src.services.agent_service import AgentService
    context, _ = full_case()
    before = deepcopy(context)
    with open_db() as db:
        agent = AgentService(db, provider=SimpleNamespace())
        writes = db.total_changes
        with pytest.raises(ValueError, match="request_activity_coverage_not_complete"):
            agent._initial_staged_pipeline_context(context, context["resolvedTripDates"])
        assert context == before and db.total_changes == writes


def test_staged_context_preserves_a_frozen_contract_without_rotating_it():
    from backend.tests.unit.test_agent_service import open_db
    from src.services.agent_service import AgentService
    context, output = full_case()
    result, _ = run_full(context, output)
    assert result.controller_full_succeeded
    contract = context["requestIntentContract"]
    contract["planningRequestEnvelope"] = {"sourcePlanningRootTurnId": "original_root"}
    contract["systemCreativeVariant"] = "frozen_original_variant"
    context["canonicalRequestContext"] = {"requestIntentContract": deepcopy(contract)}
    context["agentDecision"] = result.decision.model_dump(by_alias=True)
    before = deepcopy(contract)
    with open_db() as db:
        agent = AgentService(db, provider=SimpleNamespace())
        writes = db.total_changes
        pipeline = agent._initial_staged_pipeline_context(context, context["resolvedTripDates"])
        assert pipeline["requestIntentContract"] == before
        assert pipeline["canonicalRequestContext"]["requestIntentContract"] == before
        assert context["requestIntentContract"] == before and db.total_changes == writes


def test_full_compilation_freezes_new_fingerprint_and_preserves_it_through_proposal():
    from backend.tests.unit.test_agent_service import open_db
    from backend.tests.unit.test_simple_open_direction_workflow import _snapshot, _route_contract
    from src.services.agent_service import AgentService
    from src.services.conversation_service import ConversationService
    from src.services.simple_open_direction_service import SimpleOpenDirectionService
    from src.services.clarification_checkpoint_service import ClarificationCheckpointService
    context, output = full_case()
    context["canonicalRequestContext"] = {"requestIntentContract": deepcopy(context["requestIntentContract"])}
    old_fingerprint = ClarificationCheckpointService._fingerprint(context["requestIntentContract"])
    context["requestContractFingerprint"] = old_fingerprint
    context["canonicalRequestContext"]["requestContractFingerprint"] = old_fingerprint
    result, _ = run_full(context, output)
    assert result.controller_full_succeeded
    contract = context["requestIntentContract"]
    contract["routeDecisionContract"] = _route_contract()
    with open_db() as db:
        session = ConversationService(db).create_session("北京", "coverage fingerprint fixture")
        agent = AgentService(db, provider=SimpleNamespace())
        agent.initial_planning_mode = "simple_open_v1"
        user_id = agent._insert_turn(session.session_id, "user", REQUEST, "active")
        assistant_id = agent._insert_turn(session.session_id, "assistant", "待确认", "active")
        context.update(sessionId=session.session_id, currentUserTurnId=user_id)
        context["requestIntentContract"] = contract
        context["agentDecision"] = result.decision.model_dump(by_alias=True)
        context["agentDecisionState"] = {
            "source": "controller", "controlOwner": "model_controller", "decisionPath": "full",
            "accepted": True, "primaryAction": "draft_itinerary",
            "actionDirective": result.decision.action_directive.model_dump(by_alias=True),
        }
        pipeline = agent._initial_staged_pipeline_context(context, context["resolvedTripDates"])
        assert pipeline["requestIntentContract"]["requestActivityCoverage"] == contract["requestActivityCoverage"]
        occurrences = agent._simple_open_goal_occurrence_plan(pipeline)
        assert {item["sourceGoalId"] for item in occurrences["occurrences"]} == {goal["goalId"] for goal in contract["requiredIntents"]}
        from src.api.schemas.agent import AgentInitialPlanOutput
        agent._prepare_simple_open_direction_frontier(
            session_id=session.session_id, user_turn_id=user_id, assistant_turn_id=assistant_id,
            city="北京", active_version_id=None, request_context=context, pipeline_context=pipeline,
            initial_plan=AgentInitialPlanOutput(mode="plan", reply="", daySlots=[], intentPools=[]),
            resolved_dates=context["resolvedTripDates"], tool_events=[],
        )
        service = SimpleOpenDirectionService(db)
        root = service.latest_root(session_id=session.session_id)
        frozen = root["requestIntentContract"]
        fingerprint = root["requestContractFingerprint"]
        assert fingerprint != old_fingerprint
        assert fingerprint == ClarificationCheckpointService._fingerprint(frozen)
        assert pipeline["simpleDirectionFrontierPreparation"]["requestContractFingerprint"] == fingerprint
        for view in (context, pipeline, context["canonicalRequestContext"], pipeline["canonicalRequestContext"]):
            assert view["requestIntentContract"] == frozen
            assert view["requestContractFingerprint"] == fingerprint
        snapshot = _snapshot(agent._session(session.session_id)["active_plan_id"], "馆藏与城市漫步", "B000A")
        template = snapshot["days"][0]["segments"][0]
        segments = []
        for index, goal in enumerate(frozen["requiredIntents"]):
            segment = deepcopy(template)
            segment.update(id=f"coverage_segment_{index}", kind=goal["intentType"], startTime="09:00" if index == 0 else "14:00", endTime="10:30" if index == 0 else "15:30", durationMinutes=90)
            segment["poi"].update(id=f"coverage_poi_{index}", amapId=f"coverage_fixture_{index}",
                                  name="测试馆藏场所" if index == 0 else "测试城市广场", category=goal["intentType"],
                                  type="科教文化服务;博物馆" if index == 0 else "风景名胜;广场", providerTypeCode="140100" if index == 0 else "110105")
            segment["semanticMetadata"].update(intentType=goal["intentType"], goalId=goal["goalId"], sourceGoalId=goal["goalId"],
                                                occurrenceId=f"occ:{goal['goalId']}:day:1", planningSlotId=f"coverage_slot_{index}")
            segments.append(segment)
        snapshot["days"][0]["segments"] = segments
        snapshot["desiredDensityAnchorTargets"] = {"1": 2}
        snapshot["requestActivityCoverage"] = deepcopy(frozen["requestActivityCoverage"])
        material = service.offer_direction(
            session_id=session.session_id, planning_root_id=user_id, source_user_turn_id=user_id,
            source_assistant_turn_id=assistant_id, expected_base_version_id=None,
            source_observation_fingerprint="a" * 64, request_contract_fingerprint=fingerprint,
            request_contract=frozen, snapshot=snapshot,
        )
        assert material["proposalDelta"] == 1
        assert service.latest_root(session_id=session.session_id)["requestIntentContract"] == frozen
        assert all(choice.get("requestContractFingerprint") == fingerprint for choice in material["choiceOptions"])
        assert db.execute("SELECT count(*) FROM itinerary_versions WHERE session_id=?", (session.session_id,)).fetchone()[0] == 0
