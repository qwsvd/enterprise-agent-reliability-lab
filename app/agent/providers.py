from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Protocol

import httpx

from app.agent.types import ModelResponse, ToolCall


class LLMProvider(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> ModelResponse: ...


class ProviderError(RuntimeError):
    """A model provider request or response failed."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        kind: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


class OpenAICompatibleProvider:
    """Minimal client for OpenAI-compatible Chat Completions tool calling."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("An API key is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> OpenAICompatibleProvider:
        api_key = os.getenv("LLM_API_KEY", "")
        model = os.getenv("LLM_MODEL", "")
        if not api_key or not model:
            raise ValueError("LLM_API_KEY and LLM_MODEL must be configured")
        return cls(
            api_key=api_key,
            model=model,
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> ModelResponse:
        request_timeout = timeout_seconds or self.timeout_seconds
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "tools": tools},
                timeout=request_timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"OpenAI-compatible request timed out: {exc}",
                retryable=True,
                kind="provider_timeout",
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = status in {408, 429} or status >= 500
            raise ProviderError(
                f"OpenAI-compatible request failed with HTTP {status}",
                retryable=retryable,
                kind="provider_http_error",
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"OpenAI-compatible request failed: {exc}",
                retryable=True,
                kind="provider_connection_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI-compatible request failed: {exc}") from exc

        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"OpenAI-compatible response was malformed: {exc}") from exc

        try:
            calls: list[ToolCall] = []
            for raw_call in message.get("tool_calls", []):
                function = raw_call.get("function", {})
                raw_arguments = function.get("arguments", {})
                try:
                    arguments: Any = (
                        json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    )
                except json.JSONDecodeError:
                    arguments = raw_arguments
                calls.append(
                    ToolCall(id=raw_call["id"], name=function["name"], arguments=arguments)
                )
            return ModelResponse(content=message.get("content"), tool_calls=calls)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"OpenAI-compatible response was malformed: {exc}") from exc


class ScriptedProvider:
    """Deterministic provider for tests and local demonstrations."""

    def __init__(self, responses: list[ModelResponse | ProviderError]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> ModelResponse:
        self.requests.append({
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "timeout_seconds": timeout_seconds,
        })
        if not self._responses:
            raise ProviderError("Scripted provider has no response remaining")
        response = self._responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response
