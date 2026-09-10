from datetime import datetime, timezone

from src.providers.travel_tools import WebSearchItem, WebSearchResponse
from src.services.web_search_evidence_service import WebSearchEvidenceService


def _response(*, snippet: str = "官方公告内容") -> WebSearchResponse:
    queried_at = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
    return WebSearchResponse(
        query="故宫 预约 官方公告",
        results=[
            WebSearchItem(
                title="故宫博物院参观须知",
                url="HTTPS://WWW.DPM.ORG.CN/visit#tickets",
                snippet=snippet,
                source_name="故宫博物院",
                queried_at=queried_at,
                confidence=0.94,
                credibility_rank="official",
                provider_name="anysearch",
                published_at="2026-08-20",
            )
        ],
        queried_at=queried_at,
        confidence=0.94,
        provider_name="anysearch",
    )


def test_project_builds_stable_opaque_source_refs_and_frozen_result_fingerprint():
    first = WebSearchEvidenceService.project(_response(), freshness="oneMonth")
    second = WebSearchEvidenceService.project(_response(), freshness="oneMonth")

    assert first == second
    assert first["schemaVersion"] == "trip-web-search-evidence-v1"
    assert first["status"] == "search_results_ready"
    assert first["queryFingerprint"]
    assert first["resultFingerprint"]
    assert first["sourceRefIds"] == [first["results"][0]["refId"]]
    assert first["results"][0]["refId"].startswith("webref_")
    assert first["results"][0]["sourceFingerprint"]
    assert first["results"][0]["url"] == "https://www.dpm.org.cn/visit"
    assert "故宫" not in first["results"][0]["refId"]


def test_project_fingerprint_changes_when_source_material_changes():
    original = WebSearchEvidenceService.project(_response(), freshness="oneMonth")
    changed = WebSearchEvidenceService.project(_response(snippet="预约规则已更新"), freshness="oneMonth")

    assert original["results"][0]["refId"] != changed["results"][0]["refId"]
    assert original["results"][0]["sourceFingerprint"] != changed["results"][0]["sourceFingerprint"]
    assert original["resultFingerprint"] != changed["resultFingerprint"]


def test_project_keeps_empty_provider_result_truthful():
    response = WebSearchResponse(
        query="待核验规则",
        results=[],
        queried_at=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        confidence=0.0,
        provider_name="anysearch",
        failure_reason="all_web_search_providers_failed_or_empty",
    )

    projected = WebSearchEvidenceService.project(response, freshness="oneWeek")

    assert projected["status"] == "search_provider_failed"
    assert projected["results"] == []
    assert projected["sourceRefIds"] == []
    assert projected["failureReason"] == "all_web_search_providers_failed_or_empty"
