# Repository guidance

- Phase 1 is the local after-sales FastAPI backend. Phase 2 adds the bounded,
  single-agent tool-calling runtime in `app/agent/`.
- Keep routes in `app/api.py`, contracts in `app/schemas.py`, persistence in `app/models.py`, and business rules in `app/services.py`.
- Add pytest coverage for every behavior change and use `Decimal` for money.
- Preserve refund idempotency. Never add secrets, local databases, caches, or fabricated results.
- Keep providers behind the `LLMProvider` protocol and keep tests deterministic with
  `ScriptedProvider`; tests must not require network access or an API key.
- Agent tools must call the Phase 1 service layer instead of reimplementing business rules.
- Do not add MCP, Agent Skills, tracing, RAG, multi-agent systems, a frontend,
  Docker, or later-phase infrastructure yet.
- Preserve third-party licenses and attribution when reusing external code.

