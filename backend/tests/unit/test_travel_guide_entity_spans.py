from __future__ import annotations

import copy
import hashlib
import json

import pytest

from src.services.travel_guide_advice_service import TravelGuideAdviceService


def _advice(sources: list[tuple[str, str]], *, intent: str = "park") -> dict:
    refs = []
    recommendations = []
    for index, (title, summary) in enumerate(sources):
        material = {
            "title": title,
            "summary": summary,
            "url": f"https://travel.example/guide/{index}",
            "queriedAt": "2026-09-05T10:00:00+08:00",
        }
        fingerprint = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        ref = {key: value for key, value in material.items() if key != "summary"}
        ref.update(refId=f"guide-{index}", sourceFingerprint=fingerprint)
        refs.append(ref)
        recommendations.append({**ref, "text": summary, "sourceUrl": material["url"]})
    evidence_fingerprint = hashlib.sha256(
        json.dumps(
            {"queryFingerprint": "a" * 64, "sourceFingerprints": [ref["sourceFingerprint"] for ref in refs]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "queryFingerprint": "a" * 64,
        "evidenceFingerprint": evidence_fingerprint,
        "sourceRefs": refs,
        "recommendations": recommendations,
        "conclusion": {
            "generationMethod": "deterministic_fallback_v1",
            "placeMentions": [],
            "takeaways": [{"intentType": intent, "text": sources[0][1], "sourceRefIds": ["guide-0"]}],
        },
    }


@pytest.mark.parametrize(
    ("text", "intent", "expected"),
    [
        ("只需要花2块钱就能登上景山公园", "park", ["景山公园"]),
        ("北京本地宝为您提供北京高校开放信息，包括北京各个大学开放时间。", "campus_visit", []),
        ("全国高校和各个大学参观攻略", "campus_visit", []),
        ("参观北京大学和清华大学", "campus_visit", ["北京大学", "清华大学"]),
        ("南海子公园和景山公园适合游览", "park", ["南海子公园", "景山公园"]),
        ("四季民福烤鸭店适合午餐", "meal", ["四季民福烤鸭店"]),
        ("和平公园适合散步", "park", ["和平公园"]),
    ],
)
def test_entity_spans_split_prose_and_reject_generic_categories(text, intent, expected):
    assert TravelGuideAdviceService._place_mentions_in_text(text, intent) == expected


@pytest.mark.parametrize(
    ("title", "summary", "expected"),
    [
        ("北京公园游玩攻略", "只需要花2块钱就能登上景山公园", "景山公园"),
        ("南海子公园游玩攻略", "北京周末出游可提前查阅开放时间。", "南海子公园"),
    ],
)
def test_deterministic_mentions_can_come_from_title_or_summary(title, summary, expected):
    advice = _advice([(title, summary)])
    admitted, _ = TravelGuideAdviceService._guide_payload_evidence(advice)
    mentions = TravelGuideAdviceService._deterministic_place_mentions(
        admitted=admitted, themes=TravelGuideAdviceService._theme_specs(["park"])
    )
    assert [mention["mentionText"] for mention in mentions] == [expected]


def test_modern_explicit_empty_mentions_never_enter_legacy_takeaway_extraction(monkeypatch):
    advice = _advice([("北京公园游览攻略", "只需要花2块钱就能登上景山公园")])

    def forbidden_legacy(**_kwargs):
        raise AssertionError("explicit empty modern mentions entered legacy fallback")

    monkeypatch.setattr(TravelGuideAdviceService, "_legacy_conclusion_place_mentions", forbidden_legacy)
    assert TravelGuideAdviceService.extract_place_hints(advice) == []


@pytest.mark.parametrize(
    ("title", "summary", "mention", "intent"),
    [
        ("北京公园攻略", "只需要花2块钱就能登上景山公园", "只需要花2块钱就能登上景山公园", "park"),
        ("北京高校攻略", "包括北京各个大学开放时间", "包括北京各个大学", "campus_visit"),
        ("北京高校攻略", "全国高校开放信息", "全国高校", "campus_visit"),
        ("北京大学攻略", "北京大学参观需预约", "清华大学", "campus_visit"),
        ("南海子公园攻略", "南海子公园游览需查开放时间", "海子公园", "park"),
    ],
)
def test_structured_model_mentions_require_entity_spans_not_just_source_substrings(title, summary, mention, intent):
    advice = _advice([(title, summary)], intent=intent)
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": mention, "intentType": intent, "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={intent},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize("mention", ["北京大学", "南海子公园", "四季民福"])
def test_source_grounded_names_survive_without_venue_dictionary(mention):
    intent = "campus_visit" if mention.endswith("大学") else "park" if mention.endswith("公园") else "meal"
    advice = _advice([(f"{mention}游览攻略", f"推荐{mention}，提前查阅预约信息。")], intent=intent)
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    hints = TravelGuideAdviceService._normalize_place_mentions(
        [{"mentionText": mention, "intentType": intent, "sourceRefIds": ["guide-0"]}],
        admitted=admitted,
        allowed_intent_types={intent},
        guide_evidence_fingerprint=fingerprint,
        require_explicit_bindings=False,
    )
    assert hints[0]["mentionText"] == mention


def test_legacy_takeaways_rederive_only_safe_spans_from_validated_source():
    advice = _advice([("北京公园攻略", "只需要花2块钱就能登上景山公园")])
    advice["conclusion"].pop("placeMentions")
    original = copy.deepcopy(advice)
    hints = TravelGuideAdviceService.extract_place_hints(advice)
    assert [hint["mentionText"] for hint in hints] == ["景山公园"]
    assert advice == original
    assert hints[0]["guideEvidenceFingerprint"] == advice["evidenceFingerprint"]


@pytest.mark.parametrize("tamper", ["source", "reference", "evidence"])
def test_legacy_safe_derivation_does_not_repair_tampered_evidence(tamper):
    advice = _advice([("景山公园攻略", "景山公园可以游览")])
    advice["conclusion"].pop("placeMentions")
    if tamper == "source":
        advice["recommendations"][0]["text"] = "南海子公园可以游览"
    elif tamper == "reference":
        advice["conclusion"]["takeaways"][0]["sourceRefIds"] = ["missing"]
    else:
        advice["evidenceFingerprint"] = "0" * 64
    assert TravelGuideAdviceService.extract_place_hints(advice) == []


def test_legacy_empty_hints_require_explicit_derivation_and_keep_original_evidence():
    advice = _advice([("北京公园攻略", "只需要花2块钱就能登上景山公园")])
    original = copy.deepcopy(advice)
    assert TravelGuideAdviceService.extract_place_hints(advice) == []
    hints = TravelGuideAdviceService.extract_place_hints(advice, allow_legacy_source_derivation=True)
    assert [hint["mentionText"] for hint in hints] == ["景山公园"]
    assert hints[0]["guideEvidenceFingerprint"] == advice["evidenceFingerprint"]
    assert advice == original
    advice["conclusion"]["placeMentionExtractionVersion"] = "source_entity_span_v2"
    assert TravelGuideAdviceService.extract_place_hints(advice, allow_legacy_source_derivation=True) == []


def _legacy_sentence_hint() -> dict:
    advice = _advice([("北京公园攻略", "只需要花2块钱就能登上景山公园")])
    advice["placeHints"] = [
        {
            "mentionText": "只需要花2块钱就能登上景山公园",
            "intentType": "park",
            "sourceRefIds": ["guide-0"],
            "sourceFingerprints": [advice["sourceRefs"][0]["sourceFingerprint"]],
            "guideEvidenceFingerprint": advice["evidenceFingerprint"],
            "verificationStatus": "unresolved_amap_grounding",
        }
    ]
    return advice


def test_old_persisted_sentence_hint_is_rederived_without_mutating_signed_material():
    advice = _legacy_sentence_hint()
    original = copy.deepcopy(advice)
    assert TravelGuideAdviceService.extract_place_hints(advice) == []
    hints = TravelGuideAdviceService.extract_place_hints(advice, allow_legacy_source_derivation=True)
    assert [hint["mentionText"] for hint in hints] == ["景山公园"]
    assert advice == original
    assert hints[0]["sourceFingerprints"] == original["placeHints"][0]["sourceFingerprints"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sourceRefIds", ["missing"]),
        ("sourceFingerprints", ["0" * 64]),
        ("guideEvidenceFingerprint", "0" * 64),
        ("verificationStatus", "verified"),
        ("mentionText", "只需要花2块钱就能登上不存在公园"),
    ],
)
def test_old_sentence_recovery_cannot_bypass_original_hint_bindings(field, value):
    advice = _legacy_sentence_hint()
    advice["placeHints"][0][field] = value
    assert TravelGuideAdviceService.extract_place_hints(advice, allow_legacy_source_derivation=True) == []


def test_old_empty_guide_builds_stable_new_overlay_without_rewriting_guide_or_claim():
    from backend.tests.unit.test_agent_service import open_db
    from backend.tests.unit.test_guide_grounded_continuation import _persist_guide_carrier
    from src.services.agent_service import AgentService
    from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService

    with open_db() as db:
        session, selected = _persist_guide_carrier(db)
        payload = json.loads(
            db.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?",
                (selected["sourceAssistantTurnId"],),
            ).fetchone()[0]
        )
        payload["guideAdvice"] = {"status": "completed", **_legacy_sentence_hint()}
        original_json = json.dumps(payload, ensure_ascii=False)
        db.execute(
            "UPDATE conversation_turns SET agent_response_json = ? WHERE id = ?",
            (original_json, selected["sourceAssistantTurnId"]),
        )
        db.execute(
            "UPDATE agent_choice_executions SET outcome_json = ? WHERE action = 'search_travel_guide_advice'",
            (original_json,),
        )
        db.commit()
        requirements = GuideContinuationRequirementService(db)
        current = requirements.build(session_id=session, selected_choice=selected, active_version_id=None)
        repeated = requirements.build(session_id=session, selected_choice=selected, active_version_id=None)
        assert current == repeated
        assert current["placeHints"][0]["mentionText"] == "景山公园"
        assert current["evidenceFingerprint"] == payload["guideAdvice"]["evidenceFingerprint"]

        old_requirement = copy.deepcopy(current)
        old_requirement["placeHints"][0]["mentionText"] = "只需要花2块钱就能登上景山公园"
        old_requirement["requirementFingerprint"] = requirements.requirement_fingerprint(old_requirement)
        assert current["requirementFingerprint"] != old_requirement["requirementFingerprint"]
        service = AgentService(db)
        service.conversation_intent_router.routing_mode = "active-all"
        selected["_serverGuideEvidenceFingerprint"] = current["evidenceFingerprint"]
        request = service._insert_turn(session, "user", "参考攻略生成方案", "active")
        claim = service._claim_fallback_choice_execution(
            session, request, selected, guide_binding_requirement=old_requirement
        )
        stored_claim = db.execute(
            "SELECT continuation_json FROM agent_choice_executions WHERE id = ?", (claim["id"],)
        ).fetchone()[0]
        assert json.loads(stored_claim)["guideContinuationRequirement"] == old_requirement
        # Re-deriving the fresh overlay is read-only. The caller must retain its
        # existing claim/replay mismatch guard instead of replacing that claim.
        assert requirements.build(session_id=session, selected_choice=selected, active_version_id=None) == current
        assert (
            db.execute("SELECT continuation_json FROM agent_choice_executions WHERE id = ?", (claim["id"],)).fetchone()[
                0
            ]
            == stored_claim
        )
        assert (
            db.execute(
                "SELECT agent_response_json FROM conversation_turns WHERE id = ?", (selected["sourceAssistantTurnId"],)
            ).fetchone()[0]
            == original_json
        )


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("北京有景山公园和颐和园", "park"),
        ("北京的景山公园", "park"),
        ("北京城市公园开放信息", "park"),
        ("北京大学附属中学参观攻略", "campus_visit"),
        ("清华大学附属小学开放信息", "campus_visit"),
    ],
)
def test_entity_span_rejects_ambiguous_context_and_attached_facility_prefixes(text, intent):
    assert TravelGuideAdviceService._place_mentions_in_text(text, intent) == []


