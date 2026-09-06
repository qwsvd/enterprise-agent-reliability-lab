from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode, Tracer
from sqlalchemy import select

from app.agent.providers import ProviderError, ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.database import Database
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order
from app.reliability import ReliabilityConfig, RetryPolicy, TerminationReason
from app.seed import seed_demo_data
from app.skills.demo import scripted_responses
from app.skills.loader import SkillRegistry
from app.skills.tools import SkillAwareTools
from app.tracing import InMemoryTracing, create_in_memory_tracing


SKILL_ROOT = Path(__file__).parents[1] / ".agents" / "skills"


@pytest.fixture
def tracing() -> Iterator[InMemoryTracing]:
    setup = create_in_memory_tracing()
    yield setup
    setup.shutdown()


def traced_tools(
    tmp_path: Path, tracing: InMemoryTracing, filename: str = "tracing.db"
) -> tuple[str, MCPToolAdapter, SkillRegistry, SkillAwareTools]:
    database_url = f"sqlite:///{(tmp_path / filename).as_posix()}"
    adapter = MCPToolAdapter(create_mcp_server(database_url))
    registry = SkillRegistry(SKILL_ROOT)
    tools = SkillAwareTools(adapter, registry)
    return database_url, adapter, registry, tools


def spans_named(spans: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def parent_name(span: ReadableSpan, spans: tuple[ReadableSpan, ...]) -> str | None:
    if span.parent is None:
        return None
    names = {
        candidate.context.span_id: candidate.name
        for candidate in spans
        if candidate.context is not None
    }
    return names.get(span.parent.span_id)


def sanitized_trace_text(spans: tuple[ReadableSpan, ...]) -> str:
    parts: list[str] = []
    for span in spans:
        parts.append(span.name)
        parts.extend(f"{key}={value}" for key, value in span.attributes.items())
        for event in span.events:
            parts.append(event.name)
            parts.extend(f"{key}={value}" for key, value in event.attributes.items())
        if span.status.description:
            parts.append(span.status.description)
    return "\n".join(parts)


def test_success_trace_reconstructs_agent_skill_mcp_hierarchy(
    tmp_path: Path, tracing: InMemoryTracing
) -> None:
    database_url, adapter, registry, tools = traced_tools(tmp_path, tracing)
    provider = ScriptedProvider(scripted_responses())
    provider.model = "deterministic-test-model"
    result = AgentRuntime(
        provider,
        tools,
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-success-001",
    ).run(
        "Customer li.wei@example.com supplied sk-never-trace-this while asking about "
        "CUS-001 and ORD-1024. Resolve the delayed order."
    )
    spans = tracing.finished_spans()

    assert result.status == "completed"
    assert result.run_id == "run-success-001"
    assert registry.loaded_names == ("delayed-order-resolution",)
    assert adapter.protocol_version == "2026-07-28"
    assert len(spans_named(spans, "agent.run")) == 1
    assert len(spans_named(spans, "skill.discovery")) == 1
    assert len(spans_named(spans, "mcp.discovery")) == 1
    assert len(spans_named(spans, "agent.step")) == 4
    assert len(spans_named(spans, "agent.provider.call")) == 4
    assert len(spans_named(spans, "agent.tool.call")) == 7
    assert len(spans_named(spans, "skill.load")) == 1
    assert len(spans_named(spans, "mcp.call")) == 6

    root = spans_named(spans, "agent.run")[0]
    assert root.parent is None
    assert root.attributes["agent.run.id"] == "run-success-001"
    assert root.attributes["agent.termination.reason"] == "completed"
    assert root.attributes["agent.model.calls"] == 4
    assert root.attributes["agent.tool.calls"] == 7
    assert root.end_time > root.start_time
    assert all(
        span.context.trace_id == root.context.trace_id
        for span in spans
        if span.context is not None
    )
    assert all(
        parent_name(span, spans) == "agent.step"
        for span in spans_named(spans, "agent.provider.call")
    )
    assert all(
        span.attributes["provider.model"] == "deterministic-test-model"
        for span in spans_named(spans, "agent.provider.call")
    )
    assert all(
        parent_name(span, spans) == "agent.step"
        for span in spans_named(spans, "agent.tool.call")
    )
    assert parent_name(spans_named(spans, "skill.load")[0], spans) == "agent.tool.call"
    assert all(
        parent_name(span, spans) == "agent.tool.call"
        for span in spans_named(spans, "mcp.call")
    )

    database = Database(database_url)
    with database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        assert order.refund.status == "approved"
    database.dispose()


def test_provider_retry_and_failure_are_sanitized_and_traceable(
    tracing: InMemoryTracing,
) -> None:
    provider = ScriptedProvider([
        ProviderError(
            "transient failure contains sk-secret-provider-value",
            retryable=True,
            kind="provider_connection_error",
        ),
        ModelResponse(content="Recovered."),
    ])
    sleeps: list[float] = []
    result = AgentRuntime(
        provider,
        tools=NoTools(),
        reliability=ReliabilityConfig(
            provider_retry=RetryPolicy(
                max_attempts=2, initial_backoff_seconds=0.25
            )
        ),
        sleeper=sleeps.append,
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-retry-001",
    ).run("Private prompt containing customer@example.com")
    spans = tracing.finished_spans()

    provider_spans = sorted(
        spans_named(spans, "agent.provider.call"),
        key=lambda span: span.attributes["provider.attempt"],
    )
    retry_span = spans_named(spans, "reliability.retry")[0]
    root = spans_named(spans, "agent.run")[0]
    assert result.status == "completed"
    assert (result.model_calls, result.retries, result.failures) == (2, 1, 1)
    assert sleeps == [0.25]
    assert provider_spans[0].status.status_code == StatusCode.ERROR
    assert provider_spans[0].attributes["failure.category"] == "provider_connection_error"
    assert provider_spans[0].attributes["failure.retryable"] is True
    assert provider_spans[1].attributes["operation.outcome"] == "success"
    assert retry_span.attributes["retry.operation"] == "provider"
    assert retry_span.attributes["retry.number"] == 1
    assert parent_name(retry_span, spans) == "agent.step"
    assert root.attributes["agent.retries"] == 1
    assert "sk-secret-provider-value" not in sanitized_trace_text(spans)
    assert "customer@example.com" not in sanitized_trace_text(spans)


def test_tool_retry_attempts_are_nested_and_traceable(
    tmp_path: Path, tracing: InMemoryTracing
) -> None:
    _, adapter, _, _ = traced_tools(tmp_path, tracing, "tool-retry.db")
    tools = FailOnceTools(adapter)
    provider = ScriptedProvider([
        ModelResponse(tool_calls=[ToolCall(
            id="1", name="get_order", arguments={"order_code": "ORD-1024"}
        )]),
        ModelResponse(content="Recovered."),
    ])
    result = AgentRuntime(
        provider,
        tools,
        reliability=ReliabilityConfig(
            tool_retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0)
        ),
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-tool-retry-001",
    ).run("Inspect")
    spans = tracing.finished_spans()
    tool_spans = sorted(
        spans_named(spans, "agent.tool.call"),
        key=lambda span: span.attributes["tool.attempt"],
    )
    retry_span = next(
        span
        for span in spans_named(spans, "reliability.retry")
        if span.attributes["retry.operation"] == "tool"
    )

    assert result.status == "completed"
    assert (result.tool_calls, result.retries, result.failures) == (2, 1, 1)
    assert len(tool_spans) == 2
    assert tool_spans[0].attributes["failure.category"] == "temporary_tool_failure"
    assert tool_spans[0].status.status_code == StatusCode.ERROR
    assert tool_spans[1].attributes["operation.outcome"] == "success"
    assert retry_span.attributes["tool.name"] == "get_order"
    assert parent_name(retry_span, spans) == "agent.step"


