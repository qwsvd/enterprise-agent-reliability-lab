from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agent.providers import ProviderError, ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.database import Database
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.reliability import (
    ReliabilityConfig,
    RetryPolicy,
    TerminationReason,
    TimeoutPolicy,
)
from app.skills.demo import scripted_responses
from app.skills.loader import SkillRegistry
from app.skills.tools import SkillAwareTools


SKILL_ROOT = Path(__file__).parents[1] / ".agents" / "skills"


def response(call_id: str, name: str, arguments: Any) -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]
    )


def config(**overrides: Any) -> ReliabilityConfig:
    values: dict[str, Any] = {
        "provider_retry": RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
        "tool_retry": RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
    }
    values.update(overrides)
    return ReliabilityConfig(**values)


class EmptyTools:
    def schemas(self) -> list[dict[str, Any]]:
        return []

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        raise AssertionError("No tool call was expected")


class ToolWrapper:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, Any, float | None]] = []

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, arguments, timeout_seconds))
        return self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)


class FailOnceReadTools(ToolWrapper):
    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, arguments, timeout_seconds))
        if len(self.calls) == 1:
            return {
                "ok": False,
                "error": {
                    "type": "mcp_connection_or_protocol_error",
                    "message": "temporary MCP connection loss",
                    "retryable": True,
                    "ambiguous": True,
                },
            }
        return self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)


class TimeoutTools(ToolWrapper):
    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, arguments, timeout_seconds))
        raise TimeoutError("deterministic tool deadline")


class AmbiguousAfterSuccessTools(ToolWrapper):
    def __init__(self, delegate: Any, target: str) -> None:
        super().__init__(delegate)
        self.target = target
        self.injected = False

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((name, arguments, timeout_seconds))
        result = self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)
        if name == self.target and result.get("ok") is True and not self.injected:
            self.injected = True
            return {
                "ok": False,
                "error": {
                    "type": "tool_timeout",
                    "message": "result was lost after the operation may have committed",
                    "retryable": True,
                    "ambiguous": True,
                },
            }
        return result


def mcp_adapter(tmp_path: Path, filename: str = "reliability.db") -> tuple[str, MCPToolAdapter]:
    database_url = f"sqlite:///{(tmp_path / filename).as_posix()}"
    adapter = MCPToolAdapter(create_mcp_server(database_url))
    adapter.discover()
    return database_url, adapter


def test_reliability_configuration_validates() -> None:
    configured = config(
        max_steps=4,
        max_model_calls=5,
        max_tool_calls=7,
        repeated_tool_call_limit=2,
        consecutive_failure_limit=4,
        timeouts=TimeoutPolicy(provider_seconds=1.5, tool_seconds=0.75),
    )

    assert configured.max_steps == 4
    assert configured.timeouts.tool_seconds == 0.75
    assert configured.tool_retry.max_attempts == 2
    with pytest.raises(ValidationError):
        ReliabilityConfig(max_model_calls=0)
    with pytest.raises(ValidationError, match="must remain side-effecting"):
        ReliabilityConfig(
            side_effecting_tools=frozenset({"create_support_ticket"}),
            idempotent_side_effecting_tools=frozenset({"create_refund"}),
        )
    with pytest.raises(ValidationError, match="no idempotency guarantee"):
        ReliabilityConfig(
            idempotent_side_effecting_tools=frozenset(
                {"create_refund", "create_support_ticket"}
            )
        )


def test_delayed_order_skill_mcp_workflow_succeeds_inside_budgets(
    tmp_path: Path,
) -> None:
    database_url, adapter = mcp_adapter(tmp_path)
    registry = SkillRegistry(SKILL_ROOT)
    registry.discover()
    tools = SkillAwareTools(adapter, registry)
    result = AgentRuntime(
        ScriptedProvider(scripted_responses()),
        tools,
        reliability=config(max_model_calls=6, max_tool_calls=10, max_steps=6),
    ).run(
        "The customer says order ORD-1024 has still not arrived after 10 days. "
        "Resolve the issue according to the appropriate operational workflow."
    )

    database = Database(database_url)
    with database.session_factory() as session:
        refund_count = session.scalar(select(func.count()).select_from(Refund))
        ticket_count = session.scalar(select(func.count()).select_from(SupportTicket))
    database.dispose()

    assert result.status == "completed"
    assert result.termination_reason is None
    assert (result.model_calls, result.tool_calls, result.retries, result.failures) == (
        4,
        7,
        0,
        0,
    )
    assert registry.loaded_names == ("delayed-order-resolution",)
    assert tools.business_tools is adapter
    assert refund_count == 1
    assert ticket_count == 1


