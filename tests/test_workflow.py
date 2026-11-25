import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.codex import CodexProvider, parse_reply
from app.agent.providers import ProviderError, ScriptedProvider
from app.agent.types import ModelResponse
from app.database import Database
from app.evals.workflow import ROOT, run_arm, scripted
from app.evals.workflow_env import Boundary, fixture, snapshot
from app.orchestration.models import PlannerContext, ToolEvidence, WorkingMemory
from app.orchestration.planner import PlanningError, ProviderPlanner
from app.orchestration.reviewer import has_unsupported_claim
from app.orchestration.scenarios import build_delayed_order_plan


def test_unrelated_status_cannot_approve_refund():
    memory = WorkingMemory(
        goal="inspect shipment",
        tool_evidence=[
            ToolEvidence(
                task_id="shipment",
                tool_name="get_shipping",
                ok=True,
                data={"status": "approved", "refund_status": "approved"},
            )
        ],
    )
    assert has_unsupported_claim("Refund completed", memory)


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        '{"calls":[],"final":null}',
        '{"calls":[{"name":"unknown","arguments":{}}],"final":null}',
        '{"calls":[],"calls":[],"final":"ok"}',
        '{"calls":[],"final":"ok","extra":1}',
    ],
)
def test_invalid_decision(raw):
    with pytest.raises(ProviderError):
        parse_reply(raw, [{"function": {"name": "get_order"}}])


@pytest.mark.parametrize("quota", [False, True])
def test_native_transport_without_network(monkeypatch, quota):
    p = CodexProvider.__new__(CodexProvider)
    p.cap, p.stopped, p.records, p.cli, p.env, p.version, p.model = (
        1,
        False,
        [],
        "fixture",
        dict(os.environ),
        "mock",
        "requested",
    )
    original = subprocess.Popen

    def fake(args, **kwargs):
        assert args[args.index("--sandbox") + 1] == "read-only" and "shell_tool" in args
        assert Path(kwargs["cwd"]) != Path.cwd()
        code = (
            "print('HTTP 429 rate limit')"
            if quota
            else "import pathlib,json; pathlib.Path('decision.txt').write_text('{}'); print(json.dumps({'type':'turn.completed','usage':{'input_tokens':13}}))"
        )
        return original(
            [sys.executable, "-c", "import sys; sys.stdin.read(); " + code], **kwargs
        )

    monkeypatch.setattr(subprocess, "Popen", fake)
    if quota:
        with pytest.raises(ProviderError):
            p.complete([], [])
        assert p.stopped and p.records[0]["error"] == "account_stop"
    else:
        assert p.complete([], []).content == "{}"
        assert p.records[0]["usage"] == {"input_tokens": 13}
    with pytest.raises(ProviderError):
        p.complete([], [])
    assert len(p.records) == 1


def test_planner_cannot_seed_execution_evidence():
    plan = build_delayed_order_plan()
    plan.tasks[0].result = {"ok": True}
    provider = ScriptedProvider([ModelResponse(content=plan.model_dump_json())])
    with pytest.raises(PlanningError):
        ProviderPlanner(provider).plan(PlannerContext(goal="test", available_tools=[]))


def test_authority_and_argument_guards(tmp_path):
    case = {"id": "normal"}
    path = tmp_path / "db"
    fixture(path, case)
    db = Database("sqlite:///" + path.as_posix())
    try:
        e = Boundary(None, db, "read", "none")
        before = snapshot(db)
        assert not e.execute("create_refund", {"amount": -1})["ok"]
        assert not e.execute("create_support_ticket", {})["ok"]
        assert not e.execute("report_disposition", [])["ok"]
        assert snapshot(db) == before
    finally:
        db.dispose()


@pytest.mark.parametrize("arm", "ABC")
def test_actual_runtime_and_approval(tmp_path, arm):
    cases = json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))
    assert len({c["id"] for c in cases}) >= 16
    c = next(c for c in cases if c["id"] == "approval")
    db = tmp_path / "db"
    fixture(db, c)
    row = run_arm(c, arm, scripted(c, arm), db, tmp_path / "mem")
    assert (
        row["passed"]
        and row["after"]["refunds"][0]["status"] == "pending_human_approval"
    )
    assert row["tool_calls"] <= 12 and row["model_calls"] <= 4
