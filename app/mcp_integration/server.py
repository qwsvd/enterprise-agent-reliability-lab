from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app import models  # noqa: F401 - registers SQLAlchemy metadata
from app.agent.tools import ToolRegistry
from app.database import Database
from app.seed import seed_demo_data


def create_mcp_server(database_url: str | None = None) -> MCPServer[Database]:
    database = Database(database_url or os.getenv("DATABASE_URL", "sqlite:///./after_sales.db"))

    @asynccontextmanager
    async def lifespan(server: MCPServer[Database]) -> AsyncIterator[Database]:
        database.create_schema()
        with database.session_factory() as session:
            seed_demo_data(session)
        yield database
        database.dispose()

    server: MCPServer[Database] = MCPServer(
        name="enterprise-after-sales",
        version="0.3.0",
        instructions="Use these tools to inspect and operate the after-sales business system.",
        lifespan=lifespan,
    )

    def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with database.session_factory() as session:
            result = ToolRegistry(session).execute(name, arguments)
        if not result["ok"]:
            error = result["error"]
            raise ToolError(f"{error['type']}: {error['message']}")
        return result

    @server.tool(description="Look up a customer by its business customer code.")
    def get_customer(customer_code: str) -> dict[str, Any]:
        return execute("get_customer", {"customer_code": customer_code})

    @server.tool(description="Look up an order and its current refund state.")
    def get_order(order_code: str) -> dict[str, Any]:
        return execute("get_order", {"order_code": order_code})

    @server.tool(description="Look up carrier and delay state for an order shipment.")
    def get_shipping(order_code: str) -> dict[str, Any]:
        return execute("get_shipping", {"order_code": order_code})

    @server.tool(description="Read the current refund eligibility and approval policy.")
    def get_refund_policy() -> dict[str, Any]:
        return execute("get_refund_policy", {})

    @server.tool(description="Create an eligible, idempotent refund request for an order.")
    def create_refund(
        order_code: str,
        amount: Decimal,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return execute(
            "create_refund",
            {
                "order_code": order_code,
                "amount": amount,
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
        )

    @server.tool(description="Create a support ticket linked to a customer and optional order.")
    def create_support_ticket(
        customer_code: str,
        subject: str,
        description: str,
        order_code: str | None = None,
        priority: Literal["low", "normal", "high", "urgent"] = "normal",
    ) -> dict[str, Any]:
        return execute(
            "create_support_ticket",
            {
                "customer_code": customer_code,
                "order_code": order_code,
                "subject": subject,
                "description": description,
                "priority": priority,
            },
        )

    return server


def main() -> None:
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()

