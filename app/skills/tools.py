from __future__ import annotations

from typing import Any

from app.agent.runtime import AgentTools
from app.skills.loader import SkillError, SkillRegistry


class SkillAwareTools:
    """Add progressive skill loading while delegating business tools unchanged."""

    def __init__(self, business_tools: AgentTools, skills: SkillRegistry) -> None:
        self.business_tools = business_tools
        self.skills = skills
        self._catalog = skills.catalog()
        if any(item["function"]["name"] == "load_skill" for item in business_tools.schemas()):
            raise ValueError("Business tool name conflicts with load_skill")

    def context(self) -> str:
        lines = [
            "Available Agent Skills (metadata only; full instructions are not loaded):"
        ]
        lines.extend(f"- {item.name}: {item.description}" for item in self._catalog)
        lines.append("Call load_skill with the selected skill name before following its workflow.")
        return "\n".join(lines)

    def schemas(self) -> list[dict[str, Any]]:
        return [*self.business_tools.schemas(), self._load_skill_schema()]

    def execute(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if name != "load_skill":
            return self.business_tools.execute(
                name, arguments, timeout_seconds=timeout_seconds
            )
        if not isinstance(arguments, dict) or set(arguments) != {"name"}:
            return self._failure("invalid_skill_arguments", "load_skill requires only a name")
        skill_name = arguments.get("name")
        if not isinstance(skill_name, str):
            return self._failure("invalid_skill_arguments", "Skill name must be a string")
        try:
            skill = self.skills.load(skill_name)
        except SkillError as exc:
            return self._failure("skill_load_error", str(exc))
        return {
            "ok": True,
            "data": {
                "name": skill.metadata.name,
                "description": skill.metadata.description,
                "instructions": skill.instructions,
            },
        }

    def _load_skill_schema(self) -> dict[str, Any]:
        names = [item.name for item in self._catalog]
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "Load the full instructions for one selected Agent Skill.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": names}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        }

    @staticmethod
    def _failure(kind: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"type": kind, "message": message}}

