from __future__ import annotations

import json

from app.benchmarks.cli import build_parser as build_benchmark_parser
from app.demo import main, render_demo, run_demo


def test_final_demo_exercises_the_real_offline_stack(monkeypatch) -> None:
    def forbid_http(*args, **kwargs):
        raise AssertionError("The deterministic final demo must not call an HTTP LLM")

    monkeypatch.setattr("app.agent.providers.httpx.post", forbid_http)
    result = run_demo()

    assert result.mode == "deterministic_offline"
    assert result.regression_gate_passed is True
    assert (result.scenarios_passed, result.scenarios_total) == (3, 3)
    assert "AgentRuntime" in result.components
    assert "MCP discovery and tool calls" in result.components
    assert "OpenTelemetry in-memory tracing" in result.components

    scenarios = {scenario.case_id: scenario for scenario in result.scenarios}
    delayed = scenarios["delayed-order-resolution"]
    assert delayed.loaded_skills == ["delayed-order-resolution"]
    assert delayed.tool_sequence == [
        "load_skill",
        "get_order",
        "get_customer",
        "get_shipping",
        "get_refund_policy",
        "create_refund",
        "create_support_ticket",
    ]
    assert delayed.business_state.refund_status == "approved"
    assert delayed.business_state.refund_completed is True
    assert delayed.business_state.ticket_count == 1
    assert delayed.trace_span_counts["mcp.call"] == 6
    assert delayed.trace_span_counts["skill.load"] == 1

    recovery = scenarios["provider-transient-recovery"]
    assert recovery.counters.retries == 1
    assert recovery.counters.failures == 1
    assert recovery.trace_span_counts["reliability.retry"] == 1

    approval = scenarios["high-value-human-approval"]
    assert approval.loaded_skills == ["high-value-refund-escalation"]
    assert approval.business_state.refund_status == "pending_human_approval"
    assert approval.business_state.refund_completed is False
    assert "not completed" in approval.final_response
    assert all(
        scenario.checks_passed == scenario.checks_total
        for scenario in result.scenarios
    )


def test_final_demo_cli_supports_text_and_machine_readable_output(capsys) -> None:
    result = run_demo()
    text = render_demo(result)

    assert "[PASS] Normal delayed-order resolution" in text
    assert "MCP discovery and tool calls" in text
    assert "Regression gate: PASS (3/3 scenarios passed)" in text

    assert main(["--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "deterministic_offline"
    assert payload["regression_gate_passed"] is True
    assert payload["scenarios_total"] == 3


def test_documented_benchmark_help_is_safe_for_basic_console_encodings() -> None:
    assert build_benchmark_parser().format_help().isascii()
