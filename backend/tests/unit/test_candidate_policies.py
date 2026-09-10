from src.models.poi import POI
from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy
from src.services.night_view_candidate_policy import NightViewCandidatePolicy


def poi(name: str, poi_type: str = "", category: str = "scenic") -> POI:
    return POI(
        id=name,
        name=name,
        city="北京",
        category=category,
        latitude=39.9,
        longitude=116.4,
        type=poi_type,
        source="amap-place-search",
        amap_id="B0TESTPOI01",
        confidence=0.92,
    )


def test_campus_policy_rejects_weak_education_entities_but_allows_named_college():
    policy = CampusCandidatePolicy()

    assert policy.reject_reason(poi("建行大学", "科教文化服务;成人教育")) == "weak_campus_entity"
    assert policy.reject_reason(poi("商务部老年大学", "科教文化服务;学校")) == "weak_campus_entity"
    assert policy.reject_reason(poi("北京电影学院", "科教文化服务;学校;高等院校")) == ""
    assert policy.reject_reason(poi("示例大学新校区", "科教文化服务;学校;高等院校")) == "campus_remote_branch"
    assert policy.reject_reason(poi("示例大学新校区", "科教文化服务;学校;高等院校"), "参观示例大学新校区") == ""


def test_meal_policy_rejects_institutional_lodging_and_out_of_city_food():
    policy = MealCandidateQualityPolicy()

    assert policy.evaluate(
        "午餐 当地特色美食", [], poi("某大学食堂", "餐饮服务;食堂", "food"), city="北京"
    ).hard_reject_reasons == ["institutional_meal"]
    assert policy.evaluate(
        "晚餐 当地特色美食", [], poi("酒店中餐厅", "住宿服务;宾馆酒店", "food"), city="北京"
    ).hard_reject_reasons == ["hotel_meal"]
    assert policy.evaluate(
        "午餐 北京当地特色美食", [], poi("南京大牌档", "餐饮服务;中餐厅", "food"), city="北京"
    ).hard_reject_reasons == ["local_food_evidence_missing"]
    assert (
        policy.evaluate(
            "午餐 北京当地特色美食",
            [],
            {
                "name": "社区餐馆",
                "type": "餐饮服务;中餐厅",
                "city": "北京",
                "providerTypeCode": "050100",
                "tags": ["地方风味"],
                "sourceClaims": [{"claimKey": "local_food", "stance": "support", "locality": "北京"}],
            },
            city="北京",
        ).acceptable
        is True
    )


def test_night_view_policy_rejects_weak_entities_and_subpoints_without_parent():
    policy = NightViewCandidatePolicy()

    assert policy.reject_reason(poi("亚朵竹居流动图书馆", "科教文化服务;图书馆")) == "weak_night_view_entity"
    assert policy.reject_reason(poi("鸟巢文化中心", "科教文化服务;文化中心")) == "weak_night_view_entity"
    assert policy.reject_reason(poi("中央广播电视总台总台文创店", "购物服务;专卖店")) == "weak_night_view_entity"
    assert policy.reject_reason(poi("朝阳公园-生命之源大树", "风景名胜;公园")) == "night_view_subpoi_requires_parent"
    assert policy.reject_reason(poi("景山公园", "风景名胜;公园;观景点")) == ""
