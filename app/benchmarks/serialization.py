from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.benchmarks.base import BenchmarkIntegrationError


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkIntegrationError(
            "invalid_json", f"Unable to read benchmark JSON {source}: {exc}"
        ) from exc


def canonical_json(value: BaseModel | Any, *, indent: int | None = None) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        payload,
        ensure_ascii=True,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
        sort_keys=True,
    )
