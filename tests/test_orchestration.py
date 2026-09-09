from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agent.providers import ProviderError, ScriptedProvider
from app.agent.types import ModelResponse
from app.database import Database
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.orchestration import (
    DependencyScheduler,
    DeterministicPlanner,
    EpisodicMemoryStore,
    MultiAgentOrchestrator,
    OrchestrationConfig,
    OrchestrationTermination,
    PlanTask,
    PlannedToolCall,
    ProviderPlanner,
    SchedulerError,
    ScriptedPlanner,
    TaskPlan,
    TaskStatus,
)
from app.orchestration.models import PlannerContext, ToolEvidence, WorkingMemory
from app.orchestration.reviewer import EvidenceReviewer
from app.orchestration.scenarios import DELAYED_ORDER_GOAL, build_delayed_order_plan
from app.reliability import ReliabilityConfig, RetryPolicy
from app.seed import seed_demo_data
from app.skills import SkillAwareTools, SkillRegistry
from app.tracing import InMemoryTracing, create_in_memory_tracing


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".agents" / "skills"


class ToolWrapper:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def set_tracer(self, tracer: Any) -> None:
        self.delegate.set_tracer(tracer)

    def prepare(self) -> None:
        self.delegate.prepare()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def context(self) -> str:
        return self.delegate.context()

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        return self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)


class FailOnceShippingTools(ToolWrapper):
    def __init__(self, delegate: Any, *, empty_evidence: bool = False) -> None:
        super().__init__(delegate)
        self.failed = False
        self.empty_evidence = empty_evidence

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if name == "get_shipping" and not self.failed:
            self.failed = True
            if self.empty_evidence:
                return {"ok": True, "data": {}}
            return {
                "ok": False,
                "error": {"type": "temporary_transport", "retryable": True},
            }
        return super().execute(name, arguments, timeout_seconds=timeout_seconds)


class AlwaysFailShippingTools(ToolWrapper):
    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if name == "get_shipping":
            return {
                "ok": False,
                "error": {"type": "temporary_transport", "retryable": True},
            }
        return super().execute(name, arguments, timeout_seconds=timeout_seconds)


class AmbiguousTicketTools(ToolWrapper):
    def __init__(self, delegate: Any) -> None:
        super().__init__(delegate)
        self.ticket_calls = 0

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        result = super().execute(name, arguments, timeout_seconds=timeout_seconds)
        if name == "create_support_ticket":
            self.ticket_calls += 1
            if result.get("ok") is True:
                return {
                    "ok": False,
                    "error": {
                        "type": "ambiguous_transport",
                        "retryable": True,
                        "ambiguous": True,
                    },
                }
        return result


