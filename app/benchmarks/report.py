from __future__ import annotations

from app.benchmarks.models import BenchmarkReport


def render_benchmark_report(report: BenchmarkReport) -> str:
    aggregate = report.aggregate
    pass_rate = "n/a" if aggregate.pass_rate is None else f"{aggregate.pass_rate:.1%}"
    mean_score = "n/a" if aggregate.mean_score is None else f"{aggregate.mean_score:.4f}"
    lines = [
        (
            f"Benchmark: {report.benchmark.name} {report.benchmark.version} "
            f"({report.benchmark.implementation})"
        ),
        (
            f"Runs: total={aggregate.total}, scored={aggregate.scored}, "
            f"passed={aggregate.passed}, failed={aggregate.failed}, "
            f"unscored={aggregate.unscored}, errors={aggregate.errors}"
        ),
        f"Pass rate: {pass_rate}",
        f"Mean score: {mean_score}",
        "",
        "Domains:",
    ]
    for domain, values in aggregate.by_domain.items():
        domain_rate = values["pass_rate"]
        rendered_rate = "n/a" if domain_rate is None else f"{domain_rate:.1%}"
        lines.append(
            f"- {domain}: passed={values['passed']}/{values['scored']} "
            f"(pass rate {rendered_rate})"
        )
    lines.extend(["", "Imported executions:"])
    for item in report.results:
        score = "n/a" if item.score is None else f"{item.score:.4f}"
        lines.append(
            f"- {item.execution_id}: task={item.case_id}, outcome={item.outcome.value}, "
            f"score={score}, termination={item.termination_reason or 'unknown'}"
        )
    return "\n".join(lines)
