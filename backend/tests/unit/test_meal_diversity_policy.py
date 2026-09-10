from types import SimpleNamespace

from src.services.meal_diversity_policy import MealDiversityPolicy


def test_canonical_meal_brand_removes_generic_branch_suffixes():
    policy = MealDiversityPolicy()

    assert policy.canonical_meal_brand("样例小馆(大学城店)") == policy.canonical_meal_brand("样例小馆（中心店）")


def test_canonical_meal_brand_keeps_short_restaurant_names():
    policy = MealDiversityPolicy()

    assert policy.canonical_meal_brand("样例饭店") == "样例饭店"
    assert policy.canonical_meal_brand("同品牌饭店(一店)") == "同品牌饭店"
    assert policy.duplicate_reason("同品牌饭店(二店)", {"同品牌饭店"}, set()) == "duplicate_meal_brand_same_day"


def test_local_food_relevance_uses_generic_intent_not_city_answers():
    policy = MealDiversityPolicy()
    candidate = SimpleNamespace(name="目的地传统小吃馆", type="餐饮服务;中餐厅", category="food", address="", district="", providerTypeCode="050100", tags=["地方风味"], sourceClaims=[{"stance": "support"}])
    generic = SimpleNamespace(name="普通快餐", type="餐饮服务;快餐厅", category="food", address="", district="")

    assert policy.local_relevance_score("体验当地美食", ["当地小吃"], candidate) > 0
    assert policy.local_relevance_score("体验当地美食", ["当地小吃"], generic) < 0


def test_generic_cuisine_family_blocks_repeat_without_city_dish_answers():
    policy = MealDiversityPolicy()

    assert policy.dish_family("社区面馆") == "面馆"
    assert policy.dish_family("街角饺子馆") == "饺子"
    assert policy.duplicate_reason("另一家面馆", set(), set(), set(), {"面馆"}) == (
        "duplicate_meal_dish_family_trip"
    )
    assert policy.duplicate_reason("街角饺子馆", set(), set(), set(), {"面馆"}) == ""
