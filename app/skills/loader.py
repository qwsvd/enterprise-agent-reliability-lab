from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillError(RuntimeError):
    """Base error for repository skill discovery and loading."""


class SkillDirectoryError(SkillError):
    pass


class SkillMetadataError(SkillError):
    pass


class DuplicateSkillError(SkillError):
    pass


class UnknownSkillError(SkillError):
    pass


class UnsafeSkillPathError(SkillError):
    pass


class SkillReadError(SkillError):
    pass


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str
    description: str
    path: Path


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: SkillMetadata
    instructions: str


class SkillRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._metadata: dict[str, SkillMetadata] = {}
        self._loaded: dict[str, Skill] = {}
        self._discovered = False

    @property
    def loaded_names(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def discover(self) -> list[SkillMetadata]:
        if not self.root.exists() or not self.root.is_dir():
            raise SkillDirectoryError(f"Skill directory not found: {self.root}")

        discovered: dict[str, SkillMetadata] = {}
        for directory in sorted((item for item in self.root.iterdir() if item.is_dir()), key=lambda p: p.name):
            skill_file = directory / "SKILL.md"
            self._ensure_safe(skill_file)
            if not skill_file.is_file():
                raise SkillMetadataError(f"Missing SKILL.md in skill directory: {directory.name}")
            raw_metadata = self._read_frontmatter(skill_file)
            metadata = self._validate_metadata(raw_metadata, skill_file)
            if metadata.name in discovered:
                raise DuplicateSkillError(f"Duplicate skill name: {metadata.name}")
            discovered[metadata.name] = metadata

        self._metadata = discovered
        self._loaded = {}
        self._discovered = True
        return self.catalog()

    def catalog(self) -> list[SkillMetadata]:
        if not self._discovered:
            raise SkillDirectoryError("Skills have not been discovered")
        return list(self._metadata.values())

    def load(self, name: str) -> Skill:
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise UnsafeSkillPathError(f"Unsafe skill name: {name}")
        if not self._discovered:
            raise SkillDirectoryError("Skills have not been discovered")
        if name in self._loaded:
            return self._loaded[name]
        metadata = self._metadata.get(name)
        if metadata is None:
            raise UnknownSkillError(f"Unknown skill: {name}")
        self._ensure_safe(metadata.path)
        try:
            content = metadata.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillReadError(f"Unable to read skill {name}: {exc}") from exc
        _, instructions = self._split_document(content, metadata.path)
        instructions = instructions.strip()
        if not instructions:
            raise SkillMetadataError(f"Skill {name} has no instructions")
        skill = Skill(metadata=metadata, instructions=instructions)
        self._loaded[name] = skill
        return skill

    def _ensure_safe(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise UnsafeSkillPathError(f"Skill path escapes configured root: {path}") from exc

    def _read_frontmatter(self, path: Path) -> dict[str, Any]:
        lines: list[str] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                if handle.readline().strip() != "---":
                    raise SkillMetadataError(f"SKILL.md must start with YAML frontmatter: {path}")
                for line in handle:
                    if line.strip() == "---":
                        break
                    lines.append(line)
                else:
                    raise SkillMetadataError(f"Unclosed YAML frontmatter: {path}")
        except SkillError:
            raise
        except (OSError, UnicodeError) as exc:
            raise SkillReadError(f"Unable to read skill metadata: {path}: {exc}") from exc
        try:
            parsed = yaml.safe_load("".join(lines))
        except yaml.YAMLError as exc:
            raise SkillMetadataError(f"Malformed YAML frontmatter in {path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SkillMetadataError(f"Skill frontmatter must be a mapping: {path}")
        return parsed

    def _validate_metadata(self, raw: dict[str, Any], path: Path) -> SkillMetadata:
        missing = [field for field in ("name", "description") if not raw.get(field)]
        if missing:
            raise SkillMetadataError(f"Missing required skill metadata: {', '.join(missing)}")
        if not isinstance(raw["name"], str) or not SKILL_NAME_PATTERN.fullmatch(raw["name"]):
            raise SkillMetadataError(f"Invalid skill name: {raw['name']!r}")
        if not isinstance(raw["description"], str) or not raw["description"].strip():
            raise SkillMetadataError("Skill description must be a non-empty string")
        try:
            payload = dict(raw)
            payload["path"] = path.resolve()
            return SkillMetadata.model_validate(payload)
        except ValidationError as exc:
            raise SkillMetadataError(f"Invalid skill metadata in {path}: {exc}") from exc

    @staticmethod
    def _split_document(content: str, path: Path) -> tuple[str, str]:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillMetadataError(f"SKILL.md must start with YAML frontmatter: {path}")
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
        raise SkillMetadataError(f"Unclosed YAML frontmatter: {path}")
