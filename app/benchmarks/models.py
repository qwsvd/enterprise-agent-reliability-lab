from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkIdentity(BenchmarkModel):
    name: str
    implementation: str
    version: str
    source_url: str
    license: str


class BenchmarkPolicyMetadata(BenchmarkModel):
    identifier: str
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkTool(BenchmarkModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None


class ExpectedObservableOutcome(BenchmarkModel):
    score_basis: list[str] = Field(default_factory=list)
    reference_actions: list[dict[str, Any]] = Field(default_factory=list)
    communicate_info: list[str] = Field(default_factory=list)
    environment_assertions: list[dict[str, Any]] = Field(default_factory=list)
    natural_language_assertions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkCase(BenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    benchmark: BenchmarkIdentity
    domain: str
    case_id: str
    policy: BenchmarkPolicyMetadata
    tools: list[BenchmarkTool]
    benchmark_input: dict[str, Any]
    expected_outcome: ExpectedObservableOutcome
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNSCORED = "unscored"
    ERROR = "error"


class BenchmarkError(BenchmarkModel):
    code: str
    message: str
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkExecutionResult(BenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    benchmark: BenchmarkIdentity
    domain: str
    case_id: str
    execution_id: str
    score: float | None = Field(default=None, ge=0, le=1)
    passed: bool | None = None
    outcome: BenchmarkOutcome
    termination_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    errors: list[BenchmarkError] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome(self) -> BenchmarkExecutionResult:
        if self.outcome == BenchmarkOutcome.PASSED and self.passed is not True:
            raise ValueError("A passed benchmark outcome requires passed=true")
        if self.outcome == BenchmarkOutcome.FAILED and self.passed is not False:
            raise ValueError("A failed benchmark outcome requires passed=false")
        if self.outcome in {BenchmarkOutcome.UNSCORED, BenchmarkOutcome.ERROR}:
            if self.passed is not None:
                raise ValueError("Unscored and error outcomes require passed=null")
        return self


class BenchmarkAggregate(BenchmarkModel):
    total: int = Field(ge=0)
    scored: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unscored: int = Field(ge=0)
    errors: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0, le=1)
    mean_score: float | None = Field(default=None, ge=0, le=1)
    by_domain: dict[str, dict[str, int | float | None]] = Field(default_factory=dict)


class BenchmarkReport(BenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    benchmark: BenchmarkIdentity
    results: list[BenchmarkExecutionResult]
    aggregate: BenchmarkAggregate
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkAvailability(BenchmarkModel):
    benchmark: BenchmarkIdentity
    provider: str
    command: str
    available: bool
    executable_path: str | None = None
    external_python_requirement: str | None = None
    supported_contract_versions: str


class ExternalExecutionRequest(BenchmarkModel):
    benchmark: BenchmarkIdentity
    executable: str
    arguments: list[str]
    timeout_seconds: float = Field(default=3600, gt=0)
    working_directory: str | None = None


class ExternalExecutionResult(BenchmarkModel):
    status: Literal["completed", "provider_error"]
    return_code: int | None = None
    errors: list[BenchmarkError] = Field(default_factory=list)
