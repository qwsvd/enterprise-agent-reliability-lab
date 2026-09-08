# Changelog

All notable changes to this project are documented in this file.

## [1.1.0] - 2026-09-09

### Added

- Official LangGraph-based Planner, dependency scheduler, Executor, Reviewer,
  reflection, and bounded-replanning orchestration alongside the existing
  Single-Agent runtime.
- Typed working memory and persistent SQLite episodic memory with deterministic,
  bounded lexical retrieval.
- Multi-agent reliability budgets, side-effect replay protection, payload-safe
  OpenTelemetry spans, deterministic evals, regression gates, and Single-Agent
  comparison.
- Offline `after-sales-multi-agent-demo` and `after-sales-multi-agent-eval` commands.

## [1.0.1] - 2026-09-07

### Added

- Public recruiter-facing project homepage at `GET /` with direct API and source links.
- Regression coverage for the homepage, standard FastAPI documentation surfaces,
  OpenAPI metadata, and container port resolution.

### Changed

- Synchronized package and FastAPI/OpenAPI metadata at version `1.0.1`.
- Made the container runtime honor Render's `PORT` environment variable while
  retaining port `8000` as the local default and health-check target.
- Added live deployment, API documentation, health, CI, and release links to the
  README header.
