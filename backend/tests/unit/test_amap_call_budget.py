import copy

from src.services.amap_call_budget import AmapCallBudget
from src.services.creative_planning_models import canonical_fingerprint


def _exact_repair_scope(
    *,
    planning_root: str = "planning_root_1",
    slot_id: str = "slot_1",
    adjacent_anchor_ids=None,
    route_contract_fingerprint: str = "",
) -> dict:
    scope = {
        "continuationMode": "repair_exact_slot",
        "rootPortfolioId": "portfolio_root_1",
        "planningSelectionRootTurnId": planning_root,
        "briefId": "brief_1",
        "poolId": "pool_1",
        "dayNumber": 1,
        "planningSlotId": slot_id,
        "candidatePhysicalId": "B000ROUTEB",
        "adjacentAnchorIds": adjacent_anchor_ids or ["anchor_segment_1"],
        "adjacentRouteLedgerKeys": [
            {
                "fromPhysicalId": "B000ROUTEA",
                "toPhysicalId": "B000ROUTEB",
                "mode": "transit",
            }
        ],
        "routeContractFingerprint": route_contract_fingerprint
        or canonical_fingerprint({"preferredMode": "transit", "status": "ready"}),
    }
    scope["scopeFingerprint"] = canonical_fingerprint(scope)
    return scope


def test_creative_portfolio_candidate_budget_is_centralized_and_24_calls():
    budget = AmapCallBudget.for_creative_portfolio_candidates()

    assert budget.place_text_max == 12
    assert budget.place_around_max == 8
    assert budget.total_external_max == 24
    assert budget.route_refresh_max == 0
    assert budget.source == "creative_portfolio_candidate_grounding"


def test_creative_portfolio_route_preflight_budget_is_separate_from_candidate_calls():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()

    assert budget.place_text_max == 0
    assert budget.place_around_max == 0
    assert budget.route_refresh_max == 0
    assert budget.total_external_max == 0
    assert budget.source == "creative_portfolio_route_preflight"
    assert budget.snapshot()["derivation"] == {
        "schemaVersion": "creative-portfolio-route-budget-v1",
        "preferredMode": "",
        "baselineAdjacentPairCount": 0,
        "insertionBypassPairCount": 0,
        "replacementCandidateCount": 0,
        "replacementPreferredPairCount": 0,
        "conditionalWalkingPairCount": 0,
        "themeWalkingPairCount": 0,
        "nearbySearchMax": 0,
        "routeRequests": [],
        "routeWorkLeaseCount": 0,
        "routeWorkLeases": [],
    }


def test_creative_portfolio_route_authorization_is_exact_idempotent_and_wrong_work_is_zero_cost():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    authorized = {
        "fromPhysicalId": "B000ROUTEA",
        "toPhysicalId": "B000ROUTEB",
        "mode": "transit",
        "reason": "baseline_adjacent",
        "condition": "always",
    }

    budget.authorize_route_work([authorized])
    budget.authorize_route_work(
        [
            {
                **authorized,
                "fromPhysicalId": "b000routea",
                "toPhysicalId": "b000routeb",
                "mode": "public_transit",
            }
        ]
    )

    assert budget.route_refresh_max == 1
    assert budget.total_external_max == 1
    assert len(budget.snapshot()["derivation"]["routeRequests"]) == 1

    assert not budget.try_acquire(
        endpoint="route/walking",
        keyword="wrong mode",
        category="walking",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="walking",
    )
    assert budget.last_denial_reason == "route_work_unauthorized"
    assert not budget.try_acquire(
        endpoint="route/transit",
        keyword="wrong pair",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEC",
        mode="transit",
    )
    assert budget.last_denial_reason == "route_work_unauthorized"
    assert budget.used_route == 0
    assert budget.used_total_external == 0
    assert budget.skipped_because_budget == 0

    assert budget.try_acquire(
        endpoint="route/transit",
        keyword="authorized pair",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="transit",
    )
    assert budget.used_route == 1
    assert budget.used_total_external == 1


