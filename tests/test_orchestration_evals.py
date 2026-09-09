from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.orchestration.evals import (
    OrchestrationEvalRunner,
    load_orchestration_eval_suite,
)
from app.orchestration.eval_cli import main


def test_versioned_orchestration_eval_suite_and_metrics_pass() -> None:
    suite = load_orchestration_eval_suite()
    result = OrchestrationEvalRunner(suite).evaluate()

    assert suite.schema_version == "1.0"
    assert [case.id for case in suite.cases] == [
        "multi-agent-delayed-order",
        "multi-agent-high-value",
        "multi-agent-reflection-recovery",
        "multi-agent-replan-budget",
    ]
    assert result.aggregate.total_cases == 4
    assert result.aggregate.passed_cases == 4
    assert result.aggregate.failed_cases == 0
    assert result.aggregate.case_success_rate == 1.0
    assert result.regression_gate_passed is True
    assert result.regression_violations == []
    assert {
        "task_completion",
        "plan_validity",
        "dependency_correctness",
        "tool_selection_correctness",
        "tool_execution_correctness",
        "final_state_correctness",
        "unsupported_claim_detection",
        "human_approval_correctness",
        "reflection_recovery",
        "termination_reason",
        "replan_count",
        "reflection_count",
        "orchestration_step_count",
        "tool_call_count",
        "model_call_count",
        "latency_measured",
    } == set(result.aggregate.metrics)
    assert result.aggregate.measured_latency_ms >= 0


def test_selected_orchestration_eval_and_machine_serialization() -> None:
    result = OrchestrationEvalRunner(load_orchestration_eval_suite()).evaluate(
        ["multi-agent-high-value"]
    )
    payload = result.model_dump(mode="json")

    assert payload["cases"][0]["business_state"]["refund_status"] == (
        "pending_human_approval"
    )
    assert payload["cases"][0]["business_state"]["refund_completed"] is False
    assert payload["cases"][0]["model_calls"] == 0
    with pytest.raises(ValueError, match="Unknown orchestration eval cases"):
        OrchestrationEvalRunner(load_orchestration_eval_suite()).evaluate(["missing"])


def test_orchestration_regression_gate_fails_on_observable_degradation() -> None:
    suite = deepcopy(load_orchestration_eval_suite())
    suite.cases[0].expected.ticket_count = 2

    result = OrchestrationEvalRunner(suite).evaluate([suite.cases[0].id])

    assert result.regression_gate_passed is False
    assert result.aggregate.failed_cases == 1
    assert result.regression_violations


def test_orchestration_eval_cli_supports_selected_json_case(capsys) -> None:
    assert main(["--case", "multi-agent-high-value", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["regression_gate_passed"] is True
    assert [case["case_id"] for case in payload["cases"]] == [
        "multi-agent-high-value"
    ]
