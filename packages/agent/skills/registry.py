"""Skill: a reusable capability recipe (instructions), configured in Markdown rather than code.

Modeled after claude-code skills: a skill is a block of Markdown instructions + frontmatter metadata.
The Agent loads relevant skill instructions into context on demand and then follows them.
``allowed_tools`` is an optional sandbox hint: which tools the skill is permitted to use.
"""
from dataclasses import dataclass, field
from pathlib import Path

from agent.engine.decisions import ToolExecution, text_block
from agent.frontmatter import parse_frontmatter
from agent.tools.definition import ToolDefinition, ToolOutput, define_tool


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    keywords: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    path: Path | None = None


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def relevant(self, query: str, limit: int = 3) -> list[Skill]:
        """Return the most relevant skills by keyword matching (simple scoring; can switch to embeddings later)."""
        q = query.lower()
        scored: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            score = 0
            for kw in skill.keywords:
                if kw.lower() in q:
                    score += 1
            if skill.name.lower() in q:
                score += 2
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:limit]]

    @classmethod
    def from_dir(cls, directory: Path) -> "SkillRegistry":
        """Scan a skills directory for skill files.

        Two layouts are supported under ``directory``:
        - flat ``<dir>/<name>.skill.md``
        - claude-code style ``<dir>/<name>/SKILL.md``
        """
        registry = cls()
        root = Path(directory)
        if not root.is_dir():
            return registry
        for f in sorted(root.glob("*.skill.md")):
            skill = cls._parse(f, default_name=f.name.removesuffix(".skill.md"))
            if skill:
                registry.register(skill)
        for sub in sorted(root.iterdir()):
            if sub.is_dir():
                f = sub / "SKILL.md"
                if f.is_file():
                    skill = cls._parse(f, default_name=sub.name)
                    if skill:
                        registry.register(skill)
        return registry

    @staticmethod
    def _parse(path: Path, default_name: str) -> Skill | None:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        if not body:
            return None
        keywords = [k.strip() for k in meta.get("keywords", "").split(",") if k.strip()]
        allowed = [k.strip() for k in meta.get("allowed_tools", "").split(",") if k.strip()]
        return Skill(
            name=meta.get("name") or default_name,
            description=meta.get("description", ""),
            instructions=body,
            keywords=keywords,
            allowed_tools=allowed,
            path=path,
        )


def _escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SkillCatalog:
    """Compressed one-line skill directory for the static prompt prefix.

    Only ``name + description`` (no instructions) — the body is fetched lazily via the
    ``skill()`` meta-tool, so the prompt stays small however many skills are installed.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def render(
        self,
        *,
        limit_chars: int = 500,
        desc_chars: int = 80,
    ) -> str:
        """Sorted one-line entries (``- name: description``) truncated to a budget.

        Each description is capped at ``desc_chars`` so the directory advertises far more
        skills within the budget; the full body is fetched lazily via the ``skill()`` tool.
        """
        lines: list[str] = []
        used = 0
        for skill in sorted(self._registry.all(), key=lambda s: s.name):
            desc = _escape_xml(skill.description)
            desc = desc if len(desc) <= desc_chars else desc[: desc_chars - 1].rstrip() + "…"
            line = f"- {skill.name}: {desc}"
            if used and used + len(line) > limit_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def skill_tool(registry: SkillRegistry) -> ToolDefinition:
    """The builtin ``skill`` meta-tool: lazily load a skill's full instructions.

    The model sees only the catalog in the prompt; calling this reads the complete
    SKILL.md body on demand (and reports the skill's ``allowed_tools`` so the sandbox can
    apply a scoped allowlist).
    """

    async def execute(args: dict, exec: ToolExecution) -> dict:
        name = args["name"]
        skill = registry.get(name)
        if skill is None:
            raise KeyError(f"unknown skill: {name}")
        return {
            "name": skill.name,
            "description": skill.description,
            "allowed_tools": skill.allowed_tools,
            "instructions": skill.instructions,
        }

    return define_tool(
        name="skill",
        description=(
            "Load the full instructions of a skill by name (from the catalog). Returns "
            "the complete SKILL.md body plus its allowed_tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name (kebab-case, as listed in the catalog).",
                },
            },
            "required": ["name"],
        },
        output=ToolOutput(
            schema={"type": "object"},
            render=lambda args, value: [
                text_block(
                    f"# skill:{value['name']}\n"
                    + (f"allowed_tools: {', '.join(value['allowed_tools'])}\n" if value["allowed_tools"] else "")
                    + value["instructions"]
                )
            ],
        ),
        execute=execute,
        is_concurrency_safe=True,
    )
