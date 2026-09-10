from src.services.timeline_command_planner import TimelineCommandPlanner


def test_parse_night_view_concrete_grounding_request():
    command = TimelineCommandPlanner().parse(
        "第二天的夜景观景点不是具体的地点，改为真实的地点", has_active_timeline=True
    )

    assert command is not None
    assert command.scope == "night_view"
    assert command.operation == "ground_or_replace_specific_poi"
    assert command.day_number == 2
    assert command.constraints["intentType"] == "night_view"
    assert command.constraints["requireConcreteAmapPoi"] is True
    assert command.constraints["rejectGenericPlaceholder"] is True
    assert command.constraints["rejectCompositePoi"] is True
    assert command.constraints["avoidTicketLookup"] is True
    assert command.constraints["avoidWebSearch"] is True
    assert "query" not in command.constraints


def test_parse_plain_night_view_replacement_keeps_existing_operation():
    command = TimelineCommandPlanner().parse("第一天晚上换个更适合拍照的夜景", has_active_timeline=True)

    assert command is not None
    assert command.scope == "night_view"
    assert command.operation == "replace_kind"
    assert command.day_number == 1
    assert "requireConcreteAmapPoi" not in command.constraints


def test_parse_night_view_local_options_request():
    command = TimelineCommandPlanner().parse("第二天的夜景观景点我先看北京的城市夜景", has_active_timeline=True)

    assert command is not None
    assert command.scope == "night_view"
    assert command.operation == "offer_local_poi_options"
    assert command.day_number == 2
    assert command.constraints["optionCount"] == 3
    assert command.constraints["includeCustomOption"] is True
    assert command.constraints["routeAwareOptions"] is True
    assert command.constraints["avoidTicketLookup"] is True
    assert command.constraints["avoidWebSearch"] is True


def test_parse_previous_night_view_option_selection_by_text_or_index():
    text_command = TimelineCommandPlanner().parse("那就去国贸CBD吧", has_active_timeline=True)
    index_command = TimelineCommandPlanner().parse("我选第三个", has_active_timeline=True)
    custom_command = TimelineCommandPlanner().parse("我选择第4个：国贸CBD", has_active_timeline=True)

    assert text_command is not None
    assert text_command.operation == "choose_previous_local_poi_option"
    assert text_command.constraints["selectedOptionText"] == "国贸CBD"
    assert index_command is not None
    assert index_command.operation == "choose_previous_local_poi_option"
    assert index_command.constraints["selectedOptionIndex"] == 3
    assert custom_command is not None
    assert custom_command.operation == "choose_previous_local_poi_option"
    assert custom_command.constraints["selectedOptionIndex"] == 4
    assert custom_command.constraints["selectedOptionText"] == "国贸CBD"


def test_parse_direct_specific_night_view_replacement_query():
    command = TimelineCommandPlanner().parse("第二天夜景改成国贸CBD", has_active_timeline=True)

    assert command is not None
    assert command.scope == "night_view"
    assert command.operation == "ground_or_replace_specific_poi"
    assert command.day_number == 2
    assert command.constraints["query"] == "国贸CBD"
    assert command.constraints["requireConcreteAmapPoi"] is True


def test_parse_exact_night_place_prefers_positive_target_and_keeps_negative_exclusion():
    command = TimelineCommandPlanner().parse(
        "第二天夜景不去旧观景点。直接去河畔公共观景空间就行",
        has_active_timeline=True,
    )

    assert command is not None
    assert command.scope == "night_view"
    assert command.operation == "ground_or_replace_specific_poi"
    assert command.constraints["intentType"] == "exact_place"
    assert command.constraints["query"] == "河畔公共观景空间"
    assert command.constraints["requireConcreteAmapPoi"] is True
    assert "旧观景点" in command.constraints["excludeNames"]


def test_parse_meal_replacement_inside_complex_keeps_food_and_area_separate():
    command = TimelineCommandPlanner().parse(
        "第一天的炸酱面太远了，改到大融城里面的店吃吧",
        has_active_timeline=True,
    )

    assert command is not None
    assert command.scope == "meal"
    assert command.operation == "replace_or_fill"
    assert command.day_number == 1
    assert command.constraints["foodIntent"] == "炸酱面"
    assert command.constraints["targetMealText"] == "炸酱面"
    assert command.constraints["areaIntent"] == "大融城"
    assert command.constraints["containmentMode"] == "inside_or_same_complex"


def test_parse_meal_options_and_selection_keep_the_write_boundary_explicit():
    options = TimelineCommandPlanner().parse(
        "第一天的炸酱面太远了，改到大融城里面的店，先看看候选再决定", has_active_timeline=True
    )
    selection = TimelineCommandPlanner().parse("我选择第2个：川味酸菜鱼（餐饮候选）", has_active_timeline=True)

    assert options is not None
    assert options.scope == "meal"
    assert options.operation == "offer_local_poi_options"
    assert options.constraints["areaIntent"] == "大融城"
    assert options.constraints["avoidWebSearch"] is True
    assert options.constraints["avoidTicketLookup"] is True
    assert selection is not None
    assert selection.scope == "meal"
    assert selection.operation == "choose_previous_local_poi_option"
    assert selection.constraints["selectedOptionIndex"] == 2


def test_parse_school_cafeteria_replacement_and_generic_candidate_choice():
    planner = TimelineCommandPlanner()
    options = planner.parse("将第一天的护国寺小吃改为去清华学校里面的食堂吃", has_active_timeline=True)
    choice = planner.parse("选择第二个候选", has_active_timeline=True)

    assert options is not None
    assert options.scope == "meal"
    assert options.operation == "offer_local_poi_options"
    assert options.day_number == 1
    assert choice is not None
    assert choice.operation == "choose_previous_local_poi_option"
    assert choice.constraints["selectedOptionIndex"] == 2
