import json

from src.services.deepseek_agent_provider import DeepSeekAgentProvider


class _ScriptedProvider(DeepSeekAgentProvider):
    def __init__(self, responses):
        super().__init__(api_key="test-key", model="deepseek-test", timeout_seconds=1)
        self.responses = list(responses)
        self.payloads = []

    def _post_json(self, payload):
        self.payloads.append(payload)
        return self.responses.pop(0)


class _Registry:
    def __init__(self):
        self.events = []

    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "resolve_poi",
                    "description": "test",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            }
        ]

    def execute(self, _tool_call_id, _tool_name, _arguments):
        return {"ok": True, "resolvedCount": 1}


def test_thinking_tool_call_replays_reasoning_content_without_exposing_it_as_an_event():
    provider = _ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "private provider reasoning",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "resolve_poi",
                                        "arguments": json.dumps({"name": "北京大学"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "已核验地点。"}}]},
        ]
    )
    provider.thinking_mode = "enabled"

    result = provider.run_tool_loop(
        {"latestUserMessage": "核验北京大学", "runtimeLimits": {"maxToolRounds": 2}},
        _Registry(),
    )

    assert result.reply == "已核验地点。"
    assert provider.payloads[0]["thinking"] == {"type": "enabled"}
    assert "temperature" not in provider.payloads[0]
    replayed_assistant = next(message for message in provider.payloads[1]["messages"] if message.get("role") == "assistant")
    assert replayed_assistant["reasoning_content"] == "private provider reasoning"
    assert all("reasoning_content" not in event for event in result.tool_events)
