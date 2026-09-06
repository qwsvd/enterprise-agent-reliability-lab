# Enterprise Agent Reliability Lab

This repository contains a typed local after-sales backend, a small LLM tool-calling agent, official-SDK MCP integration, and repository Agent Skills. It is built with FastAPI, Pydantic, SQLAlchemy, SQLite, HTTPX, MCP Python SDK v2, and the open `SKILL.md` convention. Tracing, RAG, and later reliability work remain out of scope.

## Setup and run

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

The default database is `sqlite:///./after_sales.db`. Override it with `DATABASE_URL` as shown in `.env.example`. API docs are at `http://127.0.0.1:8000/docs`.

## Demo data and API

Startup idempotently seeds customer `CUS-001`, order `ORD-1024` for `299.00 CNY`, and its shipment delayed by 10 days.

- `GET /health`
- `GET /customers/{customer_code}`
- `GET /orders/{order_code}`
- `GET /orders/{order_code}/shipping`
- `GET /policies/refund`
- `POST /refunds` (requires `Idempotency-Key` header)
- `POST /tickets`

Shipments delayed at least 7 days may qualify. Orders can only be refunded once; the refund cannot exceed the order amount; and refunds above 1000 CNY require human approval. An identical idempotent retry returns the original refund, while reusing a key for another request is rejected.

## Phase 2 architecture

The agent implementation is split into small boundaries:

- `app/agent/runtime.py`: bounded model/tool loop and conversation state
- `app/agent/providers.py`: provider protocol, optional OpenAI-compatible client, and deterministic scripted provider
- `app/agent/tools.py`: JSON schemas, Pydantic argument validation, tool dispatch, and structured errors
- `app/agent/types.py`: typed model responses, tool calls, events, and run results
- `app/services.py`: shared Phase 1 business rules used by both HTTP routes and agent tools

The six available tools are `get_customer`, `get_order`, `get_shipping`, `get_refund_policy`, `create_refund`, and `create_support_ticket`.

The representative workflow is:

> The customer says order ORD-1024 has still not arrived after 10 days. Check the relevant customer, order, shipment and refund policy. If the order is eligible, issue the appropriate refund and create a support ticket. Then report what happened.

The model decides which tools to call. The runtime validates and executes each call, adds structured results to the conversation, and continues until the model answers or the step limit is reached. Refund writes still go through the Phase 1 service, including eligibility, duplicate, amount, idempotency, and pending-approval behavior.

## Phase 3 MCP architecture

Phase 2 executes `ToolRegistry` directly. Phase 3 adds a protocol boundary without replacing that local path:

- `app/mcp_integration/server.py` uses the official SDK v2 `MCPServer` and exposes the same six typed business tools.
- MCP handlers delegate to the existing `ToolRegistry`, which delegates writes and policy decisions to the Phase 1 service layer.
- `app/mcp_integration/client.py` uses the official high-level `Client` to discover tools with `tools/list` and invoke them with `tools/call`.
- `MCPToolAdapter` converts the discovered names, descriptions, and input schemas into the existing AgentRuntime tool interface. It does not hard-code the six schemas.
- Tests use the SDK's in-process client/server connection. The local demo uses the SDK's stdio transport and negotiates protocol behavior through the SDK.

```text
User task
  -> AgentRuntime
  -> MCP-discovered tool schema
  -> MCP client
  -> MCP server
  -> existing Phase 1 ToolRegistry/service layer
  -> SQLite database
  -> structured MCP result
  -> AgentRuntime
```

Start the stdio MCP server for a local MCP client or inspector:

```bash
after-sales-mcp-server
```

Run the fully local deterministic MCP Agent demonstration. It launches the real MCP server over stdio, uses `ScriptedProvider` instead of a paid model, and creates a temporary database unless `--database-url` is supplied:

```bash
after-sales-mcp-demo
```

The integration is tested against this repository's server and official SDK client paths. Interoperability with external MCP hosts is not claimed.

## Phase 4 Agent Skills

An Agent Skill is a version-controlled, reusable workflow. Repository skills live at `.agents/skills/<skill-name>/SKILL.md` and use YAML frontmatter followed by concise procedural instructions:

```markdown
---
name: example-skill
description: Explain when this workflow should be selected.
---

Workflow instructions...
```

`SkillRegistry` discovers and validates only lightweight `name`, `description`, and file-location metadata at first. `SkillAwareTools` adds a `load_skill` capability to the existing tool set. Only when the model selects that capability does the registry read the selected complete `SKILL.md`; the resulting instructions remain in the conversation for the rest of that run. Other skill bodies are not preloaded.

The repository includes:

- `delayed-order-resolution`: inspect a delayed order, consult authoritative policy, use refund and ticket tools, and report the exact outcome.
- `high-value-refund-escalation`: preserve human-approval behavior and distinguish pending, approved, rejected, and failed states.

The boundaries are deliberate:

- **Skill:** reusable workflow and instructions.
- **Tool:** executable capability such as looking up an order or creating a refund.
- **MCP:** protocol and discovery boundary for those executable capabilities.
- **Service layer:** authoritative business rules and state changes.

Skills do not contain executable refund thresholds, mutate the database, or replace service validation.

```text
User task
  -> AgentRuntime
  -> skill metadata catalog
  -> selected SKILL.md
  -> skill-guided reasoning
  -> MCP-discovered business tools
  -> MCP server
  -> existing service layer
  -> SQLite database
  -> tool results
  -> AgentRuntime
  -> final response
```

Run the deterministic Agent + Skill + in-process MCP demonstration:

```bash
after-sales-skills-demo
```

The demo uses `ScriptedProvider`, loads `delayed-order-resolution` through `load_skill`, discovers business tools from the MCP server, and uses a temporary SQLite database. It requires no API key or external network.

## Optional real model

Tests use `ScriptedProvider` and never need credentials or network access. To run against an OpenAI-compatible Chat Completions endpoint, configure values from `.env.example` in your shell:

```bash
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_API_KEY=your-runtime-key
export LLM_MODEL=your-tool-calling-model
after-sales-agent "Handle delayed order ORD-1024 and report the outcome."
```

On PowerShell, use `$env:NAME="value"` instead of `export`. The CLI creates and seeds the configured SQLite database before running. No real-model execution result is claimed by this repository.

## Test

```bash
pytest
```

Tests use isolated temporary databases, the official SDK's in-process MCP path, mocked HTTP where needed, and the deterministic scripted provider. The complete suite runs without external network access, an API key, or a paid model.
