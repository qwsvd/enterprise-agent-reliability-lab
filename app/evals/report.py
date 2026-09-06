from __future__ import annotations

from app.evals.models import EvalSuiteResult


def render_human_report(result: EvalSuiteResult) -> str:
    aggregate = result.aggregate
    lines = [
        f"Evaluation suite: {result.suite_id} (schema {result.schema_version})",
        (
            f"Cases: {aggregate.passed_cases}/{aggregate.total_cases} passed "
            f"({aggregate.case_success_rate:.1%})"
        ),
        (
            "Execution totals: "
            f"steps={aggregate.total_steps}, model_calls={aggregate.total_model_calls}, "
            f"tool_calls={aggregate.total_tool_calls}, retries={aggregate.total_retries}, "
            f"failures={aggregate.total_failures}"
        ),
        "",
        "Cases:",
    ]
    for case in result.cases:
        failed = [check.metric for check in case.checks if not check.passed]
        suffix = "" if not failed else f"; failed metrics: {', '.join(failed)}"
        lines.append(f"- {'PASS' if case.passed else 'FAIL'} {case.case_id}{suffix}")
    lines.extend(["", "Metrics:"])
    for name, metric in result.aggregate.metrics.items():
        lines.append(
            f"- {name}: passed={metric.passed}, failed={metric.failed}, "
            f"total={metric.total} ({metric.success_rate:.1%})"
        )
    lines.extend(
        [
            "",
            f"Regression gate: {'PASS' if result.regression_gate.passed else 'FAIL'}",
        ]
    )
    lines.extend(f"- {violation}" for violation in result.regression_gate.violations)
    return "\n".join(lines)
