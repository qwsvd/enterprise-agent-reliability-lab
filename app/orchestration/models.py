from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reliability import ReliabilityConfig


class OrchestrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskExecutionKind(StrEnum):
    TOOL = "tool"


class ReviewDecision(StrEnum):
    COMPLETE = "complete"
    CONTINUE = "continue"
    REFLECT = "reflect"
    REPLAN = "replan"
    FAIL = "fail"


class OrchestrationTermination(StrEnum):
    COMPLETED = "completed"
    INVALID_PLAN = "invalid_plan"
    INVALID_DEPENDENCY = "invalid_dependency"
    CYCLIC_DEPENDENCY = "cyclic_dependency"
    DEADLOCK = "deadlock"
    NO_READY_TASK = "no_ready_task"
    MAX_STEPS_EXCEEDED = "max_orchestration_steps_exceeded"
    MAX_REPLANS_EXCEEDED = "max_replans_exceeded"
    MAX_REFLECTIONS_EXCEEDED = "max_reflections_exceeded"
    MODEL_CALL_BUDGET_EXHAUSTED = "model_call_budget_exhausted"
    TOOL_CALL_BUDGET_EXHAUSTED = "tool_call_budget_exhausted"
    PROVIDER_FAILURE = "provider_failure"
    EXECUTOR_FAILURE = "executor_failure"
    REVIEWER_FAILURE = "reviewer_failure"
    MEMORY_FAILURE = "memory_failure"
    UNSAFE_RETRY_BLOCKED = "unsafe_retry_blocked"
    REPEATED_TASK = "repeated_task"
    REPEATED_PLAN = "repeated_plan"
    UNSUPPORTED_COMPLETION_CLAIM = "unsupported_completion_claim"


class PlannedToolCall(OrchestrationModel):
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlanTask(OrchestrationModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    objective: str = Field(min_length=3, max_length=500)
    execution_kind: TaskExecutionKind = TaskExecutionKind.TOOL
    dependencies: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    tool_call: PlannedToolCall | None = None
    priority: int = Field(default=100, ge=0, le=1000)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    result: dict[str, Any] | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_tool_contract(self) -> PlanTask:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Task dependencies must be unique")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("Required tools must be unique")
        if self.execution_kind == TaskExecutionKind.TOOL and self.tool_call is None:
            raise ValueError("A tool task requires an executable tool call")
        if self.tool_call is not None and self.required_tools != [self.tool_call.name]:
            raise ValueError("required_tools must exactly describe the planned tool call")
        return self


class TaskPlan(OrchestrationModel):
    plan_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=3, max_length=4000)
    tasks: list[PlanTask] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_task_ids(self) -> TaskPlan:
        task_ids = [task.task_id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Task ids must be unique")
        return self


class ToolEvidence(OrchestrationModel):
    task_id: str
    tool_name: str | None = None
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    failure_type: str | None = None
    retryable: bool = False
    ambiguous: bool = False


class TaskExecution(OrchestrationModel):
    task_id: str
    status: TaskStatus
    attempt: int = Field(ge=1)
    tool_name: str | None = None
    failure_type: str | None = None


class Reflection(OrchestrationModel):
    reflection_id: str
    task_id: str | None = None
    failure_type: str
    reason: str
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_action: ReviewDecision


class ReviewResult(OrchestrationModel):
    completed: bool
    decision: ReviewDecision
    failure_type: str | None = None
    reason: str
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_action: ReviewDecision | None = None


class EpisodeSummary(OrchestrationModel):
    run_id: str
    goal: str
    outcome: str
    termination_reason: str
    score: int = Field(default=0, ge=0)
    plan_summary: list[str] = Field(default_factory=list)
    evidence_summary: list[dict[str, Any]] = Field(default_factory=list)
    reflection_summary: list[str] = Field(default_factory=list)
    created_at: datetime


class PlannerContext(OrchestrationModel):
    goal: str
    available_tools: list[dict[str, Any]]
    retrieved_episodes: list[EpisodeSummary] = Field(default_factory=list)
    reflections: list[Reflection] = Field(default_factory=list)
    previous_plan: TaskPlan | None = None


class WorkingMemory(OrchestrationModel):
    goal: str
    current_plan: TaskPlan | None = None
    tool_evidence: list[ToolEvidence] = Field(default_factory=list)
    reflection_history: list[Reflection] = Field(default_factory=list)
    execution_history: list[TaskExecution] = Field(default_factory=list)
    retrieved_episodes: list[EpisodeSummary] = Field(default_factory=list)
    review_history: list[ReviewResult] = Field(default_factory=list)
    final_outcome: str | None = None


class OrchestrationCounters(OrchestrationModel):
    orchestration_steps: int = Field(default=0, ge=0)
    planner_calls: int = Field(default=0, ge=0)
    reviewer_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    reflections: int = Field(default=0, ge=0)


class OrchestrationConfig(OrchestrationModel):
    max_orchestration_steps: int = Field(default=64, ge=4, le=1000)
    max_replans: int = Field(default=2, ge=0, le=20)
    max_reflections: int = Field(default=3, ge=0, le=50)
    max_task_attempts: int = Field(default=3, ge=1, le=20)
    repeated_task_limit: int = Field(default=3, ge=1, le=20)
    repeated_plan_limit: int = Field(default=2, ge=1, le=20)
    memory_retrieval_limit: int = Field(default=3, ge=0, le=20)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)


class OrchestrationResult(OrchestrationModel):
    run_id: str
    status: str
    goal: str
    termination_reason: OrchestrationTermination
    final_response: str
    plan: TaskPlan | None = None
    task_execution_order: list[str] = Field(default_factory=list)
    tool_sequence: list[str] = Field(default_factory=list)
    memory_retrieved: list[EpisodeSummary] = Field(default_factory=list)
    reviews: list[ReviewResult] = Field(default_factory=list)
    reflections: list[Reflection] = Field(default_factory=list)
    evidence: list[ToolEvidence] = Field(default_factory=list)
    counters: OrchestrationCounters
    latency_ms: float = Field(ge=0)
    token_usage: dict[str, int] | None = None
    estimated_cost: float | None = None

    @model_validator(mode="after")
    def reject_fabricated_cost(self) -> OrchestrationResult:
        if self.token_usage is None and self.estimated_cost is not None:
            raise ValueError("Cost cannot be estimated without real usage metadata")
        return self
