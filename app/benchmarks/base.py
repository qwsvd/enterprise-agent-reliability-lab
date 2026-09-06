from __future__ import annotations

from typing import Any, Protocol

from app.benchmarks.models import (
    BenchmarkCase,
    BenchmarkIdentity,
    BenchmarkReport,
)


class BenchmarkIntegrationError(RuntimeError):
    """A benchmark artifact or external provider could not be used safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BenchmarkAdapter(Protocol):
    @property
    def identity(self) -> BenchmarkIdentity: ...

    def convert_task(self, document: dict[str, Any]) -> BenchmarkCase: ...

    def import_results(self, document: dict[str, Any]) -> BenchmarkReport: ...
