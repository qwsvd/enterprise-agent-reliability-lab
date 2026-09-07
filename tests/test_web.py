from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_public_homepage_is_recruiter_facing_and_self_contained(
    client: TestClient,
) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Enterprise Agent Reliability Lab" in response.text
    assert "Production-oriented reference implementation" in response.text
    assert "AgentRuntime" in response.text
    assert "MCP client/server" in response.text
    assert "OpenTelemetry tracing" in response.text
    assert "Regression gates" in response.text
    assert "GitHub Actions" in response.text
    assert "@media (max-width: 560px)" in response.text
    assert "<script" not in response.text.casefold()

    for link in (
        'href="/docs"',
        'href="/redoc"',
        'href="/health"',
        'href="/openapi.json"',
        'href="https://github.com/qwsvd/enterprise-agent-reliability-lab"',
    ):
        assert link in response.text


def test_standard_fastapi_public_surfaces_remain_enabled(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200

    response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.")
    assert document["info"] == {
        "title": "Enterprise Agent Reliability Lab API",
        "description": (
            "Production-oriented reference API for a reliable enterprise after-sales "
            "Agent, including typed business state, refund policy, and support workflows."
        ),
        "version": __version__,
    }

    assert {
        "/health",
        "/customers/{customer_code}",
        "/orders/{order_code}",
        "/orders/{order_code}/shipping",
        "/policies/refund",
        "/refunds",
        "/tickets",
    } <= set(document["paths"])


def test_package_and_application_versions_are_synchronized() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["version"] == __version__ == "1.0.1"


def test_readme_public_links_and_release_are_visible_at_the_top() -> None:
    header = "\n".join(
        (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:20]
    )

    assert "https://enterprise-agent-reliability-lab.onrender.com/" in header
    assert "https://enterprise-agent-reliability-lab.onrender.com/docs" in header
    assert "https://enterprise-agent-reliability-lab.onrender.com/health" in header
    assert "https://github.com/qwsvd/enterprise-agent-reliability-lab/actions" in header
    assert "Release v1.0.1" in header