def test_model_call_budget_blocks_the_next_provider_call(tmp_path: Path) -> None:
    _, adapter = mcp_adapter(tmp_path)
    provider = ScriptedProvider([
        response("1", "get_order", {"order_code": "ORD-1024"}),
        ModelResponse(content="This response must not be consumed."),
    ])
    result = AgentRuntime(
        provider,
        adapter,
        reliability=config(max_model_calls=1),
    ).run("Inspect the order")

    assert result.status == "reliability_failure"
    assert result.termination_reason == TerminationReason.MODEL_CALL_BUDGET_EXHAUSTED
    assert result.model_calls == 1
    assert result.tool_calls == 1
    assert len(provider.requests) == 1


def test_tool_call_budget_blocks_the_next_tool_execution(tmp_path: Path) -> None:
    _, adapter = mcp_adapter(tmp_path)
    tools = ToolWrapper(adapter)
    provider = ScriptedProvider([ModelResponse(tool_calls=[
        ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
        ToolCall(id="2", name="get_shipping", arguments={"order_code": "ORD-1024"}),
    ])])
    result = AgentRuntime(
        provider,
        tools,
        reliability=config(max_tool_calls=1),
    ).run("Inspect the order and shipping")

    assert result.termination_reason == TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED
    assert result.tool_calls == 1
    assert [item[0] for item in tools.calls] == ["get_order"]


def test_step_budget_preserves_bounded_loop_behavior(tmp_path: Path) -> None:
    _, adapter = mcp_adapter(tmp_path)
    provider = ScriptedProvider([
        response("1", "get_order", {"order_code": "ORD-1024"}),
        response("2", "get_order", {"order_code": "ORD-1024"}),
    ])
    result = AgentRuntime(provider, adapter, max_steps=2).run("Keep looking")

    assert result.status == "max_steps_exceeded"
    assert result.termination_reason == TerminationReason.STEP_BUDGET_EXHAUSTED
    assert result.model_calls == 2
    assert result.tool_calls == 2


def test_retryable_provider_timeout_recovers_without_real_sleep() -> None:
    provider = ScriptedProvider([
        ProviderError(
            "temporary timeout", retryable=True, kind="provider_timeout"
        ),
        ModelResponse(content="Recovered safely."),
    ])
    sleeps: list[float] = []
    reliability = config(
        provider_retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0.5),
        timeouts=TimeoutPolicy(provider_seconds=1.25, tool_seconds=1),
    )
    result = AgentRuntime(
        provider, EmptyTools(), reliability=reliability, sleeper=sleeps.append
    ).run("Recover")

    assert result.status == "completed"
    assert result.response == "Recovered safely."
    assert (result.model_calls, result.retries, result.failures) == (2, 1, 1)
    assert result.consecutive_failures == 0
    assert sleeps == [0.5]
    assert [item["timeout_seconds"] for item in provider.requests] == [1.25, 1.25]


def test_provider_retry_exhaustion_is_bounded() -> None:
    provider = ScriptedProvider([
        ProviderError("transient 1", retryable=True),
        ProviderError("transient 2", retryable=True),
        ProviderError("transient 3", retryable=True),
    ])
    sleeps: list[float] = []
    result = AgentRuntime(
        provider,
        EmptyTools(),
        reliability=config(
            provider_retry=RetryPolicy(
                max_attempts=3,
                initial_backoff_seconds=0.1,
                backoff_multiplier=2,
            ),
            consecutive_failure_limit=5,
        ),
        sleeper=sleeps.append,
    ).run("Fail safely")

    assert result.status == "provider_error"
    assert result.termination_reason == TerminationReason.PROVIDER_RETRY_EXHAUSTED
    assert (result.model_calls, result.retries, result.failures) == (3, 2, 3)
    assert sleeps == [0.1, 0.2]


