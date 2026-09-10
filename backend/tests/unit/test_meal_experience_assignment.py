from src.models.poi_intent import DaySlot
from src.services.meal_experience_assignment import MealExperienceAssignmentPolicy


def _meal_slots():
    return [
        DaySlot("day1_lunch", 1, "2026-10-01", "12:00-13:00", "12:00", 60, "meal", "午餐 当地特色美食", True),
        DaySlot("day1_dinner", 1, "2026-10-01", "18:00-19:00", "18:00", 60, "meal", "晚餐 当地特色美食", True),
    ]


def test_generic_meal_assignment_is_identical_across_cities_and_keeps_pending():
    policy = MealExperienceAssignmentPolicy()
    shenzhen = policy.assign("深圳", _meal_slots(), request_text="想吃当地特色，但暂不指定餐厅", seed="a")
    guangzhou = policy.assign("广州", _meal_slots(), request_text="想吃当地特色，但暂不指定餐厅", seed="b")

    assert list(shenzhen) == list(guangzhou)
    assert all(item.dish_family == "" and item.search_terms == () for item in shenzhen.values())
    assert all(item.allow_pending is True for item in shenzhen.values())
    assert [item.meal_label for item in shenzhen.values()] == ["lunch", "dinner"]


def test_user_explicit_food_constraint_is_carried_without_city_answer_table():
    assignment = MealExperienceAssignmentPolicy().assign(
        "成都",
        _meal_slots()[:1],
        request_text="午餐明确要吃钟水饺",
        seed="ignored",
        user_explicit_food_constraints={"day1_lunch": "钟水饺"},
    )["day1_lunch"]

    assert assignment.dish_family == "钟水饺"
    assert assignment.search_terms == ("钟水饺",)
    assert assignment.user_explicit is True


def test_meal_assignment_preserves_route_and_dietary_context_only():
    assignment = MealExperienceAssignmentPolicy().assign(
        "青岛",
        _meal_slots()[:1],
        request_text="父母不能吃太辣，午餐需要休息，安排在路线附近",
        seed="ignored",
        route_context={"previousAnchorId": "poi_a", "nextAnchorId": "poi_b"},
        dietary_restrictions=["不能太辣"],
    )["day1_lunch"]

    payload = assignment.to_camel_dict()
    assert payload["routeContext"]["previousAnchorId"] == "poi_a"
    assert payload["dietaryRestrictions"] == ["不能太辣"]
    assert payload["restRequired"] is True
