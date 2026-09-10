from src.services.meal_grounding_policy import MealGroundingPolicy


def test_explicit_local_food_request_creates_route_anchor_lunch_and_dinner():
    result = MealGroundingPolicy().analyze(
        "杭州两日游，中等预算，午餐和晚餐都想体验当地特色美食",
        city="杭州",
        budget="中等",
        day_count=2,
    )

    assert result.explicit_food_experience is True
    assert [slot.meal_label for slot in result.meal_slots] == ["lunch", "dinner"]
    assert all(slot.route_anchor for slot in result.meal_slots)
    assert all(slot.estimated_cost > 0 for slot in result.meal_slots)
    hint_text = " ".join(hint for slot in result.meal_slots for hint in slot.candidate_hints)
    assert "杭州" in hint_text
    assert "当地特色餐厅" in hint_text
    assert not any(term in hint_text for term in ["杭帮菜", "片儿川", "西湖醋鱼", "知味观小吃"])


def test_user_explicit_dish_is_preserved_without_a_city_dish_dictionary():
    result = MealGroundingPolicy().analyze(
        "晚餐想吃潮汕生腌，不要太辣",
        city="深圳",
    )

    assert result.explicit_food_experience is True
    assert result.meal_slots[0].raw_need == "晚餐 潮汕生腌"
    assert any("潮汕生腌" in hint for hint in result.meal_slots[0].candidate_hints)


def test_provider_cuisine_category_wins_over_unrelated_schedule_quantity() -> None:
    message = (
        "每天严格按“北京985高校→12:00至13:30北京当地特色午餐→独立公共公园”的顺序安排3个不同地点；"
        "午餐必须为高德分类中的北京菜，不接受高校食堂或泛餐饮。"
    )

    result = MealGroundingPolicy().analyze(
        message,
        city="北京",
        budget="中等",
        day_count=2,
    )

    assert result.explicit_food_experience is True
    assert [slot.meal_label for slot in result.meal_slots] == ["lunch"]
    assert result.meal_slots[0].raw_need == "午餐 北京菜"
    assert any("北京菜" in hint for hint in result.meal_slots[0].candidate_hints)
    assert all("3个不同地点" not in value for value in result.preserved_user_intents)
    assert all("3个不同地点" not in hint for hint in result.meal_slots[0].candidate_hints)


def test_passive_lunch_time_does_not_force_restaurant_grounding():
    result = MealGroundingPolicy().analyze(
        "北京一天游，中午留时间吃午餐，下午继续逛景点",
        city="北京",
        budget="低预算",
    )

    assert result.explicit_food_experience is False
    assert [slot.meal_label for slot in result.meal_slots] == ["lunch"]
    assert result.meal_slots[0].route_anchor is False
    assert result.meal_slots[0].candidate_hints == []


def test_budget_tier_uses_complete_phrases_not_characters_inside_unrelated_words():
    policy = MealGroundingPolicy()

    assert policy.budget_tier(None, "参观高校，中等预算，午餐吃当地特色") == "medium"
    assert policy.budget_tier(None, "使用高德地图，预算适中") == "medium"
    assert policy.budget_tier(None, "高校游，低预算") == "low"
    assert policy.budget_tier(None, "预算宽裕，想吃好一点") == "high"
    assert policy.estimate_cost("lunch", None, "参观高校，中等预算") == 80


def test_meal_cost_range_is_provisional_until_a_verified_price_exists():
    cost = MealGroundingPolicy().estimate_cost_range("dinner", "medium")

    assert cost["min"] < cost["preferred"] < cost["max"]
    assert cost["budgetTier"] == "medium"
    assert cost["provisional"] is True
