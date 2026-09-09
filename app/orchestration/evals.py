from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select

from app.database import Database
from app.evals.models import EvalCheck, MetricAggregate, ObservedBusinessState
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.orchestration.graph import MultiAgentOrchestrator
from app.orchestration.memory import EpisodicMemoryStore
from app.orchestration.models import OrchestrationConfig, OrchestrationResult
from app.orchestration.planner import ScriptedPlanner
from app.orchestration.scenarios import build_delayed_order_plan
from app.orchestration.scheduler import DependencyScheduler, SchedulerError
from app.seed import seed_demo_data
from app.skills import SkillAwareTools, SkillRegistry
from app.tracing import create_in_memory_tracing


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = ROOT / "evals" / "orchestration_cases.yaml"
SKILL_ROOT = ROOT / ".agents" / "skills"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrchestrationEvalSetup(EvalModel):
    order_amount: Decimal = Field(gt=0)


class OrchestrationExpected(EvalModel):
    status: Literal["completed", "failed"]
    termination_reason: str
    refund_status: str | None
    refund_completed: bool | None
    refund_count: int = Field(ge=0)
    ticket_count: int = Field(ge=0)
    reflections: int = Field(ge=0)
    replans: int = Field(ge=0)
    required_tools: list[str]


class OrchestrationEvalCase(EvalModel):
    schema_version: Literal["1.0"]
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    goal: str
    setup: OrchestrationEvalSetup
    fault: Literal["none", "transient_shipping", "persistent_shipping"] = "none"
    reliability: dict[str, Any] = Field(default_factory=dict)
    expected: OrchestrationExpected


class OrchestrationThresholds(EvalModel):
    minimum_case_pass_rate: float = Field(default=1.0, ge=0, le=1)
    minimum_metric_pass_rate: float = Field(default=1.0, ge=0, le=1)
    maximum_failed_cases: int = Field(default=0, ge=0)


class OrchestrationEvalSuite(EvalModel):
    schema_version: Literal["1.0"]
    suite_id: str
    thresholds: OrchestrationThresholds
    cases: list[OrchestrationEvalCase]

    @model_validator(mode="after")
    def unique_case_ids(self) -> OrchestrationEvalSuite:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Orchestration eval case ids must be unique")
        return self


class OrchestrationCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    name: str
    passed: bool
    checks: list[EvalCheck]
    status: str
    termination_reason: str
    business_state: ObservedBusinessState
    tool_sequence: list[str]
    task_execution_order: list[str]
    reflections: int
    replans: int
    orchestration_steps: int
    tool_calls: int
    model_calls: int
    latency_ms: float


class OrchestrationAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_cases: int
    passed_cases: int
    failed_cases: int
    case_success_rate: float
    metrics: dict[str, MetricAggregate]
    total_orchestration_steps: int
    total_tool_calls: int
    total_model_calls: int
    total_replans: int
    total_reflections: int
    measured_latency_ms: float


class OrchestrationEvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    cases: list[OrchestrationCaseResult]
    aggregate: OrchestrationAggregate
    regression_gate_passed: bool
    regression_violations: list[str]


class _FaultTools:
    def __init__(self, delegate: SkillAwareTools, fault: str) -> None:
        self.delegate = delegate
        self.fault = fault
        self.failed = False

    def set_tracer(self, tracer: Any) -> None:
        self.delegate.set_tracer(tracer)

    def prepare(self) -> None:
        self.delegate.prepare()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        should_fail = name == "get_shipping" and (
            self.fault == "persistent_shipping"
            or (self.fault == "transient_shipping" and not self.failed)
        )
        if should_fail:
            self.failed = True
            return {
                "ok": False,
                "error": {"type": "temporary_transport", "retryable": True},
            }
        return self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)


def load_orchestration_eval_suite(
    path: str | Path = DEFAULT_SUITE,
) -> OrchestrationEvalSuite:
    with Path(path).open("r", encoding="utf-8") as stream:
        payload = yaml.safe_load(stream)
    return OrchestrationEvalSuite.model_validate(payload)


