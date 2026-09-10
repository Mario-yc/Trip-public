from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.night_view_candidate_policy import NightViewCandidatePolicy


def _grounded(**values):
    values.setdefault("night_availability_status", "verified_open")
    return SimpleNamespace(
        amap_id=values.pop("amap_id", "B0NIGHT001"),
        source=values.pop("source", "amap-place-search"),
        longitude=values.pop("longitude", 116.4),
        latitude=values.pop("latitude", 39.9),
        city=values.pop("city", "北京"),
        **values,
    )


def test_night_view_policy_rejects_closed_dining_and_unverified_controlled_access():
    policy = NightViewCandidatePolicy()

    assert (
        policy.reject_reason(
            _grounded(
                name="北京奥林匹克塔(暂停开放)",
                type="风景名胜;观景台;电视塔",
                category="scenic",
            )
        )
        == "night_view_explicitly_unavailable"
    )
    assert (
        policy.reject_reason(
            _grounded(
                name="中央广播电视塔餐厅",
                type="风景名胜;观景台;电视塔;餐饮服务",
                category="scenic",
            )
        )
        == "night_view_dining_or_non_view"
    )
    assert (
        policy.reject_reason(
            _grounded(
                name="中央广播电视塔",
                type="风景名胜;观景台;电视塔",
                category="scenic",
                night_availability_status="unknown",
            )
        )
        == "night_view_availability_unverified"
    )
    assert (
        policy.reject_reason(
            _grounded(
                name="国贸CBD购物中心",
                type="购物服务;购物中心",
                category="shopping",
            )
        )
        == "night_view_public_access_type_incompatible"
    )


def test_provider_evidence_dict_preserves_opening_window_for_final_gate():
    candidate = {
        "id": "B0NIGHT010",
        "name": "城市观景塔A",
        "type": "风景名胜;观景台;电视塔",
        "category": "scenic",
        "city": "北京",
        "source": "amap-place-search",
        "longitude": 116.4,
        "latitude": 39.9,
        "openTimeToday": "18:00-22:00",
    }

    assert (
        NightViewCandidatePolicy().reject_reason(
            candidate,
            amap_identity=candidate["id"],
        )
        == ""
    )


def test_public_city_view_rejects_waterfront_or_park_evidence_without_city_view_signal():
    policy = NightViewCandidatePolicy()
    wetland = _grounded(
        name="湿地水岸夜游步道",
        type="风景名胜;水域景观;湿地公园;滨水步道",
        category="风景名胜",
    )

    public_city = policy.evaluate(
        wetland,
        amap_identity="B0NIGHTWETLAND",
        experience_family="public_city_view",
    )
    waterfront = policy.evaluate(
        wetland,
        amap_identity="B0NIGHTWETLAND",
        experience_family="waterfront",
    )

    assert public_city["decision"] == "rejected"
    assert public_city["rejectReason"] == "public_city_view_evidence_missing"
    assert waterfront["decision"] == "accepted"


def test_experience_access_policy_requires_fresh_controlled_access_evidence():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    policy = NightViewCandidatePolicy()
    candidate = {
        "id": "B0NIGHT010",
        "name": "城市观景塔A",
        "type": "风景名胜;观景台;电视塔",
        "category": "scenic",
        "city": "北京",
        "source": "amap-place-search",
        "longitude": 116.4,
        "latitude": 39.9,
        "openTimeToday": "18:00-22:00",
        "providerEvidenceQueriedAt": (now - timedelta(hours=25)).isoformat(),
    }

    result = policy.evaluate_access_policy(
        candidate,
        access_policy="verified_controlled_access",
        evidence_freshness={"maxAgeHours": 24},
        now=now,
        spec_fingerprint="spec-night-v1",
    )

    assert result["decision"] == "pending_evidence"
    assert result["reasonCode"] == "experience_access_evidence_stale"
    assert result["accessClass"] == "controlled_access"
    assert result["evidenceAgeHours"] == 25.0


