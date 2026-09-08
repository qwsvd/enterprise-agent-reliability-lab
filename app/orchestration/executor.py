from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.runtime import AgentTools
from app.orchestration.models import (
    OrchestrationConfig,
    OrchestrationCounters,
    OrchestrationTermination,
    PlanTask,
    TaskExecution,
    TaskStatus,
    ToolEvidence,
    WorkingMemory,
)
from app.reliability import canonical_tool_call


class ExecutorError(RuntimeError):
    def __init__(self, reason: OrchestrationTermination, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ExecutionOutcome:
    task: PlanTask
    evidence: ToolEvidence
    execution: TaskExecution
    fingerprint: str | None = None


def _resolve_reference(reference: str, memory: WorkingMemory) -> Any:
    task_id, separator, path = reference.partition(".")
    if not separator or not path:
        raise ValueError("Evidence references must use task_id.field syntax")
    evidence = next(
        (item for item in reversed(memory.tool_evidence) if item.task_id == task_id and item.ok),
        None,
    )
    if evidence is None:
        raise ValueError(f"No successful evidence is available for task {task_id}")
    value: Any = evidence.data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"Evidence reference {reference} is unavailable")
        value = value[part]
    return value


def resolve_arguments(value: Any, memory: WorkingMemory) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise ValueError("Evidence reference must be a string")
            return _resolve_reference(reference, memory)
        return {key: resolve_arguments(item, memory) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_arguments(item, memory) for item in value]
    return value


class ExecutorAgent:
    """Execute one dependency-ready task through the existing AgentTools boundary."""

    def __init__(self, tools: AgentTools) -> None:
        self.tools = tools
        self._tool_names: set[str] = set()

    def prepare(self) -> None:
        prepare = getattr(self.tools, "prepare", None)
        if callable(prepare):
            prepare()
        self._tool_names = {
            item["function"]["name"]
            for item in self.tools.schemas()
            if isinstance(item.get("function"), dict)
            and isinstance(item["function"].get("name"), str)
        }

    def execute(
        self,
        task: PlanTask,
        memory: WorkingMemory,
        *,
        config: OrchestrationConfig,
        counters: OrchestrationCounters,
        repeated_calls: dict[str, int],
        side_effect_calls: set[str],
    ) -> ExecutionOutcome:
        task.attempts += 1
        task.status = TaskStatus.RUNNING
        if task.tool_call is None:
            task.status = TaskStatus.COMPLETED
            task.result = {"ok": True, "data": {"objective_completed": True}}
            evidence = ToolEvidence(task_id=task.task_id, ok=True, data=task.result["data"])
            return ExecutionOutcome(
                task=task,
                evidence=evidence,
                execution=TaskExecution(
                    task_id=task.task_id,
                    status=task.status,
                    attempt=task.attempts,
                ),
            )

        call = task.tool_call
        if call.name not in self._tool_names:
            return self._failed(task, call.name, "unknown_tool", retryable=False)
        if counters.tool_calls >= config.reliability.max_tool_calls:
            raise ExecutorError(
                OrchestrationTermination.TOOL_CALL_BUDGET_EXHAUSTED,
                "Tool-call budget was exhausted before the next task execution",
            )
        try:
            arguments = resolve_arguments(call.arguments, memory)
        except ValueError:
            return self._failed(task, call.name, "invalid_evidence_reference", retryable=False)

        fingerprint = canonical_tool_call(call.name, arguments)
        repetitions = repeated_calls.get(fingerprint, 0)
        if repetitions >= config.repeated_task_limit:
            raise ExecutorError(
                OrchestrationTermination.REPEATED_TASK,
                "Repeated task execution limit was reached",
            )
        repeated_calls[fingerprint] = repetitions + 1

        side_effecting = call.name in config.reliability.side_effecting_tools
        idempotent = call.name in config.reliability.idempotent_side_effecting_tools
        if side_effecting and not idempotent and fingerprint in side_effect_calls:
            raise ExecutorError(
                OrchestrationTermination.UNSAFE_RETRY_BLOCKED,
                "Replay of a non-idempotent side effect was blocked",
            )
        if side_effecting:
            side_effect_calls.add(fingerprint)

        counters.tool_calls += 1
        try:
            result = self.tools.execute(
                call.name,
                arguments,
                timeout_seconds=config.reliability.timeouts.tool_seconds,
            )
        except Exception:
            result = {
                "ok": False,
                "error": {
                    "type": "executor_exception",
                    "message": "Tool execution raised an internal exception",
                },
            }

        if result.get("ok") is True and isinstance(result.get("data"), dict):
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.failure_reason = None
            evidence = ToolEvidence(
                task_id=task.task_id,
                tool_name=call.name,
                ok=True,
                data=result["data"],
            )
        else:
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            failure_type = str(error.get("type", "tool_failure"))
            retryable = error.get("retryable") is True
            ambiguous = error.get("ambiguous") is True
            task.status = TaskStatus.FAILED
            task.failure_reason = failure_type
            task.result = result
            counters.failures += 1
            evidence = ToolEvidence(
                task_id=task.task_id,
                tool_name=call.name,
                ok=False,
                failure_type=failure_type,
                retryable=retryable,
                ambiguous=ambiguous,
            )
        execution = TaskExecution(
            task_id=task.task_id,
            status=task.status,
            attempt=task.attempts,
            tool_name=call.name,
            failure_type=evidence.failure_type,
        )
        return ExecutionOutcome(task, evidence, execution, fingerprint)

    @staticmethod
    def _failed(
        task: PlanTask, tool_name: str, failure_type: str, *, retryable: bool
    ) -> ExecutionOutcome:
        task.status = TaskStatus.FAILED
        task.failure_reason = failure_type
        task.result = {"ok": False, "error": {"type": failure_type}}
        evidence = ToolEvidence(
            task_id=task.task_id,
            tool_name=tool_name,
            ok=False,
            failure_type=failure_type,
            retryable=retryable,
        )
        return ExecutionOutcome(
            task=task,
            evidence=evidence,
            execution=TaskExecution(
                task_id=task.task_id,
                status=task.status,
                attempt=task.attempts,
                tool_name=tool_name,
                failure_type=failure_type,
            ),
        )
