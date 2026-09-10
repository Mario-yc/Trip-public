from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from src.api.schemas.maps import MapPoiResponse, MapPoiSearchResponse
from src.services.simple_open_itinerary_executor import SimpleOpenItineraryExecutor


def _hint(name="国家博物馆"):
    return {
        "schemaVersion": "guide-place-hint-v1",
        "mentionText": name,
        "intentType": "museum",
        "sourceRefIds": ["guide-1"],
        "sourceFingerprints": ["a" * 64],
        "guideEvidenceFingerprint": "b" * 64,
        "verificationStatus": "unresolved_amap_grounding",
    }


def _candidate(name, identity="B0MUSEUM01", **updates):
    candidate = MapPoiResponse(
        id=identity,
        name=name,
        type="科教文化服务;博物馆",
        providerTypeCode="140100",
        city="北京市",
        district="东城区",
        address="东长安街16号",
        longitude=116.401304,
        latitude=39.905374,
        category="museum",
        source="amap-place-search",
        sourceNote="recorded provider shape",
        confidence=0.86,
        providerQueryReceiptFingerprint="c" * 64,
        providerQueriedAt=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    return candidate.model_copy(update=updates)


def _search(candidates, hint=None):
    class RecordedResults:
        calls = 0

        def search(self, city, keyword, category, **kwargs):
            self.calls += 1
            return MapPoiSearchResponse(
                city=city,
                keyword=keyword,
                category=category,
                providerName="amap-place-search",
                queriedAt=datetime(2026, 9, 6, tzinfo=timezone.utc),
                providerQueryReceiptFingerprint="c" * 64,
                pois=candidates,
            )

    hint = hint or _hint()
    provider = RecordedResults()
    executor = SimpleOpenItineraryExecutor(provider)
    query_state = {}
    result = executor._search_candidate(
        city="北京",
        query=hint["mentionText"],
        category="museum",
        intent_type="museum",
        raw_need="参观一处博物馆",
        exact_entity=None,
        optional_experience_family="",
        used_identity_ids=set(),
        used_physical_keys=set(),
        guide_hint=hint,
        guide_query_state=query_state,
    )
    assert provider.calls == 1
    return result, query_state


@pytest.mark.parametrize(
    "mention,name,aliases,method",
    [
        ("国家博物馆", "中国国家博物馆", ["国家博物馆"], "provider_alias_exact"),
        ("ABC博物馆", "ＡＢＣ博物馆", [], "unicode_name_equivalent"),
        ("国家博物馆", "国家博物馆", [], "provider_name_exact"),
    ],
)
def test_unique_provider_name_identity_records_traceable_binding(mention, name, aliases, method):
    result, state = _search([_candidate(name, provider_aliases=aliases)], _hint(mention))
    assert result[1] is not None
    proof = state["guideIdentityMatches"]["B0MUSEUM01"]
    assert proof["matchMethod"] == method
    assert proof["sourceField"] == ("alias" if aliases else "name")
    assert proof["providerName"] == "amap-place-search"
    assert proof["providerQueryReceiptFingerprint"] == "c" * 64
    assert len(proof["candidateFingerprint"]) == 64
    assert len(proof["candidateSetFingerprint"]) == 64
    assert proof["sourceFingerprints"] == ["a" * 64]
    assert proof["canonicalName"] == name
    assert proof["matchedAlias"] == ("国家博物馆" if aliases else None)


def test_recorded_missing_alias_remains_unresolved_despite_city_type_address_and_substring():
    result, _ = _search([_candidate("中国国家博物馆")])
    assert result[1] is None
    assert result[-1][0]["reasonCodes"] == ["guide_identity_evidence_missing"]


@pytest.mark.parametrize("name", ["国家博物馆", "中国国家博物馆"])
def test_two_matching_provider_identities_are_rejected_before_selecting_or_reserving(name):
    result, state = _search(
        [
            _candidate(name, "B0MUSEUM01", provider_aliases=["国家博物馆"]),
            _candidate(name, "B0MUSEUM02", address="另一条路10号", provider_aliases=["国家博物馆"]),
        ]
    )
    assert result[1] is None
    assert result[4] == []
    assert state["guideMatchIds"] == ["B0MUSEUM01", "B0MUSEUM02"]
    assert {reason for item in result[-1] for reason in item["reasonCodes"]} == {"guide_identity_ambiguous"}


@pytest.mark.parametrize(
    "name,updates",
    [
        ("中国国家博物馆分馆", {}),
        ("中国国家博物馆-附属馆", {}),
        ("中国国家博物馆南门", {}),
        ("中国国家博物馆", {"parent_poi_id": "B0PARENT01"}),
        ("中国国家博物馆", {"indoor_parent_poi_id": "B0PARENT01"}),
        ("中国国家博物馆", {"provider_query_receipt_fingerprint": None}),
        ("中国国家博物馆", {"city": "天津市"}),
        ("中国国家博物馆", {"type": ""}),
        ("中国国家博物馆", {"source": "model-generated"}),
    ],
)
def test_alias_never_erases_branch_parent_city_or_provider_evidence(name, updates):
    result, _ = _search([_candidate(name, provider_aliases=["国家博物馆"], **updates)])
    assert result[1] is None


def test_self_reported_alias_claims_are_not_provider_identity_evidence():
    result, _ = _search(
        [
            _candidate(
                "中国国家博物馆",
                source_claims=[{"alias": "国家博物馆", "verified": True, "provider": "amap"}],
            )
        ]
    )
    assert result[1] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "不同博物馆"),
        ("address", "不同路10号"),
        ("amap_id", "B0OTHER001"),
        ("parent_poi_id", "B0PARENT01"),
        ("provider_query_receipt_fingerprint", "e" * 64),
    ],
)
def test_binding_is_revalidated_against_selected_candidate_material(field, value):
    from src.services.guide_poi_identity_service import GuidePoiIdentityService

    result, state = _search([_candidate("国家博物馆")])
    proof = state["guideIdentityMatches"]["B0MUSEUM01"]
    selected = result[1]
    assert GuidePoiIdentityService.validate_evidence(hint=_hint(), candidate=selected, evidence=proof)
    altered = copy.deepcopy(selected)
    setattr(altered, field, value)
    assert not GuidePoiIdentityService.validate_evidence(hint=_hint(), candidate=altered, evidence=proof)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sourceFingerprints", ["e" * 64]),
        ("guideEvidenceFingerprint", "e" * 64),
        ("mentionText", "不同博物馆"),
    ],
)
def test_binding_is_revalidated_against_current_source_hint(field, value):
    from src.services.guide_poi_identity_service import GuidePoiIdentityService

    result, state = _search([_candidate("国家博物馆")])
    proof = state["guideIdentityMatches"]["B0MUSEUM01"]
    changed = {**_hint(), field: value}
    assert not GuidePoiIdentityService.validate_evidence(hint=changed, candidate=result[1], evidence=proof)


