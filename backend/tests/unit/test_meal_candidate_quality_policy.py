from src.services.meal_candidate_quality_policy import MealCandidateQualityPolicy


def test_local_food_requires_structured_provider_evidence_and_claim():
    policy = MealCandidateQualityPolicy()
    name_only = policy.evaluate(
        "当地特色晚餐",
        ["本地美食"],
        {"name": "本地特色餐厅", "type": "餐饮服务", "city": "深圳"},
        city="深圳",
    )
    grounded = policy.evaluate(
        "当地特色晚餐",
        [],
        {
            "name": "社区餐馆",
            "type": "餐饮服务;中餐厅",
            "providerTypeCode": "050100",
            "tags": ["地方菜"],
            "sourceClaims": [{"claimKey": "local_food", "stance": "support", "locality": "广州"}],
        },
        city="广州",
    )

    assert name_only.acceptable is False
    assert "local_food_evidence_missing" in name_only.hard_reject_reasons
    assert grounded.acceptable is True
    assert grounded.local_relevance_score > 0


def test_generic_negative_filters_remain_city_neutral():
    policy = MealCandidateQualityPolicy()
    dinner_coffee = policy.evaluate("当地特色晚餐", [], {"name": "社区咖啡店", "type": "咖啡厅"})
    canteen = policy.evaluate("当地特色午餐", [], {"name": "员工食堂", "type": "餐饮服务"})
    assert "coffee_not_complete_dinner" in dinner_coffee.hard_reject_reasons
    assert "institutional_meal" in canteen.hard_reject_reasons


def test_authoritative_amap_city_specific_cuisine_subtype_is_a_positive_quality_signal():
    quality = MealCandidateQualityPolicy().evaluate(
        "当地特色午餐",
        [],
        {
            "id": "B0LOCALFOOD",
            "name": "嘉宴小厨饺子馆(五道口店)",
            "type": "餐饮服务;中餐厅;北京菜",
            "providerTypeCode": "050118",
            "tags": [],
            "source": "amap-place-search",
            "sourceClaims": [],
        },
        city="北京",
    )

    assert quality.acceptable is True
    assert quality.soft_reasons == ["authoritative_amap_local_food_subtype"]


def test_authoritative_amap_exact_city_cuisine_tag_is_a_positive_quality_signal():
    policy = MealCandidateQualityPolicy()
    exact = policy.evaluate(
        "当地特色午餐",
        [],
        {
            "id": "B0LOCALFOOD",
            "name": "示例餐厅",
            "type": "餐饮服务;中餐厅;中餐厅",
            "providerTypeCode": "050100",
            "tags": ["北京菜", "烤鸭"],
            "source": "amap-place-search",
        },
        city="北京",
    )
    generic = policy.evaluate(
        "当地特色午餐",
        [],
        {
            "id": "B0GENERIC",
            "name": "示例餐厅",
            "type": "餐饮服务;中餐厅;中餐厅",
            "providerTypeCode": "050100",
            "tags": ["地方风味", "烤鸭"],
            "source": "amap-place-search",
        },
        city="北京",
    )
    wrong_city = policy.evaluate(
        "当地特色午餐",
        [],
        {
            "id": "B0WRONGCITY",
            "name": "示例餐厅",
            "type": "餐饮服务;中餐厅;中餐厅",
            "providerTypeCode": "050100",
            "tags": ["北京菜"],
            "source": "amap-place-search",
        },
        city="上海",
    )

    assert exact.acceptable is True
    assert exact.soft_reasons == ["authoritative_amap_local_food_subtype"]
    assert generic.hard_reject_reasons == ["local_food_evidence_missing"]
    assert wrong_city.hard_reject_reasons == ["local_food_evidence_missing"]


def test_unrelated_locality_support_claim_does_not_prove_local_food():
    quality = MealCandidateQualityPolicy().evaluate(
        "当地特色午餐",
        [],
        {
            "name": "社区餐馆",
            "type": "餐饮服务;中餐厅",
            "providerTypeCode": "050100",
            "sourceClaims": [
                {
                    "claimKey": "wheelchair_accessible",
                    "stance": "support",
                    "locality": "北京",
                }
            ],
        },
        city="北京",
    )

    assert quality.acceptable is False
    assert quality.hard_reject_reasons == ["local_food_evidence_missing"]


def test_non_food_poi_with_local_food_claim_is_rejected():
    quality = MealCandidateQualityPolicy().evaluate(
        "当地特色午餐",
        [],
        {
            "name": "北京民俗文化馆",
            "type": "科教文化服务;文化馆",
            "providerTypeCode": "140000",
            "sourceClaims": [
                {
                    "claimKey": "local_food",
                    "stance": "support",
                    "locality": "北京",
                }
            ],
        },
        city="北京",
    )

    assert quality.acceptable is False
    assert quality.hard_reject_reasons == ["local_food_evidence_missing"]
