from src.services.clarification_plan_compiler import ClarificationPlanCompiler


def test_safe_route_defaults_are_visible_editable_and_not_user_confirmed():
    seed, defaults = ClarificationPlanCompiler.safe_route_default_seed(
        request_text="北京两日高校游",
        route_contract={"status": "awaiting_clarification", "missingFields": ["mobilityProfile", "detourTolerance"]},
        mobility_profile={"transportMode": "transit", "paceClass": "standard"},
    )

    assert seed["mobilityProfile"]["transportMode"] == "transit"
    assert seed["detourTolerance"] == {"maxGeneralizedCostDelta": 35.0, "maxDetourRatio": 0.35}
    assert {item["source"] for item in defaults} == {"server_safe_default_v1"}
    assert all(item["editable"] is True and item["userConfirmed"] is False for item in defaults)


def test_mobility_sensitive_request_remains_blocking():
    seed, defaults = ClarificationPlanCompiler.safe_route_default_seed(
        request_text="带老人出行，希望少走路",
        route_contract={"status": "awaiting_clarification", "missingFields": ["mobilityProfile", "detourTolerance"]},
        mobility_profile={"transportMode": "transit", "paceClass": "standard"},
    )

    assert defaults == []
    assert "mobilityProfile" not in seed


def test_detour_registry_has_locked_product_semantics():
    assert [item["semanticValue"]["detourTolerance"] for item in ClarificationPlanCompiler.detour_options()] == [
        {"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15},
        {"maxGeneralizedCostDelta": 35.0, "maxDetourRatio": 0.35},
        {"maxGeneralizedCostDelta": 60.0, "maxDetourRatio": 0.60},
    ]


def test_explicit_minimal_detour_overrides_default_without_becoming_long_term_memory():
    seed, defaults = ClarificationPlanCompiler.safe_route_default_seed(
        request_text="公共交通为主，并且尽量少绕路",
        route_contract={"status": "awaiting_clarification", "missingFields": ["detourTolerance"]},
        mobility_profile={"transportMode": "transit", "paceClass": "standard"},
    )

    assert seed["detourTolerance"] == {"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15}
    assert seed["detourToleranceSource"] == "user_explicit"
    assert defaults == []