def test_conflicting_rows_of_one_amap_id_do_not_hide_identity_ambiguity():
    result, state = _search(
        [
            _candidate("国家博物馆"),
            _candidate("国家博物馆", address="冲突地址8号"),
        ]
    )
    assert state["guideIdentityAmbiguous"] is True
    assert result[1] is None


@pytest.mark.parametrize("parent_field", ["parent_poi_id", "indoor_parent_poi_id"])
def test_provider_building_self_parent_preserves_same_canonical_identity(parent_field):
    result, _ = _search([_candidate("中国国家博物馆", provider_aliases=["国家博物馆"], **{parent_field: "B0MUSEUM01"})])
    assert result[1] is not None


def test_empty_normalized_mention_and_alias_never_match():
    result, _ = _search([_candidate("中国国家博物馆", provider_aliases=["---"])], _hint("（）"))
    assert result[1] is None


@pytest.mark.parametrize("name", ["国家博物馆", "国家 博物馆"])
@pytest.mark.parametrize("parent_field", ["parent_poi_id", "indoor_parent_poi_id"])
def test_exact_and_unicode_museum_names_cannot_bind_another_parent_entity(name, parent_field):
    result, _ = _search([_candidate(name, **{parent_field: "B0PARENT01"})])
    assert result[1] is None


@pytest.mark.parametrize("name", ["国家博物馆", "国家 博物馆"])
@pytest.mark.parametrize("parent_field", ["parent_poi_id", "indoor_parent_poi_id"])
def test_exact_and_unicode_museum_self_parent_is_still_canonical(name, parent_field):
    result, _ = _search([_candidate(name, **{parent_field: "B0MUSEUM01"})])
    assert result[1] is not None


@pytest.mark.parametrize(
    "intent,name,mention,expected",
    [
        ("meal", "四季民福烤鸭店(故宫店)", "四季民福", "provider_restaurant_brand"),
        ("meal", "四季民福烤鸭店(故宫店)", "四季民福烤鸭店(故宫店)", "provider_name_exact"),
        ("campus_visit", "北京大学(燕园校区)", "北京大学(燕园校区)", "provider_name_exact"),
    ],
)
def test_requested_restaurant_branches_and_campuses_keep_existing_semantic_admission(intent, name, mention, expected):
    from src.services.guide_poi_identity_service import GuidePoiIdentityService

    candidate = _candidate(name, indoor_parent_poi_id="B0PARENT01")
    hint = {**_hint(mention), "intentType": intent}
    assert GuidePoiIdentityService.candidate_method(hint, candidate, "北京") == expected