class OrchestrationEvalRunner:
    def __init__(self, suite: OrchestrationEvalSuite) -> None:
        self.suite = suite

    def evaluate(self, case_ids: list[str] | None = None) -> OrchestrationEvalResult:
        selected = self.suite.cases
        if case_ids:
            requested = set(case_ids)
            selected = [case for case in selected if case.id in requested]
            missing = requested - {case.id for case in selected}
            if missing:
                raise ValueError(f"Unknown orchestration eval cases: {', '.join(sorted(missing))}")
        results = [self.evaluate_case(case) for case in selected]
        aggregate = _aggregate(results)
        violations = _gate(aggregate, self.suite.thresholds)
        return OrchestrationEvalResult(
            suite_id=self.suite.suite_id,
            cases=results,
            aggregate=aggregate,
            regression_gate_passed=not violations,
            regression_violations=violations,
        )

    def evaluate_case(self, case: OrchestrationEvalCase) -> OrchestrationCaseResult:
        with TemporaryDirectory(prefix=f"orchestration-eval-{case.id}-") as directory:
            root = Path(directory)
            database_url = f"sqlite:///{(root / 'business.db').as_posix()}"
            database = Database(database_url)
            database.create_schema()
            with database.session_factory() as session:
                seed_demo_data(session)
                order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
                if order is None:
                    raise RuntimeError("Seed order is unavailable")
                order.amount = case.setup.order_amount
                session.commit()
            database.dispose()

            tracing = create_in_memory_tracing(case.id)
            memory = EpisodicMemoryStore(
                f"sqlite:///{(root / 'memory.db').as_posix()}"
            )
            base_tools = SkillAwareTools(
                MCPToolAdapter(create_mcp_server(database_url), tracer=tracing.tracer),
                SkillRegistry(SKILL_ROOT, tracer=tracing.tracer),
            )
            tools: Any = (
                base_tools if case.fault == "none" else _FaultTools(base_tools, case.fault)
            )
            plan_count = 2 if case.fault != "none" else 1
            plans = [
                build_delayed_order_plan(f"{case.id}-{index}")
                for index in range(1, plan_count + 1)
            ]
            config = OrchestrationConfig.model_validate(case.reliability)
            try:
                result = MultiAgentOrchestrator(
                    planner=ScriptedPlanner(plans),
                    tools=tools,
                    memory_store=memory,
                    config=config,
                    tracer=tracing.tracer,
                    sleeper=lambda _: None,
                    run_id_factory=lambda: case.id,
                ).run(case.goal)
                state = _observe(database_url)
                checks = _grade(case, result, state)
            finally:
                tracing.shutdown()
                memory.close()
        return OrchestrationCaseResult(
            case_id=case.id,
            name=case.name,
            passed=all(check.passed for check in checks),
            checks=checks,
            status=result.status,
            termination_reason=result.termination_reason.value,
            business_state=state,
            tool_sequence=result.tool_sequence,
            task_execution_order=result.task_execution_order,
            reflections=result.counters.reflections,
            replans=result.counters.replans,
            orchestration_steps=result.counters.orchestration_steps,
            tool_calls=result.counters.tool_calls,
            model_calls=result.counters.model_calls,
            latency_ms=result.latency_ms,
        )


def _observe(url: str) -> ObservedBusinessState:
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


def _check(metric: str, passed: bool, detail: str) -> EvalCheck:
    return EvalCheck(metric=metric, passed=passed, detail=detail)


