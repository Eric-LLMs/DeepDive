"""Skill: a reusable capability recipe (instructions), configured in Markdown rather than code.

Modeled after claude-code skills: a skill is a block of Markdown instructions + frontmatter metadata.
The Agent loads relevant skill instructions into context on demand and then follows them.
``allowed_tools`` is an optional sandbox hint: which tools the skill is permitted to use.
"""
from dataclasses import dataclass, field
from pathlib import Path

from agent.engine.context import current_turn
from agent.engine.decisions import Guard, ToolExecution, text_block
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

    def render(self) -> str:
        """Sorted one-line entries (``- name: description``) — every skill, never dropped.

        Earlier versions truncated the directory to a character budget, so skills past the cap
        silently vanished from the prompt. Now the complete directory is emitted; startup is
        refused (``AgentKernel.ensure_capacity`` → :func:`agent.tools.tool_gateway.check_index_capacity`)
        when the combined tool + skill index overflows the hard capacity ceiling.
        """
        return "\n".join(
            f"- {skill.name}: {_escape_xml(skill.description)}"
            for skill in sorted(self._registry.all(), key=lambda s: s.name)
        )


def skill_tool(registry: SkillRegistry) -> ToolDefinition:
    """The builtin ``skill`` meta-tool: lazily load a skill's full instructions.

    The model sees only the catalog in the prompt; calling this reads the complete
    SKILL.md body on demand (and reports the skill's ``allowed_tools``). Loading a skill
    also marks it active for the turn, so :class:`SkillScopeEnforcer` applies its
    ``allowed_tools`` as a hard scoped allowlist for subsequent tool calls.
    """

    async def execute(args: dict, exec: ToolExecution) -> dict:
        name = args["name"]
        skill = registry.get(name)
        if skill is None:
            raise KeyError(f"unknown skill: {name}")
        turn = current_turn()
        if turn is not None:
            turn.activate_skill(name)
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


class SkillScopeEnforcer:
    """Hard-enforce a skill's declared ``allowed_tools`` as a scoped allowlist.

    Once a turn loads a skill via the ``skill`` meta-tool, tools outside the union of that
    skill's ``allowed_tools`` are denied for the rest of the turn — except a small core set
    (the ``skill`` meta-tool itself, tool discovery, memory, and planning) that the agent
    needs to navigate regardless of scope. A skill that declares no ``allowed_tools``
    imposes no restriction, so legacy skills keep working untouched.

    Installed in :class:`~agent.engine.kernel.AgentKernel` as a monotonic
    :class:`~agent.engine.runtime.ToolRuntime` guard; the deny reason is fed back to the
    model so it can pick a permitted tool, or the skill author can widen the allowlist.
    """

    def __init__(self, registry: SkillRegistry, core_tools: set[str] | None = None) -> None:
        self._registry = registry
        # Everything outside this set must be declared in a skill's allowed_tools while
        # that skill is active.
        self._core = core_tools if core_tools is not None else {
            "skill", "tool_search", "memory_search", "memory_save", "plan",
        }

    def _declared(self, skills: list[str]) -> list[Skill]:
        """The active skills that actually declare an ``allowed_tools`` allowlist."""
        declared: list[Skill] = []
        for name in skills:
            skill = self._registry.get(name)
            if skill is not None and skill.allowed_tools:
                declared.append(skill)
        return declared

    def _allowed(self, skills: list[str]) -> set[str]:
        allowed = set(self._core)
        for skill in self._declared(skills):
            allowed.update(skill.allowed_tools)
        return allowed

    def guard(self) -> Guard:
        """A monotonic guard: deny reason string, or ``None`` to pass."""

        async def _guard(exec: ToolExecution) -> str | None:
            turn = current_turn()
            if turn is None or not turn.active_skills:
                return None
            declared = self._declared(turn.active_skills)
            # Unknown skill names (not resolvable in the registry) can't be trusted to
            # carry an allowlist, so they impose a core-only scope (fail closed).
            unknown = [n for n in turn.active_skills if self._registry.get(n) is None]
            if not declared and not unknown:
                return None  # every active skill is known and declares no scope
            allowed = self._allowed(turn.active_skills)
            if exec.name in allowed:
                return None
            scoped = [s.name for s in declared] + unknown
            return (
                f"skill scope: '{exec.name}' is not in the active skill "
                f"[{', '.join(scoped)}] allowed_tools "
                f"[{', '.join(sorted(allowed))}]"
            )

        return _guard