@pytest.mark.parametrize(
    "policy,proof_present,accepted",
    [
        ("absent", False, True),
        ("guide-poi-identity-v1", False, False),
        ("guide-poi-identity-v1", True, True),
        ("unknown-policy", True, False),
        (None, True, False),
    ],
)
def test_server_identity_policy_requires_even_exact_name_proofs(policy, proof_present, accepted):
    from backend.tests.unit.test_guide_grounded_continuation import _guide_requirement
    from src.services.agent_service import AgentService
    from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    hint = _hint()
    result, state = _search([_candidate("国家博物馆")], hint)
    selected = result[1]
    proof = state["guideIdentityMatches"]["B0MUSEUM01"]
    constraints = SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
        hint,
        selected=selected,
        day_number=1,
        planning_slot_id="guide",
        primary_result_count=1,
        guide_match_count=1,
        semantic_rejection=False,
        duplicate_rejection=False,
        provider_called=True,
        provider_outcome="success",
        identity_evidence=proof,
    )
    requirement = _guide_requirement(
        evidence_fingerprint="b" * 64,
        mention_text="国家博物馆",
        intent_type="museum",
        source_ref_id="guide-1",
        source_fingerprint="a" * 64,
    )
    if policy != "absent":
        requirement["identityPolicy"] = policy
    requirement["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(requirement)
    if not proof_present:
        constraints["guideEvidence"].pop("identityMatch")
    snapshot = {
        "guideContinuationRequirement": requirement,
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": object.__new__(AgentService)._persistable_poi_payload(selected),
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "planningSlotId": "guide",
                            "scheduleConstraints": constraints,
                        },
                    }
                ],
            }
        ],
    }
    usage = SimpleOpenDirectionService._guide_evidence_usage(
        snapshot, route_verified=True, hard_constraints_passed=True
    )
    assert (usage["status"] == "satisfied") is accepted


@pytest.mark.parametrize(
    "tamper", [None, "providerAliases", "parentPoiId", "identityMatch", "sourceExcerpt", "deleteProof"]
)
def test_alias_proof_survives_real_payload_projection_and_usage_revalidates_it(tamper):
    from backend.tests.unit.test_guide_grounded_continuation import _guide_requirement
    from src.services.agent_service import AgentService
    from src.services.simple_open_direction_service import SimpleOpenDirectionService

    hint = {**_hint(), "sourceExcerpt": "14:00国家博物馆（提前预约）", "sourceDocumentFingerprints": ["e" * 64]}
    result, state = _search([_candidate("中国国家博物馆", provider_aliases=["国家博物馆"])], hint)
    selected = result[1]
    assert selected is not None
    proof = state["guideIdentityMatches"]["B0MUSEUM01"]
    constraints = SimpleOpenItineraryExecutor._guide_evidence_schedule_constraints(
        hint,
        selected=selected,
        day_number=1,
        planning_slot_id="guide",
        primary_result_count=1,
        guide_match_count=1,
        semantic_rejection=False,
        duplicate_rejection=False,
        provider_called=True,
        provider_outcome="success",
        identity_evidence=proof,
    )
    assert constraints["guideEvidence"]["identityMatch"] == proof
    payload = object.__new__(AgentService)._persistable_poi_payload(selected)
    assert payload["providerAliases"] == ["国家博物馆"]
    requirement = _guide_requirement(
        evidence_fingerprint="b" * 64,
        mention_text="国家博物馆",
        intent_type="museum",
        source_ref_id="guide-1",
        source_fingerprint="a" * 64,
    )
    if tamper == "identityMatch":
        constraints["guideEvidence"]["identityMatch"]["candidateFingerprint"] = "f" * 64
    elif tamper == "deleteProof":
        constraints["guideEvidence"].pop("identityMatch")
    elif tamper == "sourceExcerpt":
        constraints["guideEvidence"]["sourceExcerpt"] = "篡改正文"
    elif tamper:
        payload[tamper] = ["伪造别名"] if tamper == "providerAliases" else "B0PARENT01"
    snapshot = {
        "guideContinuationRequirement": requirement,
        "days": [
            {
                "dayNumber": 1,
                "segments": [
                    {
                        "poi": payload,
                        "semanticMetadata": {
                            "routeAnchor": True,
                            "planningSlotId": "guide",
                            "scheduleConstraints": constraints,
                        },
                    }
                ],
            }
        ],
    }
    usage = SimpleOpenDirectionService._guide_evidence_usage(
        snapshot, route_verified=True, hard_constraints_passed=True
    )
    if tamper:
        assert usage["status"] == "unsatisfied"
        assert usage["rejectionCounts"]["guide_evidence_lineage_invalid"] == 1
    else:
        assert usage["status"] == "satisfied"
        assert usage["usedPlaces"][0]["identityMatch"] == proof
