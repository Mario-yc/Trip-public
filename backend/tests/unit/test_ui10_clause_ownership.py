"""Prompt obligations and strict refusal replay; never a live-model guarantee."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
from src.services.agent_action_directive import DraftItineraryDirective
from src.services.deepseek_agent_provider import FULL_TYPED_COVERAGE_PROMPT, REQUEST_ACTIVITY_COVERAGE_PROMPT
from src.services.request_activity_coverage_service import RequestActivityCoverageError, RequestActivityCoverageService


def recorded():
    return json.loads((Path(__file__).parents[1] / 'fixtures/ui10_clause_ownership.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('prompt', [FULL_TYPED_COVERAGE_PROMPT, REQUEST_ACTIVITY_COVERAGE_PROMPT], ids=['typed', 'legacy'])
def test_prompt_explicitly_binds_modifier_clauses_to_existing_activity(prompt):
    # These instructions were absent in the prompt that produced the saved refusal.
    # Checking their delivery proves the omission is repaired, not that an LLM obeys.
    for obligation in ['Read adjacent clauses together', 'time/duration/order modifier',
                       'existing goalId', 'occurrenceScheduleHints', 'Do not merge distinct activities']:
        assert obligation in prompt


def test_saved_real_response_still_refused_without_formal_writes():
    evidence = recorded()
    contract = evidence['requestIntentContract']
    before_contract = deepcopy(contract)
    with sqlite3.connect(sqlite_path_from_url(get_settings().database_url)) as db:
        tables = ['itinerary_versions', 'itinerary_patches', 'route_options', 'agent_plan_proposals']
        counts = lambda: [db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables]
        before = counts()
        with pytest.raises(RequestActivityCoverageError, match='^request_activity_coverage_legacy_goal_mapping_ambiguous$'):
            RequestActivityCoverageService.compile(contract, evidence['rawDecision']['actionDirective']['requestCoverage'])
        assert counts() == before
    assert contract == before_contract


def test_single_named_activity_modifier_keeps_typed_time_and_duration():
    evidence = recorded()
    directive = evidence['rawDecision']['actionDirective']
    # Deliberately corrected output exercises the existing representation, not a model oracle.
    modifier = directive['requestCoverage'][2]
    assert modifier['activities'][0]['sourceText'] == '上午9点开始游览1小时'
    modifier.update(classification='constraint', activities=[])
    typed = DraftItineraryDirective.model_validate(directive)
    payload = typed.model_dump(by_alias=True)
    compiled = RequestActivityCoverageService.compile(evidence['requestIntentContract'], payload['requestCoverage'])
    RequestActivityCoverageService.validate_directive(compiled, payload)
    assert len(compiled['requiredIntents']) == 1
    goal = compiled['requiredIntents'][0]
    assert goal['exactEntity'] == '景山公园'
    assert goal['goalId'] == 'goal_request_2_1'
    assert len(payload['occurrenceScheduleHints']) == 1
    hint = payload['occurrenceScheduleHints'][0]
    assert hint['goalId'] == goal['goalId']
    assert hint['preferredStartTime'] == '09:00'
    assert hint['durationEstimate'] == {'min': 60, 'preferred': 60, 'max': 60}


@pytest.mark.parametrize('places,intents', [(['景山公园', '故宫博物院'], ['park', 'museum']),
                                         (['景山公园', '北海公园'], ['park', 'park'])])
def test_independent_named_activities_are_never_merged(places, intents):
    contract = RequestActivityCoverageService.prepare({'dayCount': 1, 'requiredIntents': []},
                                                       f'上午去{places[0]}，下午去{places[1]}')
    coverage = []
    for index, clause in enumerate(contract['requestActivityCoverage']['clauses']):
        coverage.append({'clauseId': clause['clauseId'], 'classification': 'activity', 'activities': [{
            'goalId': clause['goalIds'][0], 'sourceText': clause['text'], 'intentType': intents[index],
            'exactEntity': places[index], 'polarity': 'required', 'allowedDayNumbers': [1],
            'minCount': 1, 'dayPart': ['morning', 'afternoon'][index]}]})
    compiled = RequestActivityCoverageService.compile(contract, coverage)
    assert [goal['exactEntity'] for goal in compiled['requiredIntents']] == places
    assert len({goal['goalId'] for goal in compiled['requiredIntents']}) == 2


def test_conflicting_modifier_daypart_is_rejected():
    evidence = recorded()
    directive = evidence['rawDecision']['actionDirective']
    directive['requestCoverage'][2].update(classification='constraint', activities=[])
    compiled = RequestActivityCoverageService.compile(evidence['requestIntentContract'], directive['requestCoverage'])
    directive['occurrenceScheduleHints'][0]['dayPart'] = 'afternoon'
    with pytest.raises(RequestActivityCoverageError, match='directive_time_mismatch'):
        RequestActivityCoverageService.validate_directive(compiled, directive)
