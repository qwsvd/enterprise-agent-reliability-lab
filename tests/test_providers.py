import json
from copy import deepcopy
from typing import Any

from fastapi.testclient import TestClient

from app.agent.providers import OpenAICompatibleProvider
from app.agent.runtime import AgentRuntime
from app.agent.tools import ToolRegistry


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self.payload


def tool_call_response() -> dict[str, Any]:
    return {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "get_order",
                        "arguments": '{"order_code":"ORD-1024"}',
                    },
                }],
            }
        }]
    }


def test_openai_compatible_provider_formats_and_parses_tool_call(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured.update({"url": url, **kwargs})
        return FakeResponse(tool_call_response())

    monkeypatch.setattr("app.agent.providers.httpx.post", fake_post)
    provider = OpenAICompatibleProvider(
        api_key="test-only-key", model="test-model", base_url="https://llm.example/v1"
    )
    response = provider.complete(
        [{"role": "user", "content": "Look up ORD-1024"}],
        [{"type": "function", "function": {"name": "get_order"}}],
    )

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer test-only-key"}
    assert captured["json"]["model"] == "test-model"
    assert response.tool_calls[0].name == "get_order"
    assert response.tool_calls[0].arguments == {"order_code": "ORD-1024"}


def test_runtime_sends_valid_tool_result_in_second_openai_request(
    monkeypatch, client: TestClient
) -> None:
    responses = [
        FakeResponse(tool_call_response()),
        FakeResponse({
            "choices": [{"message": {"content": "Order ORD-1024 was inspected."}}]
        }),
    ]
    requests: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        requests.append(deepcopy({"url": url, **kwargs}))
        return responses.pop(0)

    monkeypatch.setattr("app.agent.providers.httpx.post", fake_post)
    provider = OpenAICompatibleProvider(
        api_key="test-only-key", model="test-model", base_url="https://llm.example/v1"
    )
    with client.app.state.database.session_factory() as session:
        result = AgentRuntime(provider, ToolRegistry(session)).run("Inspect ORD-1024")

    assert result.status == "completed"
    assert result.steps == 2
    assert result.response == "Order ORD-1024 was inspected."
    assert len(requests) == 2
    tool_message = requests[1]["json"]["messages"][-1]
    assert set(tool_message) == {"role", "tool_call_id", "content"}
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-1"
    assert json.loads(tool_message["content"])["data"]["code"] == "ORD-1024"
    assert "name" not in tool_message
