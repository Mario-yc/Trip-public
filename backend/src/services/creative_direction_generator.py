"""Generate bounded, supply-aware creative direction candidates."""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from typing import Any


class CreativeDirectionGenerator:
    """Compose directions from semantic seeds and admitted area supply."""

    MAX_DIRECTION_BATCH = 12
    _FAMILY_SEEDS: dict[str, dict[str, str]] = {
        "heritage_walk": {
            "label": "历史街区",
            "axis": "culture_deep_dive",
            "activityMode": "history_walk",
        },
        "art_walk": {
            "label": "艺术空间",
            "axis": "classic",
            "activityMode": "art_walk",
        },
        "local_life": {
            "label": "社区生活",
            "axis": "local_immersion",
            "activityMode": "neighborhood_walk",
        },
        "market_walk": {
            "label": "市井市场",
            "axis": "food_led",
            "activityMode": "market_walk",
        },
        "park_relax": {
            "label": "公园休憩",
            "axis": "nature_relaxed",
            "activityMode": "green_space",
        },
        "night_view": {
            "label": "夜景观察",
            "axis": "photo_night",
            "activityMode": "night_walk",
        },
        "local_food": {
            "label": "地方风味",
            "axis": "food_led",
            "activityMode": "food_tasting",
        },
    }
    _AXIS_FAMILY_PRIORITY: dict[str, tuple[str, ...]] = {
        "culture_deep_dive": ("heritage_walk", "art_walk"),
        "classic": ("heritage_walk", "art_walk"),
        "local_immersion": ("local_life", "market_walk"),
        "citywalk_hidden_gems": ("local_life", "heritage_walk"),
        "food_led": ("local_food", "market_walk"),
        "photo_night": ("night_view", "art_walk"),
        "nature_relaxed": ("park_relax", "heritage_walk"),
        "family_light": ("park_relax", "art_walk"),
    }
    _HARD_GOAL_THEME_FAMILIES: dict[str, frozenset[str]] = {
        "night_view": frozenset({"night_view"}),
        "meal": frozenset({"local_food"}),
        "local_food": frozenset({"local_food"}),
    }

    @classmethod
    def generate_next(
        cls,
        *,
        hard_goal_strategy: list[str],
        day_anchor_targets: dict[int, int],
        used_signatures: list[str],
        limit: int,
        candidate_inventory: list[dict[str, Any]] | None = None,
        excluded_candidate_ids: list[str] | None = None,
        experience_intent: dict[str, Any] | None = None,
        city: str = "",
    ) -> list[dict[str, Any]]:
        bounded_limit = max(0, min(cls.MAX_DIRECTION_BATCH, int(limit)))
        if bounded_limit == 0:
            return []
        used = {str(item) for item in used_signatures if str(item)}
        if candidate_inventory is not None:
            candidates = cls._inventory_directions(
                candidate_inventory,
                hard_goal_strategy=hard_goal_strategy,
                day_anchor_targets=day_anchor_targets,
                experience_intent=experience_intent or {},
                city=city,
                excluded_candidate_ids={str(item) for item in (excluded_candidate_ids or []) if str(item)},
            )
        else:
            candidates = cls._seed_directions(
                hard_goal_strategy=hard_goal_strategy,
                day_anchor_targets=day_anchor_targets,
                experience_intent=experience_intent or {},
                city=city,
            )
        output: list[dict[str, Any]] = []
        for direction in candidates:
            signature = cls.signature(direction)
            if signature in used:
                continue
            output.append({**direction, "directionSignature": signature})
            if len(output) >= bounded_limit:
                break
        return output

    @classmethod
    def seed_for_axis(
        cls,
        axis: str,
        *,
        hard_goal_strategy: list[str],
        day_anchor_targets: dict[int, int],
        city: str = "",
    ) -> dict[str, Any] | None:
        families = list(cls._AXIS_FAMILY_PRIORITY.get(str(axis), ()))
        blocked_families = cls._families_already_required(hard_goal_strategy)
        families = [family for family in families if family not in blocked_families]
        if not families:
            return None
        payload = cls._direction_payload(
            families=families,
            hard_goal_strategy=hard_goal_strategy,
            day_anchor_targets=day_anchor_targets,
            area_key="inventory_pending",
            city=city,
            generation_source="seed_composition",
            candidate_ids=[],
            family_counts={family: 0 for family in families},
        )
        payload["primaryAxis"] = str(axis)
        payload["secondaryAxes"] = []
        payload["directionSignature"] = cls.signature(payload)
        return payload

    @classmethod
    def _inventory_directions(
        cls,
        inventory: list[dict[str, Any]],
        *,
        hard_goal_strategy: list[str],
        day_anchor_targets: dict[int, int],
        experience_intent: dict[str, Any],
        city: str,
        excluded_candidate_ids: set[str],
    ) -> list[dict[str, Any]]:
        by_area: dict[str, list[dict[str, Any]]] = defaultdict(list)
        blocked_families = cls._families_already_required(hard_goal_strategy)
        for raw in inventory:
            if not isinstance(raw, dict) or raw.get("scoreEligible") is not True:
                continue
            family = str(raw.get("family") or raw.get("themeFamily") or "")
            candidate_id = str(raw.get("candidateId") or raw.get("amapId") or raw.get("id") or "")
            if family not in cls._FAMILY_SEEDS or not candidate_id:
                continue
            if family in blocked_families:
                continue
            if candidate_id in excluded_candidate_ids:
                continue
            if raw.get("longitude") is None or raw.get("latitude") is None:
                continue
            area_key = str(raw.get("areaKey") or raw.get("businessArea") or raw.get("district") or "").strip()
            if not area_key:
                area_key = cls._coordinate_area_key(raw)
            by_area[area_key].append({**raw, "family": family, "candidateId": candidate_id})

        axis_rank = cls._axis_rank(experience_intent)
        directions: list[dict[str, Any]] = []
        for area_key, area_candidates in sorted(by_area.items()):
            families = cls._rank_families(
                {str(item["family"]) for item in area_candidates},
                axis_rank=axis_rank,
            )[:3]
            if not families:
                continue
            selected = [item for item in area_candidates if item["family"] in families]
            candidate_ids = sorted({str(item["candidateId"]) for item in selected})
            family_counts = {
                family: len({str(item["candidateId"]) for item in selected if item["family"] == family})
                for family in families
            }
            directions.append(
                cls._direction_payload(
                    families=families,
                    hard_goal_strategy=hard_goal_strategy,
                    day_anchor_targets=day_anchor_targets,
                    area_key=area_key,
                    city=city,
                    generation_source="admitted_candidate_inventory",
                    candidate_ids=candidate_ids,
                    family_counts=family_counts,
                )
            )
        return sorted(
            directions,
            key=lambda item: (
                -int((item.get("candidateSupply") or {}).get("scoreEligibleCount") or 0),
                axis_rank.get(str(item.get("primaryAxis") or ""), len(axis_rank)),
                str(item.get("areaClusterKey") or ""),
            ),
        )

    @classmethod
    def _seed_directions(
        cls,
        *,
        hard_goal_strategy: list[str],
        day_anchor_targets: dict[int, int],
        experience_intent: dict[str, Any],
        city: str,
    ) -> list[dict[str, Any]]:
        axis_rank = cls._axis_rank(experience_intent)
        families = cls._rank_families(
            set(cls._FAMILY_SEEDS) - cls._families_already_required(hard_goal_strategy),
            axis_rank=axis_rank,
        )
        combinations: list[list[str]] = [[family] for family in families]
        combinations.extend(
            [families[index], families[(index + offset) % len(families)]]
            for offset in (1, 2)
            for index in range(len(families))
            if families[index] != families[(index + offset) % len(families)]
        )
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for raw_families in combinations:
            key = tuple(sorted(set(raw_families)))
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(
                cls._direction_payload(
                    families=list(raw_families),
                    hard_goal_strategy=hard_goal_strategy,
                    day_anchor_targets=day_anchor_targets,
                    area_key="inventory_pending",
                    city=city,
                    generation_source="seed_composition",
                    candidate_ids=[],
                    family_counts={family: 0 for family in raw_families},
                )
            )
            if len(output) >= cls.MAX_DIRECTION_BATCH:
                break
        return output

    @classmethod
    def _direction_payload(
        cls,
        *,
        families: list[str],
        hard_goal_strategy: list[str],
        day_anchor_targets: dict[int, int],
        area_key: str,
        city: str,
        generation_source: str,
        candidate_ids: list[str],
        family_counts: dict[str, int],
    ) -> dict[str, Any]:
        normalized_families = list(dict.fromkeys(families))
        seeds = [cls._FAMILY_SEEDS[family] for family in normalized_families]
        primary_axis = str(seeds[0]["axis"])
        secondary_axes = list(
            dict.fromkeys(str(seed["axis"]) for seed in seeds[1:] if str(seed["axis"]) != primary_axis)
        )[:3]
        labels = [str(seed["label"]) for seed in seeds]
        area_title = "" if area_key == "inventory_pending" else area_key
        title = " · ".join(item for item in (area_title, "与".join(labels)) if item) or f"{city or '目的地'}主题探索"
        score_eligible_count = len(candidate_ids)
        required_theme_supply = len(normalized_families)
        candidate_sufficient = score_eligible_count >= required_theme_supply
        declared_targets = {str(day): target for day, target in sorted(day_anchor_targets.items())}
        return {
            "title": title,
            "primaryAxis": primary_axis,
            "secondaryAxes": secondary_axes,
            "themeFamilies": normalized_families,
            "experienceFamilies": normalized_families,
            "activityModes": [str(seed["activityMode"]) for seed in seeds],
            "dayRoleSignature": [f"day_{day}:anchors_{target}" for day, target in sorted(day_anchor_targets.items())],
            "dayAnchorTargets": declared_targets,
            "dayRoles": [
                {
                    "dayNumber": day,
                    "role": f"{title} · Day {day}",
                    "targetRouteAnchors": target,
                }
                for day, target in sorted(day_anchor_targets.items())
            ],
            "requiredGoals": list(hard_goal_strategy),
            "hardSoftGoalPlacementStrategy": list(hard_goal_strategy),
            "avoidExperienceTypes": [
                "functional_facility",
                "duplicate_physical_poi",
            ],
            "areaClusterKey": area_key,
            "candidateSupply": {
                "supplyState": (
                    "observed_complete"
                    if generation_source == "admitted_candidate_inventory"
                    else "pending_search"
                ),
                "scoreEligibleCount": score_eligible_count,
                "requiredThemeSupply": required_theme_supply,
                "candidateIds": candidate_ids,
                "familyCounts": family_counts,
            },
            "noveltyEvidence": {
                "areaClusterKey": area_key,
                "newThemeFamilies": normalized_families,
                "newPhysicalPoiIds": candidate_ids,
            },
            "feasibilityEvidence": {
                "candidateSufficient": candidate_sufficient,
                "requiresBoundedSearch": not candidate_sufficient,
                "declaredDayAnchorTargets": declared_targets,
            },
            "generationSource": generation_source,
        }

    @staticmethod
    def match_candidate(
        *,
        primary_axis: str,
        secondary_axes: list[str],
        experience_families: list[str],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        secondary = set(str(item) for item in secondary_axes if str(item))
        families = set(str(item) for item in experience_families if str(item))
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("primaryAxis") or "") != str(primary_axis or ""):
                continue
            if set(str(item) for item in candidate.get("secondaryAxes") or [] if str(item)) != secondary:
                continue
            if set(str(item) for item in candidate.get("experienceFamilies") or [] if str(item)) != families:
                continue
            return candidate
        return None

    @staticmethod
    def signature(direction: dict[str, Any]) -> str:
        supply = direction.get("candidateSupply") if isinstance(direction.get("candidateSupply"), dict) else {}
        structural = {
            "primaryAxis": direction.get("primaryAxis"),
            "secondaryAxes": sorted(direction.get("secondaryAxes") or []),
            "themeFamilies": sorted(direction.get("themeFamilies") or direction.get("experienceFamilies") or []),
            "activityModes": sorted(direction.get("activityModes") or []),
            "dayRoleSignature": direction.get("dayRoleSignature") or [],
            "dayAnchorTargets": direction.get("dayAnchorTargets") or {},
            "hardSoftGoalPlacementStrategy": direction.get("hardSoftGoalPlacementStrategy") or [],
            "areaClusterKey": direction.get("areaClusterKey") or "",
            "candidateIds": sorted(supply.get("candidateIds") or []),
            # The same family/count is materially different when it is an
            # admitted, complete inventory versus a bounded future search.
            "supplyState": supply.get("supplyState") or "",
        }
        payload = json.dumps(structural, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _rank_families(
        cls,
        families: set[str],
        *,
        axis_rank: dict[str, int],
    ) -> list[str]:
        family_order = list(cls._FAMILY_SEEDS)
        return sorted(
            families,
            key=lambda family: (
                axis_rank.get(
                    str(cls._FAMILY_SEEDS[family]["axis"]),
                    len(axis_rank),
                ),
                family_order.index(family),
            ),
        )

    @staticmethod
    def _axis_rank(experience_intent: dict[str, Any]) -> dict[str, int]:
        axes = [str(item) for item in experience_intent.get("decisionAxes") or [] if str(item)]
        return {axis: index for index, axis in enumerate(axes)}

    @classmethod
    def _families_already_required(
        cls,
        hard_goal_strategy: list[str],
    ) -> set[str]:
        blocked: set[str] = set()
        for raw in hard_goal_strategy:
            intent_type = str(raw or "").partition(":")[0]
            blocked.update(cls._HARD_GOAL_THEME_FAMILIES.get(intent_type, ()))
        return blocked

    @staticmethod
    def _coordinate_area_key(candidate: dict[str, Any]) -> str:
        return f"grid:{float(candidate['longitude']):.2f},{float(candidate['latitude']):.2f}"
