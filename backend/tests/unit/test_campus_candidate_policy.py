from types import SimpleNamespace
import copy

from src.services.campus_candidate_policy import CampusCandidatePolicy
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy


def candidate(name: str):
    return SimpleNamespace(
        name=name,
        city="北京",
        type="科教文化服务;学校;高等院校",
        category="campus",
        address="北京市",
        district="海淀区",
        source_note="",
    )


def test_985_constraint_accepts_canonical_beijing_985_and_rejects_other_campus():
    policy = CampusCandidatePolicy()

    assert policy.reject_reason(candidate("北京大学"), "985高校参观") == ""
    assert policy.reject_reason(candidate("北京语言大学"), "985高校参观") == "campus_tier_985_mismatch"


def test_normal_campus_request_does_not_enable_tier_filter():
    assert CampusCandidatePolicy().reject_reason(candidate("北京语言大学"), "北京高校参观") == ""


def test_server_qualification_binding_enforces_tier_when_slot_raw_need_is_generic():
    binding = qualification_binding("北京大学")
    policy = CampusCandidatePolicy()

    assert (
        policy.reject_reason(
            candidate("北京大学昌平校区"),
            "高校参观",
            qualification_binding=binding,
            expected_entity="北京大学",
        )
        == ""
    )
    assert (
        policy.reject_reason(
            candidate("北京科技大学管庄校区"),
            "高校参观",
            qualification_binding=binding,
            expected_entity="北京大学",
        )
        == "campus_tier_985_mismatch"
    )


def test_bound_985_entity_rejects_different_beijing_985_even_when_candidate_qualifies():
    binding = qualification_binding("北京大学")

    assert CampusCandidatePolicy().reject_reason(
        candidate("清华大学"),
        "高校参观",
        qualification_binding=binding,
        expected_entity="北京大学",
    ) == "campus_tier_985_mismatch"


def test_qualification_evidence_is_versioned_and_substring_does_not_prove_parent_identity():
    evidence = EntityQualificationEvidenceService.load("moe_project_classification", "985")
    assert evidence is not None
    assert evidence["source"]["publisher"] == "中华人民共和国教育部"
    assert len(evidence["contentSha256"]) == 64
    policy = CampusCandidatePolicy()
    assert policy.matches_tier(candidate("清华大学"), "985") is True
    assert policy.matches_tier(candidate("清华大学附属中学"), "985") is False
    assert policy.matches_tier(candidate("清华大学科技服务中心"), "985") is False
    assert policy.matches_tier({**candidate("清华大学").__dict__, "city": "上海"}, "985") is False


def qualification_binding(canonical_name: str) -> dict:
    evidence = EntityQualificationEvidenceService.qualified_entities(
        locality="北京",
        scheme="moe_project_classification",
        value="985",
    )
    assert evidence is not None
    entity = next(item for item in evidence["entities"] if item["canonicalName"] == canonical_name)
    return EntityQualificationEvidenceService.build_binding(
        evidence=evidence,
        entity=entity,
        planning_root_id="root_beijing_985",
        request_contract_fingerprint="f" * 64,
    )


def test_bound_985_entity_allows_real_main_campus_suffix_without_weakening_identity():
    policy = CampusCandidatePolicy()
    semantic_policy = IntentCandidateSemanticPolicy()

    for canonical_name, poi_name in (
        ("中国人民大学", "中国人民大学中关村校区"),
        ("北京航空航天大学", "北京航空航天大学学院路校区"),
    ):
        binding = qualification_binding(canonical_name)
        assert (
            policy.reject_reason(
                candidate(poi_name),
                "985高校参观",
                qualification_binding=binding,
                expected_entity=canonical_name,
            )
            == ""
        )
        assert semantic_policy.evaluate(
            "campus_visit",
            candidate(poi_name),
            raw_need="985高校参观",
            exact_entity=canonical_name,
            qualification_binding=binding,
        ).passed is True


def test_bound_985_entity_still_rejects_subfacilities_and_wrong_city():
    policy = CampusCandidatePolicy()
    binding = qualification_binding("中国人民大学")

    assert (
        policy.reject_reason(
            candidate("中国人民大学附属中学"),
            "985高校参观",
            qualification_binding=binding,
            expected_entity="中国人民大学",
        )
        == "weak_campus_entity"
    )
    assert (
        policy.reject_reason(
            candidate("中国人民大学为民楼"),
            "985高校参观",
            qualification_binding=binding,
            expected_entity="中国人民大学",
        )
        == "campus_affiliated_subentity"
    )
    sports_department = candidate("中国人民大学体育部")
    sports_decision = IntentCandidateSemanticPolicy().evaluate(
        "campus_visit",
        sports_department,
        raw_need="985高校参观",
        exact_entity="中国人民大学",
        qualification_binding=binding,
    )
    assert sports_decision.passed is False
    assert sports_decision.reason_code == "campus_affiliated_subentity"
    wrong_school = IntentCandidateSemanticPolicy().evaluate(
        "campus_visit",
        candidate("北京语言大学"),
        raw_need="985高校参观",
        exact_entity="中国人民大学",
        qualification_binding=binding,
    )
    assert wrong_school.passed is False
    assert wrong_school.reason_code == "exact_entity_mismatch"
    wrong_city = {**candidate("中国人民大学中关村校区").__dict__, "city": "上海", "address": "上海市"}
    assert (
        policy.reject_reason(
            wrong_city,
            "985高校参观",
            qualification_binding=binding,
            expected_entity="中国人民大学",
        )
        == "campus_tier_985_mismatch"
    )


def test_qualification_binding_fails_closed_on_tampering_epoch_and_cross_root():
    binding = qualification_binding("中国人民大学")
    entity_fingerprint = binding["evidenceEntityFingerprint"]

    assert (
        EntityQualificationEvidenceService.validate_binding(
            binding,
            expected_planning_root_id="root_beijing_985",
            expected_request_contract_fingerprint="f" * 64,
            expected_entity_fingerprint=entity_fingerprint,
            expected_canonical_name="中国人民大学",
        )
        == ""
    )
    forged_entity = copy.deepcopy(binding)
    forged_entity["evidenceEntityFingerprint"] = "0" * 64
    assert EntityQualificationEvidenceService.validate_binding(forged_entity).startswith(
        "qualification_binding_fingerprint_mismatch"
    )
    stale_epoch = copy.deepcopy(binding)
    stale_epoch["qualificationEvidenceFingerprint"] = "1" * 64
    stale_epoch["bindingFingerprint"] = EntityQualificationEvidenceService._fingerprint(
        {key: value for key, value in stale_epoch.items() if key != "bindingFingerprint"}
    )
    assert (
        EntityQualificationEvidenceService.validate_binding(stale_epoch)
        == "qualification_binding_evidence_epoch_mismatch"
    )
    assert (
        EntityQualificationEvidenceService.validate_binding(
            binding,
            expected_planning_root_id="another_root",
        )
        == "qualification_binding_scope_mismatch"
    )
