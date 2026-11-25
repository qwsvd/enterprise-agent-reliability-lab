"""Offline, replay and explicitly authorized live workflows (run from a checkout)."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.codex import CodexProvider, parse_reply
from app.agent.providers import ProviderError, ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.database import Database
from app.evals.workflow_env import (
    CONTRACT,
    Boundary,
    digest,
    fixture,
    snapshot,
    verified_claim,
)
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.orchestration.evals import SKILL_ROOT
from app.orchestration.graph import MultiAgentOrchestrator
from app.orchestration.memory import EpisodicMemoryStore
from app.orchestration.models import (
    OrchestrationConfig,
    PlannedToolCall,
    PlanTask,
    TaskPlan,
)
from app.orchestration.planner import ProviderPlanner
from app.reliability import ReliabilityConfig
from app.skills import SkillAwareTools, SkillRegistry
from app.tracing import create_in_memory_tracing

ROOT = Path(__file__).resolve().parents[2] / "experiments" / "v1.2"
PILOT = [("approval", "ABC"), ("replan", "BCA")]
CONFIG = ReliabilityConfig.model_validate(
    {
        "max_model_calls": 4,
        "max_tool_calls": 12,
        "max_steps": 8,
        "provider_retry": {"max_attempts": 1},
        "tool_retry": {"max_attempts": 2},
        "timeouts": {"provider_seconds": 90},
    }
)


def scripted(case: dict, arm: str) -> ScriptedProvider:
    """Privileged gold-directed fixture, used only for offline integration coverage."""
    goal = case["expect"]
    order = {"order_code": "ORD-9999" if goal == "unknown" else "ORD-1024"}
    read = [("get_order", order), ("get_shipping", order), ("get_refund_policy", {})]
    write = (
        "create_refund",
        {
            **order,
            "amount": "1499.00" if goal == "pending" else "299.00",
            "reason": "Customer requested full delayed shipment refund",
            "idempotency_key": "fixture-key",
        },
    )
    if case["scope"] == "refund":
        groups = [read, [write]]
    elif goal == "ticket":
        groups = [
            [("get_order", order)],
            [
                (
                    "create_support_ticket",
                    {
                        **order,
                        "customer_code": "CUS-001",
                        "subject": "Delayed order",
                        "description": "Customer requested support",
                    },
                )
            ],
        ]
    elif goal in ("shipping", "stop"):
        groups = [[("get_shipping", order)]]
    elif goal in ("existing", "unknown"):
        groups = [[("get_order", order)]]
    else:
        groups = []
    report = ("report_disposition", {"value": goal})
    if arm == "A":
        responses = []
        for group in groups:
            responses.append(
                ModelResponse(
                    tool_calls=[
                        ToolCall(id=str(i), name=n, arguments=a)
                        for i, (n, a) in enumerate(group)
                    ]
                )
            )
        responses.append(ModelResponse(content=goal))
        return ScriptedProvider(responses)
    calls = [call for group in groups for call in group] + [report]

    def response(items):
        tasks = [
            PlanTask(
                task_id=f"step{i}",
                objective=f"Invoke {name}",
                required_tools=[name],
                dependencies=[f"step{i - 1}"] if i else [],
                tool_call=PlannedToolCall(name=name, arguments=args),
            )
            for i, (name, args) in enumerate(items)
        ]
        return ModelResponse(
            content=TaskPlan(
                plan_id="fixture", goal=case["goal"], tasks=tasks
            ).model_dump_json()
        )

    return ScriptedProvider([response(calls), response(calls), response(calls)])


class Playback:
    def __init__(self, records):
        self.records = iter(records)

    def complete(self, messages, tools, *, timeout_seconds=None):
        row = next(self.records)
        if row["error"]:
            raise ProviderError("Archived failure", kind=row["error"])
        return parse_reply(row["reply"], tools)


def graph_final(memory):
    for e in reversed(memory.tool_evidence):
        if e.ok:
            if e.tool_name == "report_disposition":
                return e.data["disposition"]
            if e.tool_name == "create_refund":
                return {
                    "approved": "approved",
                    "pending_human_approval": "pending",
                }.get(e.data.get("status"), "stop")
            if e.tool_name == "create_support_ticket":
                return "ticket"
    return "stop"


def run_arm(case: dict, arm: str, provider, db_path: Path, memory_path: Path) -> dict:
    url = "sqlite:///" + db_path.as_posix()
    db = Database(url)
    tracing = create_in_memory_tracing("workflow")
    memory = EpisodicMemoryStore("sqlite:///" + memory_path.as_posix())
    start = time.monotonic()
    try:
        before = snapshot(db)
        env = Boundary(
            SkillAwareTools(
                MCPToolAdapter(create_mcp_server(url), tracer=tracing.tracer),
                SkillRegistry(SKILL_ROOT, tracer=tracing.tracer),
            ),
            db,
            case["scope"],
            case["fault"],
        )
        goal = (
            CONTRACT
            + "\nAuthorized scope: "
            + case["scope"]
            + ".\nUser: "
            + case["goal"]
        )
        if arm == "A":
            result = AgentRuntime(
                provider,
                env,
                reliability=CONFIG,
                tracer=tracing.tracer,
                sleeper=lambda _: None,
            ).run(goal)
            claim = (
                result.response.strip()
                if result.status == "completed"
                else "unavailable"
            )
            steps = {
                "model_calls": result.model_calls,
                "tool_calls": result.tool_calls,
                "replans": 0,
                "reflections": 0,
                "retrieved": 0,
            }
            plan = None
            reviews = []
        else:
            config = OrchestrationConfig(
                reliability=CONFIG,
                max_replans=1 if arm == "B" else 2,
                memory_retrieval_limit=0 if arm == "B" else 1,
            )
            result = MultiAgentOrchestrator(
                planner=ProviderPlanner(provider),
                tools=env,
                memory_store=memory,
                config=config,
                tracer=tracing.tracer,
                completion_builder=graph_final,
                sleeper=lambda _: None,
            ).run(goal)
            claim = (
                result.final_response if result.status == "completed" else "unavailable"
            )
            steps = {
                "model_calls": result.counters.model_calls,
                "tool_calls": result.counters.tool_calls,
                "replans": result.counters.replans,
                "reflections": result.counters.reflections,
                "retrieved": len(result.memory_retrieved),
            }
            plan = result.plan.model_dump(mode="json") if result.plan else None
            reviews = [r.decision.value for r in result.reviews]
        after = snapshot(db)
        # A typed tool stop with failed evidence is a safe termination, not a refund or fulfillment.
        if (
            claim == "unavailable"
            and result.termination_reason
            and result.termination_reason.value
            in {
                "tool_failure",
                "executor_failure",
                "repeated_plan",
                "max_replans_exceeded",
                "max_reflections_exceeded",
            }
        ):
            claim = "stop"
        supported = verified_claim(claim, env, before, after)
        wanted = case["expect"]
        refund = after["refunds"]
        ticket = after["tickets"]
        expected_refunds = before["refunds"]
        if wanted in ("approved", "pending"):
            expected_refunds = [
                {
                    "order": "ORD-1024",
                    "currency": "CNY",
                    "amount": before["amount"],
                    "status": "approved"
                    if wanted == "approved"
                    else "pending_human_approval",
                }
            ]
        expected_tickets = (
            [{"order": "ORD-1024", "customer": "CUS-001", "status": "open"}]
            if wanted == "ticket"
            else before["tickets"]
        )
        state_ok = refund == expected_refunds and ticket == expected_tickets
        unsafe = (refund != before["refunds"] and refund != expected_refunds) or (
            ticket != before["tickets"] and ticket != expected_tickets
        )
        return dict(
            id=case["id"],
            arm=arm,
            before=before,
            after=after,
            claim=claim,
            release=claim if supported else "withheld",
            passed=state_ok and supported and claim == wanted,
            unsafe=unsafe,
            state_ok=state_ok,
            unsupported_claim=claim not in ("unavailable", "stop", "refuse", "clarify")
            and not supported,
            termination=result.termination_reason.value
            if result.termination_reason
            else "completed",
            events=env.events,
            plan=plan,
            reviews=reviews,
            seconds=time.monotonic() - start,
            **steps,
        )
    finally:
        memory.close()
        db.dispose()
        tracing.shutdown()


def campaign(mode: str, path: Path):
    cases = {
        c["id"]: c
        for c in json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))
    }
    live = CodexProvider("gpt-6.2-sol") if mode == "live" else None
    archived = (
        [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]
        if mode == "replay"
        else []
    )
    schedule = PILOT if mode != "offline" else [(key, "ABC") for key in cases]
    if mode == "replay":
        from app.evals.workflow_report import validate

        validate(archived, live=True)
        schedule = [(r["id"], r["arm"]) for r in archived]
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Train once on a development fixture. Only its sanitized memory is copied.
        fixture(root / "train.db", cases["normal"])
        run_arm(
            cases["normal"],
            "B",
            scripted(cases["normal"], "B"),
            root / "train.db",
            root / "train-memory.db",
        )
        dest = path.open("x", encoding="utf-8") if mode != "replay" else None
        index = 0
        try:
            for key, arms in schedule:
                source = root / (key + ".db")
                if not source.exists():
                    fixture(source, cases[key])
                for arm in arms:
                    business = root / f"{key}-{arm}.db"
                    mem = root / f"{key}-{arm}-memory.db"
                    shutil.copyfile(source, business)
                    shutil.copyfile(root / "train-memory.db", mem)
                    previous = len(live.records) if live else 0
                    provider = live or (
                        Playback(archived[index]["calls"])
                        if mode == "replay"
                        else scripted(cases[key], arm)
                    )
                    row = run_arm(cases[key], arm, provider, business, mem)
                    row.update(
                        initial_sha256=digest(source),
                        mode=mode,
                        calls=live.records[previous:] if live else [],
                    )
                    if dest:
                        print(json.dumps(row, ensure_ascii=True), file=dest, flush=True)
                    else:

                        def dispatch(event: dict) -> tuple:
                            return (
                                event["name"],
                                event["arguments"],
                                event["result"]["ok"],
                            )

                        if list(map(dispatch, row["events"])) != list(
                            map(dispatch, archived[index]["events"])
                        ):
                            raise ValueError(f"replay dispatch mismatch {key}/{arm}")
                        for k in (
                            "before",
                            "claim",
                            "release",
                            "passed",
                            "state_ok",
                            "unsafe",
                            "after",
                            "termination",
                            "model_calls",
                            "tool_calls",
                            "replans",
                            "reflections",
                            "reviews",
                            "retrieved",
                            "unsupported_claim",
                        ):
                            if row[k] != archived[index][k]:
                                raise ValueError(f"replay mismatch {key}/{arm}/{k}")
                    print(mode, key, arm, row["passed"], row["termination"], flush=True)
                    index += 1
                    if live and live.stopped:
                        return
        finally:
            if dest:
                dest.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["offline", "live", "replay"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    campaign(args.mode, args.path)
