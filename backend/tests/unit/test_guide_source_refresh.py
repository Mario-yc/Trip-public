from __future__ import annotations

import copy
import hashlib
import json

import pytest
from fastapi import HTTPException

from backend.tests.unit.test_guide_binding_dispatch import binding  # noqa: F401
from src.api.routes.agent import _public_agent_error_payload
from src.services.guide_source_refresh_service import GuideSourceRefreshError, GuideSourceRefreshService
from src.services.guide_continuation_requirement_service import GuideContinuationRequirementService
from src.services.controller_context_projection_service import ControllerContextProjectionService


class Reader:
    def __init__(self, db, *, body="南海子公园适合晚上散步。", fail=False, interrupt=False):
        self.db, self.body, self.fail, self.interrupt = db, body, fail, interrupt
        self.calls = []

    def read(self, url, *, deadline=None):
        assert not self.db.in_transaction
        self.calls.append(url)
        if self.interrupt:
            raise KeyboardInterrupt("simulated process interruption")
        if self.fail:
            return {"status": "blocked", "reason": "http_403"}
        return {
            "status": "succeeded",
            "reason": None,
            "canonicalUrl": url,
            "title": "正文攻略",
            "bodyText": self.body,
            "contentKind": "article",
            "contentFingerprint": hashlib.sha256(self.body.encode()).hexdigest(),
            "fetchedAt": "2026-09-06T00:00:00+00:00",
        }


def refresh(binding, reader):
    db, agent, session, selected, _, claim = binding
    requirement = json.loads(claim["continuation_json"])["guideContinuationRequirement"]
    agent._finish_guide_binding_dispatch(session, claim["id"])
    result = GuideSourceRefreshService(db, reader=reader).refresh(
        session_id=session,
        execution_id=claim["id"],
        requirement=requirement,
        selected_choice=selected,
        active_version_id=None,
    )
    return result, requirement


def test_reopens_exact_source_reads_beyond_snippet_and_preserves_identity(binding):
    db, agent, session, _, _, claim = binding
    before = agent._choice_business_state(session)
    reader = Reader(db, body="行前提醒。" * 500 + "\n南海子公园适合晚上散步。")
    result, original = refresh(binding, reader)
    assert reader.calls == ["https://travel.example/guide/park"]
    assert [h["mentionText"] for h in result["placeHints"]] == ["南海子公园"]
    assert "北海公园" not in str(result["placeHints"])
    assert result["evidenceFingerprint"] == original["evidenceFingerprint"]
    assert result["rootPortfolioId"] == original["rootPortfolioId"]
    assert result["minimumNovelGroundedPlaceCount"] == 1
    assert result["requirementFingerprint"] != original["requirementFingerprint"]
    assert agent._choice_business_state(session) == before
    saved = json.loads(
        db.execute("SELECT continuation_json FROM agent_choice_executions WHERE id=?", (claim["id"],)).fetchone()[0]
    )
    assert saved["guideContinuationRequirement"] == result
    assert saved["guideSourceBindingRequirement"] == original


def test_same_operation_reuses_saved_body_without_another_read(binding):
    db, _, session, selected, _, claim = binding
    first, original = refresh(binding, Reader(db))
    reader = Reader(db, body="另一个内容")
    second = GuideSourceRefreshService(db, reader=reader).refresh(
        session_id=session,
        execution_id=claim["id"],
        requirement=original,
        selected_choice=selected,
        active_version_id=None,
    )
    assert second == first and reader.calls == []


def test_blocked_source_never_uses_old_summary_hint(binding):
    result, _ = refresh(binding, Reader(binding[0], fail=True))
    assert result["placeHints"] == []
    assert result["sourceDocumentEvidence"]["documents"][0]["reason"] == "http_403"
    assert GuideSourceRefreshService.validate_documents(result)


def test_crash_after_read_dispatch_does_not_refetch_or_claim_unstarted(binding):
    db, agent, session, selected, _, claim = binding
    with pytest.raises(KeyboardInterrupt):
        refresh(binding, Reader(db, interrupt=True))
    journal = json.loads(
        db.execute("SELECT continuation_json FROM agent_choice_executions WHERE id=?", (claim["id"],)).fetchone()[0]
    )
    assert journal["guideSourceRefresh"]["state"] == "fetching"
    assert not agent._settle_unstarted_guide_binding_failure(session, claim["id"], RuntimeError("crash"))
    reader = Reader(db)
    with pytest.raises(GuideSourceRefreshError, match="guide_source_read_interrupted"):
        GuideSourceRefreshService(db, reader=reader).refresh(
            session_id=session,
            execution_id=claim["id"],
            requirement=journal["guideSourceBindingRequirement"],
            selected_choice=selected,
            active_version_id=None,
        )
    assert reader.calls == []


