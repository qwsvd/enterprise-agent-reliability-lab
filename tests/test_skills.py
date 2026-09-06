from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agent.providers import ScriptedProvider
from app.agent.runtime import AgentRuntime
from app.agent.types import ModelResponse, ToolCall
from app.database import Database
from app.mcp_integration.client import MCPToolAdapter
from app.mcp_integration.server import create_mcp_server
from app.models import Order, Refund, SupportTicket
from app.skills.loader import (
    DuplicateSkillError,
    SkillDirectoryError,
    SkillMetadataError,
    SkillReadError,
    SkillRegistry,
    UnknownSkillError,
    UnsafeSkillPathError,
)
from app.skills.tools import SkillAwareTools


SKILL_ROOT = Path(__file__).parents[1] / ".agents" / "skills"


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = "sample-skill",
    description: str | None = "Use for a sample workflow.",
    body: str = "Follow the sample procedure.",
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    metadata: list[str] = ["---"]
    if name is not None:
        metadata.append(f"name: {name}")
    if description is not None:
        metadata.append(f"description: {description}")
    metadata.extend(["---", "", body, ""])
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(metadata), encoding="utf-8")
    return path


def mcp_skill_tools(tmp_path) -> tuple[str, MCPToolAdapter, SkillRegistry, SkillAwareTools]:
    database_url = f"sqlite:///{(tmp_path / 'skills.db').as_posix()}"
    mcp_tools = MCPToolAdapter(create_mcp_server(database_url))
    mcp_tools.discover()
    registry = SkillRegistry(SKILL_ROOT)
    registry.discover()
    return database_url, mcp_tools, registry, SkillAwareTools(mcp_tools, registry)


def test_valid_skills_are_discovered_as_metadata_only() -> None:
    registry = SkillRegistry(SKILL_ROOT)
    metadata = registry.discover()

    assert [(item.name, item.description) for item in metadata] == [
        (
            "delayed-order-resolution",
            "Resolve a materially delayed customer order by inspecting business state, "
            "applying the current refund policy through tools, and documenting the outcome.",
        ),
        (
            "high-value-refund-escalation",
            "Handle a refund request that may require human approval while preserving "
            "authoritative policy and communicating escalation state accurately.",
        ),
    ]
    assert registry.loaded_names == ()
    assert all(not hasattr(item, "instructions") for item in metadata)


def test_selected_skill_loads_without_loading_unrelated_skill() -> None:
    registry = SkillRegistry(SKILL_ROOT)
    registry.discover()
    skill = registry.load("delayed-order-resolution")

    assert "get_shipping" in skill.instructions
    assert "pending_human_approval" in skill.instructions
    assert registry.loaded_names == ("delayed-order-resolution",)
    assert "high-value-refund-escalation" not in registry.loaded_names


def test_missing_directory_and_skill_file_fail_explicitly(tmp_path: Path) -> None:
    with pytest.raises(SkillDirectoryError, match="not found"):
        SkillRegistry(tmp_path / "missing").discover()

    (tmp_path / "incomplete").mkdir()
    with pytest.raises(SkillMetadataError, match="Missing SKILL.md"):
        SkillRegistry(tmp_path).discover()


@pytest.mark.parametrize("missing", ["name", "description"])
def test_missing_required_metadata_fails(tmp_path: Path, missing: str) -> None:
    write_skill(
        tmp_path,
        "broken",
        name=None if missing == "name" else "broken-skill",
        description=None if missing == "description" else "Broken skill.",
    )
    with pytest.raises(SkillMetadataError, match=missing):
        SkillRegistry(tmp_path).discover()


def test_malformed_yaml_and_duplicate_names_fail(tmp_path: Path) -> None:
    malformed_root = tmp_path / "malformed"
    malformed = malformed_root / "one"
    malformed.mkdir(parents=True)
    (malformed / "SKILL.md").write_text(
        "---\nname: [invalid\ndescription: broken\n---\nbody", encoding="utf-8"
    )
    with pytest.raises(SkillMetadataError, match="Malformed YAML"):
        SkillRegistry(malformed_root).discover()

    duplicate_root = tmp_path / "duplicates"
    write_skill(duplicate_root, "one", name="same-skill")
    write_skill(duplicate_root, "two", name="same-skill")
    with pytest.raises(DuplicateSkillError, match="same-skill"):
        SkillRegistry(duplicate_root).discover()


