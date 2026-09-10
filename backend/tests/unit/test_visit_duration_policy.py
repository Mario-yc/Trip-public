from src.services.visit_duration_policy import VisitDurationPolicy


def test_museum_duration_is_clamped_to_lower_bound():
    decision = VisitDurationPolicy().normalize_duration(15, kind="visit", category="museum")

    assert decision.duration_minutes == 90
    assert decision.source == "model_clamped"


def test_scenic_duration_is_clamped_to_upper_bound():
    decision = VisitDurationPolicy().normalize_duration(600, kind="visit", category="scenic")

    assert decision.duration_minutes == 180
    assert decision.source == "model_clamped"


def test_relaxed_family_context_adds_more_conservative_default():
    decision = VisitDurationPolicy().normalize_duration(None, kind="visit", category="scenic", context={"latestUserMessage": "亲子轻松不赶"})

    assert decision.duration_minutes == 105
    assert decision.source == "policy_default"


def test_segment_dict_end_time_matches_duration_and_notes_source():
    segment = {
        "startTime": "09:00",
        "durationMinutes": 600,
        "kind": "visit",
        "poi": {"category": "scenic"},
        "notes": "核心景点",
    }

    VisitDurationPolicy().normalize_segment_dict(segment)

    assert segment["durationMinutes"] == 180
    assert segment["endTime"] == "12:00"
    assert "durationSource=model_clamped" in segment["notes"]


def test_segment_dict_replaces_previous_duration_marker():
    segment = {
        "startTime": "09:00",
        "durationMinutes": 15,
        "kind": "visit",
        "poi": {"category": "museum"},
        "notes": "核心景点；durationSource=model_accepted; durationConfidence=0.78",
    }

    VisitDurationPolicy().normalize_segment_dict(segment)

    assert segment["durationMinutes"] == 90
    assert "durationSource=model_clamped" in segment["notes"]
    assert "durationSource=model_accepted" not in segment["notes"]
    assert segment["notes"].count("durationSource=") == 1


def test_intent_type_wins_over_generic_visit_kind_and_persists_range_metadata():
    night = VisitDurationPolicy().normalize_duration(None, kind="visit", intent_type="night_view")
    campus = VisitDurationPolicy().normalize_duration(None, kind="visit", intent_type="campus_visit")

    assert (night.min_minutes, night.preferred_minutes, night.max_minutes) == (60, 75, 90)
    assert (campus.min_minutes, campus.preferred_minutes, campus.max_minutes) == (75, 90, 120)
    assert campus.to_metadata()["duration"]["source"] == "intent_and_poi_policy"


def test_dynamic_duration_estimates_cover_campus_meal_and_tower_subtypes():
    policy = VisitDurationPolicy()

    major = policy.normalize_duration(None, kind="visit", intent_type="campus_visit", category="major_campus")
    cafeteria = policy.normalize_duration(None, kind="meal", category="校园食堂")
    tower = policy.normalize_duration(None, kind="visit", intent_type="night_view", category="中央电视塔")

    assert 120 <= major.preferred_minutes <= 150
    assert 35 <= cafeteria.preferred_minutes <= 50
    assert 75 <= tower.preferred_minutes <= 120


def test_user_locked_duration_is_never_replaced_by_policy():
    decision = VisitDurationPolicy().normalize_duration(
        60,
        kind="visit",
        intent_type="campus_visit",
        context={"estimateMetadata": {"duration": {"userLocked": True}}},
    )

    assert decision.preferred_minutes == 60
    assert decision.user_locked is True
    assert decision.source == "user_locked"


def test_photo_and_creative_metadata_do_not_trigger_deep_pace():
    decision = VisitDurationPolicy().normalize_duration(
        None,
        kind="visit",
        intent_type="campus_visit",
        context={"selectedMapPoi": {"photos": []}, "photoUrl": "https://example/photo.jpg", "creativeVariantId": "family_light"},
    )

    assert decision.preferred_minutes == 90
    assert decision.preferred_minutes % 5 == 0


def test_explicit_deep_pace_uses_five_minute_granularity():
    decision = VisitDurationPolicy().normalize_duration(
        None, kind="visit", intent_type="campus_visit", context={"effectiveUserMessage": "清华大学想深度游和摄影"}
    )

    assert decision.preferred_minutes == 105
    assert decision.preferred_minutes % 5 == 0
