import json

import pytest

from src.services.agent_output_parser_service import AgentOutputParser, INVALID_AGENT_JSON_MESSAGE


def test_agent_output_parser_normalizes_deepseek_alias_fields() -> None:
    output = AgentOutputParser().parse(
        json.dumps(
            {
                "reply": "已生成。",
                "mode": "full_itinerary",
                "operations": [{"op": "add_day", "start": "14:00", "duration": 90}],
                "fullItinerary": {
                    "tripTitle": "北京轻松 1 日游",
                    "city": "北京",
                    "days": [
                        {
                            "slotId": "day1_morning_campus",
                        "dayNumber": 1,
                            "dayTitle": "故宫与胡同",
                            "segments": [
                                {
                                    "poiName": "故宫",
                                    "category": "scenic",
                                    "start": "09:00",
                                    "duration": 120,
                                    "notes": "上午参观。",
                                    "estimatedCost": 60,
                                }
                            ],
                        }
                    ],
                },
                "poiResolutionRequests": [
                    {"poiName": "故宫", "type": "scenic"},
                    {"keyword": "胡同", "category": "scenic"},
                ],
                "warnings": [],
            },
            ensure_ascii=False,
        )
    )

    assert output.full_itinerary.title == "北京轻松 1 日游"
    assert output.full_itinerary.days[0].title == "故宫与胡同"
    assert output.full_itinerary.days[0].segments[0].start_time == "09:00"
    assert output.full_itinerary.days[0].segments[0].duration_minutes == 120
    assert output.operations[0].start_time == "14:00"
    assert output.operations[0].duration_minutes == 90
    assert output.poi_resolution_requests[0].name == "故宫"
    assert output.poi_resolution_requests[0].category == "scenic"
    assert output.poi_resolution_requests[1].name == "胡同"


def test_agent_output_parser_rejects_malformed_json_with_friendly_message() -> None:
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        AgentOutputParser().parse("not json")


def test_agent_output_parser_extracts_json_object_from_wrapped_text() -> None:
    output = AgentOutputParser().parse(
        """
        下面是 JSON:
        {"reply":"需要确认出行日期。","mode":"clarification","operations":[],"fullItinerary":null,"poiResolutionRequests":[],"warnings":[]}
        """
    )

    assert output.mode == "clarification"
    assert output.reply == "需要确认出行日期。"


def test_initial_plan_parser_accepts_day_slots_and_rejects_full_itinerary() -> None:
    parser = AgentOutputParser()
    valid_payload = {
        "reply": "已拆解时间槽。",
        "mode": "day_slots",
        "daySlots": [
            {
                "slotId": "day1_morning_campus",
                "dayNumber": 1,
                "date": "2026-10-01",
                "timeWindow": "09:00-11:00",
                "startTime": "09:00",
                "durationMinutes": 120,
                "kind": "campus",
                "rawNeed": "高校参观",
                "routeAnchor": True,
                "priority": 90,
                "notes": "",
            }
        ],
        "intentPools": [
            {
                "poolId": "campus_visit_pool",
                "rawNeed": "高校参观",
                "city": "北京",
                "intentType": "campus_visit",
                "targetCount": 1,
                "preferredTypes": ["大学", "学院", "高等院校"],
                "rejectedTypes": ["酒店", "民宿", "公寓", "公司", "停车场", "住宅", "小区"],
                "routePreference": {"sameDayUnique": True},
                "assignToSlots": ["day1_morning_campus"],
            }
        ],
        "warnings": [],
    }
    output = parser.parse_initial_plan(json.dumps(valid_payload, ensure_ascii=False))

    assert output.mode == "day_slots"
    assert output.day_slots[0].slot_id == "day1_morning_campus"
    assert output.day_slots[0].raw_need == "高校参观"
    assert output.intent_pools[0].pool_id == "campus_visit_pool"

    invalid_full_itinerary = {
        "reply": "旧合同。",
        "mode": "full_itinerary",
        "operations": [],
        "fullItinerary": {"title": "旧行程", "city": "北京", "days": []},
        "warnings": [],
    }
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        parser.parse_initial_plan(json.dumps(invalid_full_itinerary, ensure_ascii=False))

    invalid_operations = dict(valid_payload)
    invalid_operations["operations"] = []
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        parser.parse_initial_plan(json.dumps(invalid_operations, ensure_ascii=False))

    missing_intent_pools = json.loads(json.dumps(valid_payload, ensure_ascii=False))
    missing_intent_pools.pop("intentPools")
    output_without_pools = parser.parse_initial_plan(json.dumps(missing_intent_pools, ensure_ascii=False))
    assert output_without_pools.mode == "day_slots"
    assert output_without_pools.day_slots
    assert output_without_pools.intent_pools == []

    extra_fields = json.loads(json.dumps(valid_payload, ensure_ascii=False))
    extra_fields["daySlots"][0]["poiName"] = "清华大学"
    output_with_extra = parser.parse_initial_plan(json.dumps(extra_fields, ensure_ascii=False))
    assert output_with_extra.mode == "day_slots"
    assert output_with_extra.day_slots[0].raw_need == "高校参观"
    assert output_with_extra.intent_pools[0].pool_id == "campus_visit_pool"

    invalid_poi_intents = json.loads(json.dumps(valid_payload, ensure_ascii=False))
    invalid_poi_intents["poiIntents"] = [{"rawNeed": "旧字段应被拒绝"}]
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        parser.parse_initial_plan(json.dumps(invalid_poi_intents, ensure_ascii=False))

    invalid_snake_case_poi_intents = json.loads(json.dumps(valid_payload, ensure_ascii=False))
    invalid_snake_case_poi_intents["poi_intents"] = [{"rawNeed": "旧字段应被拒绝"}]
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        parser.parse_initial_plan(json.dumps(invalid_snake_case_poi_intents, ensure_ascii=False))

    invalid_search_queries = json.loads(json.dumps(valid_payload, ensure_ascii=False))
    invalid_search_queries["intentPools"][0]["searchQueries"] = ["北京 高校"]
    with pytest.raises(ValueError, match=INVALID_AGENT_JSON_MESSAGE):
        parser.parse_initial_plan(json.dumps(invalid_search_queries, ensure_ascii=False))
