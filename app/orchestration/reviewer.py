from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.orchestration.models import (
    PlanTask,
    ReviewDecision,
    ReviewResult,
    TaskPlan,
    TaskStatus,
    WorkingMemory,
)


_COMPLETION_CLAIMS = (
    "refund completed",
    "refund was approved",
    "refund was issued",
    "refund issued",
    "approved refund",
)


class ReviewerAgent(Protocol):
    def review(
        self,
        *,
        plan: TaskPlan,
        current_task: PlanTask,
        memory: WorkingMemory,
        candidate_response: str | None,
    ) -> ReviewResult: ...


def _refund_status(memory: WorkingMemory) -> str | None:
    for evidence in reversed(memory.tool_evidence):
        if not evidence.ok:
            continue
        status = evidence.data.get("refund_status") or evidence.data.get("status")
        if status in {"approved", "pending_human_approval"}:
            return str(status)
    return None


def _has_ticket(memory: WorkingMemory) -> bool:
    return any(
        evidence.ok
        and evidence.tool_name == "create_support_ticket"
        and evidence.data.get("status") == "open"
        for evidence in memory.tool_evidence
    )


def has_unsupported_claim(response: str, memory: WorkingMemory) -> bool:
    lowered = response.casefold()
    status = _refund_status(memory)
    if status != "approved" and any(claim in lowered for claim in _COMPLETION_CLAIMS):
        return True
    ticket_claim = any(
        claim in lowered
        for claim in ("ticket created", "ticket was created", "ticket opened")
    )
    return ticket_claim and not _has_ticket(memory)


class EvidenceReviewer:
    """Reviewer Agent that accepts completion only when tool evidence supports it."""

    def review(
        self,
        *,
        plan: TaskPlan,
        current_task: PlanTask,
        memory: WorkingMemory,
        candidate_response: str | None,
    ) -> ReviewResult:
        if current_task.status == TaskStatus.FAILED:
            evidence = memory.tool_evidence[-1] if memory.tool_evidence else None
            if evidence is not None and evidence.retryable:
                if evidence.ambiguous and evidence.tool_name == "create_support_ticket":
                    return ReviewResult(
                        completed=False,
                        decision=ReviewDecision.FAIL,
                        failure_type="unsafe_retry_blocked",
                        reason="A non-idempotent side effect has an ambiguous outcome.",
                    )
                return ReviewResult(
                    completed=False,
                    decision=ReviewDecision.REFLECT,
                    failure_type=evidence.failure_type or "tool_failure",
                    reason="Retryable executor evidence requires reflection and replanning.",
                    recommended_action=ReviewDecision.REPLAN,
                )
            return ReviewResult(
                completed=False,
                decision=ReviewDecision.FAIL,
                failure_type=current_task.failure_reason or "executor_failure",
                reason="The task failed with a permanent validation, tool, or business error.",
            )

        evidence = memory.tool_evidence[-1] if memory.tool_evidence else None
        if current_task.tool_call is not None and (
            evidence is None or not evidence.ok or not evidence.data
        ):
            return ReviewResult(
                completed=False,
                decision=ReviewDecision.REFLECT,
                failure_type="insufficient_evidence",
                reason="The executor reported success without usable evidence.",
                missing_evidence=[current_task.objective],
                recommended_action=ReviewDecision.REPLAN,
            )

        if all(task.status == TaskStatus.COMPLETED for task in plan.tasks):
            if candidate_response is None:
                return ReviewResult(
                    completed=False,
                    decision=ReviewDecision.FAIL,
                    failure_type="missing_completion_response",
                    reason="No candidate completion was available for final review.",
                )
            if has_unsupported_claim(candidate_response, memory):
                return ReviewResult(
                    completed=False,
                    decision=ReviewDecision.FAIL,
                    failure_type="unsupported_completion_claim",
                    reason="The proposed completion is not supported by business evidence.",
                )
            return ReviewResult(
                completed=True,
                decision=ReviewDecision.COMPLETE,
                reason="All planned tasks completed and the final claims match evidence.",
            )

        return ReviewResult(
            completed=False,
            decision=ReviewDecision.CONTINUE,
            reason="The current task is supported; dependency-ready work remains.",
        )


class ScriptedReviewer:
    """Inject review decisions deterministically, then fall back to evidence review."""

    def __init__(
        self,
        decisions: list[ReviewResult],
        fallback: ReviewerAgent | None = None,
    ) -> None:
        self._decisions = list(decisions)
        self.fallback = fallback or EvidenceReviewer()
        self.requests: list[str] = []

    def review(
        self,
        *,
        plan: TaskPlan,
        current_task: PlanTask,
        memory: WorkingMemory,
        candidate_response: str | None,
    ) -> ReviewResult:
        self.requests.append(current_task.task_id)
        if self._decisions:
            return self._decisions.pop(0)
        return self.fallback.review(
            plan=plan,
            current_task=current_task,
            memory=memory,
            candidate_response=candidate_response,
        )


def build_final_response(memory: WorkingMemory) -> str:
    status = _refund_status(memory)
    ticket_created = _has_ticket(memory)
    if status == "approved":
        refund = "The refund completed successfully."
    elif status == "pending_human_approval":
        refund = "The refund request is pending human approval and has not completed."
    else:
        refund = "No completed refund is supported by the available evidence."
    ticket = (
        " A support ticket was created and remains open."
        if ticket_created
        else " No support-ticket creation is supported by the available evidence."
    )
    return refund + ticket


CompletionBuilder = Callable[[WorkingMemory], str]