class BrokenReviewer(EvidenceReviewer):
    def review(self, **kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError("reviewer unavailable")


def prepare_database(path: Path, *, amount: Decimal = Decimal("299.00")) -> str:
    url = f"sqlite:///{path.as_posix()}"
    database = Database(url)
    database.create_schema()
    with database.session_factory() as session:
        seed_demo_data(session)
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        assert order is not None
        order.amount = amount
        session.commit()
    database.dispose()
    return url


def business_counts(url: str) -> tuple[int, int, str | None]:
    database = Database(url)
    with database.session_factory() as session:
        refunds = int(session.scalar(select(func.count()).select_from(Refund)) or 0)
        tickets = int(
            session.scalar(select(func.count()).select_from(SupportTicket)) or 0
        )
        refund = session.scalar(select(Refund).order_by(Refund.id))
    database.dispose()
    return refunds, tickets, refund.status if refund is not None else None


def business_tools(url: str, tracing: InMemoryTracing) -> SkillAwareTools:
    return SkillAwareTools(
        MCPToolAdapter(create_mcp_server(url), tracer=tracing.tracer),
        SkillRegistry(SKILL_ROOT, tracer=tracing.tracer),
    )


def orchestrator(
    tmp_path: Path,
    *,
    plans: list[TaskPlan] | None = None,
    planner: Any = None,
    amount: Decimal = Decimal("299.00"),
    wrapper: type[ToolWrapper] | None = None,
    config: OrchestrationConfig | None = None,
    completion_builder: Any = None,
    memory: EpisodicMemoryStore | None = None,
    database_name: str = "business.db",
) -> tuple[MultiAgentOrchestrator, str, EpisodicMemoryStore, InMemoryTracing, Any]:
    url = prepare_database(tmp_path / database_name, amount=amount)
    tracing = create_in_memory_tracing(database_name)
    tools: Any = business_tools(url, tracing)
    if wrapper is not None:
        tools = wrapper(tools)
    selected_planner = planner or ScriptedPlanner(
        plans or [build_delayed_order_plan(database_name.replace(".db", ""))]
    )
    store = memory or EpisodicMemoryStore(
        f"sqlite:///{(tmp_path / 'memory.db').as_posix()}"
    )
    kwargs: dict[str, Any] = {}
    if completion_builder is not None:
        kwargs["completion_builder"] = completion_builder
    runtime = MultiAgentOrchestrator(
        planner=selected_planner,
        tools=tools,
        memory_store=store,
        config=config,
        tracer=tracing.tracer,
        sleeper=lambda _: None,
        run_id_factory=lambda: database_name,
        **kwargs,
    )
    return runtime, url, store, tracing, selected_planner


def test_langgraph_normal_delayed_order_uses_skill_mcp_and_business_state(
    tmp_path: Path,
) -> None:
    runtime, url, memory, tracing, _ = orchestrator(tmp_path)
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
        spans = [span.name for span in tracing.finished_spans()]
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "completed"
    assert result.termination_reason == OrchestrationTermination.COMPLETED
    assert result.task_execution_order == [
        "load_workflow",
        "inspect_order",
        "inspect_customer",
        "inspect_shipping",
        "inspect_policy",
        "create_refund",
        "create_ticket",
        "verify_result",
    ]
    assert result.tool_sequence == [
        "load_skill",
        "get_order",
        "get_customer",
        "get_shipping",
        "get_refund_policy",
        "create_refund",
        "create_support_ticket",
        "get_order",
    ]
    assert business_counts(url) == (1, 1, "approved")
    assert result.counters.tool_calls == 8
    assert result.counters.replans == result.counters.reflections == 0
    assert result.token_usage is result.estimated_cost is None
    assert {
        "multi_agent.run",
        "memory.retrieve",
        "planner.plan",
        "scheduler.select",
        "executor.task",
        "reviewer.review",
        "memory.write",
        "skill.load",
        "mcp.call",
    } <= set(spans)
    assert runtime.graph.get_graph().nodes.keys() >= {
        "planner",
        "scheduler",
        "executor",
        "reviewer",
        "reflection",
        "replan",
    }


def test_high_value_refund_remains_pending_not_completed(tmp_path: Path) -> None:
    runtime, url, memory, tracing, _ = orchestrator(
        tmp_path, amount=Decimal("1500.00"), database_name="high-value.db"
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "completed"
    assert business_counts(url) == (1, 1, "pending_human_approval")
    assert "pending human approval" in result.final_response
    assert "has not completed" in result.final_response
    assert "refund completed" not in result.final_response.casefold()
    assert result.counters.failures == 0


@pytest.mark.parametrize("empty_evidence", [False, True])
def test_executor_failure_or_insufficient_evidence_reflects_replans_and_recovers(
    tmp_path: Path, empty_evidence: bool
) -> None:
    url = prepare_database(tmp_path / "recover.db")
    tracing = create_in_memory_tracing("recover")
    base = business_tools(url, tracing)
    tools = FailOnceShippingTools(base, empty_evidence=empty_evidence)
    memory = EpisodicMemoryStore(f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    planner = ScriptedPlanner(
        [build_delayed_order_plan("first"), build_delayed_order_plan("recovered")]
    )
    runtime = MultiAgentOrchestrator(
        planner=planner,
        tools=tools,
        memory_store=memory,
        tracer=tracing.tracer,
        sleeper=lambda _: None,
        run_id_factory=lambda: "recover",
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
        spans = [span.name for span in tracing.finished_spans()]
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "completed"
    assert result.counters.reflections == 1
    assert result.counters.replans == 1
    assert result.reflections[0].failure_type == (
        "insufficient_evidence" if empty_evidence else "temporary_transport"
    )
    assert "reflection" in spans
    assert "planner.replan" in spans
    assert business_counts(url) == (1, 1, "approved")


def test_max_replan_budget_terminates_safely(tmp_path: Path) -> None:
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        plans=[build_delayed_order_plan("one"), build_delayed_order_plan("two")],
        wrapper=AlwaysFailShippingTools,
        config=OrchestrationConfig(max_replans=1),
        database_name="replan-budget.db",
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "failed"
    assert result.termination_reason == OrchestrationTermination.MAX_REPLANS_EXCEEDED
    assert result.counters.replans == 1
    assert result.counters.reflections == 2
    assert result.counters.orchestration_steps < 64


def test_orchestration_step_reflection_tool_and_repetition_budgets(
    tmp_path: Path,
) -> None:
    cases = [
        (
            OrchestrationConfig(max_orchestration_steps=4),
            [simple_plan()],
            None,
            OrchestrationTermination.MAX_STEPS_EXCEEDED,
        ),
        (
            OrchestrationConfig(max_reflections=0),
            [build_delayed_order_plan("reflection")],
            AlwaysFailShippingTools,
            OrchestrationTermination.MAX_REFLECTIONS_EXCEEDED,
        ),
        (
            OrchestrationConfig(
                reliability=ReliabilityConfig(max_tool_calls=1)
            ),
            [build_delayed_order_plan("tool-budget")],
            None,
            OrchestrationTermination.TOOL_CALL_BUDGET_EXHAUSTED,
        ),
        (
            OrchestrationConfig(repeated_task_limit=1),
            [build_delayed_order_plan("repeated-task")],
            None,
            OrchestrationTermination.REPEATED_TASK,
        ),
    ]
    for index, (config, plans, wrapper, expected) in enumerate(cases):
        runtime, _, memory, tracing, _ = orchestrator(
            tmp_path,
            plans=plans,
            wrapper=wrapper,
            config=config,
            database_name=f"budget-{index}.db",
        )
        try:
            result = runtime.run(DELAYED_ORDER_GOAL)
        finally:
            tracing.shutdown()
            memory.close()
        assert result.termination_reason == expected


def test_repeated_effective_plan_is_bounded(tmp_path: Path) -> None:
    plan = build_delayed_order_plan("same")
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        plans=[plan, plan.model_copy(deep=True)],
        wrapper=AlwaysFailShippingTools,
        config=OrchestrationConfig(repeated_plan_limit=1),
        database_name="repeated-plan.db",
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == OrchestrationTermination.REPEATED_PLAN
    assert result.counters.replans == 1


def simple_plan(*, status: TaskStatus = TaskStatus.PENDING) -> TaskPlan:
    return TaskPlan(
        plan_id="simple",
        goal="Inspect the order safely",
        tasks=[
            PlanTask(
                task_id="inspect",
                objective="Inspect the order state.",
                required_tools=["get_order"],
                tool_call=PlannedToolCall(
                    name="get_order", arguments={"order_code": "ORD-1024"}
                ),
                status=status,
            )
        ],
    )


def test_dependency_scheduler_rejects_invalid_and_cyclic_plans() -> None:
    scheduler = DependencyScheduler()
    invalid = simple_plan()
    invalid.tasks[0].dependencies = ["missing"]
    with pytest.raises(SchedulerError) as missing:
        scheduler.validate(invalid)
    assert missing.value.reason == OrchestrationTermination.INVALID_DEPENDENCY

    cyclic = TaskPlan(
        plan_id="cycle",
        goal="Detect dependency cycle",
        tasks=[
            PlanTask(
                task_id="a",
                objective="Execute task A.",
                dependencies=["b"],
                required_tools=["get_order"],
                tool_call=PlannedToolCall(
                    name="get_order", arguments={"order_code": "ORD-1024"}
                ),
            ),
            PlanTask(
                task_id="b",
                objective="Execute task B.",
                dependencies=["a"],
                required_tools=["get_order"],
                tool_call=PlannedToolCall(
                    name="get_order", arguments={"order_code": "ORD-1024"}
                ),
            ),
        ],
    )
    with pytest.raises(SchedulerError) as cycle:
        scheduler.validate(cyclic)
    assert cycle.value.reason == OrchestrationTermination.CYCLIC_DEPENDENCY


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (TaskStatus.RUNNING, OrchestrationTermination.NO_READY_TASK),
        (TaskStatus.FAILED, OrchestrationTermination.DEADLOCK),
    ],
)
def test_no_ready_task_and_deadlock_are_typed(
    tmp_path: Path, status: TaskStatus, reason: OrchestrationTermination
) -> None:
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        plans=[simple_plan(status=status)],
        database_name=f"{status.value}.db",
    )
    try:
        result = runtime.run("Inspect the order safely")
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == reason


def test_episodic_memory_persists_retrieves_and_influences_planner_context(
    tmp_path: Path,
) -> None:
    memory_url = f"sqlite:///{(tmp_path / 'episodes.db').as_posix()}"
    memory = EpisodicMemoryStore(memory_url)
    first, _, _, first_trace, _ = orchestrator(
        tmp_path,
        memory=memory,
        database_name="first.db",
        plans=[simple_plan()],
    )
    try:
        assert first.run("Inspect the delayed order state").status == "completed"
    finally:
        first_trace.shutdown()

    def plan_from_memory(context: PlannerContext) -> TaskPlan:
        assert context.retrieved_episodes
        plan = simple_plan()
        plan.plan_id = "memory-informed"
        plan.tasks[0].priority = 1
        return plan

    planner = DeterministicPlanner(plan_from_memory)
    second, _, _, second_trace, _ = orchestrator(
        tmp_path,
        memory=memory,
        database_name="second.db",
        planner=planner,
    )
    try:
        result = second.run("Inspect the delayed order state again")
        reopened = EpisodicMemoryStore(memory_url)
        try:
            retrieved = reopened.retrieve("Inspect delayed order", limit=2)
        finally:
            reopened.close()
    finally:
        second_trace.shutdown()
        memory.close()

    assert result.status == "completed"
    assert result.plan is not None and result.plan.plan_id == "memory-informed"
    assert planner.contexts[0].retrieved_episodes
    assert len(retrieved) == 2
    assert all("email" not in str(item.evidence_summary).casefold() for item in retrieved)


def test_episode_persistence_redacts_sensitive_goal_and_objective_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "private-episodes.db"
    memory = EpisodicMemoryStore(f"sqlite:///{database_path.as_posix()}")
    secrets = [
        "alice.private@example.com",
        "+86 138-0013-8000",
        "CUS-PRIVATE-77",
        "ORD-SECRET-88",
        "SF-20260906001024",
        "api_supersecretvalue123",
    ]
    goal = (
        "Resolve delayed after-sales case for CUS-PRIVATE-77 and ORD-SECRET-88; "
        "contact alice.private@example.com at +86 138-0013-8000; "
        "tracking SF-20260906001024; token api_supersecretvalue123."
    )
    plan = simple_plan()
    plan.goal = goal
    plan.tasks[0].objective = (
        "Inspect customer code CUS-PRIVATE-77, order code ORD-SECRET-88, "
        "and tracking number SF-20260906001024 for alice.private@example.com."
    )
    working = WorkingMemory(
        goal=goal,
        current_plan=plan,
        tool_evidence=[
            ToolEvidence(
                task_id="inspect",
                tool_name="get_order",
                ok=True,
                data={
                    "code": "ORD-SECRET-88",
                    "customer_code": "CUS-PRIVATE-77",
                    "email": "alice.private@example.com",
                    "status": "shipped",
                },
            )
        ],
    )
    try:
        written = memory.write_episode(
            run_id="privacy-run",
            memory=working,
            outcome="completed",
            termination_reason="completed",
        )
        retrieved = memory.retrieve("Resolve delayed after-sales case", limit=1)
    finally:
        memory.close()

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT goal, keywords_json, plan_json, evidence_json "
            "FROM agent_episodes WHERE run_id = ?",
            ("privacy-run",),
        ).fetchone()
    assert row is not None
    persisted = " ".join(str(value) for value in row).casefold()
    returned = (
        written.model_dump_json() + " " + retrieved[0].model_dump_json()
    ).casefold()
    planner = DeterministicPlanner(lambda _: simple_plan())
    planner.plan(
        PlannerContext(
            goal="Resolve another delayed case",
            available_tools=[],
            retrieved_episodes=retrieved,
        )
    )
    planner_context = planner.contexts[0].model_dump_json().casefold()

    for secret in secrets:
        assert secret.casefold() not in persisted
        assert secret.casefold() not in returned
        assert secret.casefold() not in planner_context
    assert "[redacted-email]" in persisted
    assert "[redacted-phone]" in persisted
    assert "[redacted-business-id]" in persisted
    assert "[redacted-sensitive-id]" in persisted
    assert '"status": "shipped"' in persisted


