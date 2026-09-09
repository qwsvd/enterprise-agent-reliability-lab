from __future__ import annotations

from typing import Any, TypedDict

from app.orchestration.models import (
    OrchestrationConfig,
    OrchestrationCounters,
    OrchestrationTermination,
    ReviewDecision,
    TaskPlan,
    WorkingMemory,
)


class OrchestrationState(TypedDict):
    run_id: str
    goal: str
    config: OrchestrationConfig
    memory: WorkingMemory
    plan: TaskPlan | None
    counters: OrchestrationCounters
    current_task_id: str | None
    route: str
    review_decision: ReviewDecision | None
    termination_reason: OrchestrationTermination | None
    final_response: str
    plan_fingerprints: dict[str, int]
    task_fingerprints: dict[str, int]
    side_effect_fingerprints: set[str]
    last_error: dict[str, Any] | None
