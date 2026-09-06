# Repository guidance

- Phase 1 is the local after-sales FastAPI backend. Phase 2 adds the bounded,
  single-agent tool-calling runtime in `app/agent/`. Phase 3 adds official SDK v2
  MCP server/client integration in `app/mcp_integration/`. Phase 4 adds repository
  Agent Skills in `.agents/skills` and loading code in `app/skills/`. Phase 5 adds
  provider-neutral execution guards in `app/reliability/`. Phase 6 adds vendor-neutral
  OpenTelemetry instrumentation and local test setup in `app/tracing/`.
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
- Discover only skill metadata initially and load a selected `SKILL.md` body on demand.
  Skills guide workflows and must never duplicate or replace service-layer business rules.
- Keep reliability limits explicit. Retry only errors marked retryable, count every
  actual provider/tool attempt, and never replay a non-idempotent side effect.
- Provider and MCP timeouts must remain typed. Tests must inject failures and sleeping;
  they must not use network access or real waits.
- Business rejection and `pending_human_approval` are domain outcomes, not retryable
  infrastructure failures.
- Keep traces payload-free: record operation names, safe categories, attempts, counters,
  and outcomes, but never prompts, messages, tool arguments/results, business identifiers,
  PII, credentials, endpoints, authorization data, or exception messages.
- Keep OpenTelemetry tracer injection optional and vendor-neutral. Tests must use the
  isolated in-memory exporter and must not require a collector or network.
- Keep eval cases in the versioned `evals/cases.yaml` dataset. Grade observable Agent,
  tool, persistence, reliability, Skill, MCP, and trace outcomes; never copy service-layer
  policy thresholds into eval execution logic.
- Evals must use provider injection, temporary SQLite databases, in-process MCP, and the
  in-memory tracing exporter. Regression thresholds must remain deterministic and make
  the CLI fail with a nonzero exit status when behavior degrades.
- Do not add benchmark integration, RAG, multi-agent systems, a frontend,
  Docker, or later-phase infrastructure yet.
- Preserve third-party licenses and attribution when reusing external code.
