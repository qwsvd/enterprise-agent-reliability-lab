from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.benchmarks import BenchmarkIntegrationError, Tau3BenchmarkAdapter
from app.benchmarks.cli import main
from app.benchmarks.eval_bridge import report_from_local_evals
from app.benchmarks.models import BenchmarkOutcome
from app.benchmarks.provider import ExternalProcessBenchmarkProvider
from app.benchmarks.serialization import canonical_json, load_json
from app.benchmarks.tau3 import SUPPORTED_TAU3_VERSIONS, TAU3_EXTERNAL_PYTHON
from app.evals import EvalRunner, load_eval_suite


FIXTURES = Path(__file__).parent / "fixtures" / "benchmarks"
TASK_FIXTURE = FIXTURES / "tau3-task-contract.json"
RESULTS_FIXTURE = FIXTURES / "tau3-results-contract.json"


def test_tau3_metadata_and_task_mapping_follow_current_contract() -> None:
    adapter = Tau3BenchmarkAdapter("1.0.1")
    case = adapter.convert_task(load_json(TASK_FIXTURE))

    assert adapter.identity.name == "tau3-bench"
    assert adapter.identity.implementation == "sierra-research/tau2-bench"
    assert adapter.identity.version == "1.0.1"
    assert adapter.identity.license == "MIT"
    assert case.domain == "retail"
    assert case.case_id == "synthetic-retail-contract-001"
    assert case.benchmark_input["ticket"] == "Synthetic support request."
    assert [tool.name for tool in case.tools] == ["lookup_case", "update_case"]
    assert case.tools[0].input_schema["required"] == ["case_id"]
    assert case.policy.sha256 is not None
    assert case.policy.metadata == {"content_imported": False}
    assert "Synthetic fixture policy" not in canonical_json(case)


def test_reference_actions_are_not_mislabeled_as_required() -> None:
    adapter = Tau3BenchmarkAdapter()
    case = adapter.convert_task(load_json(TASK_FIXTURE))
    assert case.expected_outcome.score_basis == ["DB", "COMMUNICATE"]
    assert case.expected_outcome.reference_actions[0]["name"] == "update_case"
    assert case.expected_outcome.metadata["reference_actions_are_required"] is False

    action_gated = load_json(TASK_FIXTURE)
    action_gated["task"]["evaluation_criteria"]["reward_basis"] = ["ACTION"]
    assert (
        adapter.convert_task(action_gated)
        .expected_outcome.metadata["reference_actions_are_required"]
        is True
    )


def test_result_import_preserves_scores_outcomes_and_aggregate() -> None:
    report = Tau3BenchmarkAdapter().import_results(load_json(RESULTS_FIXTURE))
    assert [item.outcome for item in report.results] == [
        BenchmarkOutcome.PASSED,
        BenchmarkOutcome.FAILED,
    ]
    assert [item.passed for item in report.results] == [True, False]
    assert report.aggregate.model_dump() == {
        "total": 2,
        "scored": 2,
        "passed": 1,
        "failed": 1,
        "unscored": 0,
        "errors": 0,
        "pass_rate": 0.5,
        "mean_score": 0.5,
        "by_domain": {
            "retail": {
                "total": 2,
                "scored": 2,
                "passed": 1,
                "failed": 1,
                "pass_rate": 0.5,
                "mean_score": 0.5,
            }
        },
    }
    assert report.metadata["official_result"] is False


def test_unscored_and_provider_error_terminations_are_explicit() -> None:
    document = load_json(RESULTS_FIXTURE)
    simulation = document["results"]["simulations"][0]
    simulation["reward_info"] = None
    simulation["termination_reason"] = "infrastructure_error"
    report = Tau3BenchmarkAdapter().import_results(document)

    item = report.results[0]
    assert item.outcome == BenchmarkOutcome.ERROR
    assert item.passed is None
    assert item.score is None
    assert item.errors[0].code == "tau3_infrastructure_error"
    assert report.aggregate.errors == 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update({"contract_schema": "other/v1"}), "unsupported_contract"),
        (lambda value: value["task"].pop("user_scenario"), "malformed_task"),
        (lambda value: value.update({"domain": "airline"}), "domain_mismatch"),
    ],
)
def test_malformed_contracts_fail_safely(mutation, code: str) -> None:
    document = load_json(
        RESULTS_FIXTURE if code == "domain_mismatch" else TASK_FIXTURE
    )
    mutation(document)
    with pytest.raises(BenchmarkIntegrationError) as caught:
        if code == "domain_mismatch":
            Tau3BenchmarkAdapter().import_results(document)
        else:
            Tau3BenchmarkAdapter().convert_task(document)
    assert caught.value.code == code


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "1.1.0", "2.0.0", "latest"])
def test_unsupported_benchmark_versions_are_rejected(version: str) -> None:
    with pytest.raises(BenchmarkIntegrationError) as caught:
        Tau3BenchmarkAdapter(version)
    assert caught.value.code == "unsupported_version"


