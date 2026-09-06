from __future__ import annotations

from collections import defaultdict

from app.benchmarks.models import (
    BenchmarkAggregate,
    BenchmarkExecutionResult,
    BenchmarkOutcome,
)


def summarize_results(results: list[BenchmarkExecutionResult]) -> BenchmarkAggregate:
    """Aggregate normalized results from any benchmark adapter."""
    scored_results = [item for item in results if item.score is not None]
    by_domain_raw: dict[str, list[BenchmarkExecutionResult]] = defaultdict(list)
    for item in results:
        by_domain_raw[item.domain].append(item)

    by_domain: dict[str, dict[str, int | float | None]] = {}
    for domain, items in sorted(by_domain_raw.items()):
        domain_scored = [item for item in items if item.score is not None]
        passed = sum(item.passed is True for item in domain_scored)
        by_domain[domain] = {
            "total": len(items),
            "scored": len(domain_scored),
            "passed": passed,
            "failed": len(domain_scored) - passed,
            "pass_rate": passed / len(domain_scored) if domain_scored else None,
            "mean_score": (
                sum(item.score or 0 for item in domain_scored) / len(domain_scored)
                if domain_scored
                else None
            ),
        }

    passed = sum(item.passed is True for item in scored_results)
    return BenchmarkAggregate(
        total=len(results),
        scored=len(scored_results),
        passed=passed,
        failed=len(scored_results) - passed,
        unscored=sum(item.outcome == BenchmarkOutcome.UNSCORED for item in results),
        errors=sum(item.outcome == BenchmarkOutcome.ERROR for item in results),
        pass_rate=passed / len(scored_results) if scored_results else None,
        mean_score=(
            sum(item.score or 0 for item in scored_results) / len(scored_results)
            if scored_results
            else None
        ),
        by_domain=by_domain,
    )
