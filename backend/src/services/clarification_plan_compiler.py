import copy
import re
from typing import Any


class ClarificationPlanCompiler:
    """Compile host-owned defaults before asking the planning controller."""

    DEFAULT_SOURCE = "server_safe_default_v1"
    DEFAULT_DETOUR_TOLERANCE = {
        "maxGeneralizedCostDelta": 35.0,
        "maxDetourRatio": 0.35,
    }
    DETOUR_OPTION_REGISTRY = (
        ("detour_minimal", "尽量少绕路", 15.0, 0.15),
        ("detour_balanced", "路线与体验均衡", 35.0, 0.35),
        ("detour_flexible", "可接受较多绕路", 60.0, 0.60),
    )

    _MOBILITY_SENSITIVE = re.compile(
        r"(老人|长辈|高龄|儿童|孩子|亲子|婴儿|轮椅|行动不便|无障碍|少走路|不能久走|体力)"
    )

    @classmethod
    def safe_route_default_seed(
        cls,
        *,
        request_text: str,
        route_contract: dict[str, Any],
        mobility_profile: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return a recompilation seed plus visible, non-memory defaults."""

        existing = copy.deepcopy(route_contract) if isinstance(route_contract, dict) else {}
        missing = {str(item) for item in existing.get("missingFields") or [] if str(item)}
        if not missing or not missing.issubset({"mobilityProfile", "detourTolerance"}):
            return existing, []

        text = str(request_text or "")
        defaults: list[dict[str, Any]] = []
        if "detourTolerance" in missing:
            explicit_detour = None
            if re.search(r"(尽量少绕路|少绕路|避免绕路|不绕路)", text):
                explicit_detour = {"maxGeneralizedCostDelta": 15.0, "maxDetourRatio": 0.15}
            elif re.search(r"(可接受较多绕路|可以多绕路|体验优先.*绕路)", text):
                explicit_detour = {"maxGeneralizedCostDelta": 60.0, "maxDetourRatio": 0.60}
            elif re.search(r"(均衡路线|路线与体验均衡)", text):
                explicit_detour = copy.deepcopy(cls.DEFAULT_DETOUR_TOLERANCE)
            if explicit_detour is not None:
                existing["detourTolerance"] = explicit_detour
                existing["detourToleranceSource"] = "user_explicit"
                missing.discard("detourTolerance")
        if "mobilityProfile" in missing:
            if cls._MOBILITY_SENSITIVE.search(text):
                return existing, []
            existing["mobilityProfile"] = copy.deepcopy(mobility_profile)
            existing["mobilityProfileSource"] = cls.DEFAULT_SOURCE
            defaults.append(
                {
                    "dimensionId": "route_decision.mobility_profile",
                    "label": "公共交通、标准节奏",
                    "source": cls.DEFAULT_SOURCE,
                    "editable": True,
                    "userConfirmed": False,
                }
            )
        if "detourTolerance" in missing:
            existing["detourTolerance"] = copy.deepcopy(cls.DEFAULT_DETOUR_TOLERANCE)
            existing["detourToleranceSource"] = cls.DEFAULT_SOURCE
            defaults.append(
                {
                    "dimensionId": "route_decision.detour_tolerance",
                    "label": "均衡路线",
                    "source": cls.DEFAULT_SOURCE,
                    "editable": True,
                    "userConfirmed": False,
                }
            )
        return existing, defaults

    @classmethod
    def detour_options(cls) -> list[dict[str, Any]]:
        return [
            {
                "id": option_id,
                "defaultLabel": label,
                "semanticValue": {
                    "detourTolerance": {
                        "maxGeneralizedCostDelta": delta,
                        "maxDetourRatio": ratio,
                    }
                },
            }
            for option_id, label, delta, ratio in cls.DETOUR_OPTION_REGISTRY
        ]

    @staticmethod
    def mobility_options() -> list[dict[str, Any]]:
        return [
            {
                "id": "mobility_transit_standard",
                "defaultLabel": "公共交通、标准节奏",
                "semanticValue": {"mobilityProfile": {"transportMode": "transit", "paceClass": "standard"}},
            },
            {
                "id": "mobility_transit_relaxed",
                "defaultLabel": "公共交通、少走慢行",
                "semanticValue": {"mobilityProfile": {"transportMode": "transit", "paceClass": "relaxed"}},
            },
            {
                "id": "mobility_driving_relaxed",
                "defaultLabel": "驾车为主、少走慢行",
                "semanticValue": {"mobilityProfile": {"transportMode": "driving", "paceClass": "relaxed"}},
            },
        ]