def test_experience_access_policy_can_exempt_public_outdoor_without_closure():
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    candidate = {
        "id": "B0NIGHT011",
        "name": "亮马河国际风情水岸",
        "type": "风景名胜;水域景观;滨水步道",
        "category": "滨水步道",
        "city": "北京",
        "source": "amap-place-search",
        "longitude": 116.4,
        "latitude": 39.9,
    }

    result = NightViewCandidatePolicy().evaluate_access_policy(
        candidate,
        access_policy="public_outdoor_or_verified_controlled_access",
        evidence_freshness={
            "maxAgeHours": 24,
            "requiredForPublicOutdoor": False,
            "allowExplicitNoClosure": True,
        },
        now=now,
        spec_fingerprint="spec-night-v1",
    )

    assert result["decision"] == "accepted"
    assert result["accessClass"] == "public_outdoor"
    assert result["policyExemption"] == "public_outdoor_no_explicit_closure"


def test_city_balcony_provider_entity_uses_the_same_public_outdoor_policy_as_search():
    candidate = {
        "id": "B0MGOZVIG5",
        "amapId": "B0MGOZVIG5",
        "name": "城市阳台",
        "type": "风景名胜;风景名胜相关;旅游景点",
        "category": "experience",
        "providerTypeCode": "110000",
        "city": "北京市",
        "source": "amap-place-search",
        "longitude": 116.484895,
        "latitude": 40.005151,
    }

    result = NightViewCandidatePolicy().evaluate_access_policy(
        candidate,
        access_policy="public_outdoor_or_verified_controlled_access",
        evidence_freshness={
            "maxAgeHours": 24,
            "requiredForControlledAccess": True,
            "requiredForPublicOutdoor": False,
            "allowExplicitNoClosure": True,
        },
        spec_fingerprint="spec-night-v1",
    )

    assert NightViewCandidatePolicy().evaluate(candidate)["decision"] == "accepted"
    assert result["decision"] == "accepted"
    assert result["accessClass"] == "public_outdoor"
    assert result["policyExemption"] == "public_outdoor_no_explicit_closure"


def test_night_view_policy_rejects_generic_range_and_weak_entities():
    policy = NightViewCandidatePolicy()

    assert (
        policy.reject_reason(_grounded(name="夜景观景点", type="风景名胜", category="scenic"))
        == "generic_night_view_placeholder"
    )
    assert (
        policy.reject_reason(_grounded(name="景山公园附近范围", type="风景名胜", category="scenic"))
        == "range_night_view_anchor"
    )
    assert (
        policy.reject_reason(_grounded(name="某小区广场", type="地名地址信息", category="scenic"))
        == "weak_night_view_entity"
    )


def test_night_view_policy_requires_strong_city_night_view_signal_for_ordinary_parks():
    policy = NightViewCandidatePolicy()

    assert (
        policy.reject_reason(_grounded(name="中山公园", type="风景名胜;公园广场;公园", category="scenic"))
        == "night_view_signal_missing"
    )
    assert (
        policy.reject_reason(_grounded(name="太庙", type="风景名胜;寺庙道观", category="scenic"))
        == "night_view_signal_missing"
    )
    assert (
        policy.reject_reason(_grounded(name="中塔公园", type="风景名胜;公园广场;公园", category="scenic"))
        == "weak_night_view_entity"
    )
    assert (
        policy.reject_reason(_grounded(name="人塔雕塑", type="风景名胜;地标", category="scenic"))
        == "weak_night_view_entity"
    )
    assert (
        policy.reject_reason(
            _grounded(name="北京奥林匹克公园-碧玉公园", type="风景名胜;公园广场;公园", category="scenic")
        )
        == "night_view_subpoi_requires_parent"
    )
    assert policy.reject_reason(_grounded(name="景山公园", type="风景名胜;观景台;城市观景点", category="scenic")) == ""
    assert (
        policy.reject_reason(_grounded(name="中央广播电视塔", type="风景名胜;观景台;电视塔", category="scenic")) == ""
    )


