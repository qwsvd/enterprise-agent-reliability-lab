from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.orchestration.models import EpisodeSummary, WorkingMemory


_TOKENS = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {"a", "an", "and", "for", "has", "is", "of", "on", "the", "to", "with"}
)
_SAFE_EVIDENCE_KEYS = frozenset(
    {
        "amount",
        "completed",
        "created",
        "currency",
        "delayed_days",
        "priority",
        "refund_status",
        "refunded",
        "status",
    }
)


class MemoryBase(DeclarativeBase):
    pass


class EpisodeRow(MemoryBase):
    __tablename__ = "agent_episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    keywords_json: Mapped[str] = mapped_column(Text)
    plan_json: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(64))
    termination_reason: Mapped[str] = mapped_column(String(128))
    reflections_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def keywords(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKENS.findall(value)
        if len(token) > 1 and token.casefold() not in _STOP_WORDS
    }


def _safe_evidence(memory: WorkingMemory) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in memory.tool_evidence:
        summary: dict[str, Any] = {
            "task_id": item.task_id,
            "tool_name": item.tool_name,
            "ok": item.ok,
        }
        if item.failure_type is not None:
            summary["failure_type"] = item.failure_type
        for key in sorted(_SAFE_EVIDENCE_KEYS):
            if key in item.data and isinstance(item.data[key], (bool, int, float, str)):
                summary[key] = item.data[key]
        summaries.append(summary)
    return summaries


class EpisodicMemoryStore:
    """Small persistent SQLite episode store with deterministic lexical retrieval."""

    def __init__(self, database_url: str = "sqlite:///./agent_memory.db") -> None:
        connect_args = (
            {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        )
        self.engine = create_engine(database_url, connect_args=connect_args)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        MemoryBase.metadata.create_all(self.engine)

    def write_episode(
        self,
        *,
        run_id: str,
        memory: WorkingMemory,
        outcome: str,
        termination_reason: str,
    ) -> EpisodeSummary:
        plan_summary = (
            [task.objective for task in memory.current_plan.tasks]
            if memory.current_plan is not None
            else []
        )
        evidence_summary = _safe_evidence(memory)
        reflection_summary = [item.failure_type for item in memory.reflection_history]
        created_at = datetime.now(UTC)
        row = EpisodeRow(
            run_id=run_id,
            goal=memory.goal,
            keywords_json=json.dumps(sorted(keywords(memory.goal))),
            plan_json=json.dumps(plan_summary, sort_keys=True),
            evidence_json=json.dumps(evidence_summary, sort_keys=True, default=str),
            outcome=outcome,
            termination_reason=termination_reason,
            reflections_json=json.dumps(reflection_summary, sort_keys=True),
            created_at=created_at,
        )
        with self.sessions() as session:
            session.add(row)
            session.commit()
        return EpisodeSummary(
            run_id=run_id,
            goal=memory.goal,
            outcome=outcome,
            termination_reason=termination_reason,
            plan_summary=plan_summary,
            evidence_summary=evidence_summary,
            reflection_summary=reflection_summary,
            created_at=created_at,
        )

    def retrieve(self, goal: str, *, limit: int = 3) -> list[EpisodeSummary]:
        if limit < 0:
            raise ValueError("Memory retrieval limit cannot be negative")
        if limit == 0:
            return []
        query_terms = keywords(goal)
        with self.sessions() as session:
            rows = list(session.scalars(select(EpisodeRow).order_by(EpisodeRow.id.desc())))
        ranked: list[tuple[int, EpisodeRow]] = []
        for row in rows:
            stored_terms = set(json.loads(row.keywords_json))
            score = len(query_terms & stored_terms)
            if score:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], -item[1].id))
        return [self._summary(row, score) for score, row in ranked[:limit]]

    @staticmethod
    def _summary(row: EpisodeRow, score: int) -> EpisodeSummary:
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return EpisodeSummary(
            run_id=row.run_id,
            goal=row.goal,
            outcome=row.outcome,
            termination_reason=row.termination_reason,
            score=score,
            plan_summary=json.loads(row.plan_json),
            evidence_summary=json.loads(row.evidence_json),
            reflection_summary=json.loads(row.reflections_json),
            created_at=created_at,
        )

    def close(self) -> None:
        self.engine.dispose()
