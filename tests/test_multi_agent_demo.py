from __future__ import annotations

import json

from app.orchestration.demo import main, run_multi_agent_demo


def test_deterministic_multi_agent_demo_and_comparison_are_real() -> None:
    demo = run_multi_agent_demo()

    assert demo.result.status == "completed"
    assert demo.memory_context_used is True
    assert demo.business_state.model_dump() == {
        "refund_count": 1,
        "ticket_count": 1,
        "refund_status": "approved",
        "refund_completed": True,
    }
    assert demo.result.tool_sequence[-3:] == [
        "create_refund",
        "create_support_ticket",
        "get_order",
    ]
    assert [item.architecture for item in demo.comparison] == [
        "single_agent",
        "multi_agent",
    ]
    assert all(item.success for item in demo.comparison)
    assert demo.comparison[1].steps == demo.result.counters.orchestration_steps
    assert demo.comparison[1].latency_ms is not None
    assert demo.result.token_usage is None
    assert demo.result.estimated_cost is None


def test_multi_agent_demo_json_cli(capsys) -> None:
    assert main(["--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "deterministic_offline"
    assert output["result"]["termination_reason"] == "completed"
    assert output["memory_context_used"] is True