def test_night_view_policy_accepts_only_entity_aligned_structured_hint_provenance():
    policy = NightViewCandidatePolicy()

    liangma = _grounded(
        name="亮马河国际风情水岸",
        type="风景名胜;水域景观",
        category="scenic",
        city="北京",
        source_note="来源：高德地图",
        _trip_matched_hint="亮马河夜游步道",
    )
    unrelated_olympic_park = _grounded(
        name="奥林匹克森林公园",
        type="风景名胜;公园广场;公园",
        category="scenic",
        city="北京",
        source_note="来源：高德地图",
        _trip_matched_hint="奥林匹克塔",
    )

    assert policy.reject_reason(liangma) == ""
    assert policy.reject_reason(unrelated_olympic_park) == "night_view_hint_entity_mismatch"


def test_night_view_policy_rejects_generic_or_substring_only_hint_matches():
    policy = NightViewCandidatePolicy()

    for name in ("地标", "地标观景点", "城市观景点", "观景台", "观景平台"):
        assert (
            policy.reject_reason(
                _grounded(
                    name=name,
                    type="风景名胜;观景点",
                    category="scenic",
                    city="北京",
                    source_note="来源：高德地图",
                    _trip_matched_hint="地标",
                )
            )
            == "generic_night_view_placeholder"
        )

    substring_only = _grounded(
        name="国际",
        type="地名地址信息;普通地名",
        category="address",
        city="北京",
        source_note="来源：高德地图",
        _trip_matched_hint="北京国际贸易中心夜景",
    )
    assert policy.reject_reason(substring_only) == "night_view_hint_entity_mismatch"


def test_source_note_or_query_signal_cannot_prove_unrelated_candidate():
    policy = NightViewCandidatePolicy()
    candidate = _grounded(
        name="花溪谷",
        amap_id="B0MAFLUNSL",
        type="风景名胜;旅游景点",
        category="scenic",
        source_note="query=北京夜景；matchedCandidateHint：亮马河夜游步道",
    )

    evidence = policy.evaluate(candidate)

    assert evidence["decision"] == "rejected"
    assert evidence["rejectReason"] == "night_view_hint_entity_mismatch"
    assert evidence["structuredHintCore"] == "亮马河"
    assert evidence["candidateEntityCore"] == "花溪谷"
    assert evidence["hintEntityMatched"] is False


def test_structured_hint_mismatch_rejects_candidate_even_with_intrinsic_signal():
    policy = NightViewCandidatePolicy()
    candidate = _grounded(
        name="亮马河国际风情水岸",
        amap_id="B0NIGHT002",
        type="风景名胜;水域景观",
        category="scenic",
        source_note="query=花溪谷；matchedCandidateHint：花溪谷",
    )

    evidence = policy.evaluate(candidate)

    assert evidence["intrinsicSignals"] == ["水岸"]
    assert evidence["hintEntityMatched"] is False
    assert evidence["decision"] == "rejected"
    assert evidence["rejectReason"] == "night_view_hint_entity_mismatch"


def test_non_binding_search_keyword_does_not_override_intrinsic_candidate_evidence():
    policy = NightViewCandidatePolicy()
    candidate = _grounded(
        name="奥林匹克塔",
        amap_id="B0NIGHT003",
        type="风景名胜;塔;观景点",
        category="scenic",
        _trip_matched_hint="北京 奥林匹克公园 夜景 地标",
        _trip_matched_hint_binding="search_query",
    )

    evidence = policy.evaluate(candidate)

    assert evidence["decision"] == "accepted"
    assert evidence["structuredHintBinding"] == "search_query"


def test_missing_amap_identity_is_rejected_before_semantic_acceptance():
    policy = NightViewCandidatePolicy()
    evidence = policy.evaluate(
        SimpleNamespace(
            name="中央广播电视塔",
            type="风景名胜;观景台;电视塔",
            category="scenic",
            city="北京",
            longitude=116.3,
            latitude=39.9,
            source="unresolved-map-poi",
        )
    )

    assert evidence["decision"] == "rejected"
    assert evidence["rejectReason"] == "night_view_amap_identity_missing"
