from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from sqlalchemy import func, select

from app.agent.providers import LLMProvider, ProviderError, ScriptedProvider
from app.agent.runtime import AgentRuntime, AgentTools
from app.agent.types import AgentResult, ModelResponse, ToolCall
from app.database import Database
from app.evals.loader import select_eval_cases
from app.evals.models import (
    AggregateMetrics,
    EvalCase,
    EvalCaseResult,
    EvalCheck,
    EvalSuiteDefinition,
    EvalSuiteResult,
    MetricAggregate,
    ModelTurn,
    ObservedBusinessState,
    ProviderErrorTurn,
    RegressionGateResult,
)
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.reliability import ReliabilityConfig
from app.seed import seed_demo_data
from app.skills.loader import SkillRegistry
from app.skills.tools import SkillAwareTools
from app.tracing import create_in_memory_tracing


SKILL_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
ProviderFactory = Callable[[EvalCase], LLMProvider]


class PreparedTools(AgentTools, Protocol):
    def prepare(self) -> None: ...

    def context(self) -> str: ...

    def set_tracer(self, tracer: Any) -> None: ...


class AmbiguousSupportTicketTools:
    """Lose one successful ticket result to exercise the existing replay guard."""

    def __init__(self, delegate: PreparedTools) -> None:
        self.delegate = delegate
        self.injected = False

    def set_tracer(self, tracer: Any) -> None:
        self.delegate.set_tracer(tracer)

    def prepare(self) -> None:
        self.delegate.prepare()

    def context(self) -> str:
        return self.delegate.context()

    def schemas(self) -> list[dict[str, Any]]:
        return self.delegate.schemas()

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        result = self.delegate.execute(name, arguments, timeout_seconds=timeout_seconds)
        if name == "create_support_ticket" and result.get("ok") is True and not self.injected:
            self.injected = True
            return {
                "ok": False,
                "error": {
                    "type": "tool_timeout",
                    "message": "The result was lost after the operation may have committed",
                    "retryable": True,
                    "ambiguous": True,
                },
            }
        return result


def scripted_provider(case: EvalCase) -> ScriptedProvider:
    responses: list[ModelResponse | ProviderError] = []
    for turn in case.script:
        if isinstance(turn, ProviderErrorTurn):
            responses.append(
                ProviderError(
                    turn.message, retryable=turn.retryable, kind=turn.kind
                )
            )
        elif isinstance(turn, ModelTurn):
            responses.append(
                ModelResponse(
                    content=turn.content,
                    tool_calls=[
                        ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                        for call in turn.tool_calls
                    ],
                )
            )
        else:  # pragma: no cover - the discriminated model prevents this
            raise TypeError(f"Unsupported scripted turn: {type(turn).__name__}")
    return ScriptedProvider(responses)


class EvalRunner:
    """Execute and grade deterministic cases through Skill, MCP, reliability, and tracing."""

    def __init__(
        self,
        suite: EvalSuiteDefinition,
        *,
        provider_factory: ProviderFactory = scripted_provider,
        skill_root: str | Path = SKILL_ROOT,
    ) -> None:
        self.suite = suite
        self.provider_factory = provider_factory
        self.skill_root = Path(skill_root)

    def evaluate(
        self, case_ids: list[str] | tuple[str, ...] | None = None
    ) -> EvalSuiteResult:
        cases = select_eval_cases(self.suite, case_ids)
        results = [self.evaluate_case(case) for case in cases]
        aggregate = aggregate_results(results)
        gate = evaluate_regression_gate(aggregate, self.suite)
        return EvalSuiteResult(
            suite_id=self.suite.suite_id,
            selected_case_ids=[case.id for case in cases],
            cases=results,
            aggregate=aggregate,
            regression_gate=gate,
        )

    def evaluate_case(self, case: EvalCase) -> EvalCaseResult:
        tracing = create_in_memory_tracing(f"eval-{case.id}")
        sleeps: list[float] = []
        try:
            with TemporaryDirectory(prefix=f"after-sales-eval-{case.id}-") as directory:
                database_url = f"sqlite:///{(Path(directory) / 'eval.db').as_posix()}"
                self._prepare_business_state(database_url, case)
                registry = SkillRegistry(self.skill_root)
                skill_tools = SkillAwareTools(
                    MCPToolAdapter(create_mcp_server(database_url)), registry
                )
                tools: AgentTools = skill_tools
                if case.fault.type == "ambiguous_support_ticket_after_success":
                    tools = AmbiguousSupportTicketTools(skill_tools)
                reliability = ReliabilityConfig.model_validate(case.reliability)
                result = AgentRuntime(
                    self.provider_factory(case),
                    tools,
                    reliability=reliability,
                    sleeper=sleeps.append,
                    tracer=tracing.tracer,
                    run_id_factory=lambda: f"eval-{case.id}",
                ).run(case.task)
                state = self._observe_business_state(database_url)
                span_names = [span.name for span in tracing.finished_spans()]
                loaded_skills = list(registry.loaded_names)
                checks = grade_case(case, result, state, loaded_skills, span_names)
                return EvalCaseResult(
                    case_id=case.id,
                    name=case.name,
                    passed=all(check.passed for check in checks),
                    checks=checks,
                    status=result.status,
                    termination_reason=(
                        result.termination_reason.value
                        if result.termination_reason is not None
                        else None
                    ),
                    response=result.response,
                    tool_sequence=[event.call.name for event in result.tool_events],
                    loaded_skills=loaded_skills,
                    trace_spans=span_names,
                    business_state=state,
                    steps=result.steps,
                    model_calls=result.model_calls,
                    tool_calls=result.tool_calls,
                    retries=result.retries,
                    failures=result.failures,
                )
        finally:
            tracing.shutdown()

    @staticmethod
    def _prepare_business_state(database_url: str, case: EvalCase) -> None:
        database = Database(database_url)
        database.create_schema()
        with database.session_factory() as session:
            seed_demo_data(session)
            if case.setup.order_amount is not None:
                order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
                if order is None:
                    raise RuntimeError("Seed order ORD-1024 is unavailable")
                order.amount = case.setup.order_amount
                session.commit()
        database.dispose()

    @staticmethod
    def _observe_business_state(database_url: str) -> ObservedBusinessState:
        database = Database(database_url)
        with database.session_factory() as session:
            refund_count = int(session.scalar(select(func.count()).select_from(Refund)) or 0)
            ticket_count = int(
                session.scalar(select(func.count()).select_from(SupportTicket)) or 0
            )
            refund = session.scalar(select(Refund).order_by(Refund.id))
            refund_status = refund.status if refund is not None else None
        database.dispose()
        return ObservedBusinessState(
            refund_count=refund_count,
            ticket_count=ticket_count,
            refund_status=refund_status,
            refund_completed=(refund_status == "approved" if refund_status else None),
        )


