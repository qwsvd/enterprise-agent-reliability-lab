from __future__ import annotations

import json
from typing import Any

from app.orchestration.models import (
    OrchestrationTermination,
    PlanTask,
    TaskPlan,
    TaskStatus,
)


class SchedulerError(RuntimeError):
    def __init__(self, reason: OrchestrationTermination, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class DependencyScheduler:
    """Validate and select work from a real dependency graph."""

    def validate(self, plan: TaskPlan) -> None:
        task_ids = {task.task_id for task in plan.tasks}
        for task in plan.tasks:
            unknown = sorted(set(task.dependencies) - task_ids)
            if unknown:
                raise SchedulerError(
                    OrchestrationTermination.INVALID_DEPENDENCY,
                    f"Task {task.task_id} references unknown dependencies: {', '.join(unknown)}",
                )
            if task.task_id in task.dependencies:
                raise SchedulerError(
                    OrchestrationTermination.INVALID_DEPENDENCY,
                    f"Task {task.task_id} cannot depend on itself",
                )

        dependencies = {task.task_id: task.dependencies for task in plan.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise SchedulerError(
                    OrchestrationTermination.CYCLIC_DEPENDENCY,
                    "Task plan contains a dependency cycle",
                )
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependencies:
            visit(task_id)

    def select(self, plan: TaskPlan) -> PlanTask | None:
        self.validate(plan)
        statuses = {task.task_id: task.status for task in plan.tasks}
        for task in plan.tasks:
            if task.status not in {TaskStatus.PENDING, TaskStatus.READY}:
                continue
            failed = [
                dependency
                for dependency in task.dependencies
                if statuses[dependency] in {TaskStatus.FAILED, TaskStatus.BLOCKED}
            ]
            if failed:
                task.status = TaskStatus.BLOCKED
                task.failure_reason = "blocked_by_failed_dependency"

        statuses = {task.task_id: task.status for task in plan.tasks}
        ready = [
            task
            for task in plan.tasks
            if task.status in {TaskStatus.PENDING, TaskStatus.READY}
            and all(statuses[item] == TaskStatus.COMPLETED for item in task.dependencies)
        ]
        for task in ready:
            task.status = TaskStatus.READY
        if not ready:
            return None
        order = {task.task_id: index for index, task in enumerate(plan.tasks)}
        return min(ready, key=lambda item: (item.priority, order[item.task_id]))

    @staticmethod
    def all_completed(plan: TaskPlan) -> bool:
        return all(task.status == TaskStatus.COMPLETED for task in plan.tasks)

    @staticmethod
    def unfinished(plan: TaskPlan) -> list[PlanTask]:
        return [task for task in plan.tasks if task.status != TaskStatus.COMPLETED]


def plan_fingerprint(plan: TaskPlan) -> str:
    payload: list[dict[str, Any]] = []
    for task in plan.tasks:
        payload.append(
            {
                "task_id": task.task_id,
                "objective": task.objective,
                "dependencies": sorted(task.dependencies),
                "required_tools": task.required_tools,
                "tool_call": (
                    task.tool_call.model_dump(mode="json")
                    if task.tool_call is not None
                    else None
                ),
                "priority": task.priority,
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
