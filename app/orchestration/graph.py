from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from opentelemetry.trace import Span, Tracer

from app.agent.providers import ProviderError
from app.agent.runtime import AgentTools
from app.orchestration.executor import ExecutorAgent, ExecutorError
from app.orchestration.memory import EpisodicMemoryStore
from app.orchestration.models import (
    OrchestrationConfig,
    OrchestrationCounters,
    OrchestrationResult,
    OrchestrationTermination,
    PlannerContext,
    Reflection,
    ReviewDecision,
    TaskPlan,
    TaskStatus,
    WorkingMemory,
)
from app.orchestration.planner import PlannerAgent, PlanningError
from app.orchestration.reviewer import (
    CompletionBuilder,
    EvidenceReviewer,
    ReviewerAgent,
    build_final_response,
)
from app.orchestration.scheduler import (
    DependencyScheduler,
    SchedulerError,
    plan_fingerprint,
)
from app.orchestration.state import OrchestrationState
from app.tracing import get_tracer, mark_failure, mark_success, safe_identifier


Route = Literal["planner", "scheduler", "executor", "reviewer", "reflection", "replan", "persist"]


class MultiAgentOrchestrator:
    """LangGraph Planner -> Scheduler -> Executor -> Reviewer orchestration runtime."""

    def __init__(
        self,
        *,
        planner: PlannerAgent,
        tools: AgentTools,
        memory_store: EpisodicMemoryStore,
        reviewer: ReviewerAgent | None = None,
        scheduler: DependencyScheduler | None = None,
        config: OrchestrationConfig | None = None,
        completion_builder: CompletionBuilder = build_final_response,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        tracer: Tracer | None = None,
        run_id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.memory_store = memory_store
        self.reviewer = reviewer or EvidenceReviewer()
        self.scheduler = scheduler or DependencyScheduler()
        self.config = config or OrchestrationConfig()
        self.completion_builder = completion_builder
        self.sleeper = sleeper
        self.clock = clock
        self.tracer = get_tracer(tracer)
        self.run_id_factory = run_id_factory
        self.executor = ExecutorAgent(tools)
        setter = getattr(self.tools, "set_tracer", None)
        if callable(setter):
            setter(self.tracer)
        self.graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(OrchestrationState)
        builder.add_node("retrieve_memory", self._retrieve_memory)
        builder.add_node("planner", self._planner)
        builder.add_node("scheduler", self._scheduler)
        builder.add_node("executor", self._executor)
        builder.add_node("reviewer", self._reviewer)
        builder.add_node("reflection", self._reflection)
        builder.add_node("replan", self._replan)
        builder.add_node("persist_memory", self._persist_memory)
        builder.add_edge(START, "retrieve_memory")
        builder.add_edge("retrieve_memory", "planner")
        builder.add_conditional_edges("planner", self._route)
        builder.add_conditional_edges("scheduler", self._route)
        builder.add_conditional_edges("executor", self._route)
        builder.add_conditional_edges("reviewer", self._route)
        builder.add_conditional_edges("reflection", self._route)
        builder.add_conditional_edges("replan", self._route)
        builder.add_edge("persist_memory", END)
        return builder.compile()

    @staticmethod
    def _route(state: OrchestrationState) -> str:
        mapping = {
            "planner": "planner",
            "scheduler": "scheduler",
            "executor": "executor",
            "reviewer": "reviewer",
            "reflection": "reflection",
            "replan": "replan",
            "persist": "persist_memory",
        }
        return mapping[state["route"]]

    def run(self, goal: str) -> OrchestrationResult:
        started = self.clock()
        state: OrchestrationState = {
            "run_id": self.run_id_factory(),
            "goal": goal,
            "config": self.config,
            "memory": WorkingMemory(goal=goal),
            "plan": None,
            "counters": OrchestrationCounters(),
            "current_task_id": None,
            "route": "planner",
            "review_decision": None,
            "termination_reason": None,
            "final_response": "",
            "plan_fingerprints": {},
            "task_fingerprints": {},
            "side_effect_fingerprints": set(),
            "last_error": None,
        }
        with self.tracer.start_as_current_span(
            "multi_agent.run",
            attributes={"agent.run.id": safe_identifier(state["run_id"])},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                self.executor.prepare()
            except Exception:
                state["termination_reason"] = OrchestrationTermination.EXECUTOR_FAILURE
                state["final_response"] = (
                    "Orchestration could not prepare its discovered tool boundary."
                )
                state["counters"].failures += 1
                persisted = self._persist_memory(state)
                state.update(cast(OrchestrationState, persisted))
                output = state
            else:
                try:
                    output = cast(
                        OrchestrationState,
                        self.graph.invoke(
                            state,
                            {
                                "recursion_limit": (
                                    self.config.max_orchestration_steps * 3 + 16
                                )
                            },
                        ),
                    )
                except Exception:
                    mark_failure(span, "orchestration_internal_error")
                    raise
            termination = output["termination_reason"] or OrchestrationTermination.REVIEWER_FAILURE
            counters = output["counters"]
            status = "completed" if termination == OrchestrationTermination.COMPLETED else "failed"
            attributes: dict[str, str | int] = {
                "agent.status": status,
                "agent.termination.reason": termination.value,
                "orchestration.steps": counters.orchestration_steps,
                "orchestration.replans": counters.replans,
                "orchestration.reflections": counters.reflections,
                "agent.model.calls": counters.model_calls,
                "agent.tool.calls": counters.tool_calls,
            }
            for key, value in attributes.items():
                span.set_attribute(key, value)
            span.add_event("multi_agent.terminated", attributes)
            if status == "completed":
                mark_success(span)
            else:
                mark_failure(span, termination.value)

        memory = output["memory"]
        plan = output["plan"]
        return OrchestrationResult(
            run_id=output["run_id"],
            status=status,
            goal=goal,
            termination_reason=termination,
            final_response=output["final_response"],
            plan=plan,
            task_execution_order=[item.task_id for item in memory.execution_history],
            tool_sequence=[
                item.tool_name for item in memory.tool_evidence if item.tool_name is not None
            ],
            memory_retrieved=memory.retrieved_episodes,
            reviews=memory.review_history,
            reflections=memory.reflection_history,
            evidence=memory.tool_evidence,
            counters=counters,
            latency_ms=max(0.0, (self.clock() - started) * 1000),
        )

    def _next_step(
        self, state: OrchestrationState
    ) -> tuple[OrchestrationCounters, dict[str, Any] | None]:
        counters = state["counters"].model_copy(deep=True)
        if counters.orchestration_steps >= state["config"].max_orchestration_steps:
            return counters, self._terminal_update(
                state,
                OrchestrationTermination.MAX_STEPS_EXCEEDED,
                "Orchestration stopped before the next node because its step budget was exhausted.",
                counters=counters,
            )
        counters.orchestration_steps += 1
        return counters, None

    def _retrieve_memory(self, state: OrchestrationState) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        memory = state["memory"].model_copy(deep=True)
        with self.tracer.start_as_current_span(
            "memory.retrieve",
            attributes={"agent.role": "memory"},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                memory.retrieved_episodes = self.memory_store.retrieve(
                    state["goal"], limit=state["config"].memory_retrieval_limit
                )
            except Exception:
                mark_failure(span, "memory_failure")
                return self._terminal_update(
                    state,
                    OrchestrationTermination.MEMORY_FAILURE,
                    "Episodic memory retrieval failed safely.",
                    counters=counters,
                    memory=memory,
                )
            span.set_attribute("memory.episode.count", len(memory.retrieved_episodes))
            mark_success(span)
        return {"memory": memory, "counters": counters, "route": "planner"}

    def _planner(self, state: OrchestrationState) -> dict[str, Any]:
        return self._invoke_planner(state, replan=False)

    def _replan(self, state: OrchestrationState) -> dict[str, Any]:
        return self._invoke_planner(state, replan=True)

    def _invoke_planner(
        self, state: OrchestrationState, *, replan: bool
    ) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        if replan:
            if counters.replans >= state["config"].max_replans:
                return self._terminal_update(
                    state,
                    OrchestrationTermination.MAX_REPLANS_EXCEEDED,
                    "The bounded replanning budget was exhausted.",
                    counters=counters,
                )
            counters.replans += 1
        span_name = "planner.replan" if replan else "planner.plan"
        context = PlannerContext(
            goal=state["goal"],
            available_tools=self.tools.schemas(),
            retrieved_episodes=state["memory"].retrieved_episodes,
            reflections=state["memory"].reflection_history,
            previous_plan=state["plan"],
        )
        attempt = 1
        planner_attributes: dict[str, str | int] = {
            "agent.role": "planner",
            "operation.attempt": attempt,
        }
        planner_model = getattr(self.planner, "model", None)
        if isinstance(planner_model, str):
            planner_attributes["provider.model"] = safe_identifier(planner_model)
        with self.tracer.start_as_current_span(
            span_name,
            attributes=planner_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            while True:
                if self.planner.uses_model:
                    if counters.model_calls >= state["config"].reliability.max_model_calls:
                        mark_failure(span, "model_call_budget_exhausted")
                        return self._terminal_update(
                            state,
                            OrchestrationTermination.MODEL_CALL_BUDGET_EXHAUSTED,
                            "Planner model-call budget was exhausted.",
                            counters=counters,
                        )
                    counters.model_calls += 1
                counters.planner_calls += 1
                try:
                    plan = self.planner.plan(
                        context,
                        timeout_seconds=state["config"].reliability.timeouts.provider_seconds,
                    )
                    self.scheduler.validate(plan)
                    break
                except SchedulerError as exc:
                    counters.failures += 1
                    mark_failure(span, exc.reason.value)
                    return self._terminal_update(
                        state, exc.reason, "Planner produced an invalid dependency graph.", counters=counters
                    )
                except (PlanningError, ProviderError) as exc:
                    counters.failures += 1
                    retryable = getattr(exc, "retryable", False)
                    if (
                        not retryable
                        or attempt >= state["config"].reliability.provider_retry.max_attempts
                    ):
                        reason = (
                            OrchestrationTermination.INVALID_PLAN
                            if isinstance(exc, PlanningError)
                            else OrchestrationTermination.PROVIDER_FAILURE
                        )
                        mark_failure(span, reason.value, retryable=retryable)
                        return self._terminal_update(
                            state,
                            reason,
                            "Planner failed to produce a usable structured plan.",
                            counters=counters,
                        )
                    attempt += 1
                    counters.retries += 1
                    span.add_event(
                        "planner.retry",
                        {"operation.attempt": attempt, "failure.retryable": True},
                    )
                    with self.tracer.start_as_current_span(
                        "reliability.retry",
                        attributes={
                            "agent.role": "planner",
                            "operation.attempt": attempt,
                            "failure.retryable": True,
                        },
                        record_exception=False,
                        set_status_on_exception=False,
                    ) as retry_span:
                        self.sleeper(
                            state["config"].reliability.provider_retry.delay_before_attempt(
                                attempt
                            )
                        )
                        mark_success(retry_span)

            fingerprint = plan_fingerprint(plan)
            fingerprints = dict(state["plan_fingerprints"])
            count = fingerprints.get(fingerprint, 0)
            if count >= state["config"].repeated_plan_limit:
                mark_failure(span, "repeated_plan")
                return self._terminal_update(
                    state,
                    OrchestrationTermination.REPEATED_PLAN,
                    "The planner repeated the same effective plan too many times.",
                    counters=counters,
                )
            fingerprints[fingerprint] = count + 1
            memory = state["memory"].model_copy(deep=True)
            memory.current_plan = plan.model_copy(deep=True)
            span.set_attribute("planner.task.count", len(plan.tasks))
            span.set_attribute("orchestration.replan.count", counters.replans)
            mark_success(span)
        return {
            "plan": plan,
            "memory": memory,
            "counters": counters,
            "plan_fingerprints": fingerprints,
            "current_task_id": None,
            "route": "scheduler",
        }

    def _scheduler(self, state: OrchestrationState) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        plan = state["plan"].model_copy(deep=True) if state["plan"] is not None else None
        with self.tracer.start_as_current_span(
            "scheduler.select",
            attributes={"agent.role": "scheduler"},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if plan is None:
                mark_failure(span, "invalid_plan")
                return self._terminal_update(
                    state,
                    OrchestrationTermination.INVALID_PLAN,
                    "Scheduler received no plan.",
                    counters=counters,
                )
            try:
                task = self.scheduler.select(plan)
            except SchedulerError as exc:
                mark_failure(span, exc.reason.value)
                return self._terminal_update(
                    state, exc.reason, "Scheduler rejected the dependency graph.", counters=counters
                )
            if task is None:
                unfinished = self.scheduler.unfinished(plan)
                reason = (
                    OrchestrationTermination.DEADLOCK
                    if any(item.status in {TaskStatus.FAILED, TaskStatus.BLOCKED} for item in unfinished)
                    else OrchestrationTermination.NO_READY_TASK
                )
                mark_failure(span, reason.value)
                return self._terminal_update(
                    state, reason, "No dependency-ready task is available.", counters=counters, plan=plan
                )
            span.set_attribute("scheduler.ready.count", sum(
                item.status == TaskStatus.READY for item in plan.tasks
            ))
            span.set_attribute("task.status", task.status.value)
            mark_success(span)
        memory = state["memory"].model_copy(deep=True)
        memory.current_plan = plan.model_copy(deep=True)
        return {
            "plan": plan,
            "memory": memory,
            "counters": counters,
            "current_task_id": task.task_id,
            "route": "executor",
        }

    def _executor(self, state: OrchestrationState) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        plan = state["plan"].model_copy(deep=True) if state["plan"] is not None else None
        task_id = state["current_task_id"]
        if plan is None or task_id is None:
            return self._terminal_update(
                state,
                OrchestrationTermination.EXECUTOR_FAILURE,
                "Executor received no scheduled task.",
                counters=counters,
            )
        task = next((item for item in plan.tasks if item.task_id == task_id), None)
        if task is None:
            return self._terminal_update(
                state,
                OrchestrationTermination.EXECUTOR_FAILURE,
                "Executor could not locate the scheduled task.",
                counters=counters,
            )
        tool_name = task.tool_call.name if task.tool_call is not None else "none"
        with self.tracer.start_as_current_span(
            "executor.task",
            attributes={
                "agent.role": "executor",
                "tool.name": safe_identifier(tool_name),
                "task.attempt": task.attempts + 1,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if task.attempts >= state["config"].max_task_attempts:
                mark_failure(span, "repeated_task")
                return self._terminal_update(
                    state,
                    OrchestrationTermination.REPEATED_TASK,
                    "Task attempt budget was exhausted.",
                    counters=counters,
                    plan=plan,
                )
            memory = state["memory"].model_copy(deep=True)
            before_failures = counters.failures
            repeated = dict(state["task_fingerprints"])
            side_effects = set(state["side_effect_fingerprints"])
            try:
                outcome = self.executor.execute(
                    task,
                    memory,
                    config=state["config"],
                    counters=counters,
                    repeated_calls=repeated,
                    side_effect_calls=side_effects,
                )
            except ExecutorError as exc:
                mark_failure(span, exc.reason.value)
                return self._terminal_update(
                    state, exc.reason, str(exc), counters=counters, plan=plan, memory=memory
                )
            if not outcome.evidence.ok and counters.failures == before_failures:
                counters.failures += 1
            memory.tool_evidence.append(outcome.evidence)
            memory.execution_history.append(outcome.execution)
            memory.current_plan = plan.model_copy(deep=True)
            if outcome.evidence.ok:
                mark_success(span)
            else:
                mark_failure(span, outcome.evidence.failure_type or "executor_failure")
        return {
            "plan": plan,
            "memory": memory,
            "counters": counters,
            "task_fingerprints": repeated,
            "side_effect_fingerprints": side_effects,
            "route": "reviewer",
        }

    def _reviewer(self, state: OrchestrationState) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        plan = state["plan"].model_copy(deep=True) if state["plan"] is not None else None
        task_id = state["current_task_id"]
        if plan is None or task_id is None:
            return self._terminal_update(
                state,
                OrchestrationTermination.REVIEWER_FAILURE,
                "Reviewer received incomplete orchestration state.",
                counters=counters,
            )
        current = next(item for item in plan.tasks if item.task_id == task_id)
        memory = state["memory"].model_copy(deep=True)
        memory.current_plan = plan.model_copy(deep=True)
        candidate = (
            self.completion_builder(memory)
            if self.scheduler.all_completed(plan)
            else None
        )
        counters.reviewer_calls += 1
        with self.tracer.start_as_current_span(
            "reviewer.review",
            attributes={"agent.role": "reviewer", "task.status": current.status.value},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                review = self.reviewer.review(
                    plan=plan,
                    current_task=current,
                    memory=memory,
                    candidate_response=candidate,
                )
            except Exception:
                counters.failures += 1
                mark_failure(span, "reviewer_failure")
                return self._terminal_update(
                    state,
                    OrchestrationTermination.REVIEWER_FAILURE,
                    "Reviewer failed safely.",
                    counters=counters,
                    plan=plan,
                    memory=memory,
                )
            memory.review_history.append(review)
            if (
                review.decision
                in {ReviewDecision.REFLECT, ReviewDecision.REPLAN, ReviewDecision.FAIL}
                and current.status == TaskStatus.COMPLETED
            ):
                counters.failures += 1
            span.set_attribute("review.decision", review.decision.value)
            span.set_attribute("review.completed", review.completed)
            mark_success(span)

        if review.decision == ReviewDecision.COMPLETE:
            memory.final_outcome = "completed"
            return {
                "memory": memory,
                "counters": counters,
                "review_decision": review.decision,
                "termination_reason": OrchestrationTermination.COMPLETED,
                "final_response": candidate or "",
                "route": "persist",
            }
        if review.decision == ReviewDecision.CONTINUE:
            route: Route = "scheduler"
        elif review.decision == ReviewDecision.REFLECT:
            route = "reflection"
        elif review.decision == ReviewDecision.REPLAN:
            route = "replan"
        else:
            if review.failure_type == "unsupported_completion_claim":
                reason = OrchestrationTermination.UNSUPPORTED_COMPLETION_CLAIM
            elif review.failure_type == "unsafe_retry_blocked":
                reason = OrchestrationTermination.UNSAFE_RETRY_BLOCKED
            else:
                reason = OrchestrationTermination.EXECUTOR_FAILURE
            return self._terminal_update(
                state,
                reason,
                review.reason,
                counters=counters,
                plan=plan,
                memory=memory,
            )
        return {
            "memory": memory,
            "counters": counters,
            "review_decision": review.decision,
            "route": route,
        }

    def _reflection(self, state: OrchestrationState) -> dict[str, Any]:
        counters, terminal = self._next_step(state)
        if terminal is not None:
            return terminal
        memory = state["memory"].model_copy(deep=True)
        if counters.reflections >= state["config"].max_reflections:
            return self._terminal_update(
                state,
                OrchestrationTermination.MAX_REFLECTIONS_EXCEEDED,
                "The bounded reflection budget was exhausted.",
                counters=counters,
                memory=memory,
            )
        review = memory.review_history[-1]
        counters.reflections += 1
        reflection = Reflection(
            reflection_id=f"reflection-{counters.reflections}",
            task_id=state["current_task_id"],
            failure_type=review.failure_type or "insufficient_evidence",
            reason=review.reason,
            missing_evidence=review.missing_evidence,
            recommended_action=review.recommended_action or ReviewDecision.REPLAN,
        )
        with self.tracer.start_as_current_span(
            "reflection",
            attributes={
                "agent.role": "reviewer",
                "reflection.count": counters.reflections,
                "failure.category": safe_identifier(reflection.failure_type),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            memory.reflection_history.append(reflection)
            mark_success(span)
        return {"memory": memory, "counters": counters, "route": "replan"}

    def _persist_memory(self, state: OrchestrationState) -> dict[str, Any]:
        memory = state["memory"].model_copy(deep=True)
        memory.current_plan = state["plan"].model_copy(deep=True) if state["plan"] else None
        reason = state["termination_reason"] or OrchestrationTermination.REVIEWER_FAILURE
        outcome = "completed" if reason == OrchestrationTermination.COMPLETED else "failed"
        memory.final_outcome = outcome
        with self.tracer.start_as_current_span(
            "memory.write",
            attributes={"agent.role": "memory", "memory.outcome": outcome},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                self.memory_store.write_episode(
                    run_id=state["run_id"],
                    memory=memory,
                    outcome=outcome,
                    termination_reason=reason.value,
                )
            except Exception:
                mark_failure(span, "memory_failure")
                return {
                    "memory": memory,
                    "termination_reason": OrchestrationTermination.MEMORY_FAILURE,
                    "final_response": "Orchestration ended, but episodic memory persistence failed.",
                }
            mark_success(span)
        return {"memory": memory}

    @staticmethod
    def _terminal_update(
        state: OrchestrationState,
        reason: OrchestrationTermination,
        response: str,
        *,
        counters: OrchestrationCounters,
        plan: TaskPlan | None = None,
        memory: WorkingMemory | None = None,
    ) -> dict[str, Any]:
        return {
            "termination_reason": reason,
            "final_response": response,
            "counters": counters,
            "plan": plan if plan is not None else state["plan"],
            "memory": memory if memory is not None else state["memory"],
            "route": "persist",
        }
