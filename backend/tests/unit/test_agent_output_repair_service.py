from src.services.agent_output_parser_service import AgentOutputParser
from src.services.agent_output_repair_service import AgentOutputRepairService


class RepairingProvider:
    def __init__(self, repaired: str):
        self.repaired = repaired
        self.prompts: list[str] = []

    def repair_structured_output(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.repaired


class EmptyRepairProvider:
    def __init__(self):
        self.calls = 0

    def repair_structured_output(self, _prompt: str) -> str:
        self.calls += 1
        return ""


def test_repair_service_repairs_missing_required_field_then_parser_continues():
    raw = '{"mode":"clarification"}'
    parser = AgentOutputParser()
    try:
        parser.parse(raw)
    except Exception as error:
        repaired, metadata = AgentOutputRepairService(
            RepairingProvider('{"reply":"请补充日期","mode":"clarification"}')
        ).repair(raw, error, "AgentStructuredOutput", {"latestUserMessage": "北京"})

    assert metadata["status"] == "completed"
    assert metadata["attemptCount"] == 1
    assert parser.parse(repaired).reply == "请补充日期"


def test_repair_service_has_hard_attempt_limit():
    provider = EmptyRepairProvider()

    repaired, metadata = AgentOutputRepairService(provider, max_attempts=2).repair(
        "not-json",
        ValueError("bad json"),
        "AgentStructuredOutput",
        {},
    )

    assert repaired is None
    assert provider.calls == 2
    assert metadata["status"] == "failed"
