from src.services.cross_day_poi_reuse_policy import CrossDayPoiReusePolicy


def test_same_day_duplicate_is_rejected_but_cross_day_same_amap_is_penalized():
    policy = CrossDayPoiReusePolicy()

    same_day = policy.evaluate(
        amap_id="A1",
        canonical_entity="清华大学",
        family="campus",
        day_number=1,
        prior_occurrences=[{"dayNumber": 1, "amapId": "A1", "canonicalEntity": "清华大学"}],
    )
    cross_day = policy.evaluate(
        amap_id="A1",
        canonical_entity="清华大学",
        family="campus",
        day_number=2,
        prior_occurrences=[{"dayNumber": 1, "amapId": "A1", "canonicalEntity": "清华大学"}],
    )

    assert same_day.allowed is False
    assert same_day.reason_code == "same_day_duplicate_amap"
    assert cross_day.allowed is True
    assert cross_day.penalty == 0.35
    assert cross_day.previous_day_numbers == [1]


def test_explicit_repeat_removes_cross_day_penalty():
    decision = CrossDayPoiReusePolicy().evaluate(
        amap_id="A1",
        canonical_entity="清华大学",
        family="campus",
        day_number=2,
        prior_occurrences=[{"dayNumber": 1, "amapId": "A1", "canonicalEntity": "清华大学"}],
        explicit_repeat_requested=True,
    )

    assert decision.allowed is True
    assert decision.penalty == 0
    assert decision.reuse_allowed_reason == "explicit_repeat_requested"
