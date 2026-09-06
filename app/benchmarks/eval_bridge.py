from __future__ import annotations

from app.benchmarks.aggregate import summarize_results
from app.benchmarks.models import (
    BenchmarkExecutionResult,
    BenchmarkIdentity,
    BenchmarkOutcome,
    BenchmarkReport,
)
from app.evals.models import EvalSuiteResult


def report_from_local_evals(result: EvalSuiteResult) -> BenchmarkReport:
    """Project local eval output into the generic report shape without relabeling it τ³."""
    identity = BenchmarkIdentity(
        name="after-sales-local-evals",
        implementation="app.evals",
        version=result.schema_version,
        source_url="repository://evals/cases.yaml",
        license="repository-license",
    )
    normalized = [
        BenchmarkExecutionResult(
            benchmark=identity,
            domain="enterprise-after-sales",
            case_id=case.case_id,
            execution_id=f"local-eval:{case.case_id}",
            score=1.0 if case.passed else 0.0,
            passed=case.passed,
            outcome=BenchmarkOutcome.PASSED if case.passed else BenchmarkOutcome.FAILED,
            termination_reason=case.termination_reason or case.status,
            metadata={
                "steps": case.steps,
                "model_calls": case.model_calls,
                "tool_calls": case.tool_calls,
                "retries": case.retries,
                "failures": case.failures,
            },
        )
        for case in result.cases
    ]
    return BenchmarkReport(
        benchmark=identity,
        results=normalized,
        aggregate=summarize_results(normalized),
        metadata={
            "source_kind": "deterministic_local_eval",
            "suite_id": result.suite_id,
            "regression_gate_passed": result.regression_gate.passed,
        },
    )
