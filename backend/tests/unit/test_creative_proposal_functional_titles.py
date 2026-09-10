from __future__ import annotations

from src.services.creative_proposal_title_service import CreativeProposalTitleService


def _segment(
    segment_id: str,
    amap_id: str,
    name: str,
    start_time: str,
    *,
    intent_type: str = "",
    family: str = "",
) -> dict:
    return {
        "id": segment_id,
        "startTime": start_time,
        "poi": {
            "id": amap_id,
            "amapId": amap_id,
            "name": name,
            "city": "北京",
            "source": "amap-place-search",
            "latitude": 39.9,
            "longitude": 116.4,
        },
        "semanticMetadata": {
            "intentType": intent_type,
            "optionalExperienceFamily": family or None,
        },
    }


def _snapshot(*, place: str = "三源里菜市场", family: str = "market_walk") -> dict:
    return {
        "city": "北京",
        "title": "市井市场方向｜北京真实地点草案",
        "creativeBrief": {"briefId": "brief-market", "title": "市井市场方向"},
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment("campus", "B00000001", "清华大学", "09:00", intent_type="campus_visit"),
                    _segment("meal", "B00000002", "四季民福烤鸭店", "12:00", intent_type="meal"),
                    _segment("theme", "B00000003", place, "14:30", family=family),
                    _segment("night", "B00000004", "中央电视塔", "19:10", intent_type="night_view"),
                ],
            }
        ],
        "portfolioPendingSlots": [{"planningSlotId": "pending-art", "displayNeed": "艺术街区与创意园区"}],
    }


def _agent_candidates(snapshot: dict, titles: list[str]) -> dict:
    evidence_ids = [segment["poi"]["amapId"] for day in snapshot["days"] for segment in day["segments"]]
    return {
        "schemaVersion": CreativeProposalTitleService.AGENT_SCHEMA_VERSION,
        "candidates": [{"title": title, "evidenceAmapIds": evidence_ids} for title in titles],
    }


def test_incomplete_material_never_receives_legacy_or_marketing_title():
    snapshot = _snapshot()

    assert CreativeProposalTitleService.project(snapshot, city="北京") == {}
    assert CreativeProposalTitleService.title_variants(snapshot, city="北京") == []
    assert CreativeProposalTitleService.incomplete_status_title(snapshot) == "方案待补全"


def test_agent_titles_remain_content_specific_and_bind_every_materialized_place():
    market = _snapshot()
    heritage = _snapshot(place="模式口历史文化街区", family="heritage_walk")
    market["portfolioPendingSlots"] = []
    heritage["portfolioPendingSlots"] = []

    market_projection = CreativeProposalTitleService.select_agent_candidate(
        market,
        _agent_candidates(
            market,
            ["三源里烟火与京城灯影", "三源里市井风味漫游", "三源里午后寻味慢行"],
        ),
    )
    heritage_projection = CreativeProposalTitleService.select_agent_candidate(
        heritage,
        _agent_candidates(
            heritage,
            ["模式口旧巷与京城灯影", "模式口寻旧漫游京城", "模式口古街风物慢行"],
        ),
    )

    assert market_projection["title"] != heritage_projection["title"]
    assert market_projection["generationSource"] == "agent_generated_title_candidates"
    assert heritage_projection["generationSource"] == "agent_generated_title_candidates"
    assert len(market_projection["validCandidates"]) == 3
    assert len(heritage_projection["validCandidates"]) == 3
    assert set(market_projection["selectedAmapIds"]) == {
        "B00000001",
        "B00000002",
        "B00000003",
        "B00000004",
    }
    assert "艺术街区" not in market_projection["title"]
