from __future__ import annotations

import argparse
import sys
from typing import Any

from app.benchmarks.base import BenchmarkIntegrationError
from app.benchmarks.provider import ExternalProcessBenchmarkProvider
from app.benchmarks.report import render_benchmark_report
from app.benchmarks.serialization import canonical_json, load_json
from app.benchmarks.tau3 import (
    SUPPORTED_TAU3_VERSIONS,
    TAU3_CONTRACT_SCHEMA,
    TAU3_EXTERNAL_PYTHON,
    Tau3BenchmarkAdapter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and import external Agent benchmark contracts"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser(
        "inspect", help="Inspect tau3-bench adapter/provider availability"
    )
    _format_argument(inspect_parser)

    for name, help_text in (
        ("validate-task", "Validate and normalize a tau3-bench task contract"),
        (
            "validate-results",
            "Validate a tau3-bench Results contract or raw Results file",
        ),
        ("summarize", "Import and summarize external tau3-bench Results"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("path")
        sub.add_argument(
            "--benchmark-version",
            help="Required for raw upstream files; contract files carry their own version",
        )
        sub.add_argument("--domain", help="Required for a raw task; optional Results cross-check")
        _format_argument(sub)

    command_parser = commands.add_parser(
        "command", help="Render (but do not execute) an official external tau2 run command"
    )
    command_parser.add_argument("--domain", required=True)
    command_parser.add_argument("--agent", required=True)
    command_parser.add_argument("--agent-llm", required=True)
    command_parser.add_argument("--user-llm", required=True)
    command_parser.add_argument("--save-to", required=True)
    command_parser.add_argument("--task-id", action="append", dest="task_ids")
    _format_argument(command_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            adapter = Tau3BenchmarkAdapter()
            availability = _provider(adapter).inspect()
            payload = {
                "adapter": {
                    "benchmark": adapter.identity.model_dump(mode="json"),
                    "contract_schema": TAU3_CONTRACT_SCHEMA,
                    "supported_versions": SUPPORTED_TAU3_VERSIONS,
                    "external_python_requirement": TAU3_EXTERNAL_PYTHON,
                },
                "provider": availability.model_dump(mode="json"),
            }
            _emit(payload, args.format, "Available" if availability.available else "Unavailable")
            return 0

        if args.command == "command":
            adapter = Tau3BenchmarkAdapter()
            request = adapter.build_run_request(
                domain=args.domain,
                agent=args.agent,
                agent_llm=args.agent_llm,
                user_llm=args.user_llm,
                save_to=args.save_to,
                task_ids=args.task_ids,
            )
            payload = {
                "executed": False,
                "argv": [request.executable, *request.arguments],
                "note": "Run this only in a separately installed compatible tau3-bench environment.",
            }
            _emit(payload, args.format, " ".join(payload["argv"]))
            return 0

        document = load_json(args.path)
        adapter, wrapped = _adapter_for_document(document, args.benchmark_version)
        if args.command == "validate-task":
            if wrapped:
                case = adapter.convert_task(document)
            else:
                if not isinstance(document, dict) or not args.domain:
                    raise BenchmarkIntegrationError(
                        "missing_context", "Raw task validation requires --domain"
                    )
                case = adapter.convert_official_task(document, domain=args.domain)
            _emit(case, args.format, f"Valid tau3-bench task: {case.case_id} ({case.domain})")
            return 0

        if wrapped:
            report = adapter.import_results(document)
        else:
            if not isinstance(document, dict):
                raise BenchmarkIntegrationError("malformed_results", "Results must be an object")
            report = adapter.import_official_results(document, domain=args.domain)
        if args.command == "validate-results":
            _emit(
                report,
                args.format,
                f"Valid tau3-bench Results: {report.aggregate.total} simulations",
            )
        else:
            _emit(report, args.format, render_benchmark_report(report))
        return 0
    except BenchmarkIntegrationError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        if getattr(args, "format", "text") == "json":
            print(canonical_json(payload, indent=2))
        else:
            print(f"Error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


def _adapter_for_document(
    document: Any, explicit_version: str | None
) -> tuple[Tau3BenchmarkAdapter, bool]:
    wrapped = isinstance(document, dict) and "contract_schema" in document
    if wrapped:
        version = document.get("benchmark_version")
        if not isinstance(version, str):
            raise BenchmarkIntegrationError(
                "malformed_contract", "Contract benchmark_version must be a string"
            )
    else:
        if explicit_version is None:
            raise BenchmarkIntegrationError(
                "missing_version", "Raw upstream artifacts require --benchmark-version"
            )
        version = explicit_version
    return Tau3BenchmarkAdapter(version), wrapped


def _provider(adapter: Tau3BenchmarkAdapter) -> ExternalProcessBenchmarkProvider:
    return ExternalProcessBenchmarkProvider(
        adapter.identity,
        supported_contract_versions=SUPPORTED_TAU3_VERSIONS,
        external_python_requirement=TAU3_EXTERNAL_PYTHON,
    )


def _format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text")


def _emit(value: Any, output_format: str, text_value: str) -> None:
    if output_format == "json":
        print(canonical_json(value, indent=2))
    else:
        print(text_value)


if __name__ == "__main__":
    raise SystemExit(main())
