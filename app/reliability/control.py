from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.reliability.models import (
    ReliabilityConfig,
    ReliabilityFailure,
    RetryPolicy,
    TerminationReason,
)


def canonical_tool_call(name: str, arguments: Any) -> str:
    """Build a stable comparison key without changing the supplied arguments."""

    def fallback(value: Any) -> str:
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value)

    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=fallback,
    )
    return f"{name}:{encoded}"


@dataclass
class ReliabilityController:
    config: ReliabilityConfig
    sleeper: Callable[[float], None]
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    failures: list[ReliabilityFailure] = field(default_factory=list)
    consecutive_failures: int = 0
    repeated_calls: Counter[str] = field(default_factory=Counter)

    def model_budget_reason(self) -> TerminationReason | None:
        if self.model_calls >= self.config.max_model_calls:
            return TerminationReason.MODEL_CALL_BUDGET_EXHAUSTED
        return None

    def tool_budget_reason(self) -> TerminationReason | None:
        if self.tool_calls >= self.config.max_tool_calls:
            return TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED
        return None

    def repeated_call_reason(self, name: str, arguments: Any) -> TerminationReason | None:
        key = canonical_tool_call(name, arguments)
        if self.repeated_calls[key] >= self.config.repeated_tool_call_limit:
            return TerminationReason.REPEATED_TOOL_CALL
        return None

    def record_validated_call(self, name: str, arguments: Any) -> None:
        self.repeated_calls[canonical_tool_call(name, arguments)] += 1

    def record_failure(
        self,
        *,
        operation: str,
        kind: str,
        message: str,
        retryable: bool,
        attempt: int,
        tool_name: str | None = None,
    ) -> None:
        self.failures.append(
            ReliabilityFailure(
                operation=operation,
                type=kind,
                message=message,
                retryable=retryable,
                attempt=attempt,
                tool_name=tool_name,
            )
        )
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def failure_limit_reason(self) -> TerminationReason | None:
        if self.consecutive_failures >= self.config.consecutive_failure_limit:
            return TerminationReason.CONSECUTIVE_FAILURE_LIMIT
        return None

    def retry(self, policy: RetryPolicy, next_attempt: int) -> None:
        self.retries += 1
        delay = policy.delay_before_attempt(next_attempt)
        if delay:
            self.sleeper(delay)

    def tool_retry_is_safe(self, name: str, arguments: Any) -> bool:
        if name not in self.config.side_effecting_tools:
            return True
        if name not in self.config.idempotent_side_effecting_tools:
            return False
        return (
            isinstance(arguments, dict)
            and isinstance(arguments.get("idempotency_key"), str)
            and bool(arguments["idempotency_key"].strip())
        )

    @staticmethod
    def tool_result_is_validated(result: dict[str, Any]) -> bool:
        # Every current local and MCP tool validates before returning a successful result.
        return result.get("ok") is True
