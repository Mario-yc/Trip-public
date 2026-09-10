from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from src.services.agent_service import AgentService
from src.services.creative_output_quality_service import (
    CreativeOutputQualityService,
)
from src.services.creative_proposal_title_service import (
    CreativeProposalTitleService,
)
from src.services.deepseek_agent_provider import DeepSeekAgentProvider
from src.services.plan_portfolio_store import PlanPortfolioStore


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
        "endTime": start_time,
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


def _snapshot() -> dict:
    return {
        "city": "北京",
        "title": "市井市场方向｜北京真实地点草案",
        "creativeBrief": {
            "briefId": "brief-market",
            "title": "市井市场方向",
        },
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    _segment(
                        "campus",
                        "B00000001",
                        "清华大学",
                        "09:00",
                        intent_type="campus_visit",
                    ),
                    _segment(
                        "meal",
                        "B00000002",
                        "四季民福烤鸭店(故宫店)",
                        "12:00",
                        intent_type="meal",
                    ),
                    _segment(
                        "market",
                        "B00000003",
                        "三源里菜市场",
                        "14:30",
                        family="market_walk",
                    ),
                    _segment(
                        "night",
                        "B00000004",
                        "中央电视塔",
                        "19:10",
                        intent_type="night_view",
                    ),
                ],
            }
        ],
        "portfolioPendingSlots": [
            {
                "planningSlotId": "pending-art",
                "dayNumber": 1,
                "displayNeed": "艺术街区与创意园区",
            }
        ],
    }


def test_simple_direction_server_fallback_title_is_sealed_intent_summary_not_poi_compilation():
    snapshot = _snapshot()

    titled = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
    )

    assert titled["title"] == "清华学府寻味"
    assert "清华大学" not in titled["title"]
    assert "四季民福" not in titled["title"]
    assert CreativeProposalTitleService.is_valid_server_fallback_title(titled)
    assert titled["portfolioTitleGeneration"]["fallbackSource"] == "server_sealed_intent_summary"


def test_server_fallback_title_never_claims_a_daytime_park_is_an_evening_activity():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"] = [
        _segment(
            "campus",
            "B00000001",
            "清华大学",
            "09:00",
            intent_type="campus_visit",
        ),
        _segment(
            "park",
            "B00000005",
            "海淀公园",
            "15:00",
            intent_type="park",
        ),
    ]
    for segment in snapshot["days"][0]["segments"]:
        segment["semanticMetadata"]["schedulePreference"] = {
            "dayPart": "afternoon" if segment["id"] == "park" else "morning"
        }

    titled = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
    )

    assert titled["title"] == "清华学府游园"
    assert "晚间" not in titled["title"]


def test_title_signals_can_be_restricted_to_a_sibling_distinguishing_real_anchor():
    snapshot = _snapshot()
    snapshot["proposalSpecificTitleSignals"] = ["三源里"]
    ids = [item["poi"]["amapId"] for item in snapshot["days"][0]["segments"]]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": title, "evidenceAmapIds": ids}
            for title in ("三源里街巷寻味", "三源里烟火慢游", "三源里城市风物")
        ],
    }

    projection = CreativeProposalTitleService.select_agent_candidate(snapshot, payload)

    assert projection["requiredTitleSignals"] == ["三源里"]
    assert projection["usedTitleSignal"] == "三源里"
    assert projection["title"] == "三源里街巷寻味"


def test_sibling_title_morphology_rejects_common_prefix_suffix_and_bigram_overlap():
    assert not CreativeProposalTitleService._is_morphologically_distinct(
        "海淀三源里慢游",
        {"海淀书香访校"},
    )
    assert not CreativeProposalTitleService._is_morphologically_distinct(
        "烟火三源里寻味",
        {"书香旧城寻味"},
    )
    assert not CreativeProposalTitleService._is_morphologically_distinct(
        "辛乙丙丁戊己壬",
        {"甲乙丙丁戊己庚"},
    )


