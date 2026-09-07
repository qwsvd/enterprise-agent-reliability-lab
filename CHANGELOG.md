# Changelog

All notable changes to this project are documented in this file.

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
