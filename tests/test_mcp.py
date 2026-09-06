from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from mcp.server import MCPServer
from sqlalchemy import func, select

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.database import Database
from app.mcp_integration.client import MCPClientError, MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket


EXPECTED_TOOLS = {
    "get_customer",
    "get_order",
    "get_shipping",
    "get_refund_policy",
    "create_refund",
    "create_support_ticket",
}


@pytest.fixture
def mcp_environment(tmp_path) -> tuple[str, MCPToolAdapter]:
    database_url = f"sqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    adapter = MCPToolAdapter(create_mcp_server(database_url))
    adapter.discover()
    return database_url, adapter


def test_server_discovery_exposes_six_typed_tools(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    _, adapter = mcp_environment
    schemas = adapter.schemas()
    by_name = {item["function"]["name"]: item["function"] for item in schemas}

    assert set(by_name) == EXPECTED_TOOLS
    assert adapter.protocol_version == "2026-07-28"
    assert by_name["get_order"]["parameters"]["properties"]["order_code"]["type"] == "string"
    refund_schema = by_name["create_refund"]["parameters"]
    assert set(refund_schema["required"]) == {
        "order_code", "amount", "reason", "idempotency_key"
    }
    assert {option["type"] for option in refund_schema["properties"]["amount"]["anyOf"]} == {
        "number", "string"
    }


def test_get_order_and_refund_policy_through_mcp(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    _, adapter = mcp_environment
    order = adapter.execute("get_order", {"order_code": "ORD-1024"})
    policy = adapter.execute("get_refund_policy", {})

    assert order["ok"] is True
    assert order["data"]["customer_code"] == "CUS-001"
    assert order["data"]["refunded"] is False
    assert policy["ok"] is True
    assert policy["data"]["minimum_delay_days"] == 7


def test_refund_and_support_ticket_persist_through_mcp(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    database_url, adapter = mcp_environment
    refund = adapter.execute("create_refund", {
        "order_code": "ORD-1024", "amount": "299.00",
        "reason": "Shipment delayed by 10 days", "idempotency_key": "mcp-write-1",
    })
    ticket = adapter.execute("create_support_ticket", {
        "customer_code": "CUS-001", "order_code": "ORD-1024",
        "subject": "Delayed shipment refund",
        "description": "Refund issued after a 10-day shipment delay.", "priority": "high",
    })

    database = Database(database_url)
    with database.session_factory() as session:
        refund_count = session.scalar(select(func.count()).select_from(Refund))
        ticket_count = session.scalar(select(func.count()).select_from(SupportTicket))
    database.dispose()

    assert refund["data"]["status"] == "approved"
    assert refund["data"]["completed"] is True
    assert ticket["data"]["status"] == "open"
    assert refund_count == 1
    assert ticket_count == 1


def test_invalid_arguments_and_unknown_tools_fail_safely(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    _, adapter = mcp_environment
    invalid = adapter.execute("get_order", {})
    unknown = adapter.execute("delete_order", {"order_code": "ORD-1024"})

    assert invalid["ok"] is False
    assert invalid["error"]["type"] == "mcp_tool_error"
    assert "order_code" in invalid["error"]["message"]
    assert unknown == {
        "ok": False,
        "error": {"type": "unknown_mcp_tool", "message": "Unknown MCP tool: delete_order"},
    }


def test_refund_idempotency_and_duplicate_protection_survive_mcp(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    database_url, adapter = mcp_environment
    request = {
        "order_code": "ORD-1024", "amount": "299.00",
        "reason": "Delayed shipment", "idempotency_key": "mcp-idempotent-1",
    }
    first = adapter.execute("create_refund", request)
    retry = adapter.execute("create_refund", request)
    duplicate = adapter.execute(
        "create_refund", {**request, "idempotency_key": "mcp-idempotent-2"}
    )

    database = Database(database_url)
    with database.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(Refund))
    database.dispose()

    assert first["data"]["created"] is True
    assert retry["data"]["created"] is False
    assert retry["data"]["code"] == first["data"]["code"]
    assert duplicate["ok"] is False
    assert duplicate["error"]["type"] == "mcp_tool_error"
    assert "already been refunded" in duplicate["error"]["message"]
    assert count == 1


def test_pending_human_approval_is_not_completed_through_mcp(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    database_url, adapter = mcp_environment
    database = Database(database_url)
    with database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()
    database.dispose()

    refund = adapter.execute("create_refund", {
        "order_code": "ORD-1024", "amount": "1200.00",
        "reason": "Eligible high-value refund", "idempotency_key": "mcp-pending-1",
    })
    order = adapter.execute("get_order", {"order_code": "ORD-1024"})

    assert refund["data"]["status"] == "pending_human_approval"
    assert refund["data"]["completed"] is False
    assert order["data"]["refund_status"] == "pending_human_approval"
    assert order["data"]["refunded"] is False


def test_agent_uses_mcp_discovered_tools_end_to_end(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    database_url, adapter = mcp_environment
    provider = ScriptedProvider([
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_shipping", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="3", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[
            ToolCall(id="4", name="get_customer", arguments={"customer_code": "CUS-001"}),
            ToolCall(id="5", name="create_refund", arguments={
                "order_code": "ORD-1024", "amount": "299.00",
                "reason": "Shipment delayed by 10 days", "idempotency_key": "mcp-agent-1",
            }),
            ToolCall(id="6", name="create_support_ticket", arguments={
                "customer_code": "CUS-001", "order_code": "ORD-1024",
                "subject": "Delayed shipment refund",
                "description": "Refund issued after the delayed shipment.", "priority": "high",
            }),
        ]),
        ModelResponse(content="The 299.00 CNY refund was approved and a support ticket was opened."),
    ])

    result = AgentRuntime(provider, adapter).run(
        "Check delayed order ORD-1024, refund it if eligible, and open a ticket."
    )
    database = Database(database_url)
    with database.session_factory() as session:
        refunds = session.scalar(select(func.count()).select_from(Refund))
        tickets = session.scalar(select(func.count()).select_from(SupportTicket))
    database.dispose()

    discovered_names = {
        tool["function"]["name"] for tool in provider.requests[0]["tools"]
    }
    assert result.status == "completed"
    assert discovered_names == EXPECTED_TOOLS
    assert provider.requests[0]["tools"] == adapter.schemas()
    assert [event.call.name for event in result.tool_events] == [
        "get_order", "get_shipping", "get_refund_policy", "get_customer",
        "create_refund", "create_support_ticket",
    ]
    assert refunds == 1
    assert tickets == 1


def test_connection_failure_and_malformed_results_are_explicit(
    mcp_environment: tuple[str, MCPToolAdapter]
) -> None:
    _, adapter = mcp_environment
    adapter.server = object()
    connection_error = adapter.execute("get_order", {"order_code": "ORD-1024"})
    assert connection_error["ok"] is False
    assert connection_error["error"]["type"] == "mcp_connection_or_protocol_error"

    malformed_server = MCPServer("malformed-test")

    @malformed_server.tool(structured_output=False)
    def malformed() -> str:
        return "not structured"

    malformed_adapter = MCPToolAdapter(malformed_server)
    malformed_adapter.discover()
    malformed_result = malformed_adapter.execute("malformed", {})
    assert malformed_result == {
        "ok": False,
        "error": {
            "type": "malformed_mcp_result",
            "message": "MCP result has no usable structured content",
        },
    }


def test_discovery_failure_is_explicit() -> None:
    with pytest.raises(MCPClientError, match="MCP tool discovery failed"):
        MCPToolAdapter(object()).discover()
