from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CURRENT_SIDE_EFFECTING_TOOLS = frozenset({"create_refund", "create_support_ticket"})
CURRENT_IDEMPOTENT_SIDE_EFFECTS = frozenset({"create_refund"})


class TerminationReason(StrEnum):
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RETRY_EXHAUSTED = "provider_retry_exhausted"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_FAILURE = "tool_failure"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    MODEL_CALL_BUDGET_EXHAUSTED = "model_call_budget_exhausted"
    TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
    STEP_BUDGET_EXHAUSTED = "step_budget_exhausted"
    CONSECUTIVE_FAILURE_LIMIT = "consecutive_failure_limit"
    UNSAFE_RETRY_BLOCKED = "unsafe_retry_blocked"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.25, ge=0, le=60)
    backoff_multiplier: float = Field(default=2.0, ge=1, le=10)
    max_backoff_seconds: float = Field(default=2.0, ge=0, le=300)

    def delay_before_attempt(self, attempt: int) -> float:
        """Return the delay before a one-based retry attempt."""
        if attempt <= 1:
            return 0.0
        delay = self.initial_backoff_seconds * self.backoff_multiplier ** (attempt - 2)
        return min(delay, self.max_backoff_seconds)


class TimeoutPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_seconds: float = Field(default=30.0, gt=0, le=300)
    tool_seconds: float = Field(default=30.0, gt=0, le=300)


class ReliabilityConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_steps: int = Field(default=8, ge=1, le=100)
    max_model_calls: int = Field(default=12, ge=1, le=200)
    max_tool_calls: int = Field(default=32, ge=1, le=1000)
    repeated_tool_call_limit: int = Field(default=3, ge=1, le=100)
    consecutive_failure_limit: int = Field(default=3, ge=1, le=100)
    provider_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    tool_retry: RetryPolicy = Field(
        default_factory=lambda: RetryPolicy(max_attempts=2)
    )
    timeouts: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    side_effecting_tools: frozenset[str] = CURRENT_SIDE_EFFECTING_TOOLS
    idempotent_side_effecting_tools: frozenset[str] = CURRENT_IDEMPOTENT_SIDE_EFFECTS

    @model_validator(mode="after")
    def validate_tool_sets(self) -> ReliabilityConfig:
        if not CURRENT_SIDE_EFFECTING_TOOLS <= self.side_effecting_tools:
            raise ValueError("Current mutating tools must remain side-effecting")
        if not self.idempotent_side_effecting_tools <= self.side_effecting_tools:
            raise ValueError("Idempotent side-effecting tools must also be side-effecting")
        if "create_support_ticket" in self.idempotent_side_effecting_tools:
            raise ValueError("create_support_ticket has no idempotency guarantee")
        return self


class ReliabilityFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation: Literal["provider", "tool"]
    type: str
    message: str
    retryable: bool
    attempt: int = Field(ge=1)
    tool_name: str | None = None
