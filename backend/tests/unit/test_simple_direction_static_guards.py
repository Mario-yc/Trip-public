from __future__ import annotations

import ast
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_simple_direction_business_code_does_not_embed_specific_campuses_or_park_blacklists() -> None:
    backend_paths = [
        REPO_ROOT / "backend/src/services/agent_service.py",
        REPO_ROOT / "backend/src/services/simple_open_direction_service.py",
        REPO_ROOT / "backend/src/services/simple_open_itinerary_executor.py",
        REPO_ROOT / "backend/src/services/simple_direction_frontier_service.py",
        REPO_ROOT / "backend/src/services/experience_independence_service.py",
        REPO_ROOT / "backend/src/services/visit_duration_policy.py",
    ]
    frontend_paths = [path for path in (REPO_ROOT / "frontend/src").rglob("*") if path.suffix in {".ts", ".tsx"}]
    forbidden = {
        "苔园",
        "莲桥",
        "北京大学",
        "清华大学",
        "中国人民大学",
        "北京航空航天大学",
        "北京理工大学",
        "北京师范大学",
        "中国农业大学",
        "中央民族大学",
    }

    violations: list[str] = []
    for path in [*backend_paths, *frontend_paths]:
        source = path.read_text(encoding="utf-8")
        for value in sorted(forbidden):
            if value in source:
                violations.append(f"{path.relative_to(REPO_ROOT)}:{value}")

    assert violations == []


def test_runtime_campus_frontier_uses_versioned_evidence_without_city_catalogues() -> None:
    path = REPO_ROOT / "backend/src/runtime/agent_runtime.py"
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    guarded_functions = {
        "_mock_city_campus_candidates",
        "_campus_hints",
        "_qualified_campus_hints",
    }
    guarded_source = "\n".join(
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in guarded_functions
    )
    forbidden_catalogue_entries = {
        "北京大学",
        "清华大学",
        "中国人民大学",
        "北京航空航天大学",
        "北京理工大学",
        "北京师范大学",
        "中国农业大学",
        "中央民族大学",
        "复旦大学",
        "上海交通大学",
        "浙江大学",
        "中山大学",
        "四川大学",
    }

    assert "EntityQualificationEvidenceService.canonical_hints" in guarded_source
    assert "city_names" not in guarded_source
    assert all(name not in guarded_source for name in forbidden_catalogue_entries)


def test_runtime_campus_hints_are_evidence_bound_or_generic(monkeypatch) -> None:
    from src.runtime.agent_runtime import RuntimeStagedMapMockProvider
    from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService

    calls: list[tuple[str, str, str]] = []

    def canonical_hints(*, locality: str, scheme: str, value: str) -> list[str]:
        calls.append((locality, scheme, value))
        return ["资格实体甲", "资格实体乙"]

    monkeypatch.setattr(EntityQualificationEvidenceService, "canonical_hints", canonical_hints)
    provider = RuntimeStagedMapMockProvider()
    constraint = {
        "qualificationScheme": "fixture_scheme",
        "qualificationValue": "fixture_value",
    }

    assert provider._qualified_campus_hints(
        "测试城",
        qualification_constraint=constraint,
    ) == ["资格实体甲", "资格实体乙"]
    assert calls == [("测试城", "fixture_scheme", "fixture_value")]
    assert provider._campus_hints("测试城") == [
        "测试城高等院校",
        "测试城大学校园",
        "测试城学院校园",
        "测试城高校园区",
    ]


def test_runtime_initial_plan_binds_campus_pool_to_qualification_evidence(monkeypatch) -> None:
    from src.runtime.agent_runtime import RuntimeStagedMapMockProvider
    from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService

    monkeypatch.setattr(
        EntityQualificationEvidenceService,
        "canonical_hints",
        lambda **_kwargs: ["资格实体甲", "资格实体乙"],
    )
    payload = json.loads(
        RuntimeStagedMapMockProvider().generate_initial_plan(
            {
                "selectedCity": "测试城",
                "resolvedTripDates": {"dates": ["2026-10-01", "2026-10-02"]},
                "effectiveUserMessage": "安排高校两日游",
                "requestIntentContract": {
                    "entityQualificationConstraint": {
                        "qualificationScheme": "fixture_scheme",
                        "qualificationValue": "fixture_value",
                    }
                },
            }
        )
    )
    campus_pool = next(item for item in payload["intentPools"] if item["intentType"] == "campus_visit")

    assert campus_pool["candidateHints"] == ["资格实体甲", "资格实体乙"]
    assert campus_pool["routePreference"]["candidateHintSource"] == "versioned_qualification_evidence"


def test_standalone_park_category_policy_is_versioned_data_not_business_code() -> None:
    service_path = REPO_ROOT / "backend/src/services/experience_independence_service.py"
    asset_path = REPO_ROOT / "backend/src/services/amap_category_evidence_v1.json"
    service_source = service_path.read_text(encoding="utf-8")
    payload = json.loads(asset_path.read_text(encoding="utf-8"))

    assert payload["schemaVersion"] == "provider-category-evidence-v1"
    assert payload["provider"] == "amap-place-search"
    assert payload["source"]["contentVersion"]
    assert payload["source"]["url"].startswith("https://lbs.amap.com/")
    assert set(payload["experienceRoles"]) == {"standalone_park"}

    category_codes = {
        str(item["typecode"])
        for policy in payload["experienceRoles"].values()
        for bucket in ("acceptedCategories", "rejectedCategories")
        for item in policy[bucket]
    }
    assert category_codes
    assert all(code not in service_source for code in category_codes)

    forbidden_identity_keys = {
        "amapId",
        "candidateAmapId",
        "city",
        "locality",
        "longitude",
        "latitude",
        "coordinates",
        "poiName",
    }
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            assert forbidden_identity_keys.isdisjoint(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def test_live_journey_uses_the_latest_server_signed_detour_control() -> None:
    journey_source = (REPO_ROOT / "e2e/simple-direction-user-journey.spec.ts").read_text(encoding="utf-8")
    verifier_source = (REPO_ROOT / "scripts/verify_live_simple_direction_e2e.py").read_text(encoding="utf-8")

    assert "expect(question.allowFreeText).toBe(false);" in journey_source
    assert "expect(question.allowFreeText).toBe(true);" not in journey_source
    assert "MANUAL_DETOUR_PREFERENCE" not in journey_source
    assert '"maxGeneralizedCostDelta":15' not in journey_source
    assert '"maxDetourRatio":0\\.15' not in journey_source
    clarification_body = journey_source.split("async function completeClarificationBatches", 1)[1].split(
        "function latestAwaitingBatchCheckpoint", 1
    )[0]
    assert "selectStrictDetourOption(question)" in clarification_body
    assert 'submissionMode: "persisted_option"' in journey_source
    assert "journey_manual_dimensions == []" in verifier_source
    assert "clarification_detour_free_text_unexpected" in verifier_source


def test_live_journey_persists_commit_and_post_continuation_server_evidence() -> None:
    journey_source = (REPO_ROOT / "e2e/simple-direction-user-journey.spec.ts").read_text(encoding="utf-8")

    assert "TRIP_E2E_GIT_COMMIT" in journey_source
    assert "gitCommit," in journey_source
    assert "postSourceAssistantTurnId" in journey_source
    assert "postFrontierStatus" in journey_source
    assert "postCapabilityAvailable" in journey_source
    assert "postChoiceId" in journey_source
    assert "activeVersionAfter" in journey_source
    assert "directionBContinuationEvidence" in journey_source
    assert "VAGUE_DIRECTION_REQUEST" not in journey_source
