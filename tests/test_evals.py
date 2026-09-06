from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evals import (
    EvalDefinitionError,
    EvalRunner,
    load_eval_suite,
    render_human_report,
    select_eval_cases,
)
from app.evals.models import EvalCheck, EvalSuiteDefinition
from app.evals.runner import (
    aggregate_results,
    evaluate_regression_gate,
    scripted_provider,
)


@pytest.fixture(scope="module")
def full_eval_result():
    return EvalRunner(load_eval_suite()).evaluate()


def test_versioned_eval_definition_loads_ten_distinct_cases() -> None:
    suite = load_eval_suite()

    assert suite.schema_version == "1.0"
    assert len(suite.cases) == 10
    assert len({case.id for case in suite.cases}) == 10
    assert all(case.schema_version == "1.0" for case in suite.cases)
    assert suite.thresholds.minimum_case_pass_rate == 1.0
    assert {case.id for case in suite.cases} == {
        "delayed-order-resolution",
        "eligible-refund",
        "high-value-human-approval",
        "provider-transient-recovery",
        "provider-retry-exhaustion",
        "repeated-tool-call-protection",
        "unsafe-ticket-retry-blocked",
        "unknown-tool-rejection",
        "invalid-tool-arguments",
        "support-ticket-creation",
    }


def test_definition_validation_and_case_selection_are_explicit(tmp_path: Path) -> None:
    suite = load_eval_suite()
    selected = select_eval_cases(
        suite, ["eligible-refund", "support-ticket-creation"]
    )
    assert [case.id for case in selected] == [
        "eligible-refund",
        "support-ticket-creation",
    ]
    with pytest.raises(EvalDefinitionError, match="Unknown eval case ids"):
        select_eval_cases(suite, ["missing-case"])

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("schema_version: ['bad'\n", encoding="utf-8")
    with pytest.raises(EvalDefinitionError, match="Unable to read eval definition"):
        load_eval_suite(malformed)

    duplicated = suite.model_dump(mode="json")
    duplicated["cases"].append(duplicated["cases"][0])
    with pytest.raises(ValidationError, match="Duplicate eval case ids"):
        EvalSuiteDefinition.model_validate(duplicated)


def test_full_eval_suite_exercises_agent_skill_mcp_reliability_and_tracing(
    full_eval_result,
) -> None:
    result = full_eval_result
    assert result.aggregate.total_cases == 10
    assert result.aggregate.passed_cases == 10
    assert result.aggregate.failed_cases == 0
    assert result.aggregate.case_success_rate == 1.0
    assert result.regression_gate.passed is True
    assert result.regression_gate.violations == []
    assert (
        result.aggregate.total_steps,
        result.aggregate.total_model_calls,
        result.aggregate.total_tool_calls,
        result.aggregate.total_retries,
        result.aggregate.total_failures,
    ) == (25, 27, 24, 2, 6)
    assert all(metric.success_rate == 1.0 for metric in result.aggregate.metrics.values())
    assert all(metric.failed == 0 for metric in result.aggregate.metrics.values())

    normal = next(case for case in result.cases if case.case_id == "delayed-order-resolution")
    assert normal.loaded_skills == ["delayed-order-resolution"]
    assert normal.business_state.refund_status == "approved"
    assert normal.business_state.refund_completed is True
    assert normal.business_state.ticket_count == 1
    assert {"skill.load", "mcp.call", "reliability.retry"} - set(normal.trace_spans) == {
        "reliability.retry"
    }


def test_high_value_and_unsafe_side_effect_cases_preserve_safety(
    full_eval_result,
) -> None:
    by_id = {case.case_id: case for case in full_eval_result.cases}
    high = by_id["high-value-human-approval"]
    assert high.business_state.refund_status == "pending_human_approval"
    assert high.business_state.refund_completed is False
    assert high.failures == 0
    assert "pending human approval" in high.response
    assert "not completed" in high.response

    unsafe = by_id["unsafe-ticket-retry-blocked"]
    assert unsafe.termination_reason == "unsafe_retry_blocked"
    assert unsafe.business_state.ticket_count == 1
    assert unsafe.tool_calls == 1
    assert unsafe.retries == 0
    assert unsafe.tool_sequence == ["create_support_ticket"]


def test_required_metrics_cover_outcomes_tools_state_reliability_and_costs(
    full_eval_result,
) -> None:
    assert set(full_eval_result.aggregate.metrics) == {
        "task_success",
        "final_outcome_correctness",
        "required_tool_usage",
        "forbidden_or_unnecessary_tool_usage",
        "tool_call_sequence_correctness",
        "structured_argument_correctness",
        "business_rule_preservation",
        "refund_correctness",
        "human_approval_correctness",
        "side_effect_safety",
        "retry_reliability_behavior",
        "termination_reason_correctness",
        "unsupported_claim_risk",
        "skill_usage",
        "trace_coverage",
        "step_count",
        "model_call_count",
        "tool_call_count",
        "retry_count",
        "failure_count",
    }


def test_one_or_selected_cases_use_injected_provider_without_network(monkeypatch) -> None:
    def forbid_http(*args, **kwargs):
        raise AssertionError("The deterministic eval must not use an HTTP LLM provider")

    monkeypatch.setattr("app.agent.providers.httpx.post", forbid_http)
    suite = load_eval_suite()
    created: list[str] = []

    def provider_factory(case):
        created.append(case.id)
        return scripted_provider(case)

    selected = EvalRunner(suite, provider_factory=provider_factory).evaluate(
        ["provider-transient-recovery", "invalid-tool-arguments"]
    )
    assert selected.selected_case_ids == created == [
        "provider-transient-recovery",
        "invalid-tool-arguments",
    ]
    assert selected.aggregate.total_cases == 2
    assert selected.regression_gate.passed is True

    one = EvalRunner(suite).evaluate(["support-ticket-creation"])
    assert one.selected_case_ids == ["support-ticket-creation"]
    assert one.aggregate.passed_cases == 1


def test_machine_and_human_reports_are_complete(full_eval_result) -> None:
    payload = json.loads(full_eval_result.model_dump_json())
    assert payload["schema_version"] == "1.0"
    assert payload["aggregate"]["passed_cases"] == 10
    assert payload["regression_gate"] == {"passed": True, "violations": []}

    report = render_human_report(full_eval_result)
    assert "Cases: 10/10 passed (100.0%)" in report
    assert "Execution totals: steps=25, model_calls=27, tool_calls=24" in report
    assert "Regression gate: PASS" in report


def test_regression_gate_fails_when_a_case_or_metric_degrades(full_eval_result) -> None:
    original = full_eval_result.cases[0]
    checks = list(original.checks)
    checks[0] = EvalCheck(metric="task_success", passed=False, detail="regression")
    degraded = original.model_copy(update={"passed": False, "checks": checks})
    aggregate = aggregate_results([degraded, *full_eval_result.cases[1:]])
    gate = evaluate_regression_gate(aggregate, load_eval_suite())

    assert gate.passed is False
    assert any("case pass rate" in item for item in gate.violations)
    assert any("failed cases" in item for item in gate.violations)
    assert any("metric task_success" in item for item in gate.violations)
