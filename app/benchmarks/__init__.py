"""Provider-neutral external Agent benchmark integration APIs."""

from app.benchmarks.base import BenchmarkAdapter, BenchmarkIntegrationError
from app.benchmarks.models import (
    BenchmarkCase,
    BenchmarkExecutionResult,
    BenchmarkIdentity,
    BenchmarkReport,
)
from app.benchmarks.report import render_benchmark_report
from app.benchmarks.tau3 import Tau3BenchmarkAdapter

__all__ = [
    "BenchmarkAdapter",
    "BenchmarkCase",
    "BenchmarkExecutionResult",
    "BenchmarkIdentity",
    "BenchmarkIntegrationError",
    "BenchmarkReport",
    "Tau3BenchmarkAdapter",
    "render_benchmark_report",
]