def test_tool_task_contract_rejects_missing_executable_call() -> None:
    with pytest.raises(ValidationError, match="requires an executable tool call"):
        PlanTask(task_id="reason", objective="Decide what happened from memory.")


def test_provider_planner_rejects_structured_tool_free_task(tmp_path: Path) -> None:
    tool_free_plan = """{
      "plan_id": "tool-free-provider-plan",
      "goal": "Decide what happened from memory",
      "tasks": [{
        "task_id": "reason",
        "objective": "Decide what happened from memory."
      }]
    }"""
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        planner=ProviderPlanner(
            ScriptedProvider([ModelResponse(content=tool_free_plan)])
        ),
        database_name="provider-tool-free.db",
    )
    try:
        result = runtime.run("Decide what happened from memory")
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "failed"
    assert result.termination_reason == OrchestrationTermination.INVALID_PLAN
    assert result.counters.model_calls == 1
    assert result.counters.tool_calls == 0
    assert result.evidence == []


def test_executor_defense_rejects_mutated_tool_free_plan_without_success_evidence(
    tmp_path: Path,
) -> None:
    plan = simple_plan()
    plan.tasks[0].tool_call = None
    plan.tasks[0].required_tools = []
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        plans=[plan],
        database_name="tool-free.db",
    )
    try:
        result = runtime.run("Inspect the order safely")
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "failed"
    assert result.termination_reason == OrchestrationTermination.INVALID_PLAN
    assert result.counters.tool_calls == 0
    assert result.evidence == []


