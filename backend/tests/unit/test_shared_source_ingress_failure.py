"""Source read success must not mislabel a later ingress failure as planner failure."""

from backend.tests.unit.test_shared_travel_source import _counts, _send, intake
from src.services.shared_travel_source_service import SharedTravelSourceService


def test_source_ingress_failure_preserves_stage_and_pending_source(intake, monkeypatch):
    read = _send(intake)
    before = _counts(intake.db)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("semantic provider deadline")

    monkeypatch.setattr(intake.semantic, "decide_autonomy_lite", unavailable)
    result = _send(
        intake,
        content="按刚才攻略安排北京2026年10月1日一日游，上午博物馆、下午地标，1人中等预算公交优先，09:00到20:00，不住宿",
        request_id="source-ingress-unavailable",
    )
    assert len(calls) == 1
    assert "尚未调用规划控制器" in result.assistant_turn.content
    assert "通用意图解释" in result.assistant_turn.content
    assert "规划控制器本轮未形成" not in result.assistant_turn.content
    assert _counts(intake.db) == before
    source = SharedTravelSourceService(intake.db).initial_source(session_id=intake.session)
    assert source["sourceAssistantTurnId"] == read.assistant_turn.id
    assert source["carrierAssistantTurnId"] == result.assistant_turn.id
