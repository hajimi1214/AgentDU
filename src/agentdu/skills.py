from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import AgentEvent
from .tools import ToolDefinition, ToolRegistry, ToolResult


_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
_MAX_SKILL_BYTES = 128_000


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    instructions: str


class SkillsExtension:
    """Progressively exposes skill metadata and instructions as read-only tools."""

    name = "skills"

    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = tuple(dict.fromkeys(root.expanduser().resolve() for root in roots))
        self._skills: dict[str, Skill] | None = None

    def register_tools(self, registry: ToolRegistry) -> None:
        self._discover()
        registry.register(
            ToolDefinition(
                "list_skills",
                "List available Agent Skills by name and description. Load a skill only when it applies to the task.",
                {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                "read",
                self._list_skills,
            )
        )
        registry.register(
            ToolDefinition(
                "load_skill",
                "Load the instruction body of one available Agent Skill.",
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "read",
                self._load_skill,
            )
        )
        registry.register(
            ToolDefinition(
                "read_skill_resource",
                "Read a text resource inside an available Agent Skill directory.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["name", "path"],
                    "additionalProperties": False,
                },
                "read",
                self._read_resource,
            )
        )

    async def on_event(self, event: AgentEvent) -> None:
        return

    def _discover(self) -> dict[str, Skill]:
        if self._skills is not None:
            return self._skills
        skills: dict[str, Skill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                resolved = directory.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                skill_file = resolved / "SKILL.md"
                if not skill_file.is_file():
                    continue
                try:
                    skill_file.resolve().relative_to(resolved)
                except ValueError:
                    continue
                if skill_file.stat().st_size > _MAX_SKILL_BYTES:
                    raise ValueError(f"Skill file exceeds {_MAX_SKILL_BYTES} bytes: {skill_file}")
                text = skill_file.read_text(encoding="utf-8")
                name, description, instructions = _parse_skill(text, resolved.name)
                if name in skills:
                    raise ValueError(f"Duplicate Agent Skill name: {name}")
                skills[name] = Skill(name, description, resolved, instructions)
        self._skills = skills
        return skills

    def _list_skills(self, arguments: dict[str, Any], workspace: Path) -> str:
        summaries = [
            {"name": skill.name, "description": skill.description}
            for skill in self._discover().values()
        ]
        return json.dumps(summaries, ensure_ascii=False, indent=2)

    def _load_skill(self, arguments: dict[str, Any], workspace: Path) -> str | ToolResult:
        skill = self._discover().get(arguments["name"])
        if skill is None:
            return ToolResult(f"Unknown Agent Skill: {arguments['name']}", True)
        return skill.instructions

    def _read_resource(self, arguments: dict[str, Any], workspace: Path) -> str | ToolResult:
        skill = self._discover().get(arguments["name"])
        if skill is None:
            return ToolResult(f"Unknown Agent Skill: {arguments['name']}", True)
        relative = Path(arguments["path"])
        if relative.is_absolute() or ".." in relative.parts:
            return ToolResult("Skill resource path must stay inside the skill directory", True)
        resource = (skill.directory / relative).resolve()
        try:
            resource.relative_to(skill.directory)
        except ValueError:
            return ToolResult("Skill resource path must stay inside the skill directory", True)
        if not resource.is_file():
            return ToolResult(f"Skill resource does not exist: {arguments['path']}", True)
        if resource.stat().st_size > _MAX_SKILL_BYTES:
            return ToolResult(f"Skill resource exceeds {_MAX_SKILL_BYTES} bytes", True)
        try:
            return resource.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ToolResult("Skill resource is not a UTF-8 text file", True)


def _parse_skill(text: str, directory_name: str) -> tuple[str, str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Skill in '{directory_name}' must start with YAML frontmatter")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if end is None:
        raise ValueError(f"Skill in '{directory_name}' has unterminated YAML frontmatter")

    fields: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        match = _FRONTMATTER_FIELD.match(line)
        if not match:
            if line.strip() and not line.lstrip().startswith("#"):
                raise ValueError(f"Unsupported Agent Skill frontmatter in '{directory_name}'")
            index += 1
            continue
        key, raw_value = match.groups()
        value = (raw_value or "").strip()
        if key in {"name", "description"}:
            if key in fields:
                raise ValueError(f"Duplicate '{key}' in Agent Skill frontmatter")
            if key == "description" and value in {"|", "|-", "|+", ">", ">-", ">+"}:
                block: list[str] = []
                index += 1
                while index < end and (not lines[index].strip() or lines[index][:1].isspace()):
                    block.append(lines[index].strip())
                    index += 1
                fields[key] = (" " if value.startswith(">") else "\n").join(
                    item for item in block if item
                ).strip()
                continue
            fields[key] = _yaml_scalar(value)
        index += 1

    name = fields.get("name", "")
    description = fields.get("description", "")
    if not _SKILL_NAME.fullmatch(name):
        raise ValueError(f"Invalid Agent Skill name in '{directory_name}'")
    if name != directory_name:
        raise ValueError(f"Agent Skill name '{name}' must match its directory '{directory_name}'")
    if not description or len(description) > 1_024:
        raise ValueError(f"Agent Skill '{name}' needs a description of at most 1,024 characters")
    instructions = "\n".join(lines[end + 1 :]).strip()
    return name, description, instructions


def _yaml_scalar(value: str) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        value = _strip_yaml_comment(value)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("Invalid quoted YAML scalar in Agent Skill frontmatter") from error
        if not isinstance(parsed, str):
            raise ValueError("Agent Skill metadata values must be strings")
        return parsed
    if value.startswith("'"):
        value = _strip_yaml_comment(value)
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("Invalid quoted YAML scalar in Agent Skill frontmatter")
        return value[1:-1].replace("''", "'")
    return _strip_yaml_comment(value).strip()


def _strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                if quote == "'" and value[index + 1 : index + 2] == "'":
                    continue
                quote = None
        elif character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value