def test_retry_exhaustion_and_repeated_call_terminal_reasons_are_traced(
    tmp_path: Path, tracing: InMemoryTracing
) -> None:
    exhausted = AgentRuntime(
        ScriptedProvider([
            ProviderError("first", retryable=True, kind="provider_connection_error"),
            ProviderError("second", retryable=True, kind="provider_connection_error"),
        ]),
        NoTools(),
        reliability=ReliabilityConfig(
            provider_retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
            consecutive_failure_limit=5,
        ),
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-exhausted-001",
    ).run("Fail")
    exhausted_spans = tracing.finished_spans()
    exhausted_root = spans_named(exhausted_spans, "agent.run")[0]

    assert exhausted.termination_reason == TerminationReason.PROVIDER_RETRY_EXHAUSTED
    assert exhausted_root.attributes["agent.termination.reason"] == "provider_retry_exhausted"
    assert exhausted_root.attributes["agent.retries"] == 1
    assert exhausted_root.status.status_code == StatusCode.ERROR

    tracing.clear()
    _, adapter, _, _ = traced_tools(tmp_path, tracing, "repeat.db")
    repeated = AgentRuntime(
        ScriptedProvider([
            ModelResponse(tool_calls=[ToolCall(
                id="1", name="get_order", arguments={"order_code": "ORD-1024"}
            )]),
            ModelResponse(tool_calls=[ToolCall(
                id="2", name="get_order", arguments={"order_code": "ORD-1024"}
            )]),
        ]),
        adapter,
        reliability=ReliabilityConfig(repeated_tool_call_limit=1),
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-repeated-001",
    ).run("Repeat")
    repeated_root = spans_named(tracing.finished_spans(), "agent.run")[0]

    assert repeated.termination_reason == TerminationReason.REPEATED_TOOL_CALL
    assert repeated.tool_calls == 1
    assert repeated_root.attributes["agent.termination.reason"] == "repeated_tool_call"