def test_creative_portfolio_route_authorization_rejects_an_invalid_batch_atomically():
    valid = {
        "fromPhysicalId": "B000ROUTEA",
        "toPhysicalId": "B000ROUTEB",
        "mode": "transit",
        "reason": "baseline_adjacent",
    }
    invalid_requests = [
        {**valid, "fromPhysicalId": "not-an-amap-id"},
        {**valid, "toPhysicalId": "B000ROUTEA"},
        {**valid, "mode": "hoverboard"},
    ]

    for invalid in invalid_requests:
        budget = AmapCallBudget.for_creative_portfolio_route_preflight()
        budget.authorize_route_work([valid, invalid])

        assert budget.route_refresh_max == 0
        assert budget.total_external_max == 0
        assert budget.snapshot()["derivation"]["routeRequests"] == []
        assert not budget.try_acquire(
            endpoint="route/transit",
            keyword="valid member from rejected batch",
            category="transit",
            from_amap_id="B000ROUTEA",
            to_amap_id="B000ROUTEB",
            mode="transit",
        )
        assert budget.last_denial_reason == "route_work_unauthorized"
        assert budget.used_route == 0
        assert budget.used_total_external == 0


def test_exact_repair_scope_rejects_tampered_identity_before_consuming_route_work():
    valid_scope = _exact_repair_scope()
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    assert budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000ROUTEA",
                "toPhysicalId": "B000ROUTEB",
                "mode": "transit",
                "reason": "replacement_adjacent",
                "candidatePhysicalId": "B000ROUTEB",
                "repairScopeCertificate": valid_scope,
            }
        ]
    )
    tampered_scopes = []
    for field, value in (
        ("planningSelectionRootTurnId", "planning_root_2"),
        ("planningSlotId", "slot_2"),
        ("adjacentAnchorIds", ["other_anchor_segment"]),
        (
            "routeContractFingerprint",
            canonical_fingerprint({"preferredMode": "walking", "status": "ready"}),
        ),
    ):
        tampered = copy.deepcopy(valid_scope)
        tampered[field] = value
        tampered.pop("scopeFingerprint")
        tampered["scopeFingerprint"] = canonical_fingerprint(tampered)
        tampered_scopes.append(tampered)

    for tampered in tampered_scopes:
        assert not budget.try_acquire(
            endpoint="route/transit",
            keyword="tampered scope",
            category="transit",
            from_amap_id="B000ROUTEA",
            to_amap_id="B000ROUTEB",
            mode="transit",
            repair_scope_certificate=tampered,
        )
        assert budget.last_denial_reason == "route_scope_unauthorized"

    assert not budget.try_acquire(
        endpoint="route/transit",
        keyword="wrong pair",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEC",
        mode="transit",
        repair_scope_certificate=valid_scope,
    )
    assert not budget.try_acquire(
        endpoint="route/walking",
        keyword="wrong mode",
        category="walking",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="walking",
        repair_scope_certificate=valid_scope,
    )
    assert budget.used_route == 0
    assert budget.used_total_external == 0
    assert budget.cache_hit_count == 0
    assert budget.skipped_because_budget == 0


def test_exact_repair_scope_rejects_mismatched_candidate_authorization_atomically():
    valid_scope = _exact_repair_scope()
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()

    assert not budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000ROUTEA",
                "toPhysicalId": "B000ROUTEB",
                "mode": "transit",
                "reason": "replacement_adjacent",
                "candidatePhysicalId": "B000ROUTEC",
                "repairScopeCertificate": valid_scope,
            }
        ]
    )

    assert budget.last_denial_reason == "route_scope_candidate_mismatch"
    assert budget.route_refresh_max == 0
    assert budget.used_route == 0


def test_exact_repair_authorization_without_scope_is_rejected_atomically():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()

    assert not budget.authorize_route_work(
        [
            {
                "fromPhysicalId": "B000ROUTEA",
                "toPhysicalId": "B000ROUTEB",
                "mode": "transit",
                "reason": "replacement_adjacent",
                "candidatePhysicalId": "B000ROUTEB",
            }
        ]
    )

    assert budget.last_denial_reason == "route_scope_missing"
    assert budget.route_refresh_max == 0
    assert budget.used_route == 0
    assert budget.snapshot()["derivation"]["routeRequests"] == []


def test_creative_route_snapshot_keeps_complete_immutable_receipt_ledger():
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    requests = [
        {
            "fromPhysicalId": "B000ROUTEA",
            "toPhysicalId": f"B{i:08d}",
            "mode": "transit",
            "reason": "baseline_adjacent",
        }
        for i in range(1, 22)
    ]
    assert budget.authorize_route_work(requests)
    for request in requests:
        assert budget.try_acquire(
            endpoint="route/transit",
            keyword="receipt completeness",
            category="transit",
            from_amap_id=request["fromPhysicalId"],
            to_amap_id=request["toPhysicalId"],
            mode=request["mode"],
        )

    first_snapshot = budget.snapshot()
    assert first_snapshot["usedRoute"] == 21
    assert len(first_snapshot["calls"]) == 21
    first_snapshot["calls"][0]["fromAmapId"] = "B0TAMPERED"
    assert budget.snapshot()["calls"][0]["fromAmapId"] == "B000ROUTEA"


