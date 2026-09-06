from __future__ import annotations

import re
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, Status, StatusCode, Tracer


INSTRUMENTATION_NAME = "enterprise-agent-reliability-lab"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SENSITIVE_IDENTIFIER_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
)


def get_tracer(tracer: Tracer | None = None) -> Tracer:
    """Return an injected tracer or the process-configured OpenTelemetry tracer."""
    return tracer or trace.get_tracer(INSTRUMENTATION_NAME)


def safe_identifier(value: object) -> str:
    """Keep bounded operational identifiers and redact unrestricted text."""
    lowered = value.lower() if isinstance(value, str) else ""
    if (
        isinstance(value, str)
        and SAFE_IDENTIFIER.fullmatch(value)
        and not lowered.startswith("sk-")
        and not any(marker in lowered for marker in SENSITIVE_IDENTIFIER_MARKERS)
    ):
        return value
    return "redacted"


def mark_success(span: Span) -> None:
    span.set_attribute("operation.outcome", "success")
    span.set_status(Status(StatusCode.OK))


def mark_failure(span: Span, category: str, *, retryable: bool = False) -> None:
    """Record a sanitized failure classification without exception text or payloads."""
    span.set_attribute("operation.outcome", "failure")
    span.set_attribute("failure.category", category)
    span.set_attribute("failure.retryable", retryable)
    span.set_status(Status(StatusCode.ERROR))


@dataclass(frozen=True)
class InMemoryTracing:
    """Deterministic local tracing setup for tests and development."""

    tracer: Tracer
    exporter: InMemorySpanExporter
    provider: TracerProvider

    def finished_spans(self) -> tuple[ReadableSpan, ...]:
        return self.exporter.get_finished_spans()

    def clear(self) -> None:
        self.exporter.clear()

    def shutdown(self) -> None:
        self.provider.shutdown()


def create_in_memory_tracing(
    service_name: str = "enterprise-agent-reliability-lab-tests",
) -> InMemoryTracing:
    """Create an isolated provider without changing OpenTelemetry global state."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(INSTRUMENTATION_NAME)
    return InMemoryTracing(tracer=tracer, exporter=exporter, provider=provider)
