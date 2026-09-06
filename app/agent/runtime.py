from __future__ import annotations

import json
from typing import Any

from app.agent.providers import LLMProvider, ProviderError
from app.agent.tools import ToolRegistry
from app.agent.types import AgentResult, ToolEvent


SYSTEM_PROMPT = (
    "You are an after-sales assistant. Inspect business state with the available tools, "
    "use write tools only when appropriate, and accurately distinguish approved refunds "
    "from requests pending human approval."
)


class AgentRuntime:
    def __init__(
        self, provider: LLMProvider, tools: ToolRegistry, *, max_steps: int = 8
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps

    def run(self, task: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        events: list[ToolEvent] = []

        for step in range(1, self.max_steps + 1):
            try:
                turn = self.provider.complete(messages, self.tools.schemas())
            except ProviderError as exc:
                return AgentResult(
                    status="provider_error", response=str(exc), steps=step, tool_events=events
                )

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": turn.content,
            }
            if turn.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, default=str),
                        },
                    }
                    for call in turn.tool_calls
                ]
            messages.append(assistant_message)

            if not turn.tool_calls:
                return AgentResult(
                    status="completed",
                    response=turn.content or "",
                    steps=step,
                    tool_events=events,
                )

            for call in turn.tool_calls:
                result = self.tools.execute(call.name, call.arguments)
                events.append(ToolEvent(call=call, result=result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, default=str),
                    }
                )

        return AgentResult(
            status="max_steps_exceeded",
            response=f"Agent stopped after reaching the maximum of {self.max_steps} steps.",
            steps=self.max_steps,
            tool_events=events,
        )
