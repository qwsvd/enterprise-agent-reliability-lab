"""Opt-in Codex CLI transport. Business tools are represented only as JSON data."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Timer
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.providers import ProviderError
from app.agent.types import ModelResponse, ToolCall

LIMIT = re.compile(
    r"quota|rate[ _-]?limit|usage[ _-]?limit|payment|purchase|billing|out of credits|too many requests|account.{0,30}(disabled|restricted)|(?:http|status)\D{0,8}(429|402|401|403)\b|\d+%.{0,12}(remaining|left)",
    re.IGNORECASE,
)


class DecisionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    arguments: dict[str, Any]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    calls: list[DecisionCall] = Field(max_length=12)
    final: str | None


def parse_reply(raw: str, tools: list[dict]) -> ModelResponse:
    def unique(pairs: list) -> dict:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate key")
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise TypeError("expected object")
        if not tools:  # ProviderPlanner performs TaskPlan schema validation.
            return ModelResponse(content=raw)
        decision = Decision.model_validate(value)
        if bool(decision.calls) == bool(decision.final):
            raise ValueError("expected either calls or a final decision")
        known = {t["function"]["name"] for t in tools}
        if any(call.name not in known for call in decision.calls):
            raise ValueError("unknown tool")
        return ModelResponse(
            content=decision.final,
            tool_calls=[
                ToolCall(id=f"call-{i}", name=c.name, arguments=c.arguments)
                for i, c in enumerate(decision.calls)
            ],
        )
    except (TypeError, ValueError, RecursionError, ValidationError) as exc:
        raise ProviderError("Invalid model decision", kind="invalid_decision") from exc


class CodexProvider:
    def __init__(self, model: str, cap: int = 24) -> None:
        if not 1 <= cap <= 24:
            raise ValueError("campaign cap must be 1..24")
        self.model, self.cap = model, cap
        self.records: list[dict] = []
        self.stopped = False
        cli = shutil.which("codex.exe" if os.name == "nt" else "codex")
        if not cli and os.name == "nt" and (shim := shutil.which("codex.cmd")):
            matches = list(
                Path(shim).parent.glob(
                    "node_modules/@openai/codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"
                )
            )
            cli = str(matches[0]) if len(matches) == 1 else None
        if not cli:
            raise ProviderError("Native Codex CLI unavailable")
        self.cli = cli
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() not in {"OPENAI_API_KEY", "CODEX_API_KEY"}
        }
        login = subprocess.run(
            [cli, "login", "status"],
            env=self.env,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if (
            login.returncode
            or b"Logged in using ChatGPT" not in login.stderr + login.stdout
        ):
            raise ProviderError("Existing ChatGPT login required")
        self.version = subprocess.check_output(
            [cli, "--version"], env=self.env, text=True, timeout=15
        ).strip()

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        timeout_seconds: float | None = None,
    ) -> ModelResponse:
        if self.stopped or len(self.records) >= self.cap:
            raise ProviderError("Campaign stopped", kind="campaign_stop")
        start = time.monotonic()
        record = {
            "cli": self.version,
            "requested_model": self.model,
            "observed_model": None,
            "cost": None,
            "usage": None,
            "reply": None,
            "error": None,
        }
        self.records.append(record)
        protocol = (
            'Return only {"calls":[{"name":"...","arguments":{}}],"final":null} or '
            '{"calls":[],"final":"disposition"}. Batch independent reads.'
            if tools
            else 'Return the TaskPlan JSON itself. References use {"$ref":"task_id.field"}.'
        )
        payload = json.dumps(
            {"protocol": protocol, "messages": messages, "tools": tools},
            ensure_ascii=True,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = [
                    self.cli,
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "-m",
                    self.model,
                    "-c",
                    'web_search="disabled"',
                    "-c",
                    'model_reasoning_effort="low"',
                ]
                for feature in ("apps", "plugins", "shell_tool", "multi_agent"):
                    args.extend(["--disable", feature])
                args.extend(["--output-last-message", "decision.txt", "-"])
                with subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=tmp,
                    env=self.env,
                ) as proc:
                    timer = Timer(timeout_seconds or 90, proc.kill)
                    timer.start()
                    try:
                        proc.stdin.write(payload.encode())
                        proc.stdin.close()
                        for line in proc.stdout:
                            text = line.decode("utf-8", errors="replace")
                            if LIMIT.search(text):
                                self.stopped = True
                                proc.kill()
                                raise ProviderError(
                                    "Account or quota signal", kind="account_stop"
                                )
                            try:
                                event = json.loads(text)
                            except ValueError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            if event.get("type") == "turn.completed":
                                usage = event.get("usage") or {}
                                record["usage"] = {
                                    k: v for k, v in usage.items() if type(v) is int
                                } or None
                            if event.get("type") in {"turn.failed", "error"}:
                                self.stopped = True
                                proc.kill()
                                raise ProviderError(
                                    "Provider failure", kind="transport_stop"
                                )
                        if proc.wait():
                            self.stopped = True
                            raise ProviderError(
                                "CLI exit or timeout", kind="transport_stop"
                            )
                    finally:
                        timer.cancel()
                with (Path(tmp) / "decision.txt").open(encoding="utf-8") as stream:
                    raw = stream.read(32001)
                if len(raw) > 32000:
                    raise ProviderError("Decision too large", kind="invalid_decision")
                record["reply"] = raw
                return parse_reply(raw, tools)
        except OSError as exc:
            self.stopped = True
            record["error"] = "transport_stop"
            raise ProviderError("Transport unavailable", kind="transport_stop") from exc
        except ProviderError as exc:
            record["error"] = exc.kind
            raise
        finally:
            record["seconds"] = time.monotonic() - start
