from types import SimpleNamespace

import pytest

from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy


@pytest.mark.parametrize(
    ("name", "poi_type"),
    [
        ("中国美术馆", "科教文化服务;美术馆"),
        ("今日美术馆", "科教文化服务;展览馆"),
        ("清华大学艺术博物馆", "科教文化服务;博物馆"),
        ("UCCA 尤伦斯当代艺术中心", "科教文化服务;展览馆"),
        ("北京画院", "科教文化服务;文化艺术场馆"),
        ("红砖艺术中心", "科教文化服务;展览馆"),
        ("故宫博物院", "科教文化服务;博物馆"),
    ],
)
def test_museum_semantic_policy_accepts_museum_evidence(name, poi_type):
    result = IntentCandidateSemanticPolicy().evaluate("museum", SimpleNamespace(name=name, type=poi_type, category="culture"))

    assert result.passed is True
    assert result.confidence >= 0.8


@pytest.mark.parametrize(
    ("name", "poi_type"),
    [
        ("花海畔溪谷", "风景名胜;风景名胜;风景名胜"),
        ("北京西山秘境", "风景名胜;风景名胜"),
        ("城市公园", "风景名胜;公园广场;公园"),
        ("购物中心艺术打卡点", "购物服务;商场"),
        ("文化广场", "风景名胜;公园广场;城市广场"),
        ("餐厅内艺术墙", "餐饮服务;中餐厅"),
    ],
)
def test_museum_semantic_policy_rejects_non_museum_candidates(name, poi_type):
    result = IntentCandidateSemanticPolicy().evaluate("museum", SimpleNamespace(name=name, type=poi_type, category=""))

    assert result.passed is False
    assert result.reason_code == "museum_semantic_mismatch"


def test_exact_entity_cannot_be_silently_replaced_by_another_museum():
    result = IntentCandidateSemanticPolicy().evaluate(
        "museum",
        {"name": "今日美术馆", "type": "科教文化服务;美术馆"},
        exact_entity="中国美术馆",
    )

    assert result.passed is False
    assert result.reason_code == "exact_entity_mismatch"


@pytest.mark.parametrize(
    ("name", "poi_type", "expected"),
    [
        ("清华大学", "科教文化服务;学校;高等院校", True),
        ("北京大学(燕园校区)", "科教文化服务;学校;高等院校", True),
        ("世界公园-飞机参观", "风景名胜;风景名胜;寺庙道观", False),
        ("参观路", "地名地址信息;交通地名;道路名", False),
        ("对外经贸大学北(公交站)", "交通设施服务;公交车站", False),
    ],
)
def test_campus_semantic_policy_requires_campus_name_and_provider_type(name, poi_type, expected):
    result = IntentCandidateSemanticPolicy().evaluate(
        "campus_visit", SimpleNamespace(name=name, type=poi_type, category="all")
    )

    assert result.passed is expected
    if not expected:
        assert result.reason_code == "campus_visit_semantic_mismatch"


def test_area_walk_rejects_zoo_provider_category() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="北京动物园",
            type="风景名胜;公园广场;动物园",
            category="scenic",
            address="西直门外大街",
        ),
        optional_experience_family="heritage_walk",
    )

    assert result.passed is False
    assert result.reason_code == "area_walk_provider_type_mismatch"


def test_unknown_creative_optional_family_rejects_generic_scenic_candidate() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="花海畔溪谷",
            type="风景名胜;风景名胜",
            category="scenic",
            address="北京",
        ),
        optional_experience_family="scenic",
    )

    assert result.passed is False
    assert result.reason_code == "creative_optional_family_unregistered"


def test_unknown_intent_in_registered_creative_optional_family_fails_closed() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "scenic_visit",
        SimpleNamespace(
            name="南锣鼓巷历史文化街区",
            type="风景名胜;文化街区",
            category="scenic",
            address="东城区南锣鼓巷",
        ),
        optional_experience_family="heritage_walk",
    )

    assert result.passed is False
    assert result.reason_code == "creative_optional_intent_unregistered"


def test_area_walk_accepts_structured_street_evidence() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="南锣鼓巷历史文化街区",
            type="风景名胜;风景名胜相关;文化街区",
            category="scenic",
            address="东城区南锣鼓巷",
        ),
        optional_experience_family="heritage_walk",
    )

    assert result.passed is True
    assert result.reason_code == "area_walk_structured_provider_match"


@pytest.mark.parametrize(
    "family",
    ["heritage_walk", "local_life", "market_walk", "art_walk"],
)
def test_area_walk_family_rejects_generic_temple_park_relabeling(family: str) -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="天坛公园",
            type="风景名胜;公园广场;公园",
            category="scenic",
            address="东城区天坛路",
        ),
        optional_experience_family=family,
    )

    assert result.passed is False
    assert result.reason_code == "area_walk_family_mismatch"