def _check(metric: str, passed: bool, detail: str) -> EvalCheck:
    return EvalCheck(metric=metric, passed=passed, detail=detail)


def grade_case(
    case: EvalCase,
    result: AgentResult,
    state: ObservedBusinessState,
    loaded_skills: list[str],
    span_names: list[str],
) -> list[EvalCheck]:
    expected = case.expected
    expected_reason = (
        expected.termination_reason.value
        if expected.termination_reason is not None
        else None
    )
    actual_reason = (
        result.termination_reason.value if result.termination_reason is not None else None
    )
    actual_sequence = [event.call.name for event in result.tool_events]
    actual_calls = [
        {"name": event.call.name, "arguments": event.call.arguments}
        for event in result.tool_events
    ]
    expected_calls = [item.model_dump() for item in expected.tool_calls]
    response = result.response.casefold()
    includes = all(value.casefold() in response for value in expected.response_contains)
    excludes = all(value.casefold() not in response for value in expected.response_excludes)
    actual_names = set(actual_sequence)
    required_names = set(expected.required_tools)
    unexpected = actual_names - required_names
    forbidden = actual_names & set(expected.forbidden_tools)

    expected_state = expected.business_state
    state_matches = (
        state.refund_count == expected_state.refund_count
        and state.ticket_count == expected_state.ticket_count
        and state.refund_status == expected_state.refund_status
        and state.refund_completed == expected_state.refund_completed
    )
    refund_results = [
        event.result.get("data", {})
        for event in result.tool_events
        if event.call.name == "create_refund" and event.result.get("ok") is True
    ]
    refund_matches = state.refund_count == expected_state.refund_count
    if expected_state.refund_count:
        refund_matches = refund_matches and len(refund_results) == 1 and all(
            item.get("status") == expected_state.refund_status
            and item.get("completed") is expected_state.refund_completed
            for item in refund_results
        )
    else:
        refund_matches = refund_matches and not refund_results

    completed_claims = (
        "refund completed",
        "refund was approved",
        "refund was issued",
        "refund issued",
        "approved refund",
    )
    completed_claim = any(claim in response for claim in completed_claims)
    ticket_claim = any(
        claim in response
        for claim in ("ticket was opened", "ticket opened", "ticket was created", "ticket created")
    )
    pending_correct = True
    if expected_state.refund_status == "pending_human_approval":
        pending_correct = (
            state.refund_completed is False
            and "pending human approval" in response
            and not completed_claim
        )
    elif state.refund_status == "pending_human_approval":
        pending_correct = False

    unsupported_claim_free = excludes
    if completed_claim and state.refund_status != "approved":
        unsupported_claim_free = False
    if ticket_claim and state.ticket_count == 0:
        unsupported_claim_free = False

    reliability_matches = (
        result.retries == expected.counters.retries
        and result.failures == expected.counters.failures
        and len(result.failure_details) == result.failures
        and span_names.count("reliability.retry") == result.retries
    )
    trace_matches = all(name in span_names for name in expected.required_trace_spans)
    termination_matches = actual_reason == expected_reason
    status_matches = result.status == expected.status

    return [
        _check(
            "task_success",
            status_matches and termination_matches and state_matches,
            "Observed status, termination, and business state match the case objective.",
        ),
        _check(
            "final_outcome_correctness",
            includes and excludes,
            "The final response contains required outcome statements and excludes forbidden ones.",
        ),
        _check(
            "required_tool_usage",
            required_names <= actual_names,
            f"Required={sorted(required_names)} actual={actual_sequence}",
        ),
        _check(
            "forbidden_or_unnecessary_tool_usage",
            not forbidden and not unexpected,
            f"Forbidden={sorted(forbidden)} unexpected={sorted(unexpected)}",
        ),
        _check(
            "tool_call_sequence_correctness",
            actual_sequence == expected.tool_sequence,
            f"Expected={expected.tool_sequence} actual={actual_sequence}",
        ),
        _check(
            "structured_argument_correctness",
            actual_calls == expected_calls,
            "Executed tool names and structured arguments match the versioned case.",
        ),
        _check(
            "business_rule_preservation",
            state_matches,
            f"Expected={expected_state.model_dump()} actual={state.model_dump()}",
        ),
        _check(
            "refund_correctness",
            refund_matches,
            "Persisted refund state and structured refund result agree.",
        ),
        _check(
            "human_approval_correctness",
            pending_correct,
            "Pending approval is distinct from a completed refund.",
        ),
        _check(
            "side_effect_safety",
            state_matches and result.retries == expected.counters.retries,
            "Persisted side-effect counts and retry behavior match the safety expectation.",
        ),
        _check(
            "retry_reliability_behavior",
            reliability_matches,
            "Retries, failures, failure records, and retry spans are consistent.",
        ),
        _check(
            "termination_reason_correctness",
            termination_matches,
            f"Expected={expected_reason} actual={actual_reason}",
        ),
        _check(
            "unsupported_claim_risk",
            unsupported_claim_free,
            "No configured or state-contradicting completion claim was observed.",
        ),
        _check(
            "skill_usage",
            loaded_skills == expected.loaded_skills,
            f"Expected={expected.loaded_skills} actual={loaded_skills}",
        ),
        _check(
            "trace_coverage",
            trace_matches,
            f"Required trace spans={expected.required_trace_spans}",
        ),
        _check("step_count", result.steps == expected.counters.steps, f"Actual={result.steps}"),
        _check(
            "model_call_count",
            result.model_calls == expected.counters.model_calls,
            f"Actual={result.model_calls}",
        ),
        _check(
            "tool_call_count",
            result.tool_calls == expected.counters.tool_calls,
            f"Actual={result.tool_calls}",
        ),
        _check("retry_count", result.retries == expected.counters.retries, f"Actual={result.retries}"),
        _check("failure_count", result.failures == expected.counters.failures, f"Actual={result.failures}"),
    ]


