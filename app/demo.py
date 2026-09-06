from __future__ import annotations

import argparse
import logging
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.evals import EvalRunner, load_eval_suite
from app.evals.models import ObservedBusinessState


DEMO_CASE_IDS = (
    "delayed-order-resolution",
    "provider-transient-recovery",
    "high-value-human-approval",
)


class DemoCounters(BaseModel):
    model_config = ConfigDict(frozen=True)

    steps: int
    model_calls: int
    tool_calls: int
    retries: int
    failures: int


class DemoScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    name: str
    passed: bool
    agent_status: str
    termination_reason: str
    final_response: str
    loaded_skills: list[str]
    tool_sequence: list[str]
    business_state: ObservedBusinessState
    counters: DemoCounters
    trace_span_counts: dict[str, int]
    checks_passed: int
    checks_total: int


class DemoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: Literal["deterministic_offline"] = "deterministic_offline"
    components: list[str]
    scenarios: list[DemoScenario]
    scenarios_passed: int
    scenarios_total: int
    regression_gate_passed: bool


def run_demo() -> DemoResult:
    """Run curated end-to-end cases through the real deterministic eval path."""
    evaluated = EvalRunner(load_eval_suite()).evaluate(list(DEMO_CASE_IDS))
    scenarios = [
        DemoScenario(
            case_id=case.case_id,
            name=case.name,
            passed=case.passed,
            agent_status=case.status,
            termination_reason=case.termination_reason or "completed",
            final_response=case.response,
            loaded_skills=case.loaded_skills,
            tool_sequence=case.tool_sequence,
            business_state=case.business_state,
            counters=DemoCounters(
                steps=case.steps,
                model_calls=case.model_calls,
                tool_calls=case.tool_calls,
                retries=case.retries,
                failures=case.failures,
            ),
            trace_span_counts=dict(sorted(Counter(case.trace_spans).items())),
            checks_passed=sum(check.passed for check in case.checks),
            checks_total=len(case.checks),
        )
        for case in evaluated.cases
    ]
    return DemoResult(
        components=[
            "AgentRuntime",
            "ScriptedProvider",
            "Agent Skills",
            "MCP discovery and tool calls",
            "reliability controls",
            "SQLite business services",
            "OpenTelemetry in-memory tracing",
            "deterministic regression evaluation",
        ],
        scenarios=scenarios,
        scenarios_passed=sum(scenario.passed for scenario in scenarios),
        scenarios_total=len(scenarios),
        regression_gate_passed=evaluated.regression_gate.passed,
    )


def render_demo(result: DemoResult) -> str:
    lines = [
        "Enterprise Agent Reliability Lab - deterministic end-to-end demo",
        "Network/API keys/external benchmark installation: not required",
        "Execution path: " + " -> ".join(result.components),
        "",
    ]
    for scenario in result.scenarios:
        state = scenario.business_state
        skill_names = ", ".join(scenario.loaded_skills) or "none"
        trace_summary = ", ".join(
            f"{name}={count}" for name, count in scenario.trace_span_counts.items()
        )
        lines.extend(
            [
                f"[{'PASS' if scenario.passed else 'FAIL'}] {scenario.name}",
                (
                    f"  Agent: status={scenario.agent_status}, "
                    f"termination={scenario.termination_reason}"
                ),
                f"  Skill: {skill_names}",
                f"  Tools: {' -> '.join(scenario.tool_sequence) or 'none'}",
                (
                    "  Business state: "
                    f"refund={state.refund_status or 'none'}, "
                    f"refund_completed={state.refund_completed}, "
                    f"refunds={state.refund_count}, tickets={state.ticket_count}"
                ),
                (
                    "  Reliability: "
                    f"steps={scenario.counters.steps}, "
                    f"model_calls={scenario.counters.model_calls}, "
                    f"tool_calls={scenario.counters.tool_calls}, "
                    f"retries={scenario.counters.retries}, "
                    f"failures={scenario.counters.failures}"
                ),
                f"  Trace spans: {trace_summary}",
                (
                    f"  Evaluation: {scenario.checks_passed}/"
                    f"{scenario.checks_total} checks passed"
                ),
                f"  Final response: {scenario.final_response}",
                "",
            ]
        )
    lines.append(
        "Regression gate: "
        f"{'PASS' if result.regression_gate_passed else 'FAIL'} "
        f"({result.scenarios_passed}/{result.scenarios_total} scenarios passed)"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic final enterprise Agent demonstration"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    result = run_demo()
    if args.format == "json":
        print(result.model_dump_json(indent=2))
    else:
        print(render_demo(result))
    return 0 if result.regression_gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
