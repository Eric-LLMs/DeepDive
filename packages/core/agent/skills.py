"""Skill: a reusable capability recipe (instructions), configured in Markdown rather than code.

Modeled after claude-code skills: a skill is a block of Markdown instructions + frontmatter metadata.
The Agent loads relevant skill instructions into context on demand and then follows them.
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    keywords: list[str] = field(default_factory=list)
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
        """Scan the directory for *.skill.md files, parse frontmatter to build Skills."""
        registry = cls()
        if not Path(directory).is_dir():
            return registry
        for f in sorted(Path(directory).glob("*.skill.md")):
            skill = cls._parse(f)
            if skill:
                registry.register(skill)
        return registry

    @staticmethod
    def _parse(path: Path) -> Skill | None:
        text = path.read_text(encoding="utf-8")
        meta, body = SkillRegistry._parse_frontmatter(text)
        if not body:
            return None
        keywords = [k.strip() for k in meta.get("keywords", "").split(",") if k.strip()]
        return Skill(
            name=meta.get("name") or path.name.removesuffix(".skill.md"),
            description=meta.get("description", ""),
            instructions=body,
            keywords=keywords,
            path=path,
        )

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """Minimal frontmatter parsing (single-line key: value), taking the block between the first two --- markers."""
        if not text.startswith("---"):
            return {}, text
        lines = text.splitlines()
        end = len(lines)
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        meta: dict[str, str] = {}
        for line in lines[1:end]:
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = "\n".join(lines[end + 1:]).strip()
        return meta, body
