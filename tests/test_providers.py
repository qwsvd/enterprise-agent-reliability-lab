from typing import Any

from app.agent.providers import OpenAICompatibleProvider


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
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
        return FakeResponse()

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