def test_agent_title_selection_filters_sibling_morphology_before_requiring_three_candidates():
    snapshot = _snapshot()
    snapshot["proposalSpecificTitleSignals"] = ["三源里"]
    ids = [item["poi"]["amapId"] for item in snapshot["days"][0]["segments"]]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "海淀三源里慢游", "evidenceAmapIds": ids},
            {"title": "烟火三源里游园", "evidenceAmapIds": ids},
            {"title": "三源里街巷慢游", "evidenceAmapIds": ids},
            {"title": "三源里烟火慢行", "evidenceAmapIds": ids},
            {"title": "三源里城市风物", "evidenceAmapIds": ids},
        ],
    }

    projection = CreativeProposalTitleService.select_agent_candidate(
        snapshot,
        payload,
        reserved_titles={"海淀书香访校", "书香旧城游园"},
    )

    assert projection["title"] == "三源里街巷慢游"
    assert len(projection["validCandidates"]) == 3


def test_server_fallback_uses_lowest_overlap_proposal_specific_signal():
    snapshot = _snapshot()
    snapshot["proposalSpecificTitleSignals"] = ["清华", "三源里"]
    reserved = {"清华学府寻味", "清华书香烟火"}

    titled = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
        reserved_titles=reserved,
    )

    assert "三源里" in titled["title"]
    assert titled["portfolioTitleGeneration"]["usedTitleSignal"] == "三源里"
    assert CreativeProposalTitleService._is_morphologically_distinct(
        titled["title"],
        reserved,
    )


def test_embedded_or_independence_pending_poi_cannot_be_a_title_signal():
    for status in ("embedded_in_day_anchor", "independence_pending"):
        snapshot = _snapshot()
        market = snapshot["days"][0]["segments"][2]
        market["semanticMetadata"]["experienceIndependenceEvidence"] = {
            "schemaVersion": "experience-independence-v1",
            "status": status,
        }
        snapshot["proposalSpecificTitleSignals"] = ["三源里"]

        assert CreativeProposalTitleService.required_title_signals(snapshot) == []
        context = CreativeProposalTitleService.agent_generation_context(
            snapshot,
            city="北京",
        )
        assert "B00000003" not in context["evidenceAmapIds"]


def test_title_signal_inventory_removes_facts_shared_by_sibling_routes():
    current = _snapshot()
    sibling = _snapshot()
    for snapshot in (current, sibling):
        for segment in snapshot["days"][0]["segments"]:
            segment["poi"]["district"] = "海淀区"
    sibling["days"][0]["segments"][0]["poi"].update(
        {
            "amapId": "B00000009",
            "id": "B00000009",
            "name": "北京大学",
        }
    )

    signals = CreativeProposalTitleService.proposal_specific_title_signals(
        current,
        sibling_snapshots=[sibling],
    )

    assert signals == ["清华"]


def test_missing_safe_title_signal_uses_neutral_fallback_and_rejects_provider_poetry():
    snapshot = _snapshot()
    for index, segment in enumerate(snapshot["days"][0]["segments"], start=1):
        segment["poi"]["name"] = f"POI-{index}"
    ids = [item["poi"]["amapId"] for item in snapshot["days"][0]["segments"]]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": title, "evidenceAmapIds": ids}
            for title in ("城市街巷从容漫游", "城市风物自在慢行", "城市日常悠然行旅")
        ],
    }

    titled = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_signal_unavailable",
    )

    assert titled["title"] == "城市方案待核验"
    assert titled["portfolioTitleGeneration"]["titleDecisionSource"] == "neutral_unverified_server_fallback"
    assert CreativeProposalTitleService.select_agent_candidate(snapshot, payload) == {}


def test_server_fallback_titles_remain_evidence_bound_and_unique_within_one_root():
    first_snapshot = _snapshot()
    second_snapshot = _snapshot()
    second_snapshot["days"][0]["segments"][0]["poi"]["longitude"] = 116.71
    second_snapshot["days"][0]["segments"][0]["poi"]["latitude"] = 40.12

    first = CreativeProposalTitleService.with_server_fallback_title(
        first_snapshot,
        reason_code="title_provider_unavailable",
        reserved_titles=[],
    )
    second = CreativeProposalTitleService.with_server_fallback_title(
        second_snapshot,
        reason_code="title_provider_unavailable",
        reserved_titles=[first["title"]],
    )

    assert first["title"] != second["title"]
    assert "清华大学" not in first["title"]
    assert "清华大学" not in second["title"]
    assert first["portfolioTitleGeneration"]["fallbackEvidenceFingerprint"] != second[
        "portfolioTitleGeneration"
    ]["fallbackEvidenceFingerprint"]
    assert CreativeProposalTitleService.is_valid_server_fallback_title(first)
    assert CreativeProposalTitleService.is_valid_server_fallback_title(second)


