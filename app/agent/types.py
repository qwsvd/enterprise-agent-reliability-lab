from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Any


class ModelResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolEvent(BaseModel):
    call: ToolCall
    result: dict[str, Any]


class AgentResult(BaseModel):
    status: Literal["completed", "max_steps_exceeded", "provider_error"]
    response: str
    steps: int
    tool_events: list[ToolEvent] = Field(default_factory=list)