def test_area_walk_family_does_not_treat_the_search_hint_as_candidate_evidence() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="天坛公园",
            type="风景名胜;公园广场;公园",
            category="scenic",
            address="东城区天坛路",
            source_note="matchedCandidateHint=市井市场与传统市集",
        ),
        optional_experience_family="market_walk",
    )

    assert result.passed is False
    assert result.reason_code == "area_walk_family_mismatch"


@pytest.mark.parametrize(
    ("family", "name", "poi_type"),
    [
        ("heritage_walk", "南锣鼓巷历史文化街区", "风景名胜;文化街区"),
        ("local_life", "白塔寺社区生活街区", "地名地址信息;普通地名"),
        ("market_walk", "三源里菜市场", "购物服务;综合市场"),
        ("art_walk", "798艺术区", "风景名胜;文化园区"),
    ],
)
def test_area_walk_family_requires_matching_experience_evidence(
    family: str,
    name: str,
    poi_type: str,
) -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(name=name, type=poi_type, category="scenic", address="北京"),
        optional_experience_family=family,
    )

    assert result.passed is True
    assert result.reason_code == "area_walk_structured_provider_match"


@pytest.mark.parametrize(
    ("family", "name", "poi_type"),
    [
        ("heritage_walk", "南锣鼓巷历史文化街区停车场", "交通设施服务;停车场"),
        ("local_life", "幸福社区住宅小区", "商务住宅;住宅区"),
        ("local_life", "幸福里居民区", "商务住宅;住宅区"),
        ("market_walk", "三源里菜市场停车场", "交通设施服务;停车场"),
        ("art_walk", "798艺术区文化发展公司", "公司企业;公司"),
        ("park_relax", "朝阳公园地下车库", "交通设施服务;停车场"),
    ],
)
def test_creative_area_walk_family_rejects_non_experience_entities(
    family: str,
    name: str,
    poi_type: str,
) -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(name=name, type=poi_type, category="", address="北京"),
        optional_experience_family=family,
    )

    assert result.passed is False
    assert result.reason_code == "area_walk_non_experience_entity"


def test_park_relax_accepts_structured_real_park() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="玉渊潭公园",
            type="风景名胜;公园广场;公园",
            category="park",
            address="海淀区西三环中路",
        ),
        optional_experience_family="park_relax",
    )

    assert result.passed is True
    assert result.reason_code == "park_relax_structured_provider_match"


def test_park_relax_rejects_generic_scenic_result() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "area_walk",
        SimpleNamespace(
            name="花海畔溪谷",
            type="风景名胜;风景名胜",
            category="scenic",
            address="北京",
        ),
        optional_experience_family="park_relax",
    )

    assert result.passed is False
    assert result.reason_code == "park_relax_semantic_evidence_missing"


@pytest.mark.parametrize(
    ("intent_type", "name", "poi_type"),
    [
        ("campus_visit", "清华大学", "科教文化服务;学校;高等院校"),
        ("museum", "中国美术馆", "科教文化服务;美术馆"),
        ("meal", "老北京炸酱面馆", "餐饮服务;中餐厅"),
        ("night_view", "中央电视塔", "风景名胜;观景塔"),
    ],
)
def test_registered_non_creative_intents_remain_accepted_without_optional_family(
    intent_type: str,
    name: str,
    poi_type: str,
) -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        intent_type,
        SimpleNamespace(name=name, type=poi_type, category="", address="北京"),
    )

    assert result.passed is True


def test_unregistered_non_creative_intent_keeps_legacy_delegated_behavior() -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "guided_walk",
        SimpleNamespace(
            name="城市慢行空间",
            type="风景名胜;风景名胜",
            category="scenic",
            address="北京",
        ),
    )

    assert result.passed is True
    assert result.reason_code == "semantic_policy_not_required"


@pytest.mark.parametrize(
    ("name", "poi_type", "expected"),
    [
        ("798艺术区", "科教文化服务;文化场馆;艺术中心", True),
        ("北京热门景点", "风景名胜;观景点", False),
    ],
)
def test_registered_creative_experience_alias_still_uses_family_semantics(
    name: str,
    poi_type: str,
    expected: bool,
) -> None:
    result = IntentCandidateSemanticPolicy().evaluate(
        "experience",
        SimpleNamespace(name=name, type=poi_type, category="scenic", address="北京"),
        optional_experience_family="art_walk",
    )

    assert result.passed is expected
    assert result.reason_code == (
        "area_walk_structured_provider_match"
        if expected
        else "area_walk_semantic_evidence_missing"
    )