def test_provider_planner_retries_transient_error_then_validates_structured_plan(
    tmp_path: Path,
) -> None:
    plan = simple_plan()
    provider = ScriptedProvider(
        [
            ProviderError("temporary", retryable=True, kind="provider_timeout"),
            ModelResponse(content=plan.model_dump_json()),
        ]
    )
    config = OrchestrationConfig(
        reliability=ReliabilityConfig(
            max_model_calls=3,
            provider_retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
    )
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        planner=ProviderPlanner(provider),
        config=config,
        database_name="provider.db",
    )
    try:
        result = runtime.run("Inspect the order safely")
        span_names = [span.name for span in tracing.finished_spans()]
    finally:
        tracing.shutdown()
        memory.close()

    assert result.status == "completed"
    assert result.counters.model_calls == 2
    assert result.counters.retries == 1
    assert result.counters.failures == 1
    assert len(provider.requests) == 2
    assert provider.requests[0]["tools"] == []
    assert "reliability.retry" in span_names


def test_provider_model_budget_blocks_retry(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [ProviderError("temporary", retryable=True, kind="provider_timeout")]
    )
    config = OrchestrationConfig(
        reliability=ReliabilityConfig(
            max_model_calls=1,
            provider_retry=RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
        )
    )
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        planner=ProviderPlanner(provider),
        config=config,
        database_name="model-budget.db",
    )
    try:
        result = runtime.run("Inspect the order safely")
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == OrchestrationTermination.MODEL_CALL_BUDGET_EXHAUSTED
    assert result.counters.model_calls == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("provider_item", "expected"),
    [
        (ModelResponse(content="{}"), OrchestrationTermination.INVALID_PLAN),
        (
            ProviderError("permanent", retryable=False),
            OrchestrationTermination.PROVIDER_FAILURE,
        ),
    ],
)
def test_invalid_structured_plan_and_permanent_provider_failure_are_typed(
    tmp_path: Path, provider_item: Any, expected: OrchestrationTermination
) -> None:
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path,
        planner=ProviderPlanner(ScriptedProvider([provider_item])),
        database_name=f"{expected.value}.db",
    )
    try:
        result = runtime.run("Inspect the order safely")
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == expected
    assert result.counters.model_calls == 1