def _grade(
    case: OrchestrationEvalCase,
    result: OrchestrationResult,
    state: ObservedBusinessState,
) -> list[EvalCheck]:
    expected = case.expected
    tools = set(result.tool_sequence)
    plan_valid = result.plan is not None
    dependency_valid = False
    if result.plan is not None:
        try:
            DependencyScheduler().validate(result.plan)
            dependency_valid = True
        except SchedulerError:
            dependency_valid = False
    state_matches = state.model_dump() == {
        "refund_count": expected.refund_count,
        "ticket_count": expected.ticket_count,
        "refund_status": expected.refund_status,
        "refund_completed": expected.refund_completed,
    }
    pending_accurate = expected.refund_status != "pending_human_approval" or (
        "pending human approval" in result.final_response
        and "refund completed" not in result.final_response.casefold()
    )
    recovery = expected.reflections == 0 or (
        len(result.reflections) == expected.reflections
        and result.counters.replans == expected.replans
    )
    no_unsupported_claim = not (
        state.refund_status != "approved"
        and "refund completed" in result.final_response.casefold()
    )
    return [
        _check("task_completion", result.status == expected.status, result.status),
        _check("plan_validity", plan_valid, "A structured TaskPlan was produced."),
        _check("dependency_correctness", dependency_valid, "Dependencies are valid and acyclic."),
        _check(
            "tool_selection_correctness",
            set(expected.required_tools) <= tools,
            f"Actual tool sequence: {result.tool_sequence}",
        ),
        _check(
            "tool_execution_correctness",
            result.counters.tool_calls == len(result.tool_sequence),
            "Every reported tool call has execution evidence.",
        ),
        _check("final_state_correctness", state_matches, str(state.model_dump())),
        _check("unsupported_claim_detection", no_unsupported_claim, result.final_response),
        _check("human_approval_correctness", pending_accurate, result.final_response),
        _check("reflection_recovery", recovery, str(result.counters.model_dump())),
        _check(
            "termination_reason",
            result.termination_reason.value == expected.termination_reason,
            result.termination_reason.value,
        ),
        _check("replan_count", result.counters.replans == expected.replans, str(result.counters.replans)),
        _check("reflection_count", result.counters.reflections == expected.reflections, str(result.counters.reflections)),
        _check("orchestration_step_count", result.counters.orchestration_steps > 0, str(result.counters.orchestration_steps)),
        _check("tool_call_count", result.counters.tool_calls >= 0, str(result.counters.tool_calls)),
        _check("model_call_count", result.counters.model_calls >= 0, str(result.counters.model_calls)),
        _check("latency_measured", result.latency_ms >= 0, f"{result.latency_ms:.3f}ms"),
    ]


def _aggregate(results: list[OrchestrationCaseResult]) -> OrchestrationAggregate:
    metric_values: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        for check in result.checks:
            metric_values[check.metric].append(check.passed)
    metrics = {
        name: MetricAggregate(
            passed=sum(values),
            failed=len(values) - sum(values),
            total=len(values),
            success_rate=(sum(values) / len(values) if values else 0),
        )
        for name, values in sorted(metric_values.items())
    }
    passed = sum(result.passed for result in results)
    return OrchestrationAggregate(
        total_cases=len(results),
        passed_cases=passed,
        failed_cases=len(results) - passed,
        case_success_rate=(passed / len(results) if results else 0),
        metrics=metrics,
        total_orchestration_steps=sum(item.orchestration_steps for item in results),
        total_tool_calls=sum(item.tool_calls for item in results),
        total_model_calls=sum(item.model_calls for item in results),
        total_replans=sum(item.replans for item in results),
        total_reflections=sum(item.reflections for item in results),
        measured_latency_ms=sum(item.latency_ms for item in results),
    )


def _gate(
    aggregate: OrchestrationAggregate, thresholds: OrchestrationThresholds
) -> list[str]:
    violations: list[str] = []
    if aggregate.case_success_rate < thresholds.minimum_case_pass_rate:
        violations.append("case pass rate is below threshold")
    if aggregate.failed_cases > thresholds.maximum_failed_cases:
        violations.append("failed case count exceeds threshold")
    for name, metric in aggregate.metrics.items():
        if metric.success_rate < thresholds.minimum_metric_pass_rate:
            violations.append(f"metric {name} is below threshold")
    return violations