def test_high_value_pending_approval_is_successful_and_traced_accurately(
    tmp_path: Path, tracing: InMemoryTracing
) -> None:
    database_url, _, registry, tools = traced_tools(tmp_path, tracing, "high.db")
    database = Database(database_url)
    database.create_schema()
    with database.session_factory() as session:
        seed_demo_data(session)
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()
    database.dispose()

    provider = ScriptedProvider([
        ModelResponse(tool_calls=[ToolCall(
            id="skill", name="load_skill",
            arguments={"name": "high-value-refund-escalation"},
        )]),
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[ToolCall(
            id="3", name="create_refund",
            arguments={
                "order_code": "ORD-1024",
                "amount": "1200.00",
                "reason": "Eligible high-value delayed order",
                "idempotency_key": "trace-high-secret-key",
            },
        )]),
        ModelResponse(content="Pending human approval; the refund has not completed."),
    ])
    result = AgentRuntime(
        provider,
        tools,
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-high-001",
    ).run("Handle the high-value request")
    spans = tracing.finished_spans()
    refund_span = next(
        span
        for span in spans_named(spans, "agent.tool.call")
        if span.attributes["tool.name"] == "create_refund"
    )

    refund_result = next(
        event.result["data"]
        for event in result.tool_events
        if event.call.name == "create_refund"
    )
    assert result.status == "completed"
    assert (result.failures, result.retries) == (0, 0)
    assert registry.loaded_names == ("high-value-refund-escalation",)
    assert refund_result["status"] == "pending_human_approval"
    assert refund_result["completed"] is False
    assert refund_span.attributes["business.refund.status"] == "pending_human_approval"
    assert refund_span.attributes["business.refund.completed"] is False
    assert refund_span.status.status_code == StatusCode.OK
    assert "trace-high-secret-key" not in sanitized_trace_text(spans)


def test_trace_attributes_do_not_contain_prompts_arguments_results_or_pii(
    tmp_path: Path, tracing: InMemoryTracing
) -> None:
    _, adapter, _, _ = traced_tools(tmp_path, tracing, "sensitive.db")
    provider = ScriptedProvider([
        ModelResponse(tool_calls=[
            ToolCall(
                id="sensitive-call-id",
                name="get_customer",
                arguments={"customer_code": "CUS-001"},
            ),
            ToolCall(
                id="secret-tool-call-id",
                name="sk-secret-tool-name",
                arguments={"password": "hunter2"},
            ),
        ]),
        ModelResponse(content="Customer li.wei@example.com was found."),
    ])
    provider.model = "secret-model-token"
    AgentRuntime(
        provider,
        adapter,
        tracer=tracing.tracer,
        run_id_factory=lambda: "run-sensitive-001",
    ).run("Prompt secret: password=hunter2; inspect CUS-001")
    trace_text = sanitized_trace_text(tracing.finished_spans())

    for sensitive in (
        "hunter2",
        "password",
        "CUS-001",
        "ORD-1024",
        "li.wei@example.com",
        "sensitive-call-id",
        "secret-tool-call-id",
        "sk-secret-tool-name",
        "secret-model-token",
    ):
        assert sensitive not in trace_text


class NoTools:
    def schemas(self) -> list[dict[str, Any]]:
        return []

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        raise AssertionError("No tools are available")


class FailOnceTools:
    def __init__(self, delegate: MCPToolAdapter) -> None:
        self.delegate = delegate
        self.failed = False

    def set_tracer(self, tracer: Tracer) -> None:
        self.delegate.set_tracer(tracer)

    def prepare(self) -> None:
        self.delegate.prepare()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if not self.failed:
            self.failed = True
            return {
                "ok": False,
                "error": {
                    "type": "temporary_tool_failure",
                    "message": "contains private transient details",
                    "retryable": True,
                },
            }
        return self.delegate.execute(
            name, arguments, timeout_seconds=timeout_seconds
        )