def test_entity_span_keeps_safe_action_and_conjunction_boundaries():
    assert TravelGuideAdviceService._place_mentions_in_text("推荐景山公园和北海公园", "park") == [
        "景山公园",
        "北海公园",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "北京大学第一附属中学参观攻略",
        "清华大学第二附属小学开放信息",
    ],
)
def test_entity_span_rejects_ordinal_attached_facilities(text):
    assert TravelGuideAdviceService._place_mentions_in_text(text, "campus_visit") == []


def test_deterministic_projection_does_not_emit_ordinal_attached_facility_prefix():
    advice = _advice(
        [("高校参观攻略", "北京大学第一附属中学参观攻略")],
        intent="campus_visit",
    )
    admitted, _ = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert (
        TravelGuideAdviceService._deterministic_place_mentions(
            admitted=admitted,
            themes=TravelGuideAdviceService._theme_specs(["campus_visit"]),
        )
        == []
    )


def test_model_entity_binding_rejects_ordinal_attached_facility_prefix():
    advice = _advice(
        [("高校参观攻略", "北京大学第一附属中学参观攻略")],
        intent="campus_visit",
    )
    admitted, evidence_fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": "北京大学", "intentType": "campus_visit", "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={"campus_visit"},
            guide_evidence_fingerprint=evidence_fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize("intent", ["meal", "food_experience"])