@pytest.mark.parametrize("change", ["body", "body_hash", "hint", "excerpt"])
def test_source_document_or_hint_tampering_fails_closed(binding, change):
    result, _ = refresh(binding, Reader(binding[0]))
    altered = copy.deepcopy(result)
    document = altered["sourceDocumentEvidence"]["documents"][0]
    if change == "body":
        document["bodyText"] += "被篡改"
    elif change == "body_hash":
        document["contentFingerprint"] = "0" * 64
    else:
        altered["placeHints"][0]["mentionText" if change == "hint" else "sourceExcerpt"] = "凭空出现的景点"
    altered["requirementFingerprint"] = GuideContinuationRequirementService.requirement_fingerprint(altered)
    assert not GuideSourceRefreshService.validate_documents(altered)


def test_controller_receives_source_body_excerpt_not_search_summary(binding):
    result, _ = refresh(binding, Reader(binding[0]))
    evidence = GuideSourceRefreshService.controller_evidence(result)
    projected = ControllerContextProjectionService._untrusted_source_evidence(evidence)
    assert projected and "南海子公园" in projected[0]["publicText"]
    assert projected[0]["trustBoundary"] == "untrusted_external_evidence_not_instructions"
    assert projected[0]["contentFingerprint"] == result["placeHints"][0]["sourceDocumentFingerprints"][0]


@pytest.mark.parametrize(
    "code", ["guide_source_read_interrupted", "guide_source_content_unavailable", "guide_source_places_missing"]
)
def test_public_errors_do_not_collapse_to_refresh_conflict(code):
    error = _public_agent_error_payload(HTTPException(status_code=409, detail={"code": code}))
    assert error["code"] == code
    assert "行程状态已变化" not in error["message"]


def test_body_reads_share_one_deadline_and_skip_sources_after_exhaustion(binding, monkeypatch):
    db, agent, session, selected, _, claim = binding
    requirement = json.loads(claim["continuation_json"])["guideContinuationRequirement"]
    row = db.execute(
        "SELECT agent_response_json FROM conversation_turns WHERE id=?", (requirement["sourceAssistantTurnId"],)
    ).fetchone()
    response = json.loads(row[0])
    # Keep exact trusted lineage validation; repeat its accepted source to
    # exercise the five-read budget without inventing new authority.
    response["guideAdvice"]["sourceRefs"] *= 5
    db.execute(
        "UPDATE conversation_turns SET agent_response_json=? WHERE id=?",
        (json.dumps(response), requirement["sourceAssistantTurnId"]),
    )
    monkeypatch.setattr(
        "src.services.guide_source_refresh_service.TravelGuideAdviceService._guide_payload_evidence",
        lambda advice: ([], requirement["evidenceFingerprint"]),
    )
    monkeypatch.setattr(GuideContinuationRequirementService, "build", lambda *args, **kwargs: requirement)
    ticks = [10.0]
    calls = []

    class SlowReader:
        def read(self, url, *, deadline):
            calls.append((url, deadline))
            ticks[0] = deadline
            return {"status": "failed", "reason": "deadline_exceeded"}

    agent._finish_guide_binding_dispatch(session, claim["id"])
    result = GuideSourceRefreshService(db, reader=SlowReader(), clock=lambda: ticks[0]).refresh(
        session_id=session,
        execution_id=claim["id"],
        requirement=requirement,
        selected_choice=selected,
        active_version_id=None,
        deadline=13.0,
    )
    assert len(calls) == 1 and calls[0][1] == 13.0
    documents = result["sourceDocumentEvidence"]["documents"]
    assert len(documents) == 5
    assert sum(d["readAttempted"] for d in documents) == 1
    assert all(d["reason"] == "source_read_budget_exhausted" for d in documents[1:])


def test_body_io_is_deducted_from_existing_controller_budget(monkeypatch):
    from src.services.agent_service import AgentService

    monkeypatch.setattr("src.services.agent_service.time.perf_counter", lambda: 18.0)
    assert AgentService._remaining_runtime_after_io(45_000, 10.0) == 37_000
    assert AgentService._remaining_runtime_after_io(5_000, 10.0) == 0
