from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.evals.models import EvalCase, EvalSuiteDefinition


DEFAULT_CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "cases.yaml"


class EvalDefinitionError(RuntimeError):
    """The versioned evaluation dataset could not be loaded or validated."""


def load_eval_suite(path: str | Path = DEFAULT_CASES_PATH) -> EvalSuiteDefinition:
    source = Path(path)
    try:
        raw: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvalDefinitionError(f"Unable to read eval definition {source}: {exc}") from exc
    try:
        return EvalSuiteDefinition.model_validate(raw)
    except ValidationError as exc:
        raise EvalDefinitionError(f"Invalid eval definition {source}: {exc}") from exc


def select_eval_cases(
    suite: EvalSuiteDefinition, case_ids: list[str] | tuple[str, ...] | None = None
) -> list[EvalCase]:
    if not case_ids:
        return list(suite.cases)
    by_id = {case.id: case for case in suite.cases}
    unknown = [case_id for case_id in case_ids if case_id not in by_id]
    if unknown:
        raise EvalDefinitionError(f"Unknown eval case ids: {', '.join(unknown)}")
    return [by_id[case_id] for case_id in case_ids]