def test_bare_shop_types_cannot_be_deterministically_promoted_to_restaurant_names(intent):
    source = (
        "美食攻略避开景区网红陷阱，推荐地道店铺和小吃，让你品尝最正宗的京味，"
        "融入北京美食攻略、北京特色小吃等关键词。"
        "1. 北京烤鸭：必吃美食，推荐四季民福（性价比高"
    )
    advice = _advice([("北京美食攻略", source)], intent=intent)
    admitted, _ = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert TravelGuideAdviceService._place_mentions_in_text(source, intent) == []
    assert (
        TravelGuideAdviceService._deterministic_place_mentions(
            admitted=admitted, themes=TravelGuideAdviceService._theme_specs([intent])
        )
        == []
    )


@pytest.mark.parametrize("mention", ["地道店", "便民门店", "特色店家", "街边店铺"])
@pytest.mark.parametrize("intent", ["meal", "food_experience"])
def test_structured_brand_fallback_cannot_reintroduce_bare_shop_types(mention, intent):
    advice = _advice([("北京美食攻略", f"推荐{mention}，提前查询到访信息。")], intent=intent)
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": mention, "intentType": intent, "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={intent},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize("tail", ["家", "铺"])
@pytest.mark.parametrize("mention", ["四季民福", "四季民福烤鸭店"])
def test_shop_word_continuations_do_not_create_shorter_restaurant_entity_spans(tail, mention):
    text = f"推荐四季民福烤鸭店{tail}，提前查阅信息。"
    advice = _advice([("北京美食攻略", text)], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert TravelGuideAdviceService._place_mentions_in_text(text, "meal") == []
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": mention, "intentType": "meal", "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={"meal"},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize(
    "model_mentions", [[], [{"mentionText": "地道店", "intentType": "meal", "sourceRefIds": ["guide-0"]}]]
)
def test_real_shop_phrase_cannot_survive_model_or_empty_model_fallback(model_mentions):
    source = "美食攻略避开景区网红陷阱，推荐地道店铺和小吃，让你品尝最正宗的京味。"
    advice = _advice([("北京美食攻略", source)], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    themes = TravelGuideAdviceService._theme_specs(["meal"])
    fallback = TravelGuideAdviceService._deterministic_conclusion(
        admitted=admitted, themes=themes, evidence_fingerprint=fingerprint
    )
    parsed = {
        "overview": "北京美食攻略",
        "conflicts": [],
        "placeMentions": model_mentions,
        "takeaways": [
            {
                "intentType": "meal",
                "themeLabel": "当地美食",
                "text": source,
                "evidenceQuote": "推荐地道店铺和小吃",
                "sourceRefIds": ["guide-0"],
            }
        ],
    }
    if model_mentions:
        with pytest.raises(ValueError):
            TravelGuideAdviceService._validated_model_conclusion(
                parsed, admitted=admitted, themes=themes, deterministic=fallback, evidence_fingerprint=fingerprint
            )
    else:
        conclusion = TravelGuideAdviceService._validated_model_conclusion(
            parsed, admitted=admitted, themes=themes, deterministic=fallback, evidence_fingerprint=fingerprint
        )
        assert conclusion["placeMentions"] == []
    assert fallback["placeMentions"] == []


def test_complete_brand_span_in_real_source_remains_eligible_for_structured_grounding():
    advice = _advice([("北京美食攻略", "北京烤鸭：必吃美食，推荐四季民福（性价比高")], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    hints = TravelGuideAdviceService._normalize_place_mentions(
        [{"mentionText": "四季民福", "intentType": "meal", "sourceRefIds": ["guide-0"]}],
        admitted=admitted,
        allowed_intent_types={"meal"},
        guide_evidence_fingerprint=fingerprint,
        require_explicit_bindings=False,
    )
    assert hints[0]["mentionText"] == "四季民福"


@pytest.mark.parametrize("mention", ["地道小馆", "四季民福小馆", "四季民福"])
def test_restaurant_diminutive_word_is_not_truncated_into_an_entity(mention):
    text = "推荐地道小馆子" if mention == "地道小馆" else "推荐四季民福小馆子"
    advice = _advice([("北京美食攻略", text)], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert TravelGuideAdviceService._place_mentions_in_text(text, "meal") == []
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": mention, "intentType": "meal", "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={"meal"},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize(
    ("intent", "name"),
    [
        ("meal", "四季民福烤鸭店"),
        ("food_experience", "四季民福餐厅"),
        ("park", "北海公园"),
        ("campus_visit", "北京大学"),
    ],
)
@pytest.mark.parametrize("tail", ["停车场", "南门", "未知连续名称"])
def test_source_entity_right_boundary_rejects_continuous_unknown_name_components(intent, name, tail):
    text = f"推荐{name}{tail}"
    advice = _advice([("旅行建议", text)], intent=intent)
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert TravelGuideAdviceService._place_mentions_in_text(text, intent) == []
    assert (
        TravelGuideAdviceService._deterministic_place_mentions(
            admitted=admitted, themes=TravelGuideAdviceService._theme_specs([intent])
        )
        == []
    )
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": name, "intentType": intent, "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={intent},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize("tail", ["停车场", "南门", "未知连续名称"])
def test_structured_brand_cannot_skip_the_full_restaurant_entity_right_boundary(tail):
    advice = _advice([("旅行建议", f"推荐四季民福烤鸭店{tail}")], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    with pytest.raises(ValueError):
        TravelGuideAdviceService._normalize_place_mentions(
            [{"mentionText": "四季民福", "intentType": "meal", "sourceRefIds": ["guide-0"]}],
            admitted=admitted,
            allowed_intent_types={"meal"},
            guide_evidence_fingerprint=fingerprint,
            require_explicit_bindings=False,
        )


@pytest.mark.parametrize(
    ("intent", "name", "tail"),
    [
        ("park", "北海公园", ""),
        ("park", "北海公园", "，适合散步"),
        ("park", "北海公园", " 开放信息"),
        ("park", "北海公园", "开放时间需确认"),
        ("park", "北海公园", "适合散步"),
        ("park", "北海公园", "和景山公园"),
        ("campus_visit", "北京大学", "参观需预约"),
        ("museum", "首都博物馆", "游览指南"),
        ("meal", "四季民福烤鸭店", "适合午餐"),
    ],
)
def test_source_entity_right_boundary_preserves_complete_spans_and_known_descriptions(intent, name, tail):
    text = f"推荐{name}{tail}"
    advice = _advice([("旅行建议", text)], intent=intent)
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    assert name in TravelGuideAdviceService._place_mentions_in_text(text, intent)
    mentions = TravelGuideAdviceService._deterministic_place_mentions(
        admitted=admitted, themes=TravelGuideAdviceService._theme_specs([intent])
    )
    assert mentions[0]["mentionText"] == name
    bound = TravelGuideAdviceService._normalize_place_mentions(
        [{"mentionText": name, "intentType": intent, "sourceRefIds": ["guide-0"]}],
        admitted=admitted,
        allowed_intent_types={intent},
        guide_evidence_fingerprint=fingerprint,
        require_explicit_bindings=False,
    )
    assert bound[0]["mentionText"] == name


@pytest.mark.parametrize("tail", ["烤鸭店", "饭店适合午餐", "小馆开放时间需确认", "适合午餐", "（性价比高"])
def test_structured_brand_uses_same_complete_span_or_description_boundary(tail):
    advice = _advice([("旅行建议", f"推荐四季民福{tail}")], intent="meal")
    admitted, fingerprint = TravelGuideAdviceService._guide_payload_evidence(advice)
    bound = TravelGuideAdviceService._normalize_place_mentions(
        [{"mentionText": "四季民福", "intentType": "meal", "sourceRefIds": ["guide-0"]}],
        admitted=admitted,
        allowed_intent_types={"meal"},
        guide_evidence_fingerprint=fingerprint,
        require_explicit_bindings=False,
    )
    assert bound[0]["mentionText"] == "四季民福"
