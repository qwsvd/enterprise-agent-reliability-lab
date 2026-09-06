from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import StdioServerParameters

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.mcp_integration.client import MCPToolAdapter


DEFAULT_TASK = (
    "The customer says order ORD-1024 has still not arrived after 10 days. "
    "Check the customer, order, shipment and refund policy. If eligible, issue "
    "the appropriate refund and create a support ticket. Then report what happened."
)


def scripted_responses() -> list[ModelResponse]:
    return [
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_customer", arguments={"customer_code": "CUS-001"}),
            ToolCall(id="3", name="get_shipping", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="4", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[
            ToolCall(id="5", name="create_refund", arguments={
                "order_code": "ORD-1024", "amount": "299.00",
                "reason": "Shipment delayed by 10 days", "idempotency_key": "mcp-demo-1",
            }),
            ToolCall(id="6", name="create_support_ticket", arguments={
                "customer_code": "CUS-001", "order_code": "ORD-1024",
                "subject": "Delayed shipment refund",
                "description": "Refund issued after the delayed shipment.", "priority": "high",
            }),
        ]),
        ModelResponse(
            content="The 299.00 CNY refund was approved and a support ticket was opened."
        ),
    ]


def run_demo(database_url: str, task: str) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_integration.server"],
        env={"DATABASE_URL": database_url},
        cwd=Path.cwd(),
    )
    tools = MCPToolAdapter(server)
    tools.discover()
    result = AgentRuntime(ScriptedProvider(scripted_responses()), tools).run(task)
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic Phase 3 MCP agent demo")
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK)
    parser.add_argument("--database-url")
    args = parser.parse_args()

    if args.database_url:
        run_demo(args.database_url, args.task)
        return
    with TemporaryDirectory(prefix="after-sales-mcp-") as directory:
        path = (Path(directory) / "demo.db").as_posix()
        run_demo(f"sqlite:///{path}", args.task)


if __name__ == "__main__":
    main()
