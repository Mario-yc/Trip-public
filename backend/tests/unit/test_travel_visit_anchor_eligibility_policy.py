from src.services.travel_visit_anchor_eligibility_policy import TravelVisitAnchorEligibilityPolicy


def test_functional_facilities_are_area_seeds_not_final_local_life_anchors():
    policy = TravelVisitAnchorEligibilityPolicy()
    for name in ("社区服务站", "养老服务驿站", "街道便民点", "地下停车场"):
        decision = policy.evaluate({"name": name, "type": "公共设施"}, family="local_life")
        assert decision.classification != "final_visit_anchor"

    assert policy.evaluate({"name": "三源里菜市场", "type": "农贸市场"}, family="local_life").classification == "final_visit_anchor"
    assert policy.evaluate({"name": "东四社区生活街区", "type": "特色街区"}, family="local_life").classification == "final_visit_anchor"
    assert policy.evaluate({"name": "烟袋斜街", "type": "商业街"}, family="market_walk").classification == "final_visit_anchor"



def test_recognized_public_art_district_is_a_final_visit_anchor() -> None:
    decision = TravelVisitAnchorEligibilityPolicy().evaluate(
        {"name": "北京798艺术区", "type": "风景名胜;文化园区;艺术区"},
        family="art_walk",
    )

    assert decision.classification == "final_visit_anchor"
