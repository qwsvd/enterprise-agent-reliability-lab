from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.reliability.models import ReliabilityFailure, TerminationReason


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
    attempts: int = 1
    retries: int = 0


class AgentResult(BaseModel):
    status: Literal[
        "completed", "max_steps_exceeded", "provider_error", "reliability_failure"
    ]
    response: str
    steps: int
    run_id: str | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)
    termination_reason: TerminationReason | None = None
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    failure_details: list[ReliabilityFailure] = Field(default_factory=list)

