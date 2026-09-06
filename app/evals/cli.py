from __future__ import annotations

import argparse
import logging

from app.evals.loader import DEFAULT_CASES_PATH, load_eval_suite
from app.evals.report import render_human_report
from app.evals.runner import EvalRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic after-sales Agent evals")
    parser.add_argument("--cases-file", default=str(DEFAULT_CASES_PATH))
    parser.add_argument(
        "--case",
        dest="case_ids",
        action="append",
        help="Evaluate one case id; repeat to select multiple cases",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    result = EvalRunner(load_eval_suite(args.cases_file)).evaluate(args.case_ids)
    if args.format == "json":
        print(result.model_dump_json(indent=2))
    else:
        print(render_human_report(result))
    raise SystemExit(0 if result.regression_gate.passed else 1)


if __name__ == "__main__":
    main()