def test_sealed_fallback_title_survives_truthful_incomplete_status_title() -> None:
    titled = CreativeProposalTitleService.with_server_fallback_title(
        _snapshot(),
        reason_code="title_provider_unavailable",
    )
    persisted = {**titled, "title": "方案待补全"}

    assert CreativeProposalTitleService.sealed_server_fallback_title(persisted) == titled["title"]
    assert not CreativeProposalTitleService.is_valid_server_fallback_title(persisted)


def test_sealed_fallback_title_accepts_only_controller_materialized_pending_evidence() -> None:
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"] = []
    titled = CreativeProposalTitleService.with_server_fallback_title(
        snapshot,
        reason_code="title_provider_unavailable",
    )
    pending = {
        "slotId": "day2_meal",
        "planningSlotId": "day2_meal",
        "poolId": "goal_meal_pool",
        "dayNumber": 2,
        "intentType": "meal",
        "goalId": "goal_meal",
        "sourceGoalId": "goal_meal",
        "occurrenceId": "occ:goal_meal:day:2",
        "lineageAuthority": "goal_occurrence_compiler",
        "state": "pending",
        "groundingStatus": "unresolved",
        "schedulePreference": {
            "dayPart": "noon",
            "sourceGoalId": "goal_meal",
            "occurrenceId": "occ:goal_meal:day:2",
        },
        "simpleDirectionProviderExhausted": True,
        "simpleDirectionRequirementLineageConflict": False,
        "timingBasis": "simple_direction_provider_exhausted_slot",
        "reasonCode": "provider_candidates_exhausted_or_semantically_rejected",
        "requirementEvidenceSource": "request_intent_contract",
    }
    persisted = {
        **titled,
        "title": "方案待补全",
        "portfolioPendingSlots": [pending],
    }

    assert CreativeProposalTitleService.sealed_server_fallback_title(persisted) == titled["title"]

    untrusted = copy.deepcopy(persisted)
    untrusted["portfolioPendingSlots"][0]["simpleDirectionProviderExhausted"] = False
    assert CreativeProposalTitleService.sealed_server_fallback_title(untrusted) == ""

    tampered = copy.deepcopy(persisted)
    tampered["days"][0]["segments"][0]["poi"]["longitude"] = 117.0
    assert CreativeProposalTitleService.sealed_server_fallback_title(tampered) == ""


def test_server_fallback_titles_do_not_repeat_across_four_visible_directions():
    reserved: list[str] = []
    generated: list[str] = []

    for _ in range(4):
        titled = CreativeProposalTitleService.with_server_fallback_title(
            _snapshot(),
            reason_code="title_provider_unavailable",
            reserved_titles=reserved,
        )
        assert CreativeProposalTitleService.is_valid_server_fallback_title(
            titled,
            reserved_titles=reserved,
        )
        generated.append(titled["title"])
        reserved.append(titled["title"])

    assert len(set(generated)) == 4


def _legacy_title_is_a_grounded_route_story_and_never_claims_pending_labels():
    projection = CreativeProposalTitleService.project(_snapshot(), city="北京")

    assert projection["title"] == ("清华访校，四季民福烤鸭店寻味、三源里菜市场逛集，最终中央电视塔入夜｜北京1日")
    assert "方向" not in projection["title"]
    assert "真实地点草案" not in projection["title"]
    assert "艺术街区" not in projection["title"]
    assert projection["selectedAmapIds"] == [
        "B00000001",
        "B00000002",
        "B00000003",
        "B00000004",
    ]
    assert CreativeProposalTitleService.is_valid_projection(
        _snapshot(),
        projection,
    )


