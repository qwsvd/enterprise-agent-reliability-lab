from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.skills.demo import TASK, scripted_responses
from app.skills.loader import SkillRegistry
from app.skills.tools import SkillAwareTools
from app.tracing import create_in_memory_tracing


def main() -> None:
    tracing = create_in_memory_tracing("enterprise-agent-tracing-demo")
    skill_root = Path(__file__).resolve().parents[2] / ".agents" / "skills"

    with TemporaryDirectory(prefix="after-sales-tracing-") as directory:
        database_url = f"sqlite:///{(Path(directory) / 'demo.db').as_posix()}"
        tools = SkillAwareTools(
            MCPToolAdapter(create_mcp_server(database_url)),
            SkillRegistry(skill_root),
        )
        result = AgentRuntime(
            ScriptedProvider(scripted_responses()),
            tools,
            tracer=tracing.tracer,
            run_id_factory=lambda: "deterministic-trace-demo",
        ).run(TASK)

    spans = tracing.finished_spans()
    span_names = {
        span.context.span_id: span.name for span in spans if span.context is not None
    }
    payload = {
        "agent": {
            "run_id": result.run_id,
            "status": result.status,
            "termination_reason": result.termination_reason,
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "retries": result.retries,
            "failures": result.failures,
        },
        "spans": [
            {
                "name": span.name,
                "parent": (
                    span_names.get(span.parent.span_id) if span.parent is not None else None
                ),
                "attributes": dict(span.attributes),
            }
            for span in spans
        ],
    }
    print(json.dumps(payload, indent=2, default=str))
    tracing.shutdown()


if __name__ == "__main__":
    main()

