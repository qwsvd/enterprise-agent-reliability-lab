from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from opentelemetry.trace import Span, Tracer

from app.agent.providers import LLMProvider, ProviderError
from app.agent.types import AgentResult, ToolEvent
from app.reliability import (
    ReliabilityConfig,
    ReliabilityController,
    RetryPolicy,
    TerminationReason,
)
from app.tracing import get_tracer, mark_failure, mark_success, safe_identifier


SYSTEM_PROMPT = (
    "You are an after-sales assistant. Inspect business state with the available tools, "
    "use write tools only when appropriate, and accurately distinguish approved refunds "
    "from requests pending human approval."
)


class AgentTools(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]: ...


class AgentRuntime:
    def __init__(
        self,
        provider: LLMProvider,
        tools: AgentTools,
        *,
        max_steps: int | None = None,
        reliability: ReliabilityConfig | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        tracer: Tracer | None = None,
        run_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        config = reliability or ReliabilityConfig()
        if max_steps is not None:
            if max_steps < 1:
                raise ValueError("max_steps must be at least 1")
            config = ReliabilityConfig.model_validate(
                {**config.model_dump(), "max_steps": max_steps}
            )
        self.provider = provider
        self.tools = tools
        self.reliability = config
        self.max_steps = config.max_steps
        self.sleeper = sleeper
        self.tracer = get_tracer(tracer)
        self.run_id_factory = run_id_factory
        self._active_step_scope: Any = None
        self._active_step_span: Span | None = None
        self._traceable_tool_names: set[str] = set()
        tracer_setter = getattr(self.tools, "set_tracer", None)
        if callable(tracer_setter):
            tracer_setter(self.tracer)

    def run(self, task: str) -> AgentResult:
        run_id = self.run_id_factory()
        with self.tracer.start_as_current_span(
            "agent.run",
            attributes={"agent.run.id": safe_identifier(run_id)},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = self._run(task)
            except Exception:
                self._end_active_step("internal_error", failure="internal_error")
                mark_failure(span, "internal_error")
                raise
            result.run_id = run_id
            termination = (
                result.termination_reason.value
                if result.termination_reason is not None
                else "completed"
            )
            summary = {
                "agent.status": result.status,
                "agent.termination.reason": termination,
                "agent.steps": result.steps,
                "agent.model.calls": result.model_calls,
                "agent.tool.calls": result.tool_calls,
                "agent.retries": result.retries,
                "agent.failures": result.failures,
            }
            for key, value in summary.items():
                span.set_attribute(key, value)
            span.add_event("agent.terminated", summary)
            if result.status == "completed":
                mark_success(span)
            else:
                mark_failure(span, termination)
            return result

    def _run(self, task: str) -> AgentResult:
        prepare = getattr(self.tools, "prepare", None)
        if callable(prepare):
            prepare()
        self._traceable_tool_names = {
            item["function"]["name"]
            for item in self.tools.schemas()
            if isinstance(item.get("function"), dict)
            and isinstance(item["function"].get("name"), str)
        }
        system_prompt = SYSTEM_PROMPT
        context = getattr(self.tools, "context", None)
        if callable(context):
            system_prompt = f"{system_prompt}\n\n{context()}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        events: list[ToolEvent] = []
        control = ReliabilityController(self.reliability, self.sleeper)
        steps = 0

        while steps < self.reliability.max_steps:
            budget_reason = control.model_budget_reason()
            if budget_reason is not None:
                return self._terminal(
                    budget_reason,
                    "Agent stopped before another provider call because its model-call "
                    "budget was exhausted.",
                    steps,
                    events,
                    control,
                )

            steps += 1
            self._start_step(steps)
            provider_attempt = 1
            while True:
                budget_reason = control.model_budget_reason()
                if budget_reason is not None:
                    return self._terminal(
                        budget_reason,
                        "Agent stopped before another provider call because its model-call "
                        "budget was exhausted.",
                        steps,
                        events,
                        control,
                    )
                control.model_calls += 1
                try:
                    turn = self._complete_provider(
                        messages, step=steps, attempt=provider_attempt
                    )
                except ProviderError as exc:
                    control.record_failure(
                        operation="provider",
                        kind=exc.kind,
                        message=str(exc),
                        retryable=exc.retryable,
                        attempt=provider_attempt,
                    )
                    if not exc.retryable:
                        return self._terminal(
                            TerminationReason.PROVIDER_FAILURE,
                            str(exc),
                            steps,
                            events,
                            control,
                            status="provider_error",
                        )
                    if provider_attempt >= self.reliability.provider_retry.max_attempts:
                        reason = (
                            TerminationReason.PROVIDER_TIMEOUT
                            if exc.kind == "provider_timeout"
                            else TerminationReason.PROVIDER_RETRY_EXHAUSTED
                        )
                        return self._terminal(
                            reason,
                            f"Provider retries were exhausted: {exc}",
                            steps,
                            events,
                            control,
                            status="provider_error",
                        )
                    failure_reason = control.failure_limit_reason()
                    if failure_reason is not None:
                        return self._terminal(
                            failure_reason,
                            "Agent stopped after consecutive provider failures reached the "
                            "configured limit.",
                            steps,
                            events,
                            control,
                        )
                    if control.model_budget_reason() is not None:
                        return self._terminal(
                            TerminationReason.MODEL_CALL_BUDGET_EXHAUSTED,
                            "Agent stopped before retrying the provider because its model-call "
                            "budget was exhausted.",
                            steps,
                            events,
                            control,
                        )
                    provider_attempt += 1
                    self._retry(
                        control,
                        self.reliability.provider_retry,
                        provider_attempt,
                        operation="provider",
                        step=steps,
                    )
                    continue
                control.record_success()
                break

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": turn.content,
            }
            if turn.tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, default=str),
                        },
                    }
                    for call in turn.tool_calls
                ]
            messages.append(assistant_message)

            if not turn.tool_calls:
                return self._result(
                    status="completed",
                    response=turn.content or "",
                    steps=steps,
                    events=events,
                    control=control,
                )

            for call in turn.tool_calls:
                budget_reason = control.tool_budget_reason()
                if budget_reason is not None:
                    return self._terminal(
                        budget_reason,
                        "Agent stopped before another tool execution because its tool-call "
                        "budget was exhausted.",
                        steps,
                        events,
                        control,
                    )
                repeated_reason = control.repeated_call_reason(call.name, call.arguments)
                if repeated_reason is not None:
                    return self._terminal(
                        repeated_reason,
                        f"Blocked repeated tool call: {call.name} with identical arguments.",
                        steps,
                        events,
                        control,
                    )

                tool_attempt = 1
                call_recorded = False
                while True:
                    if control.tool_budget_reason() is not None:
                        return self._terminal(
                            TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
                            "Agent stopped before retrying a tool because its tool-call budget "
                            "was exhausted.",
                            steps,
                            events,
                            control,
                        )
                    control.tool_calls += 1
                    result = self._execute_tool(
                        call.name,
                        call.arguments,
                        step=steps,
                        attempt=tool_attempt,
                    )
                    if not call_recorded and control.tool_result_is_validated(result):
                        control.record_validated_call(call.name, call.arguments)
                        call_recorded = True

                    if result.get("ok") is True:
                        control.record_success()
                        events.append(
                            ToolEvent(
                                call=call,
                                result=result,
                                attempts=tool_attempt,
                                retries=tool_attempt - 1,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(result, default=str),
                            }
                        )
                        break

                    error = self._tool_error(result)
                    control.record_failure(
                        operation="tool",
                        kind=error["type"],
                        message=error["message"],
                        retryable=error["retryable"],
                        attempt=tool_attempt,
                        tool_name=call.name,
                    )
                    can_attempt_retry = (
                        error["retryable"]
                        and tool_attempt < self.reliability.tool_retry.max_attempts
                    )
                    if can_attempt_retry and not control.tool_retry_is_safe(
                        call.name, call.arguments
                    ):
                        events.append(
                            ToolEvent(
                                call=call,
                                result=result,
                                attempts=tool_attempt,
                                retries=tool_attempt - 1,
                            )
                        )
                        return self._terminal(
                            TerminationReason.UNSAFE_RETRY_BLOCKED,
                            f"Automatic retry of side-effecting tool {call.name} was "
                            "blocked because it has no strong idempotency guarantee.",
                            steps,
                            events,
                            control,
                        )
                    failure_reason = control.failure_limit_reason()
                    if failure_reason is not None:
                        events.append(
                            ToolEvent(
                                call=call,
                                result=result,
                                attempts=tool_attempt,
                                retries=tool_attempt - 1,
                            )
                        )
                        return self._terminal(
                            failure_reason,
                            "Agent stopped after consecutive execution failures reached the "
                            "configured limit.",
                            steps,
                            events,
                            control,
                        )

                    if error["retryable"]:
                        if tool_attempt < self.reliability.tool_retry.max_attempts:
                            if control.tool_budget_reason() is not None:
                                events.append(
                                    ToolEvent(
                                        call=call,
                                        result=result,
                                        attempts=tool_attempt,
                                        retries=tool_attempt - 1,
                                    )
                                )
                                return self._terminal(
                                    TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
                                    "Agent stopped before retrying a tool because its tool-call "
                                    "budget was exhausted.",
                                    steps,
                                    events,
                                    control,
                                )
                            tool_attempt += 1
                            self._retry(
                                control,
                                self.reliability.tool_retry,
                                tool_attempt,
                                operation="tool",
                                step=steps,
                                tool_name=call.name,
                            )
                            continue

                        events.append(
                            ToolEvent(
                                call=call,
                                result=result,
                                attempts=tool_attempt,
                                retries=tool_attempt - 1,
                            )
                        )
                        reason = (
                            TerminationReason.TOOL_TIMEOUT
                            if error["type"] == "tool_timeout"
                            else TerminationReason.TOOL_FAILURE
                        )
                        return self._terminal(
                            reason,
                            f"Tool {call.name} failed after its retry limit was exhausted: "
                            f"{error['message']}",
                            steps,
                            events,
                            control,
                        )

                    if error["type"] == "tool_timeout":
                        events.append(
                            ToolEvent(call=call, result=result, attempts=tool_attempt)
                        )
                        return self._terminal(
                            TerminationReason.TOOL_TIMEOUT,
                            f"Tool {call.name} timed out: {error['message']}",
                            steps,
                            events,
                            control,
                        )

                    events.append(
                        ToolEvent(
                            call=call,
                            result=result,
                            attempts=tool_attempt,
                            retries=tool_attempt - 1,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
                    break

            if steps >= self.reliability.max_steps:
                return self._terminal(
                    TerminationReason.STEP_BUDGET_EXHAUSTED,
                    f"Agent stopped after reaching the maximum of {self.max_steps} steps.",
                    steps,
                    events,
                    control,
                    status="max_steps_exceeded",
                )
            self._end_active_step("continued")

        raise AssertionError("Agent loop exited without a terminal result")

    def _complete_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        step: int,
        attempt: int,
    ):
        attributes: dict[str, str | bool | int | float] = {
            "agent.step.number": step,
            "provider.name": safe_identifier(type(self.provider).__name__),
            "provider.attempt": attempt,
            "provider.retry.number": attempt - 1,
            "operation.timeout.seconds": self.reliability.timeouts.provider_seconds,
        }
        model = getattr(self.provider, "model", None)
        if isinstance(model, str) and model:
            attributes["provider.model"] = safe_identifier(model)
        with self.tracer.start_as_current_span(
            "agent.provider.call",
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                turn = self.provider.complete(
                    messages,
                    self.tools.schemas(),
                    timeout_seconds=self.reliability.timeouts.provider_seconds,
                )
            except ProviderError as exc:
                category = safe_identifier(exc.kind)
                mark_failure(span, category, retryable=exc.retryable)
                span.add_event(
                    "reliability.failure",
                    {
                        "failure.category": category,
                        "failure.retryable": exc.retryable,
                        "operation.attempt": attempt,
                    },
                )
                raise
            span.set_attribute("provider.tool_call.count", len(turn.tool_calls))
            span.set_attribute("provider.final_response", not bool(turn.tool_calls))
            mark_success(span)
            return turn

    def _execute_tool(
        self,
        name: str,
        arguments: Any,
        *,
        step: int,
        attempt: int,
    ) -> dict[str, Any]:
        with self.tracer.start_as_current_span(
            "agent.tool.call",
            attributes={
                "agent.step.number": step,
                "tool.name": self._safe_tool_name(name),
                "tool.attempt": attempt,
                "tool.retry.number": attempt - 1,
                "operation.timeout.seconds": self.reliability.timeouts.tool_seconds,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = self.tools.execute(
                    name,
                    arguments,
                    timeout_seconds=self.reliability.timeouts.tool_seconds,
                )
            except TimeoutError as exc:
                result = {
                    "ok": False,
                    "error": {
                        "type": "tool_timeout",
                        "message": str(exc) or f"Tool {name} exceeded its timeout",
                        "retryable": True,
                        "ambiguous": True,
                    },
                }
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": {
                        "type": "tool_execution_error",
                        "message": str(exc),
                    },
                }
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                result = {
                    "ok": False,
                    "error": {
                        "type": "malformed_tool_result",
                        "message": f"Tool {name} returned an unusable result",
                    },
                }
            if result.get("ok") is True:
                mark_success(span)
                data = result.get("data")
                if name == "create_refund" and isinstance(data, dict):
                    status = data.get("status")
                    completed = data.get("completed")
                    if isinstance(status, str):
                        span.set_attribute("business.refund.status", status)
                    if isinstance(completed, bool):
                        span.set_attribute("business.refund.completed", completed)
                if name == "load_skill" and isinstance(data, dict):
                    selected = data.get("name")
                    if isinstance(selected, str):
                        span.set_attribute("skill.selected", safe_identifier(selected))
            else:
                error = self._tool_error(result)
                category = safe_identifier(error["type"])
                mark_failure(
                    span,
                    category,
                    retryable=error["retryable"],
                )
                span.add_event(
                    "reliability.failure",
                    {
                        "failure.category": category,
                        "failure.retryable": error["retryable"],
                        "operation.attempt": attempt,
                    },
                )
            return result

    def _retry(
        self,
        control: ReliabilityController,
        policy: RetryPolicy,
        next_attempt: int,
        *,
        operation: str,
        step: int,
        tool_name: str | None = None,
    ) -> None:
        delay = policy.delay_before_attempt(next_attempt)
        attributes: dict[str, str | int | float] = {
            "agent.step.number": step,
            "retry.operation": operation,
            "retry.attempt": next_attempt,
            "retry.number": next_attempt - 1,
            "retry.backoff.seconds": delay,
        }
        if tool_name is not None:
            attributes["tool.name"] = self._safe_tool_name(tool_name)
        with self.tracer.start_as_current_span(
            "reliability.retry",
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            control.retry(policy, next_attempt)
            mark_success(span)

    def _safe_tool_name(self, name: str) -> str:
        if name not in self._traceable_tool_names:
            return "unknown"
        return safe_identifier(name)

    @staticmethod
    def _tool_error(result: dict[str, Any]) -> dict[str, Any]:
        error = result.get("error")
        if not isinstance(error, dict):
            return {
                "type": "malformed_tool_result",
                "message": "Tool failure did not include a structured error",
                "retryable": False,
            }
        return {
            "type": str(error.get("type", "tool_failure")),
            "message": str(error.get("message", "Tool execution failed")),
            "retryable": error.get("retryable") is True,
        }

    def _start_step(self, step: int) -> None:
        self._active_step_scope = self.tracer.start_as_current_span(
            "agent.step",
            attributes={"agent.step.number": step},
            record_exception=False,
            set_status_on_exception=False,
        )
        self._active_step_span = self._active_step_scope.__enter__()

    def _end_active_step(
        self, outcome: str, *, failure: str | None = None
    ) -> None:
        if self._active_step_span is None or self._active_step_scope is None:
            return
        self._active_step_span.set_attribute("agent.step.outcome", outcome)
        if failure is None:
            mark_success(self._active_step_span)
        else:
            mark_failure(self._active_step_span, failure)
        self._active_step_scope.__exit__(None, None, None)
        self._active_step_scope = None
        self._active_step_span = None

    def _result(
        self,
        *,
        status: str,
        response: str,
        steps: int,
        events: list[ToolEvent],
        control: ReliabilityController,
        termination_reason: TerminationReason | None = None,
    ) -> AgentResult:
        failure = (
            termination_reason.value
            if termination_reason is not None and status != "completed"
            else None
        )
        self._end_active_step(status, failure=failure)
        return AgentResult(
            status=status,
            response=response,
            steps=steps,
            tool_events=events,
            termination_reason=termination_reason,
            model_calls=control.model_calls,
            tool_calls=control.tool_calls,
            retries=control.retries,
            failures=len(control.failures),
            consecutive_failures=control.consecutive_failures,
            failure_details=control.failures,
        )

    def _terminal(
        self,
        reason: TerminationReason,
        response: str,
        steps: int,
        events: list[ToolEvent],
        control: ReliabilityController,
        *,
        status: str = "reliability_failure",
    ) -> AgentResult:
        return self._result(
            status=status,
            response=response,
            steps=steps,
            events=events,
            control=control,
            termination_reason=reason,
        )
