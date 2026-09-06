from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from app.agent.providers import LLMProvider, ProviderError
from app.agent.types import AgentResult, ToolEvent
from app.reliability import ReliabilityConfig, ReliabilityController, TerminationReason


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

    def run(self, task: str) -> AgentResult:
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
                    turn = self.provider.complete(
                        messages,
                        self.tools.schemas(),
                        timeout_seconds=self.reliability.timeouts.provider_seconds,
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
                    control.retry(self.reliability.provider_retry, provider_attempt)
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
                    result = self._execute_tool(call.name, call.arguments)
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
                            control.retry(self.reliability.tool_retry, tool_attempt)
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

        return self._terminal(
            TerminationReason.STEP_BUDGET_EXHAUSTED,
            f"Agent stopped after reaching the maximum of {self.max_steps} steps.",
            steps,
            events,
            control,
            status="max_steps_exceeded",
        )

    def _execute_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        try:
            result = self.tools.execute(
                name,
                arguments,
                timeout_seconds=self.reliability.timeouts.tool_seconds,
            )
        except TimeoutError as exc:
            return {
                "ok": False,
                "error": {
                    "type": "tool_timeout",
                    "message": str(exc) or f"Tool {name} exceeded its timeout",
                    "retryable": True,
                    "ambiguous": True,
                },
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "type": "tool_execution_error",
                    "message": str(exc),
                },
            }
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return {
                "ok": False,
                "error": {
                    "type": "malformed_tool_result",
                    "message": f"Tool {name} returned an unusable result",
                },
            }
        return result

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

    @staticmethod
    def _result(
        *,
        status: str,
        response: str,
        steps: int,
        events: list[ToolEvent],
        control: ReliabilityController,
        termination_reason: TerminationReason | None = None,
    ) -> AgentResult:
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

    @classmethod
    def _terminal(
        cls,
        reason: TerminationReason,
        response: str,
        steps: int,
        events: list[ToolEvent],
        control: ReliabilityController,
        *,
        status: str = "reliability_failure",
    ) -> AgentResult:
        return cls._result(
            status=status,
            response=response,
            steps=steps,
            events=events,
            control=control,
            termination_reason=reason,
        )
