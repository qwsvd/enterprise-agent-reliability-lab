from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.tools import ToolRegistry
from app.agent.types import ModelResponse, ToolCall
from app.models import Order, Refund, SupportTicket


def call(call_id: str, name: str, arguments: object) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def test_tools_inspect_order_shipping_and_policy(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        tools = ToolRegistry(session)
        order = tools.execute("get_order", {"order_code": "ORD-1024"})
        shipping = tools.execute("get_shipping", {"order_code": "ORD-1024"})
        policy = tools.execute("get_refund_policy", {})

    assert order["ok"] is True
    assert order["data"]["amount"] == "299.00"
    assert shipping["data"]["status"] == "delayed"
    assert shipping["data"]["delayed_days"] == 10
    assert policy["data"]["minimum_delay_days"] == 7


def test_eligible_multistep_workflow_persists_refund_and_ticket(client: TestClient) -> None:
    provider = ScriptedProvider([
        call("1", "get_order", {"order_code": "ORD-1024"}),
        ModelResponse(tool_calls=[
            ToolCall(id="2", name="get_customer", arguments={"customer_code": "CUS-001"}),
            ToolCall(id="3", name="get_shipping", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="4", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[
            ToolCall(id="5", name="create_refund", arguments={
                "order_code": "ORD-1024", "amount": "299.00",
                "reason": "Shipment delayed by 10 days", "idempotency_key": "agent-e2e-1",
            }),
            ToolCall(id="6", name="create_support_ticket", arguments={
                "customer_code": "CUS-001", "order_code": "ORD-1024",
                "subject": "Delayed shipment refund",
                "description": "Refund issued after a 10-day shipment delay.",
                "priority": "high",
            }),
        ]),
        ModelResponse(content="The approved 299.00 CNY refund was issued and a support ticket was opened."),
    ])

    with client.app.state.database.session_factory() as session:
        result = AgentRuntime(provider, ToolRegistry(session)).run("Handle delayed order ORD-1024")
        refunds = session.scalars(select(Refund)).all()
        tickets = session.scalars(select(SupportTicket)).all()

    assert result.status == "completed"
    assert result.steps == 4
    assert [event.call.name for event in result.tool_events] == [
        "get_order", "get_customer", "get_shipping", "get_refund_policy",
        "create_refund", "create_support_ticket",
    ]
    assert len(refunds) == 1 and refunds[0].status == "approved"
    assert len(tickets) == 1 and tickets[0].status == "open"
    assert any(
        message["role"] == "tool"
        for request in provider.requests[1:]
        for message in request["messages"]
    )


def test_tool_arguments_are_validated(client: TestClient) -> None:
    provider = ScriptedProvider([
        call("bad", "get_order", {"wrong_field": "ORD-1024"}),
        ModelResponse(content="The order lookup arguments were invalid."),
    ])
    with client.app.state.database.session_factory() as session:
        result = AgentRuntime(provider, ToolRegistry(session)).run("Look up the order")

    error = result.tool_events[0].result
    assert result.status == "completed"
    assert error["ok"] is False
    assert error["error"]["type"] == "invalid_arguments"


def test_unknown_tool_is_rejected_safely(client: TestClient) -> None:
    provider = ScriptedProvider([
        call("unknown", "delete_order", {"order_code": "ORD-1024"}),
        ModelResponse(content="That tool is unavailable."),
    ])
    with client.app.state.database.session_factory() as session:
        result = AgentRuntime(provider, ToolRegistry(session)).run("Delete an order")

    assert result.status == "completed"
    assert result.tool_events[0].result == {
        "ok": False,
        "error": {"type": "unknown_tool", "message": "Unknown tool: delete_order"},
    }


def test_maximum_step_termination(client: TestClient) -> None:
    provider = ScriptedProvider([
        call("1", "get_order", {"order_code": "ORD-1024"}),
        call("2", "get_order", {"order_code": "ORD-1024"}),
    ])
    with client.app.state.database.session_factory() as session:
        result = AgentRuntime(provider, ToolRegistry(session), max_steps=2).run("Keep looking")

    assert result.status == "max_steps_exceeded"
    assert result.steps == 2
    assert len(result.tool_events) == 2


def test_pending_refund_is_not_described_as_completed(client: TestClient) -> None:
    with client.app.state.database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()

        provider = ScriptedProvider([
            call("1", "create_refund", {
                "order_code": "ORD-1024", "amount": "1200.00",
                "reason": "Eligible delayed shipment", "idempotency_key": "pending-1",
            }),
            call("2", "get_order", {"order_code": "ORD-1024"}),
            ModelResponse(content="The 1200.00 CNY request is pending human approval; no refund is completed."),
        ])
        result = AgentRuntime(provider, ToolRegistry(session)).run("Handle the high-value refund")

    refund_data = result.tool_events[0].result["data"]
    order_data = result.tool_events[1].result["data"]
    assert refund_data["status"] == "pending_human_approval"
    assert refund_data["completed"] is False
    assert order_data["refunded"] is False
    assert "pending human approval" in result.response
    assert "no refund is completed" in result.response


def test_agent_refund_tool_preserves_idempotency_and_duplicate_protection(client: TestClient) -> None:
    request = {
        "order_code": "ORD-1024", "amount": "299.00",
        "reason": "Delayed shipment", "idempotency_key": "same-agent-key",
    }
    with client.app.state.database.session_factory() as session:
        tools = ToolRegistry(session)
        first = tools.execute("create_refund", request)
        retry = tools.execute("create_refund", request)
        duplicate = tools.execute("create_refund", {**request, "idempotency_key": "other-key"})
        count = len(session.scalars(select(Refund)).all())

    assert first["data"]["created"] is True
    assert retry["data"]["created"] is False
    assert retry["data"]["code"] == first["data"]["code"]
    assert duplicate["ok"] is False
    assert duplicate["error"]["type"] == "business_error"
    assert count == 1