def _legacy_projection_remains_valid_when_city_is_supplied_by_materialized_pois():
    snapshot = _snapshot()
    snapshot.pop("city")
    for segment in snapshot["days"][0]["segments"]:
        segment["poi"]["city"] = "北京"

    projection = CreativeProposalTitleService.project(snapshot, city="")
    titled = CreativeProposalTitleService.apply(snapshot, projection=projection)

    assert projection["city"] == "北京"
    assert projection["title"].endswith("｜北京1日")
    assert CreativeProposalTitleService.is_valid_projection(snapshot, projection)
    assert titled["title"] == projection["title"]


def _legacy_title_variants_expose_material_content_for_collision_resolution():
    first = _snapshot()
    second = _snapshot()
    second["days"][0]["segments"][2] = _segment(
        "heritage",
        "B00000005",
        "模式口历史文化街区",
        "14:30",
        family="heritage_walk",
    )

    first_titles = {item["title"] for item in CreativeProposalTitleService.title_variants(first, city="北京")}
    second_titles = {item["title"] for item in CreativeProposalTitleService.title_variants(second, city="北京")}

    assert first_titles.isdisjoint(second_titles)
    assert any("三源里菜市场逛集" in item for item in first_titles)
    assert any("模式口寻旧" in item for item in second_titles)


def test_incomplete_output_quality_rejects_legacy_content_title_projection():
    snapshot = _snapshot()
    projection = {
        "schemaVersion": "creative-proposal-title-v3",
        "generationSource": "agent_grounded_factual_summary",
        "title": "北京地点拼接标题",
    }
    snapshot["portfolioTitleEvidence"] = projection
    snapshot["title"] = projection["title"]

    quality = CreativeOutputQualityService.evaluate(
        snapshot,
        requested_theme="local_food_and_area_walk",
    )

    assert quality["themeEligible"] is False
    assert quality["displayTitle"] == "方案待补全"


def test_legacy_deterministic_title_projection_api_is_closed():
    snapshot = _snapshot()
    original = copy.deepcopy(snapshot)
    snapshot["portfolioOutputQuality"] = {
        "schemaVersion": "creative-output-quality-v2",
        "originalCreativeTitle": "补全前旧标题",
        "displayTitle": "补全前旧标题",
    }

    titled = CreativeProposalTitleService.apply(snapshot, city="北京")
    assert CreativeProposalTitleService.project(snapshot, city="北京") == {}
    assert CreativeProposalTitleService.title_variants(snapshot, city="北京") == []
    assert titled == snapshot
    assert original["title"] == snapshot["title"]


def test_store_truthfulness_pass_does_not_restore_a_generic_direction_title():
    snapshot = _snapshot()
    snapshot["portfolioTitleEvidence"] = {
        "schemaVersion": "creative-proposal-title-v3",
        "generationSource": "agent_grounded_factual_summary",
        "title": "北京地点拼接标题",
    }
    snapshot["portfolioOutputQuality"] = {
        "schemaVersion": "creative-output-quality-v2",
        "mode": "enforce",
        "requestedTheme": "local_food_and_area_walk",
    }
    snapshot["title"] = "市井市场方向旧标题"

    persisted = PlanPortfolioStore._truthful_snapshot_title(
        snapshot,
        {"passed": False, "hardFailures": ["pending_slots_remaining"]},
    )

    assert persisted["title"] == "方案待补全"


def test_title_evidence_rejects_arbitrary_copy_and_becomes_stale_after_plan_change():
    snapshot = _snapshot()
    arbitrary = {
        "schemaVersion": "creative-proposal-title-v3",
        "generationSource": "agent_grounded_factual_summary",
        "title": "随手写的创意标题",
    }

    assert CreativeProposalTitleService.is_valid_projection(snapshot, arbitrary) is False

    snapshot["days"][0]["segments"].append(
        _segment(
            "museum",
            "B00000007",
            "中国国家博物馆",
            "16:00",
            intent_type="museum",
        )
    )
    assert CreativeProposalTitleService.is_valid_projection(snapshot, arbitrary) is False