def test_permanent_provider_failure_is_not_retried() -> None:
    provider = ScriptedProvider([ProviderError("bad credentials")])
    result = AgentRuntime(provider, EmptyTools(), reliability=config()).run("Stop")

    assert result.status == "provider_error"
    assert result.termination_reason == TerminationReason.PROVIDER_FAILURE
    assert (result.model_calls, result.retries, result.failures) == (1, 0, 1)
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("threshold", "executed"),
    [(1, 1), (2, 2)],
)
def test_repeated_identical_tool_call_limit_is_configurable(
    tmp_path: Path, threshold: int, executed: int
) -> None:
    _, adapter = mcp_adapter(tmp_path, f"repeat-{threshold}.db")
    provider = ScriptedProvider([
        response(str(index), "get_order", {"order_code": "ORD-1024"})
        for index in range(1, threshold + 2)
    ])
    result = AgentRuntime(
        provider,
        adapter,
        reliability=config(
            repeated_tool_call_limit=threshold,
            max_steps=threshold + 2,
        ),
    ).run("Repeat the same lookup")

    assert result.termination_reason == TerminationReason.REPEATED_TOOL_CALL
    assert result.tool_calls == executed
    assert len(result.tool_events) == executed
    assert result.steps == threshold + 1


def test_successful_tool_call_resets_consecutive_failure_accounting(
    tmp_path: Path,
) -> None:
    _, adapter = mcp_adapter(tmp_path)
    provider = ScriptedProvider([
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={}),
            ToolCall(id="2", name="get_shipping", arguments={"order_code": "ORD-1024"}),
        ]),
        ModelResponse(content="Recovered after correcting the lookup."),
    ])
    result = AgentRuntime(provider, adapter, reliability=config()).run("Inspect state")

    assert result.status == "completed"
    assert result.failures == 1
    assert result.consecutive_failures == 0
    assert result.retries == 0


def test_consecutive_tool_failure_limit_terminates_unhealthy_run(
    tmp_path: Path,
) -> None:
    _, adapter = mcp_adapter(tmp_path)
    provider = ScriptedProvider([ModelResponse(tool_calls=[
        ToolCall(id="1", name="get_order", arguments={}),
        ToolCall(id="2", name="get_shipping", arguments={}),
        ToolCall(id="3", name="get_customer", arguments={}),
    ])])
    result = AgentRuntime(
        provider,
        adapter,
        reliability=config(consecutive_failure_limit=2),
    ).run("Use malformed calls")

    assert result.termination_reason == TerminationReason.CONSECUTIVE_FAILURE_LIMIT
    assert (result.tool_calls, result.failures, result.retries) == (2, 2, 0)
    assert len(result.tool_events) == 2


def test_validation_error_is_not_automatically_retried(tmp_path: Path) -> None:
    _, adapter = mcp_adapter(tmp_path)
    provider = ScriptedProvider([
        response("1", "get_order", {"wrong": "ORD-1024"}),
        ModelResponse(content="Arguments were invalid."),
    ])
    result = AgentRuntime(
        provider,
        adapter,
        reliability=config(tool_retry=RetryPolicy(max_attempts=3)),
    ).run("Malformed lookup")

    assert result.status == "completed"
    assert result.tool_events[0].attempts == 1
    assert (result.tool_calls, result.retries, result.failures) == (1, 0, 1)


def test_read_tool_timeout_is_typed_bounded_and_uses_injected_deadline(
    tmp_path: Path,
) -> None:
    _, adapter = mcp_adapter(tmp_path)
    tools = TimeoutTools(adapter)
    result = AgentRuntime(
        ScriptedProvider([
            response("1", "get_order", {"order_code": "ORD-1024"})
        ]),
        tools,
        reliability=config(
            timeouts=TimeoutPolicy(provider_seconds=2, tool_seconds=0.75),
            tool_retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        ),
    ).run("Time out deterministically")

    assert result.termination_reason == TerminationReason.TOOL_TIMEOUT
    assert (result.tool_calls, result.retries, result.failures) == (2, 1, 2)
    assert [item[2] for item in tools.calls] == [0.75, 0.75]
    assert all(item.type == "tool_timeout" for item in result.failure_details)


