from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reliability import TerminationReason


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptToolCall(EvalModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelTurn(EvalModel):
    type: Literal["model"]
    content: str | None = None
    tool_calls: list[ScriptToolCall] = Field(default_factory=list)


class ProviderErrorTurn(EvalModel):
    type: Literal["provider_error"]
    kind: str = "provider_error"
    message: str
    retryable: bool = False


ScriptTurn = Annotated[ModelTurn | ProviderErrorTurn, Field(discriminator="type")]


class EvalSetup(EvalModel):
    order_amount: Decimal | None = Field(default=None, gt=0)


class EvalFault(EvalModel):
    type: Literal["none", "ambiguous_support_ticket_after_success"] = "none"


class ExpectedToolCall(EvalModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ExpectedCounters(EvalModel):
    steps: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    retries: int = Field(ge=0)
    failures: int = Field(ge=0)


class ExpectedBusinessState(EvalModel):
    refund_count: int = Field(default=0, ge=0)
    ticket_count: int = Field(default=0, ge=0)
    refund_status: Literal["approved", "pending_human_approval"] | None = None
    refund_completed: bool | None = None

    @model_validator(mode="after")
    def validate_refund_state(self) -> ExpectedBusinessState:
        if self.refund_count == 0 and (
            self.refund_status is not None or self.refund_completed is not None
        ):
            raise ValueError("Refund status requires an expected refund record")
        if self.refund_status == "approved" and self.refund_completed is not True:
            raise ValueError("An approved refund must be expected as completed")
        if (
            self.refund_status == "pending_human_approval"
            and self.refund_completed is not False
        ):
            raise ValueError("A pending refund must not be expected as completed")
        return self


class ExpectedOutcome(EvalModel):
    status: Literal[
        "completed", "max_steps_exceeded", "provider_error", "reliability_failure"
    ]
    termination_reason: TerminationReason | None = None
    response_contains: list[str] = Field(default_factory=list)
    response_excludes: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    tool_sequence: list[str] = Field(default_factory=list)
    tool_calls: list[ExpectedToolCall] = Field(default_factory=list)
    loaded_skills: list[str] = Field(default_factory=list)
    required_trace_spans: list[str] = Field(
        default_factory=lambda: [
            "agent.run",
            "agent.step",
            "agent.provider.call",
            "skill.discovery",
            "mcp.discovery",
        ]
    )
    business_state: ExpectedBusinessState = Field(default_factory=ExpectedBusinessState)
    counters: ExpectedCounters


class EvalCase(EvalModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    task: str
    setup: EvalSetup = Field(default_factory=EvalSetup)
    fault: EvalFault = Field(default_factory=EvalFault)
    reliability: dict[str, Any] = Field(default_factory=dict)
    script: list[ScriptTurn]
    expected: ExpectedOutcome


class RegressionThresholds(EvalModel):
    minimum_case_pass_rate: float = Field(default=1.0, ge=0, le=1)
    minimum_metric_pass_rate: float = Field(default=1.0, ge=0, le=1)
    maximum_failed_cases: int = Field(default=0, ge=0)


class EvalSuiteDefinition(EvalModel):
    schema_version: Literal["1.0"]
    suite_id: str
    description: str
    thresholds: RegressionThresholds = Field(default_factory=RegressionThresholds)
    cases: list[EvalCase]

    @model_validator(mode="after")
    def reject_duplicate_case_ids(self) -> EvalSuiteDefinition:
        ids = [case.id for case in self.cases]
        duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate eval case ids: {', '.join(duplicates)}")
        return self


class EvalCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    passed: bool
    detail: str


class ObservedBusinessState(BaseModel):
    model_config = ConfigDict(frozen=True)

    refund_count: int
    ticket_count: int
    refund_status: str | None
    refund_completed: bool | None


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    name: str
    passed: bool
    checks: list[EvalCheck]
    status: str
    termination_reason: str | None
    response: str
    tool_sequence: list[str]
    loaded_skills: list[str]
    trace_spans: list[str]
    business_state: ObservedBusinessState
    steps: int
    model_calls: int
    tool_calls: int
    retries: int
    failures: int


class MetricAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: int
    failed: int
    total: int
    success_rate: float


class AggregateMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_cases: int
    passed_cases: int
    failed_cases: int
    case_success_rate: float
    metrics: dict[str, MetricAggregate]
    total_steps: int
    total_model_calls: int
    total_tool_calls: int
    total_retries: int
    total_failures: int


class RegressionGateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    violations: list[str] = Field(default_factory=list)


class EvalSuiteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    selected_case_ids: list[str]
    cases: list[EvalCaseResult]
    aggregate: AggregateMetrics
    regression_gate: RegressionGateResult
