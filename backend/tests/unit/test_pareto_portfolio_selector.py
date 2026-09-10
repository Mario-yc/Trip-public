from src.services.creative_planning_models import CreativeBrief, PlanCandidate, PlanScoreVector
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector


def _candidate(name, axis, score=80):
    return PlanCandidate(
        proposalId=f"proposal_{name}",
        portfolioId="portfolio_1",
        brief=CreativeBrief(
            briefId=f"brief_{name}", title=name, primaryAxis=axis, requiredGoalIds=["campus", "museum"]
        ),
        itinerarySnapshot={},
        groundedEvidence=[{"name": name, "family": axis}],
        score=PlanScoreVector(
            hardConstraintPassed=True,
            preferenceFit=score,
            thematicCoherence=score,
            experienceDiversity=score,
            routeEfficiency=score,
            pacingQuality=score,
            novelty=score,
            robustness=score,
            uncertaintyPenalty=10,
        ),
        verifier={"passed": True},
        canonicalSignature=(name * 16)[:16],
    )


def test_selector_removes_signature_duplicate_and_keeps_real_axis_distance():
    selector = ParetoPortfolioSelector()
    one, two, duplicate = _candidate("a", "classic"), _candidate("b", "local_immersion"), _candidate("c", "classic", 20)
    duplicate = duplicate.model_copy(update={"canonical_signature": one.canonical_signature})
    result = selector.select([one, two, duplicate])
    assert {item.proposal_id for item in result} == {one.proposal_id, two.proposal_id}
    assert selector.min_pairwise_distance(result) >= 0.25


def _physical_candidate(name: str, poi_ids: list[str]) -> PlanCandidate:
    return PlanCandidate(
        proposalId=f"proposal_{name}",
        portfolioId="portfolio_1",
        brief=CreativeBrief(
            briefId=f"brief_{name}",
            title=name,
            primaryAxis="classic",
            requiredGoalIds=["shared_goal"],
        ),
        itinerarySnapshot={
            "days": [
                {
                    "dayNumber": 1,
                    "segments": [
                        {
                            "id": f"{name}_{index}",
                            "poi": {
                                "amapId": amap_id,
                                "name": f"地点 {index}",
                                "source": "amap-place-search",
                                "latitude": 39.9 + index / 100,
                                "longitude": 116.3 + index / 100,
                            },
                            "semanticMetadata": {"optionalExperienceFamily": f"{name}_family_{index}"},
                        }
                        for index, amap_id in enumerate(poi_ids, start=1)
                    ],
                }
            ]
        },
        groundedEvidence=[
            {"amapId": amap_id, "family": f"{name}_family_{index}"} for index, amap_id in enumerate(poi_ids, start=1)
        ],
        score=PlanScoreVector(
            hardConstraintPassed=True,
            preferenceFit=80,
            thematicCoherence=80,
            experienceDiversity=80,
            routeEfficiency=80,
            pacingQuality=80,
            novelty=80,
            robustness=80,
            uncertaintyPenalty=0,
        ),
        verifier={"passed": True},
        canonicalSignature=(name * 16)[:16],
    )


def test_selector_requires_real_new_pois_not_only_different_family_labels():
    base = _physical_candidate(
        "base",
        ["B000A0001", "B000A0002", "B000A0003", "B000A0004", "B000A0005"],
    )
    relabeled = _physical_candidate(
        "relabeled",
        ["B000A0001", "B000A0002", "B000A0003", "B000A0004", "B000A0005"],
    )
    one_swap = _physical_candidate(
        "one_swap",
        ["B000A0001", "B000A0002", "B000A0003", "B000A0004", "B000A0006"],
    )
    two_swaps = _physical_candidate(
        "two_swaps",
        ["B000A0001", "B000A0002", "B000A0003", "B000A0006", "B000A0007"],
    )
    selector = ParetoPortfolioSelector()

    assert len(selector.select([base, relabeled])) == 1
    assert len(selector.select([base, one_swap])) == 1
    assert {item.proposal_id for item in selector.select([base, two_swaps])} == {
        base.proposal_id,
        two_swaps.proposal_id,
    }


def test_selector_does_not_impose_a_fixed_three_proposal_catalog_limit():
    candidates = [
        _physical_candidate(
            f"direction_{index}",
            [f"B{index:03d}A{poi_index:04d}" for poi_index in range(1, 6)],
        )
        for index in range(1, 6)
    ]

    selected = ParetoPortfolioSelector().select(candidates)

    assert [item.proposal_id for item in selected] == [item.proposal_id for item in candidates]
