# Repository guidance

- Phase 1 is a local after-sales FastAPI backend only.
- Keep routes in `app/api.py`, contracts in `app/schemas.py`, persistence in `app/models.py`, and business rules in `app/services.py`.
- Add pytest coverage for every behavior change and use `Decimal` for money.
- Preserve refund idempotency. Never add secrets, local databases, caches, or fabricated results.
- Do not add agents, MCP, RAG, a frontend, Docker, or later-phase infrastructure yet.
- Preserve third-party licenses and attribution when reusing external code.