def test_unknown_traversal_and_unreadable_skill_fail_safely(
    tmp_path: Path, monkeypatch
) -> None:
    path = write_skill(tmp_path, "valid", name="valid-skill")
    registry = SkillRegistry(tmp_path)
    registry.discover()

    with pytest.raises(UnknownSkillError, match="Unknown skill"):
        registry.load("unknown-skill")
    with pytest.raises(UnsafeSkillPathError, match="Unsafe skill name"):
        registry.load("../valid-skill")

    original_read_text = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path.resolve():
            raise PermissionError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(SkillReadError, match="permission denied"):
        registry.load("valid-skill")


def test_progressive_disclosure_context_and_load_tool(tmp_path: Path) -> None:
    _, _, registry, tools = mcp_skill_tools(tmp_path)
    context = tools.context()

    assert "delayed-order-resolution" in context
    assert "high-value-refund-escalation" in context
    assert "Never mutate the database directly" not in context
    result = tools.execute("load_skill", {"name": "delayed-order-resolution"})
    assert result["ok"] is True
    assert "Never mutate the database directly" in result["data"]["instructions"]
    assert registry.loaded_names == ("delayed-order-resolution",)


def test_delayed_order_skill_guides_mcp_agent_and_persists_actions(tmp_path: Path) -> None:
    database_url, mcp_tools, registry, tools = mcp_skill_tools(tmp_path)
    provider = ScriptedProvider([
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
                "reason": "Shipment delayed by 10 days", "idempotency_key": "skill-mcp-1",
            }),
            ToolCall(id="6", name="create_support_ticket", arguments={
                "customer_code": "CUS-001", "order_code": "ORD-1024",
                "subject": "Delayed shipment resolution",
                "description": "Approved refund issued after a 10-day delay.", "priority": "high",
            }),
        ]),
        ModelResponse(content="The 299.00 CNY refund completed and support ticket was opened."),
    ])

    result = AgentRuntime(provider, tools).run(
        "The customer says order ORD-1024 has still not arrived after 10 days. "
        "Resolve the issue according to the appropriate operational workflow."
    )

    first_request = provider.requests[0]["messages"]
    second_request = provider.requests[1]["messages"]
    assert "delayed-order-resolution" in first_request[0]["content"]
    assert "Never mutate the database directly" not in first_request[0]["content"]
    loaded_result = json.loads(second_request[-1]["content"])
    assert loaded_result["data"]["name"] == "delayed-order-resolution"
    assert "Never mutate the database directly" in loaded_result["data"]["instructions"]
    assert registry.loaded_names == ("delayed-order-resolution",)
    assert tools.business_tools is mcp_tools
    assert [event.call.name for event in result.tool_events] == [
        "load_skill", "get_order", "get_customer", "get_shipping", "get_refund_policy",
        "create_refund", "create_support_ticket",
    ]

    database = Database(database_url)
    with database.session_factory() as session:
        refund = session.scalar(select(Refund))
        ticket_count = session.scalar(select(func.count()).select_from(SupportTicket))
    database.dispose()
    assert result.status == "completed"
    assert refund.status == "approved"
    assert ticket_count == 1


def test_high_value_skill_preserves_pending_approval_semantics(tmp_path: Path) -> None:
    database_url, _, registry, tools = mcp_skill_tools(tmp_path)
    database = Database(database_url)
    with database.session_factory() as session:
        order = session.scalar(select(Order).where(Order.code == "ORD-1024"))
        order.amount = Decimal("1500.00")
        session.commit()
    database.dispose()

    provider = ScriptedProvider([
        ModelResponse(tool_calls=[ToolCall(
            id="skill-high", name="load_skill",
            arguments={"name": "high-value-refund-escalation"},
        )]),
        ModelResponse(tool_calls=[
            ToolCall(id="1", name="get_order", arguments={"order_code": "ORD-1024"}),
            ToolCall(id="2", name="get_refund_policy", arguments={}),
        ]),
        ModelResponse(tool_calls=[ToolCall(id="3", name="create_refund", arguments={
            "order_code": "ORD-1024", "amount": "1200.00",
            "reason": "Eligible high-value delayed order", "idempotency_key": "skill-high-1",
        })]),
        ModelResponse(tool_calls=[
            ToolCall(id="4", name="get_order", arguments={"order_code": "ORD-1024"})
        ]),
        ModelResponse(
            content="The refund request is pending human approval and has not completed."
        ),
    ])
    result = AgentRuntime(provider, tools).run("Handle the high-value refund request.")

    refund_result = next(
        event.result["data"] for event in result.tool_events if event.call.name == "create_refund"
    )
    final_order = [
        event.result["data"] for event in result.tool_events if event.call.name == "get_order"
    ][-1]
    assert registry.loaded_names == ("high-value-refund-escalation",)
    assert refund_result["status"] == "pending_human_approval"
    assert refund_result["completed"] is False
    assert final_order["refunded"] is False
    assert "pending human approval" in result.response
    assert "not completed" in result.response
