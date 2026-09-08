from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.database import Database
from app.evals import EvalRunner, load_eval_suite
from app.evals.models import ObservedBusinessState
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.orchestration.graph import MultiAgentOrchestrator
from app.orchestration.memory import EpisodicMemoryStore
from app.orchestration.models import OrchestrationResult, PlannerContext, TaskPlan
from app.orchestration.planner import DeterministicPlanner, ScriptedPlanner
from app.orchestration.scenarios import DELAYED_ORDER_GOAL, build_delayed_order_plan
from app.seed import seed_demo_data
from app.skills import SkillAwareTools, SkillRegistry
from app.tracing import create_in_memory_tracing


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"


class ComparisonRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    architecture: Literal["single_agent", "multi_agent"]
    success: bool
    steps: int
    tool_calls: int
    model_calls: int
    replans: int
    reflections: int
    retries: int
    reliability_outcome: str
    latency_ms: float | None = None


class MultiAgentDemoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["deterministic_offline"] = "deterministic_offline"
    goal: str
    result: OrchestrationResult
    business_state: ObservedBusinessState
    generated_plan: list[dict[str, object]]
    memory_context_used: bool
    trace_spans: list[str]
    comparison: list[ComparisonRun]


def _prepare_database(url: str, amount: Decimal) -> None:
    database = Database(url)
    database.create_schema()
    with database.session_factory() as session:
        seed_demo_data(session)
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        if order is None:
            raise RuntimeError("Seed order is unavailable")
        order.amount = amount
        session.commit()
    database.dispose()


def _business_state(url: str) -> ObservedBusinessState:
    database = Database(url)
    with database.session_factory() as session:
        refund = session.scalar(select(Refund).order_by(Refund.id))
        refund_count = int(session.scalar(select(func.count()).select_from(Refund)) or 0)
        ticket_count = int(
            session.scalar(select(func.count()).select_from(SupportTicket)) or 0
        )
    database.dispose()
    status = refund.status if refund is not None else None
    return ObservedBusinessState(
        refund_count=refund_count,
        ticket_count=ticket_count,
        refund_status=status,
        refund_completed=(status == "approved" if status is not None else None),
    )


def _tools(database_url: str, tracer: object) -> SkillAwareTools:
    return SkillAwareTools(
        MCPToolAdapter(create_mcp_server(database_url), tracer=tracer),
        SkillRegistry(SKILL_ROOT, tracer=tracer),
    )


def _contextual_plan(context: PlannerContext) -> TaskPlan:
    suffix = "memory" if context.retrieved_episodes else "cold"
    return build_delayed_order_plan(f"delayed-order-{suffix}")


def run_multi_agent_demo() -> MultiAgentDemoResult:
    logging.getLogger("mcp").setLevel(logging.WARNING)
    with TemporaryDirectory(prefix="after-sales-multi-agent-") as directory:
        root = Path(directory)
        memory = EpisodicMemoryStore(f"sqlite:///{(root / 'memory.db').as_posix()}")
        warmup_db = f"sqlite:///{(root / 'warmup.db').as_posix()}"
        _prepare_database(warmup_db, Decimal("299.00"))
        warmup_trace = create_in_memory_tracing("multi-agent-warmup")
        try:
            MultiAgentOrchestrator(
                planner=ScriptedPlanner([build_delayed_order_plan("warmup")]),
                tools=_tools(warmup_db, warmup_trace.tracer),
                memory_store=memory,
                tracer=warmup_trace.tracer,
                sleeper=lambda _: None,
                run_id_factory=lambda: "multi-agent-warmup",
            ).run(DELAYED_ORDER_GOAL)
        finally:
            warmup_trace.shutdown()

        database_url = f"sqlite:///{(root / 'demo.db').as_posix()}"
        _prepare_database(database_url, Decimal("299.00"))
        tracing = create_in_memory_tracing("multi-agent-demo")
        planner = DeterministicPlanner(_contextual_plan)
        try:
            result = MultiAgentOrchestrator(
                planner=planner,
                tools=_tools(database_url, tracing.tracer),
                memory_store=memory,
                tracer=tracing.tracer,
                sleeper=lambda _: None,
                run_id_factory=lambda: "multi-agent-demo",
            ).run(DELAYED_ORDER_GOAL)
            trace_spans = [span.name for span in tracing.finished_spans()]
            state = _business_state(database_url)
        finally:
            tracing.shutdown()
            memory.close()

    single = EvalRunner(load_eval_suite()).evaluate_case(
        next(
            case
            for case in load_eval_suite().cases
            if case.id == "delayed-order-resolution"
        )
    )
    comparison = [
        ComparisonRun(
            architecture="single_agent",
            success=single.passed,
            steps=single.steps,
            tool_calls=single.tool_calls,
            model_calls=single.model_calls,
            replans=0,
            reflections=0,
            retries=single.retries,
            reliability_outcome=single.termination_reason or "completed",
        ),
        ComparisonRun(
            architecture="multi_agent",
            success=result.status == "completed",
            steps=result.counters.orchestration_steps,
            tool_calls=result.counters.tool_calls,
            model_calls=result.counters.model_calls,
            replans=result.counters.replans,
            reflections=result.counters.reflections,
            retries=result.counters.retries,
            reliability_outcome=result.termination_reason.value,
            latency_ms=result.latency_ms,
        ),
    ]
    plan = result.plan
    return MultiAgentDemoResult(
        goal=DELAYED_ORDER_GOAL,
        result=result,
        business_state=state,
        generated_plan=(
            [
                {
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "dependencies": task.dependencies,
                    "required_tools": task.required_tools,
                }
                for task in plan.tasks
            ]
            if plan is not None
            else []
        ),
        memory_context_used=bool(planner.contexts[0].retrieved_episodes),
        trace_spans=trace_spans,
        comparison=comparison,
    )


def render_demo(demo: MultiAgentDemoResult) -> str:
    result = demo.result
    lines = [
        "Enterprise Agent Reliability Lab - deterministic LangGraph multi-agent demo",
        "Network/API keys/paid model: not required",
        f"Goal: {demo.goal}",
        f"Memory episodes retrieved: {len(result.memory_retrieved)}",
        "Plan:",
    ]
    lines.extend(
        f"  {item['task_id']}: depends={item['dependencies']} tools={item['required_tools']}"
        for item in demo.generated_plan
    )
    lines.extend(
        [
            "Execution order: " + " -> ".join(result.task_execution_order),
            "Tool sequence: " + " -> ".join(result.tool_sequence),
            "Reviewer decisions: " + " -> ".join(item.decision.value for item in result.reviews),
            "Reflections: " + (
                " -> ".join(item.failure_type for item in result.reflections) or "none"
            ),
            f"Final business state: {demo.business_state.model_dump()}",
            f"Termination: {result.termination_reason.value}",
            f"Counters: {result.counters.model_dump()}",
            f"Measured latency_ms: {result.latency_ms:.3f}",
            "Token usage / cost: unavailable (deterministic provider exposes no usage)",
            f"Final response: {result.final_response}",
            "Trace spans: " + " -> ".join(demo.trace_spans),
            "Comparison:",
        ]
    )
    lines.extend(
        f"  {item.architecture}: success={item.success} steps={item.steps} "
        f"tools={item.tool_calls} replans={item.replans} reflections={item.reflections} "
        f"outcome={item.reliability_outcome}"
        for item in demo.comparison
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic LangGraph multi-agent demonstration"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    demo = run_multi_agent_demo()
    print(demo.model_dump_json(indent=2) if args.format == "json" else render_demo(demo))
    return 0 if demo.result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
