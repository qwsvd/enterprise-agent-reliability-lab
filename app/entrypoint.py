from __future__ import annotations

import os

import uvicorn


DEFAULT_PORT = 8000


def resolve_port(value: str | None) -> int:
    if value is None or not value.strip():
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError("PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT must be between 1 and 65535")
    return port


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=resolve_port(os.getenv("PORT")),
    )


if __name__ == "__main__":
    main()
