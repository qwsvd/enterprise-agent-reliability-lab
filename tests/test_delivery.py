from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.entrypoint import main as run_container
from app.entrypoint import resolve_port


ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_has_production_runtime_controls() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.11-slim-bookworm AS builder" in dockerfile
    assert "FROM python:3.11-slim-bookworm AS runtime" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "DATABASE_URL=sqlite:////data/after_sales.db" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "os.getenv('PORT', '8000')" in dockerfile
    assert '["python", "-m", "app.entrypoint"]' in dockerfile


def test_container_entrypoint_defaults_and_honors_valid_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resolve_port(None) == 8000
    assert resolve_port("") == 8000
    assert resolve_port("10000") == 10000
    with pytest.raises(RuntimeError, match="integer"):
        resolve_port("not-a-port")
    with pytest.raises(RuntimeError, match="between"):
        resolve_port("70000")

    called: dict[str, object] = {}

    def fake_run(application: str, **kwargs: object) -> None:
        called.update(application=application, **kwargs)

    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setattr("app.entrypoint.uvicorn.run", fake_run)
    run_container()
    assert called == {
        "application": "app.main:app",
        "host": "0.0.0.0",
        "port": 10000,
    }


def test_docker_context_is_allowlisted() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "*" in patterns
    assert "!app/**" in patterns
    assert "!pyproject.toml" in patterns
    assert not any(pattern.startswith("!.env") for pattern in patterns)


def test_ci_covers_supported_python_and_pins_actions() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "3.11" in workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    assert "python -m pytest" in workflow_text
    assert "python -m pip check" in workflow_text
    assert "docker build --pull" in workflow_text

    action_refs = re.findall(r"uses: [^@\s]+@([^\s]+)", workflow_text)
    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
