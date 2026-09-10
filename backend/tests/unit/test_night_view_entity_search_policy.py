from src.models.poi_search_profile import ExperienceSemanticInput
from src.services.experience_search_profile_compiler import (
    ExperienceSearchProfileCompiler,
)
from src.services.night_view_entity_search_policy import (
    NightViewEntitySearchPolicy,
)


def test_night_semantic_descriptors_never_become_exact_entity_hints() -> None:
    assert NightViewEntitySearchPolicy.named_entity_hints(
        [
            "北京 公共城市夜景空间 夜景",
            "北京 摄影",
            "北京 日落",
            "北京 天际线",
            "景山公园",
            "什刹海",
        ],
        city="北京",
    ) == ["景山公园", "什刹海"]
    assert NightViewEntitySearchPolicy.is_generic_entity_seed("日落", "北京") is True
    assert NightViewEntitySearchPolicy.is_generic_entity_seed("北京夜景哪里好", "北京") is True
    assert NightViewEntitySearchPolicy.is_generic_entity_seed("景山公园", "北京") is False


def test_night_profile_spends_budget_on_place_facets_and_named_entity_discovery() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night",
            briefId="brief-night",
            planningSlotId="day-1-night",
            dayNumber=1,
            requirementLevel="required",
            rawNeed="每个可用晚上安排不同的公共城市夜景",
            intentType="night_view",
            candidateHints=[
                "北京 公共城市夜景空间 夜景",
                "北京 摄影",
                "北京 日落",
                "北京 天际线",
            ],
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )

    plans = [(item.mode, item.keyword) for item in profile.queryPlans]
    assert plans == [
        ("amap_text", "夜游景区"),
        ("amap_text", "夜景公园"),
        ("amap_text", "城市观景台"),
        ("web_seed_then_amap", "城市夜景 具体地点名称 夜游景区 观景台"),
    ]
    assert not any(keyword in {"摄影", "日落", "天际线"} for _, keyword in plans)


def test_repeated_night_occurrences_compile_distinct_provider_query_ladders() -> None:
    compiler = ExperienceSearchProfileCompiler()
    common = {
        "city": "北京",
        "briefId": "brief-night",
        "requirementLevel": "required",
        "rawNeed": "每个可用晚上安排不同的公共城市夜景",
        "intentType": "night_view",
        "targetCount": 1,
        "evidenceTargetCount": 1,
        "maxQueries": 4,
    }
    day_one = compiler.compile(
        ExperienceSemanticInput(
            **common,
            poolId="pool-night-day-1",
            planningSlotId="day-1-night",
            dayNumber=1,
        )
    )
    day_two = compiler.compile(
        ExperienceSemanticInput(
            **common,
            poolId="pool-night-day-2",
            planningSlotId="day-2-night",
            dayNumber=2,
        )
    )

    day_one_keywords = [item.keyword for item in day_one.queryPlans]
    day_two_keywords = [item.keyword for item in day_two.queryPlans]
    assert day_one.dayNumber == 1
    assert day_two.dayNumber == 2
    assert set(day_one_keywords).isdisjoint(day_two_keywords)
    assert day_two_keywords[:3] == ["城市阳台", "夜游步道", "观景平台"]


def test_unselected_waterfront_candidate_hint_cannot_override_public_city_view_contract() -> None:
    profile = ExperienceSearchProfileCompiler().compile(
        ExperienceSemanticInput(
            city="北京",
            poolId="pool-night",
            briefId="brief-night",
            planningSlotId="day-1-night",
            requirementLevel="required",
            rawNeed="每个可用晚上安排不同的公共城市夜景",
            experienceGoal="城市公共观景",
            intentType="night_view",
            candidateHints=["滨水公共夜景"],
            desiredSignals=["水域景观", "公共空间"],
            targetCount=1,
            evidenceTargetCount=1,
            maxQueries=4,
        )
    )

    keywords = [item.keyword for item in profile.queryPlans]
    assert keywords[:3] == ["夜游景区", "夜景公园", "城市观景台"]
    assert all("滨水" not in keyword for keyword in keywords)