def test_spoofed_or_zero_coordinate_segments_cannot_enter_title_evidence():
    snapshot = _snapshot()
    snapshot["days"][0]["segments"].append(
        {
            "id": "fake",
            "startTime": "16:00",
            "poi": {
                "amapId": "B00000006",
                "name": "虚构创意园",
                "source": "amap-place-search",
                "latitude": 0,
                "longitude": 0,
            },
            "semanticMetadata": {"optionalExperienceFamily": "art_walk"},
        }
    )

    context = CreativeProposalTitleService.agent_generation_context(snapshot, city="北京")

    assert "B00000006" not in context["evidenceAmapIds"]
    assert all(place["name"] != "虚构创意园" for day in context["days"] for place in day["places"])


def test_agent_title_candidates_require_three_unique_factual_evidence_bindings():
    evidence_ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    raw = json.dumps(
        {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华书声映京城灯火", "evidenceAmapIds": evidence_ids},
                {"title": "烟火清华漫游记", "evidenceAmapIds": evidence_ids},
                {"title": "访校清华见城光", "evidenceAmapIds": evidence_ids},
                {"title": "京华清华慢行录", "evidenceAmapIds": evidence_ids},
            ],
        },
        ensure_ascii=False,
    )

    projection = CreativeProposalTitleService.select_agent_candidate(
        _snapshot(),
        raw,
        reserved_titles={"清华书声映京城灯火"},
    )
    titled = CreativeProposalTitleService.apply_agent_projection(_snapshot(), projection)

    assert projection["title"] == "烟火清华漫游记"
    assert projection["generationSource"] == "agent_generated_title_candidates"
    assert projection["candidateCount"] == 4
    assert projection["selectedAmapIds"] == evidence_ids
    assert titled["title"] == projection["title"]
    assert titled["creativeBrief"]["title"] == projection["title"]


def test_agent_title_candidates_fail_closed_on_templates_or_incomplete_evidence():
    ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "北京｜清华大学、中央电视塔", "evidenceAmapIds": ids},
            {"title": "顺路的北京漫游", "evidenceAmapIds": ids},
            {"title": "学府书声映京城灯火", "evidenceAmapIds": ids[:-1]},
        ],
    }

    assert CreativeProposalTitleService.select_agent_candidate(_snapshot(), payload) == {}


def test_agent_title_text_still_rejects_legacy_template_phrases():
    for title in (
        "北京真实地点草案",
        "围绕学府把夜景排成行程",
        "顺路的京城文化漫游",
    ):
        assert CreativeProposalTitleService._valid_agent_title_text(title) is False


def test_agent_title_candidates_require_three_valid_results_after_filtering():
    ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "学府书声映京城灯火", "evidenceAmapIds": ids},
            {"title": "学府书声映京城灯火", "evidenceAmapIds": ids},
            {"title": "校园烟火与京城夜色", "evidenceAmapIds": ids},
        ],
    }

    assert CreativeProposalTitleService.select_agent_candidate(_snapshot(), payload) == {}


def test_agent_title_projection_rechecks_snapshot_fingerprint_and_rejects_name_list():
    ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    name_list_payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "清华大学中央电视塔", "evidenceAmapIds": ids},
            {"title": "校园烟火与京城夜色", "evidenceAmapIds": ids},
            {"title": "书香市井共赴华灯", "evidenceAmapIds": ids},
        ],
    }
    assert CreativeProposalTitleService.select_agent_candidate(_snapshot(), name_list_payload) == {}

    valid_payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "学府书声映京城灯火", "evidenceAmapIds": ids},
            {"title": "校园烟火与京城夜色", "evidenceAmapIds": ids},
            {"title": "书香市井共赴华灯", "evidenceAmapIds": ids},
        ],
    }
    projection = CreativeProposalTitleService.select_agent_candidate(_snapshot(), valid_payload)
    projection["evidenceFingerprint"] = "forged"
    original = _snapshot()

    assert CreativeProposalTitleService.apply_agent_projection(original, projection) == original


def test_agent_title_candidates_require_six_to_eighteen_han_only():
    ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    payload = {
        "schemaVersion": "creative-proposal-title-candidates-v1",
        "candidates": [
            {"title": "学府书声映京城灯火", "evidenceAmapIds": ids},
            {"title": "校园烟火与京城夜色", "evidenceAmapIds": ids},
            {"title": "书香市井共赴华灯A", "evidenceAmapIds": ids},
        ],
    }

    assert CreativeProposalTitleService.select_agent_candidate(_snapshot(), payload) == {}