def test_reviewer_failure_is_typed_and_not_exposed(tmp_path: Path) -> None:
    url = prepare_database(tmp_path / "reviewer.db")
    tracing = create_in_memory_tracing("reviewer")
    memory = EpisodicMemoryStore(f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    runtime = MultiAgentOrchestrator(
        planner=ScriptedPlanner([simple_plan()]),
        tools=business_tools(url, tracing),
        memory_store=memory,
        reviewer=BrokenReviewer(),
        tracer=tracing.tracer,
        run_id_factory=lambda: "reviewer",
    )
    try:
        result = runtime.run("Inspect the order safely")
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == OrchestrationTermination.REVIEWER_FAILURE
    assert "reviewer unavailable" not in result.final_response


def test_unsupported_pending_completion_claim_is_rejected(tmp_path: Path) -> None:
    runtime, url, memory, tracing, _ = orchestrator(
        tmp_path,
        amount=Decimal("1500.00"),
        completion_builder=lambda _: "Refund completed and ticket created.",
        database_name="unsupported.db",
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == OrchestrationTermination.UNSUPPORTED_COMPLETION_CLAIM
    assert result.reviews[-1].failure_type == "unsupported_completion_claim"
    assert business_counts(url) == (1, 1, "pending_human_approval")


def test_non_idempotent_side_effect_is_not_replayed_after_ambiguous_result(
    tmp_path: Path,
) -> None:
    url = prepare_database(tmp_path / "side-effect.db")
    tracing = create_in_memory_tracing("side-effect")
    wrapped = AmbiguousTicketTools(business_tools(url, tracing))
    memory = EpisodicMemoryStore(f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    runtime = MultiAgentOrchestrator(
        planner=ScriptedPlanner([build_delayed_order_plan("side-effect")]),
        tools=wrapped,
        memory_store=memory,
        tracer=tracing.tracer,
        sleeper=lambda _: None,
        run_id_factory=lambda: "side-effect",
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
    finally:
        tracing.shutdown()
        memory.close()
    assert result.termination_reason == OrchestrationTermination.UNSAFE_RETRY_BLOCKED
    assert wrapped.ticket_calls == 1
    assert business_counts(url) == (1, 1, "approved")


def trace_text(spans: tuple[ReadableSpan, ...]) -> str:
    parts: list[str] = []
    for span in spans:
        parts.append(span.name)
        parts.extend(
            f"{key}={value}" for key, value in (span.attributes or {}).items()
        )
        parts.extend(event.name for event in span.events)
    return " ".join(parts)


def test_orchestration_traces_are_payload_safe_and_reconstructable(tmp_path: Path) -> None:
    runtime, _, memory, tracing, _ = orchestrator(
        tmp_path, database_name="tracing.db"
    )
    try:
        result = runtime.run(DELAYED_ORDER_GOAL)
        spans = tracing.finished_spans()
        text = trace_text(spans)
    finally:
        tracing.shutdown()
        memory.close()

    root = next(span for span in spans if span.name == "multi_agent.run")
    assert root.attributes["agent.termination.reason"] == "completed"
    assert root.attributes["orchestration.steps"] == result.counters.orchestration_steps
    for required in (
        "memory.retrieve",
        "planner.plan",
        "scheduler.select",
        "executor.task",
        "reviewer.review",
        "memory.write",
    ):
        assert required in [span.name for span in spans]
    by_id = {span.context.span_id: span for span in spans}
    mcp_call = next(span for span in spans if span.name == "mcp.call")
    assert mcp_call.parent is not None
    assert by_id[mcp_call.parent.span_id].name == "executor.task"
    for sensitive in (
        "ORD-1024",
        "CUS-001",
        "li.wei@example.com",
        "tracing-refund",
        "Shipment remains materially delayed",
    ):
        assert sensitive not in text
