# Repository guidance

- Phase 1 is the local after-sales FastAPI backend. Phase 2 adds the bounded,
  single-agent tool-calling runtime in `app/agent/`. Phase 3 adds official SDK v2
  MCP server/client integration in `app/mcp_integration/`.
- Keep routes in `app/api.py`, contracts in `app/schemas.py`, persistence in `app/models.py`, and business rules in `app/services.py`.
- Add pytest coverage for every behavior change and use `Decimal` for money.
- Preserve refund idempotency. Never add secrets, local databases, caches, or fabricated results.
- Keep providers behind the `LLMProvider` protocol and keep tests deterministic with
  `ScriptedProvider`; tests must not require network access or an API key.
- Agent tools must call the Phase 1 service layer instead of reimplementing business rules.
- MCP handlers must delegate to `ToolRegistry` or the Phase 1 service layer. MCP clients
  must discover schemas from the server rather than hard-coding them.
- Prefer the official MCP SDK's in-process client/server path for deterministic tests and
  stdio for local demos; do not hand-roll protocol negotiation or session management.
- Do not add Agent Skills, tracing, RAG, multi-agent systems, a frontend,
  Docker, or later-phase infrastructure yet.
- Preserve third-party licenses and attribution when reusing external code.