def test_agent_title_generation_context_contains_final_verifier_and_route_proof():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}
    snapshot["portfolioRouteEvidence"] = [
        {
            "fromSegmentId": "campus",
            "toSegmentId": "meal",
            "durationSeconds": 900,
            "distanceMeters": 3200,
        }
    ]

    context = CreativeProposalTitleService.agent_generation_context(
        snapshot,
        city="北京",
        primary_axis="photo_night",
        reserved_titles={"旧标题不可复用"},
    )

    assert context["verification"] == {
        "passed": True,
        "placeEvidencePassed": True,
        "readinessState": "complete",
        "verifiedRouteCount": 1,
    }
    assert context["evidenceAmapIds"] == [
        "B00000001",
        "B00000002",
        "B00000003",
        "B00000004",
    ]
    assert context["reservedTitles"] == ["旧标题不可复用"]


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_deepseek_title_provider_requests_exactly_three_evidence_bound_candidates(monkeypatch, model):
    snapshot = _snapshot()
    snapshot["portfolioPendingSlots"] = []
    snapshot["portfolioVerifier"] = {"passed": True}
    context = CreativeProposalTitleService.agent_generation_context(
        snapshot,
        city="北京",
    )
    captured: dict = {}
    raw_response = json.dumps(
        {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [],
        }
    )
    provider = DeepSeekAgentProvider(api_key="test-key", model=model)

    def fake_post(payload, *, timeout_seconds=None):
        captured["payload"] = payload
        captured["timeoutSeconds"] = timeout_seconds
        return raw_response

    monkeypatch.setattr(provider, "_post", fake_post)

    assert provider.generate_proposal_titles(context) == raw_response
    system_prompt = captured["payload"]["messages"][0]["content"]
    user_payload = json.loads(captured["payload"]["messages"][1]["content"])
    assert "exactly three" in system_prompt
    assert "6-18 Chinese characters" in system_prompt
    assert "fewer than two leading Chinese characters" in system_prompt
    assert "fewer than two trailing Chinese characters" in system_prompt
    assert "character-bigram Jaccard similarity below 0.5" in system_prompt
    assert user_payload["evidenceAmapIds"] == context["evidenceAmapIds"]
    assert user_payload["verification"]["passed"] is True
    assert captured["payload"]["model"] == model


def test_deepseek_title_provider_accepts_verified_places_in_an_editable_partial(monkeypatch):
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": False, "draftPassed": True}
    context = CreativeProposalTitleService.agent_generation_context(snapshot, city="北京")
    provider = DeepSeekAgentProvider(api_key="test-key", model="deepseek-v4-flash")
    monkeypatch.setattr(provider, "_post", lambda payload, *, timeout_seconds=None: json.dumps({"ok": True}))

    assert context["verification"]["placeEvidencePassed"] is True
    assert context["verification"]["readinessState"] == "partial_with_verified_places"
    assert json.loads(provider.generate_proposal_titles(context)) == {"ok": True}


def test_agent_title_provider_failure_retries_once_then_uses_fact_bound_fallback():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}

    calls = []

    def fail(context):
        calls.append(copy.deepcopy(context))
        raise TimeoutError("provider timed out")

    projected = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=fail,
        context=CreativeProposalTitleService.agent_generation_context(snapshot),
    )

    assert projected["title"] == "清华学府寻味"
    assert "portfolioTitleEvidence" not in projected
    generation = projected["portfolioTitleGeneration"]
    assert generation["status"] == "failed_non_blocking"
    assert generation["retryable"] is False
    assert generation["reasonCode"] == "title_provider_failed"
    assert generation["failureType"] == "TimeoutError"
    assert generation["attemptCount"] == 2
    assert generation["maxAttempts"] == 2
    assert len(generation["rejectedAttempts"]) == 2
    assert calls[0] == calls[1]
    assert generation["titleDecisionSource"] == "route_fact_bound_server_fallback"
    assert generation["usedTitleSignal"] == "清华"