def aggregate_results(results: list[EvalCaseResult]) -> AggregateMetrics:
    metric_totals: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        for check in result.checks:
            metric_totals[check.metric].append(check.passed)
    metrics = {
        name: MetricAggregate(
            passed=sum(values),
            failed=len(values) - sum(values),
            total=len(values),
            success_rate=(sum(values) / len(values) if values else 0.0),
        )
        for name, values in sorted(metric_totals.items())
    }
    passed_cases = sum(result.passed for result in results)
    total_cases = len(results)
    return AggregateMetrics(
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=total_cases - passed_cases,
        case_success_rate=(passed_cases / total_cases if total_cases else 0.0),
        metrics=metrics,
        total_steps=sum(result.steps for result in results),
        total_model_calls=sum(result.model_calls for result in results),
        total_tool_calls=sum(result.tool_calls for result in results),
        total_retries=sum(result.retries for result in results),
        total_failures=sum(result.failures for result in results),
    )


def evaluate_regression_gate(
    aggregate: AggregateMetrics, suite: EvalSuiteDefinition
) -> RegressionGateResult:
    thresholds = suite.thresholds
    violations: list[str] = []
    if aggregate.case_success_rate < thresholds.minimum_case_pass_rate:
        violations.append(
            f"case pass rate {aggregate.case_success_rate:.3f} is below "
            f"{thresholds.minimum_case_pass_rate:.3f}"
        )
    if aggregate.failed_cases > thresholds.maximum_failed_cases:
        violations.append(
            f"failed cases {aggregate.failed_cases} exceeds {thresholds.maximum_failed_cases}"
        )
    for name, metric in aggregate.metrics.items():
        if metric.success_rate < thresholds.minimum_metric_pass_rate:
            violations.append(
                f"metric {name} rate {metric.success_rate:.3f} is below "
                f"{thresholds.minimum_metric_pass_rate:.3f}"
            )
    return RegressionGateResult(passed=not violations, violations=violations)
