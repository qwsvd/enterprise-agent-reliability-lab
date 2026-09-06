from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.skills.loader import SkillRegistry
from app.skills.tools import SkillAwareTools


TASK = (
    "The customer says order ORD-1024 has still not arrived after 10 days. "
    "Resolve the issue according to the appropriate operational workflow."
)


def scripted_responses() -> list[ModelResponse]:
    return [
        ModelResponse(tool_calls=[ToolCall(
            id="skill-1", name="load_skill",
            arguments={"name": "delayed-order-resolution"},
        )]),
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_customer", arguments={"customer_code": "CUS-001"}),
            ToolCall(id="3", name="get_shipping", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="4", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[
            ToolCall(id="5", name="create_refund", arguments={
                "order_code": "ORD-1024", "amount": "299.00",
                "reason": "Shipment delayed by 10 days", "idempotency_key": "skill-demo-1",
            }),
            ToolCall(id="6", name="create_support_ticket", arguments={
                "customer_code": "CUS-001", "order_code": "ORD-1024",
                "subject": "Delayed shipment resolution",
                "description": "Approved refund issued after a 10-day delay.", "priority": "high",
            }),
        ]),
        ModelResponse(
            content="The delayed-order workflow completed: the 299.00 CNY refund was "
            "approved and a support ticket was opened."
        ),
    ]


def main() -> None:
    skill_root = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    registry = SkillRegistry(skill_root)
    registry.discover()

    with TemporaryDirectory(prefix="after-sales-skills-") as directory:
        database_url = f"sqlite:///{(Path(directory) / 'demo.db').as_posix()}"
        mcp_tools = MCPToolAdapter(create_mcp_server(database_url))
        mcp_tools.discover()
        result = AgentRuntime(
            ScriptedProvider(scripted_responses()),
            SkillAwareTools(mcp_tools, registry),
        ).run(TASK)
        print(json.dumps({
            "loaded_skills": registry.loaded_names,
            "mcp_protocol": mcp_tools.protocol_version,
            "agent": result.model_dump(mode="json"),
        }, indent=2))


if __name__ == "__main__":
    main()

