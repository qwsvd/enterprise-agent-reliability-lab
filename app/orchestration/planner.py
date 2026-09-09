from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from app.agent.providers import LLMProvider, ProviderError
from app.orchestration.models import PlannerContext, TaskPlan


class PlanningError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "invalid_plan", retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class PlannerAgent(Protocol):
    uses_model: bool

    def plan(
        self, context: PlannerContext, *, timeout_seconds: float | None = None
    ) -> TaskPlan: ...


class ScriptedPlanner:
    """Deterministic structured planner for offline tests, demos, and evals."""

    uses_model = False

    def __init__(self, plans: list[TaskPlan | PlanningError | ProviderError]) -> None:
        self._plans = list(plans)
        self.contexts: list[PlannerContext] = []

    def plan(
        self, context: PlannerContext, *, timeout_seconds: float | None = None
    ) -> TaskPlan:
        del timeout_seconds
        self.contexts.append(context.model_copy(deep=True))
        if not self._plans:
            raise PlanningError("Scripted planner has no plan remaining")
        item = self._plans.pop(0)
        if isinstance(item, Exception):
            raise item
        return item.model_copy(deep=True)


class DeterministicPlanner:
    """Offline planner whose validated plan factory can react to supplied context."""

    uses_model = False

    def __init__(self, factory: Callable[[PlannerContext], TaskPlan]) -> None:
        self.factory = factory
        self.contexts: list[PlannerContext] = []

    def plan(
        self, context: PlannerContext, *, timeout_seconds: float | None = None
    ) -> TaskPlan:
        del timeout_seconds
        snapshot = context.model_copy(deep=True)
        self.contexts.append(snapshot)
        return TaskPlan.model_validate(self.factory(snapshot).model_dump())


class ProviderPlanner:
    """Strict JSON planner backed by the existing provider-neutral LLM protocol."""

    uses_model = True

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    @property
    def model(self) -> str | None:
        value = getattr(self.provider, "model", None)
        return value if isinstance(value, str) else None

    def plan(
        self, context: PlannerContext, *, timeout_seconds: float | None = None
    ) -> TaskPlan:
        schema = TaskPlan.model_json_schema()
        prompt = {
            "goal": context.goal,
            "available_tools": context.available_tools,
            "retrieved_episodes": [
                episode.model_dump(mode="json") for episode in context.retrieved_episodes
            ],
            "reflections": [item.model_dump(mode="json") for item in context.reflections],
            "previous_plan": (
                context.previous_plan.model_dump(mode="json")
                if context.previous_plan is not None
                else None
            ),
        }
        response = self.provider.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are the Planner Agent. Return one JSON object that strictly "
                        "matches the supplied TaskPlan schema. Build a dependency graph, "
                        "use only available tools, and never mutate business state yourself."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task_plan_schema": schema, "planning_context": prompt},
                        sort_keys=True,
                        default=str,
                    ),
                },
            ],
            [],
            timeout_seconds=timeout_seconds,
        )
        if response.tool_calls or not response.content:
            raise PlanningError("Planner response must contain only a TaskPlan JSON object")
        try:
            return TaskPlan.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise PlanningError("Planner returned invalid structured output") from exc
