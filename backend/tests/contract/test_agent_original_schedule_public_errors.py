"""Public schedule-error contracts; deterministic support, not live acceptance."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app
from src.runtime.agent_runtime import TripAgentRuntime


PRIVATE_SENTINEL = "PRIVATE_SOURCE_CONTEXT_SENTINEL"
PUBLIC_ERRORS = [
    (
        "simple_direction_original_schedule_contract_outdated",
        "旧方案未完整保留原旅行需求中的到访时段或每日安排，暂不能继续沿用；请按原需求重新生成方案。本轮尚未调用规划器，未改写方案。",
    ),
    (
        "plan_proposal_original_schedule_mismatch",
        "所选旧方案的到访时段或每日安排不符合原旅行需求，不能直接采用；请按原需求重新生成方案。未写入正式行程。",
    ),
    (
        "plan_proposal_source_context_missing",
        "方案所依据的原旅行需求已失效或缺失，无法核对是否满足你的要求；请重新提供原需求并生成方案。",
    ),
]


def _raise_private_error(monkeypatch, code: str, status_code: int = 409) -> list[str]:
    calls = []

    def fail(_self, session_id, _payload, **_kwargs):
        calls.append(session_id)
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": code,
                "message": PRIVATE_SENTINEL,
                "details": {
                    "sourceUserTurnId": PRIVATE_SENTINEL,
                    "scheduleFailures": [{"rawNeed": PRIVATE_SENTINEL}],
                },
            },
        )

    monkeypatch.setattr(TripAgentRuntime, "send_agent_message", fail)
    return calls


@pytest.mark.parametrize("code,public_message", PUBLIC_ERRORS)
@pytest.mark.parametrize("stream", [False, True], ids=["ordinary", "stream"])
def test_schedule_errors_keep_public_code_and_safe_actionable_message(
    monkeypatch,
    code: str,
    public_message: str,
    stream: bool,
) -> None:
    calls = _raise_private_error(monkeypatch, code)
    with TestClient(app) as client:
        session_id = client.post("/api/agent/sessions", json={"city": "北京"}).json()["sessionId"]
        endpoint = f"/api/agent/sessions/{session_id}/messages" + ("/stream" if stream else "")
        response = client.post(endpoint, json={"content": "继续处理当前方案", "context": {}})

    assert calls == [session_id]
    assert PRIVATE_SENTINEL not in response.text
    assert "sourceUserTurnId" not in response.text
    assert "scheduleFailures" not in response.text
    expected = {"code": code, "message": public_message}
    if stream:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines() if line]
        assert not any(event["event"] == "message_response" for event in events)
        assert events[-2]["event"] == "reasoning_status"
        assert events[-2]["data"]["status"] == "failed"
        assert events[-1] == {"event": "error", "data": {**expected, "statusCode": 409}}
    else:
        assert response.status_code == 409
        assert response.json()["code"] == code
        assert response.json()["message"] == public_message
        assert response.json()["detail"] == expected
        assert response.json()["details"] == expected


@pytest.mark.parametrize("code,_public_message", PUBLIC_ERRORS)
def test_known_schedule_code_does_not_override_internal_error_redaction(
    monkeypatch,
    code: str,
    _public_message: str,
) -> None:
    _raise_private_error(monkeypatch, code, status_code=500)
    with TestClient(app) as client:
        session_id = client.post("/api/agent/sessions", json={"city": "北京"}).json()["sessionId"]
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": "继续处理", "context": {}},
        )

    assert response.status_code == 500
    assert response.json()["code"] == "agent_internal_error"
    assert response.json()["message"] == "行程规划暂时未完成，请稍后重试。"
    assert PRIVATE_SENTINEL not in response.text
    assert code not in response.text


def test_unknown_conflict_code_stays_private(monkeypatch) -> None:
    _raise_private_error(monkeypatch, PRIVATE_SENTINEL)
    with TestClient(app) as client:
        session_id = client.post("/api/agent/sessions", json={"city": "北京"}).json()["sessionId"]
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": "继续处理", "context": {}},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "agent_state_conflict"
    assert response.json()["message"] == "当前行程状态已变化，请刷新后重试。"
    assert PRIVATE_SENTINEL.casefold() not in response.text.casefold()