def test_retryable_read_tool_failure_retries_then_succeeds(tmp_path: Path) -> None:
    _, adapter = mcp_adapter(tmp_path)
    tools = FailOnceReadTools(adapter)
    provider = ScriptedProvider([
        response("1", "get_order", {"order_code": "ORD-1024"}),
        ModelResponse(content="Order lookup recovered."),
    ])
    result = AgentRuntime(provider, tools, reliability=config()).run("Inspect order")

    assert result.status == "completed"
    assert (result.tool_calls, result.retries, result.failures) == (2, 1, 1)
    assert result.tool_events[0].attempts == 2
    assert result.tool_events[0].result["data"]["code"] == "ORD-1024"


def test_idempotent_refund_can_retry_after_ambiguous_result_without_duplicate(
    tmp_path: Path,
) -> None:
    database_url, adapter = mcp_adapter(tmp_path)
    tools = AmbiguousAfterSuccessTools(adapter, "create_refund")
    request = {
        "order_code": "ORD-1024",
        "amount": "299.00",
        "reason": "Shipment delayed by 10 days",
        "idempotency_key": "reliability-refund-1",
    }
    provider = ScriptedProvider([
        response("1", "create_refund", request),
        ModelResponse(content="The idempotent refund completed."),
    ])
    result = AgentRuntime(provider, tools, reliability=config()).run("Refund safely")

    database = Database(database_url)
    with database.session_factory() as session:
        refund_count = session.scalar(select(func.count()).select_from(Refund))
    database.dispose()

    assert result.status == "completed"
    assert (result.tool_calls, result.retries, result.failures) == (2, 1, 1)
    assert result.tool_events[0].result["data"]["created"] is False
    assert refund_count == 1


def test_unsafe_support_ticket_retry_is_blocked_without_duplicate(
    tmp_path: Path,
) -> None:
    database_url, adapter = mcp_adapter(tmp_path)
    tools = AmbiguousAfterSuccessTools(adapter, "create_support_ticket")
    provider = ScriptedProvider([response("1", "create_support_ticket", {
        "customer_code": "CUS-001",
        "order_code": "ORD-1024",
        "subject": "Delayed shipment follow-up",
        "description": "Document the delayed shipment and contact the customer.",
        "priority": "high",
    })])
    result = AgentRuntime(provider, tools, reliability=config()).run("Create a ticket")

    database = Database(database_url)
    with database.session_factory() as session:
        ticket_count = session.scalar(select(func.count()).select_from(SupportTicket))
    database.dispose()

    assert result.termination_reason == TerminationReason.UNSAFE_RETRY_BLOCKED
    assert (result.tool_calls, result.retries, result.failures) == (1, 0, 1)
    assert len(tools.calls) == 1
    assert ticket_count == 1
    assert "no strong idempotency guarantee" in result.response


def test_high_value_pending_approval_is_not_a_reliability_failure(
    tmp_path: Path,
) -> None:
    database_url, adapter = mcp_adapter(tmp_path)
    database = Database(database_url)
    with database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()
    database.dispose()

    registry = SkillRegistry(SKILL_ROOT)
    registry.discover()
    tools = SkillAwareTools(adapter, registry)
    provider = ScriptedProvider([
        response("skill", "load_skill", {"name": "high-value-refund-escalation"}),
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_refund_policy", arguments={}),
        ]),
        response("3", "create_refund", {
            "order_code": "ORD-1024",
            "amount": "1200.00",
            "reason": "Eligible high-value delayed order",
            "idempotency_key": "reliability-high-1",
        }),
        ModelResponse(
            content="The refund is pending human approval and has not completed."
        ),
    ])
    result = AgentRuntime(provider, tools, reliability=config()).run(
        "Handle the high-value refund request."
    )

    refund = next(
        event.result["data"]
        for event in result.tool_events
        if event.call.name == "create_refund"
    )
    assert result.status == "completed"
    assert result.termination_reason is None
    assert (result.failures, result.retries) == (0, 0)
    assert refund["status"] == "pending_human_approval"
    assert refund["completed"] is False
    assert "pending human approval" in result.response
    assert "not completed" in result.response