def test_agent_title_second_attempt_reuses_frozen_context_and_same_validation_rules():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}
    context = CreativeProposalTitleService.agent_generation_context(snapshot)
    evidence_ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    seen = []

    def generate(received):
        seen.append(copy.deepcopy(received))
        if len(seen) == 1:
            received["requiredTitleSignals"].append("污染信号")
            return json.dumps({"schemaVersion": "creative-proposal-title-candidates-v1", "candidates": []})
        return json.dumps(
            {
                "schemaVersion": "creative-proposal-title-candidates-v1",
                "candidates": [
                    {"title": title, "evidenceAmapIds": evidence_ids}
                    for title in ("清华书声映京城灯火", "清华烟火与京城夜色", "清华市井共赴华灯")
                ],
            },
            ensure_ascii=False,
        )

    projected = CreativeProposalTitleService.generate_and_apply_agent_title(
        snapshot,
        generator=generate,
        context=context,
    )

    assert len(seen) == 2
    assert seen[0] == seen[1] == context
    assert projected["title"] == "清华书声映京城灯火"
    assert projected["portfolioTitleGeneration"]["attemptCount"] == 2
    assert projected["portfolioTitleGeneration"]["rejectedAttempts"][0]["reasonCode"] == (
        "title_candidates_invalid"
    )


def test_theme_reverification_preserves_a_still_valid_agent_title():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}
    evidence_ids = ["B00000001", "B00000002", "B00000003", "B00000004"]
    projection = CreativeProposalTitleService.select_agent_candidate(
        snapshot,
        {
            "schemaVersion": "creative-proposal-title-candidates-v1",
            "candidates": [
                {"title": "清华书声映京城灯火", "evidenceAmapIds": evidence_ids},
                {"title": "清华烟火与京城夜色", "evidenceAmapIds": evidence_ids},
                {"title": "清华市井共赴华灯", "evidenceAmapIds": evidence_ids},
            ],
        },
    )
    titled = CreativeProposalTitleService.apply_agent_projection(snapshot, projection)

    class Provider:
        def generate_proposal_titles(self, _context):
            raise AssertionError("valid Agent title must not be overwritten")

    result = AgentService._with_verified_proposal_title(
        SimpleNamespace(provider=Provider()),
        titled,
        brief={"primaryAxis": "campus_led"},
        store=None,
        portfolio_id="portfolio_title",
        proposal_id="proposal_title",
    )

    assert result["title"] == "清华书声映京城灯火"
    assert result["portfolioTitleGeneration"]["status"] == "succeeded"


def test_theme_reverification_uses_fact_bound_fallback_when_provider_is_unconfigured():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}

    class Provider:
        api_key = ""

        def generate_proposal_titles(self, _context):
            raise TimeoutError("provider unavailable")

    result = AgentService._with_verified_proposal_title(
        SimpleNamespace(provider=Provider()),
        snapshot,
        brief={"primaryAxis": "campus_led"},
        store=None,
        portfolio_id="portfolio_title",
        proposal_id="proposal_title",
    )

    assert result["title"] == "清华学府寻味"
    assert result["portfolioTitleGeneration"]["status"] == "failed_non_blocking"
    assert result["portfolioTitleGeneration"]["retryable"] is False
    assert result["portfolioTitleGeneration"]["failureType"] == "TimeoutError"


def test_theme_reverification_uses_fact_bound_fallback_when_title_provider_is_missing():
    snapshot = _snapshot()
    snapshot["portfolioVerifier"] = {"passed": True}

    result = AgentService._with_verified_proposal_title(
        SimpleNamespace(provider=object()),
        snapshot,
        brief={"primaryAxis": "campus_led"},
        store=None,
        portfolio_id="portfolio_title",
        proposal_id="proposal_title",
    )

    assert result["title"] == "清华学府寻味"
    assert result["portfolioTitleGeneration"]["status"] == "failed_non_blocking"
    assert result["portfolioTitleGeneration"]["retryable"] is False
    assert result["portfolioTitleGeneration"]["reasonCode"] == "title_provider_unavailable"
    assert result["portfolioTitleGeneration"]["titleDecisionSource"] == "route_fact_bound_server_fallback"
