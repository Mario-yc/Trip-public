from pathlib import Path

from src.services.agent_skill_service import AgentSkillService


def test_loads_curated_backend_skills():
    skills = {skill.name: skill for skill in AgentSkillService().load_skills()}

    assert {
        "amap_poi_grounding",
        "patch_before_write",
        "pending_poi_state_machine",
        "rollback_supersession",
        "source_transparency",
    }.issubset(skills)
    assert "高德" in skills["amap_poi_grounding"].content
    assert "patch" in skills["patch_before_write"].tags


def test_selects_pending_poi_skill_from_server_state():
    selected = AgentSkillService().select_skills(
        "这个候选可以吗？",
        {
            "selectedCity": "北京",
            "pendingAmapPoiCandidates": [{"id": "cand_1", "query": "胡同餐厅"}],
            "currentItinerarySnapshot": {"days": []},
        },
    )

    assert len(selected) <= 2
    assert selected[0]["name"] == "pending_poi_state_machine"
    assert any(item["name"] == "patch_before_write" for item in selected)


def test_selects_amap_and_patch_for_itinerary_edit_intent():
    selected = AgentSkillService().select_skills(
        "把故宫移动到下午，再添加一个地图上选中的餐厅",
        {
            "selectedCity": "北京",
            "selectedMapPoi": {"amapId": "B000FOOD", "name": "胡同餐厅"},
            "currentItinerarySnapshot": {"days": [{"segments": []}]},
        },
    )
    names = {item["name"] for item in selected}

    assert len(selected) <= 2
    assert "amap_poi_grounding" in names
    assert "patch_before_write" in names


def test_skill_context_respects_total_length_limit(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "long_skill.md").write_text(
        "# Long Skill\n\nTags: amap, poi\n\n" + "Use AMap grounding. " * 80,
        encoding="utf-8",
    )
    service = AgentSkillService(skills_dir=skills_dir, max_context_chars=220)

    selected = service.select_skills("高德 POI", {}, limit=2)
    context = service.build_skill_context(selected)

    assert len(context) <= 220
    assert context.startswith("## Long Skill")


def test_skill_frontmatter_controls_intent_and_budget(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "source_check.md").write_text(
        """---
title: Source Check
tags: source, ticket
applies_to: source
max_context_chars: 90
---
Use official sources and never invent ticket data. """ + "Extra details. " * 20,
        encoding="utf-8",
    )
    service = AgentSkillService(skills_dir=skills_dir, max_context_chars=500)

    selected = service.select_skills("查一下故宫开放时间和官方预约来源", {}, limit=1)
    context = service.build_skill_context(selected, max_chars=500)

    assert selected[0]["name"] == "source_check"
    assert selected[0]["appliesTo"] == ["source"]
    assert len(selected[0]["text"]) <= 90
    assert context.startswith("## Source Check")


def test_skill_applies_to_prefers_matching_intent_over_tag_overlap(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "source_only.md").write_text(
        """---
title: Source Only
tags: patch
applies_to: source
---
Use this only for source checking even though it mentions patch.
""",
        encoding="utf-8",
    )
    (skills_dir / "patch_only.md").write_text(
        """---
title: Patch Only
tags: patch
applies_to: patch
---
Use itinerary patch operations for timeline edits.
""",
        encoding="utf-8",
    )
    service = AgentSkillService(skills_dir=skills_dir, max_context_chars=500)

    selected = service.select_skills(
        "把故宫移动到下午",
        {"currentItinerarySnapshot": {"activeVersionId": "ver_1", "days": []}},
        limit=1,
    )

    assert selected[0]["name"] == "patch_only"
    assert selected[0]["appliesTo"] == ["patch"]