def test_external_provider_availability_and_errors_are_typed() -> None:
    adapter = Tau3BenchmarkAdapter()
    missing = ExternalProcessBenchmarkProvider(
        adapter.identity,
        supported_contract_versions=SUPPORTED_TAU3_VERSIONS,
        external_python_requirement=TAU3_EXTERNAL_PYTHON,
        finder=lambda _: None,
    )
    availability = missing.inspect()
    assert availability.available is False
    assert availability.external_python_requirement == ">=3.12,<3.14"

    request = adapter.build_run_request(
        domain="retail",
        agent="registered_agent",
        agent_llm="model-a",
        user_llm="model-b",
        save_to="phase8-smoke",
    )
    unavailable = missing.execute(request)
    assert unavailable.status == "provider_error"
    assert unavailable.errors[0].code == "provider_unavailable"

    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)

    timed = ExternalProcessBenchmarkProvider(
        adapter.identity,
        supported_contract_versions=SUPPORTED_TAU3_VERSIONS,
        finder=lambda _: "tau2",
        runner=timeout_runner,
    ).execute(request)
    assert timed.errors[0].code == "provider_timeout"
    assert timed.errors[0].retryable is True


def test_external_provider_success_does_not_fabricate_a_score() -> None:
    adapter = Tau3BenchmarkAdapter()
    captured: list[list[str]] = []

    def runner(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    provider = ExternalProcessBenchmarkProvider(
        adapter.identity,
        supported_contract_versions=SUPPORTED_TAU3_VERSIONS,
        finder=lambda _: "C:/external/tau2.exe",
        runner=runner,
    )
    request = adapter.build_run_request(
        domain="retail",
        agent="registered_agent",
        agent_llm="model-a",
        user_llm="model-b",
        save_to="phase8-smoke",
        task_ids=["1", "2"],
    )
    result = provider.execute(request)
    assert result.status == "completed"
    assert result.return_code == 0
    assert "--task-ids" in captured[0]
    assert captured[0][-1] == "1,2"
    assert not hasattr(result, "score")


def test_serialization_is_deterministic() -> None:
    report = Tau3BenchmarkAdapter().import_results(load_json(RESULTS_FIXTURE))
    assert canonical_json(report) == canonical_json(report)
    parsed = json.loads(canonical_json(report))
    assert list(parsed) == sorted(parsed)


def test_generic_report_shape_is_compatible_with_local_evals() -> None:
    local = EvalRunner(load_eval_suite()).evaluate(["eligible-refund"])
    report = report_from_local_evals(local)
    assert report.benchmark.name == "after-sales-local-evals"
    assert report.metadata["source_kind"] == "deterministic_local_eval"
    assert report.aggregate.total == 1
    assert report.aggregate.pass_rate == 1.0
    assert report.results[0].metadata["tool_calls"] == 4


def test_cli_inspect_validate_summarize_and_command(capsys, monkeypatch) -> None:
    monkeypatch.setattr("app.benchmarks.provider.shutil.which", lambda _: None)
    assert main(["inspect", "--format", "json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["adapter"]["benchmark"]["name"] == "tau3-bench"

    assert main(["validate-task", str(TASK_FIXTURE), "--format", "json"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["case_id"] == "synthetic-retail-contract-001"

    assert main(["summarize", str(RESULTS_FIXTURE), "--format", "json"]) == 0
    summarized = json.loads(capsys.readouterr().out)
    assert summarized["aggregate"]["pass_rate"] == 0.5

    assert main(
        [
            "command",
            "--domain",
            "retail",
            "--agent",
            "registered_agent",
            "--agent-llm",
            "model-a",
            "--user-llm",
            "model-b",
            "--save-to",
            "phase8-smoke",
            "--format",
            "json",
        ]
    ) == 0
    command = json.loads(capsys.readouterr().out)
    assert command["executed"] is False
    assert command["argv"][:3] == ["tau2", "run", "--domain"]


def test_cli_raw_result_requires_explicit_version(capsys, tmp_path: Path) -> None:
    raw = load_json(RESULTS_FIXTURE)["results"]
    path = tmp_path / "raw-results.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert main(["summarize", str(path), "--benchmark-version", "1.0.1"]) == 0
    assert "Runs: total=2" in capsys.readouterr().out

    assert main(["summarize", str(path), "--format", "json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "missing_version"
