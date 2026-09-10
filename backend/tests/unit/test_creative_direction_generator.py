from src.services.creative_direction_generator import CreativeDirectionGenerator


def test_direction_signature_is_structural_and_next_batch_excludes_prior_directions():
    first = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus_day_1", "night_view_day_2"],
        day_anchor_targets={1: 3, 2: 3},
        used_signatures=[],
        limit=4,
    )
    second = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus_day_1", "night_view_day_2"],
        day_anchor_targets={1: 3, 2: 3},
        used_signatures=[item["directionSignature"] for item in first],
        limit=4,
    )
    assert len(first) == 4
    assert len(second) == 4
    assert not ({item["directionSignature"] for item in first} & {item["directionSignature"] for item in second})
    assert all(item["themeFamilies"] for item in first)
    assert all(item["generationSource"] == "seed_composition" for item in first)
    assert len({tuple(item["themeFamilies"]) for item in [*first, *second]}) == 8


def test_title_is_not_part_of_direction_identity():
    base = {
        "primaryAxis": "photo_night",
        "secondaryAxes": [],
        "experienceFamilies": ["night_view"],
        "activityModes": ["photography"],
        "dayRoleSignature": ["day_1:anchors_2"],
        "dayAnchorTargets": {"1": 2},
        "hardSoftGoalPlacementStrategy": ["campus_day_1"],
    }
    assert CreativeDirectionGenerator.signature({**base, "title": "标题 A"}) == CreativeDirectionGenerator.signature(
        {**base, "title": "标题 B"}
    )


def test_direction_signature_binds_candidate_supply_state():
    base = {
        "primaryAxis": "culture_deep_dive",
        "themeFamilies": ["heritage_walk"],
        "candidateSupply": {"candidateIds": [], "familyCounts": {"heritage_walk": 0}},
    }

    assert CreativeDirectionGenerator.signature(
        {**base, "candidateSupply": {**base["candidateSupply"], "supplyState": "pending_search"}}
    ) != CreativeDirectionGenerator.signature(
        {**base, "candidateSupply": {**base["candidateSupply"], "supplyState": "observed_complete"}}
    )


def test_provider_brief_matches_the_authoritative_frontier_candidate():
    candidates = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus:1"],
        day_anchor_targets={1: 3},
        used_signatures=[],
        limit=4,
    )

    matched = CreativeDirectionGenerator.match_candidate(
        primary_axis=candidates[1]["primaryAxis"],
        secondary_axes=candidates[1]["secondaryAxes"],
        experience_families=candidates[1]["experienceFamilies"],
        candidates=candidates,
    )

    assert matched is candidates[1]


def test_zero_limit_returns_no_direction():
    assert (
        CreativeDirectionGenerator.generate_next(
            hard_goal_strategy=["campus:1"],
            day_anchor_targets={1: 2},
            used_signatures=[],
            limit=0,
        )
        == []
    )


def _inventory_cluster(area: str, family: str, index: int) -> list[dict]:
    return [
        {
            "candidateId": f"B{index:08d}{suffix}",
            "name": f"{area}{family}{suffix}",
            "family": family,
            "intentType": "area_walk" if family.endswith("_walk") or family == "local_life" else family,
            "dayNumber": 1 + (index % 2),
            "timeWindow": "evening" if family == "night_view" else "afternoon",
            "areaKey": area,
            "scoreEligible": True,
            "longitude": 116.30 + index * 0.01 + suffix * 0.001,
            "latitude": 39.90 + index * 0.01 + suffix * 0.001,
        }
        for suffix in (1, 2)
    ]


def test_admitted_candidate_inventory_generates_five_supply_backed_directions():
    inventory = [
        *_inventory_cluster("东城文化带", "heritage_walk", 1),
        *_inventory_cluster("朝阳艺术带", "art_walk", 2),
        *_inventory_cluster("西城社区带", "local_life", 3),
        *_inventory_cluster("南城市场带", "market_walk", 4),
        *_inventory_cluster("北城公园带", "park_relax", 5),
    ]

    directions = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus:2", "night_view:2"],
        day_anchor_targets={1: 4, 2: 4},
        used_signatures=[],
        limit=5,
        candidate_inventory=inventory,
        experience_intent={"decisionAxes": ["culture_deep_dive", "local_immersion", "photo_night"]},
        city="北京",
    )

    assert len(directions) == 5
    assert len({item["directionSignature"] for item in directions}) == 5
    assert len({item["title"] for item in directions}) == 5
    assert all(item["generationSource"] == "admitted_candidate_inventory" for item in directions)
    assert all(item["candidateSupply"]["scoreEligibleCount"] == 2 for item in directions)
    assert all(item["candidateSupply"]["candidateIds"] for item in directions)
    assert all(item["themeFamilies"] and item["requiredGoals"] and item["dayRoles"] for item in directions)
    assert all(item["noveltyEvidence"]["newPhysicalPoiIds"] for item in directions)
    assert all(item["feasibilityEvidence"]["candidateSufficient"] is True for item in directions)


def test_inventory_exhaustion_does_not_fabricate_seed_directions():
    inventory = [
        *_inventory_cluster("文化片区", "heritage_walk", 1),
        *_inventory_cluster("艺术片区", "art_walk", 2),
        *_inventory_cluster("市场片区", "market_walk", 3),
        {
            **_inventory_cluster("无效片区", "local_life", 4)[0],
            "scoreEligible": False,
        },
    ]
    first = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus:1"],
        day_anchor_targets={1: 3},
        used_signatures=[],
        limit=6,
        candidate_inventory=inventory,
        city="北京",
    )
    second = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus:1"],
        day_anchor_targets={1: 3},
        used_signatures=[item["directionSignature"] for item in first],
        limit=6,
        candidate_inventory=inventory,
        city="北京",
    )

    assert len(first) == 3
    assert second == []


def test_inventory_does_not_reuse_physical_pois_already_visible():
    inventory = [
        *_inventory_cluster("已展示片区", "heritage_walk", 1),
        *_inventory_cluster("新片区", "art_walk", 2),
    ]

    directions = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["campus_visit:2"],
        day_anchor_targets={1: 2},
        used_signatures=[],
        limit=4,
        candidate_inventory=inventory,
        excluded_candidate_ids=["B000000011", "B000000012"],
    )

    assert len(directions) == 1
    assert directions[0]["candidateSupply"]["candidateIds"] == [
        "B000000021",
        "B000000022",
    ]


def test_shared_required_goal_supply_does_not_become_a_fake_theme_variant():
    inventory = [
        *_inventory_cluster("共享夜景", "night_view", 1),
        *_inventory_cluster("独立艺术", "art_walk", 2),
    ]

    directions = CreativeDirectionGenerator.generate_next(
        hard_goal_strategy=["night_view:2", "meal:2"],
        day_anchor_targets={1: 3, 2: 3},
        used_signatures=[],
        limit=4,
        candidate_inventory=inventory,
    )

    assert len(directions) == 1
    assert directions[0]["themeFamilies"] == ["art_walk"]
