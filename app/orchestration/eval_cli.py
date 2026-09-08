from __future__ import annotations

import argparse

from app.orchestration.evals import OrchestrationEvalRunner, load_orchestration_eval_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic multi-agent evals")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = OrchestrationEvalRunner(load_orchestration_eval_suite()).evaluate(args.cases)
    if args.format == "json":
        print(result.model_dump_json(indent=2))
    else:
        for case in result.cases:
            print(
                f"[{'PASS' if case.passed else 'FAIL'}] {case.case_id}: "
                f"termination={case.termination_reason}, steps={case.orchestration_steps}, "
                f"tools={case.tool_calls}, replans={case.replans}, "
                f"reflections={case.reflections}, latency_ms={case.latency_ms:.3f}"
            )
        aggregate = result.aggregate
        print(
            f"Regression gate: {'PASS' if result.regression_gate_passed else 'FAIL'} "
            f"({aggregate.passed_cases}/{aggregate.total_cases} cases passed)"
        )
    return 0 if result.regression_gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