def test_same_pair_multiple_exact_repair_scopes_are_explicit_and_capacity_idempotent():
    first_scope = _exact_repair_scope()
    second_scope = _exact_repair_scope(
        planning_root="planning_root_2",
        slot_id="slot_2",
        adjacent_anchor_ids=["anchor_segment_2"],
    )
    budget = AmapCallBudget.for_creative_portfolio_route_preflight()
    requests = [
        {
            "fromPhysicalId": "B000ROUTEA",
            "toPhysicalId": "B000ROUTEB",
            "mode": "transit",
            "reason": "replacement_adjacent",
            "candidatePhysicalId": "B000ROUTEB",
            "repairScopeCertificate": scope,
        }
        for scope in (first_scope, second_scope, first_scope)
    ]
    assert budget.authorize_route_work(requests)
    assert budget.route_refresh_max == 2
    assert budget.total_external_max == 2
    derivation = budget.snapshot()["derivation"]
    assert derivation["routeWorkLeaseCount"] == 2
    assert [
        item["scopeFingerprint"] for item in derivation["routeWorkLeases"]
    ] == sorted([first_scope["scopeFingerprint"], second_scope["scopeFingerprint"]])

    assert not budget.try_acquire(
        endpoint="route/transit",
        keyword="ambiguous without scope",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="transit",
    )
    assert budget.last_denial_reason == "route_scope_missing"
    assert budget.used_route == 0

    assert budget.try_acquire(
        endpoint="route/transit",
        keyword="first scope",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="transit",
        repair_scope_certificate=first_scope,
    )
    assert budget.record_cache_hit(
        endpoint="route/transit",
        keyword="second scope cache reuse",
        category="transit",
        from_amap_id="B000ROUTEA",
        to_amap_id="B000ROUTEB",
        mode="transit",
        repair_scope_certificate=second_scope,
    )
    snapshot = budget.snapshot()
    assert snapshot["usedRoute"] == 1
    assert snapshot["cacheHitCount"] == 1
    assert [
        call["repairScopeCertificate"] for call in snapshot["calls"]
    ] == [first_scope, second_scope]


def test_noncreative_route_budget_keeps_legacy_capacity_only_acquisition_compatible():
    budget = AmapCallBudget(route_refresh_max=1, total_external_max=1, source="agent_run")

    assert budget.try_acquire(
        endpoint="route/transit",
        keyword="legacy adapter pair",
        category="transit",
    )
    assert budget.used_route == 1
    assert budget.used_total_external == 1


def test_run_budget_suppresses_identical_external_query_and_reports_ledger_metrics():
    budget = AmapCallBudget(place_text_max=2, total_external_max=2, source="test")

    assert budget.try_acquire(
        endpoint="place/text", keyword="北京 宫廷点心", category="food", center="110000", radius=""
    )
    assert not budget.try_acquire(
        endpoint="place/text", keyword="北京 宫廷点心", category="food", center="110000", radius=""
    )

    snapshot = budget.snapshot()
    assert snapshot["usedPlaceText"] == 1
    assert snapshot["duplicateExternalQueryCount"] == 1
    assert snapshot["reusedQueryCount"] == 1
    assert snapshot["newQueryCount"] == 1
    assert snapshot["skipped"][-1]["reason"] == "duplicate_query_suppressed"


def test_place_detail_shares_existing_text_and_total_budget_without_expansion():
    budget = AmapCallBudget(place_text_max=2, total_external_max=2, source="test")

    assert budget.try_acquire(endpoint="place/text", keyword="社区市场")
    assert budget.try_acquire(endpoint="place/detail", keyword="B000000001")
    assert not budget.try_acquire(endpoint="place/detail", keyword="B000000002")

    snapshot = budget.snapshot()
    assert snapshot["usedPlaceText"] == 1
    assert snapshot["usedPlaceDetail"] == 1
    assert snapshot["usedTotalExternal"] == 2
    assert snapshot["budget"]["amapPoiTextAndDetailMax"] == 2
