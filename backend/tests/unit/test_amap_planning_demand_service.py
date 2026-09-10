from src.models.poi_intent import IntentPool
from src.services.amap_planning_demand_service import AmapPlanningDemandService


def pool(identifier, intent, need):
    return IntentPool(pool_id=identifier, city="北京", intent_type=intent, raw_need=need, target_count=1)


def test_demand_dedupes_same_city_intent_query_and_reserves_optional_until_essentials():
    demand = AmapPlanningDemandService().plan(
        [
            pool("campus_day_1", "campus_visit", "高校参观"),
            pool("campus_day_2", "campus_visit", "高校参观"),
            pool("meal", "meal", "北京特色美食"),
            pool("walk", "area_walk", "北京街区体验"),
        ],
        {"occurrences": [
            {"intentType": "campus_visit", "requirementLevel": "hard", "distinctGroupId": "distinct:campus"},
            {"intentType": "meal", "requirementLevel": "explicit_soft"},
        ]},
    )
    assert [item.pool_id for item in demand] == ["campus_day_1", "meal", "walk"]
    assert demand[0].reason == "hard_occurrence"
    assert demand[1].reason == "explicit_soft_occurrence"
