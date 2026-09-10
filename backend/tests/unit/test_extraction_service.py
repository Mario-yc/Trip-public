from src.services.vision_service import VisionService


def test_vision_service_extracts_city_and_photo_style_from_text():
    result = VisionService().extract_from_materials(
        city_hint=None,
        text_items=["北京 三天两晚，预算 3000 元，想拍照但不想太赶"],
        social_links=["https://example.com/source"],
    )

    assert result.city_candidates == ["北京"]
    assert "拍照优先" in result.style_tags
    assert result.budget_clues
    assert result.poi_candidates


def test_vision_service_prioritizes_explicit_university_pois_over_city_defaults():
    result = VisionService().extract_from_materials(
        city_hint="北京",
        text_items=["上午去北京大学，下午去清华大学"],
        social_links=[],
    )

    names = [candidate["name"] for candidate in result.poi_candidates]
    assert names[:2] == ["北京大学", "清华大学"]
    assert "故宫博物院" not in names
    assert "天坛公园" not in names
