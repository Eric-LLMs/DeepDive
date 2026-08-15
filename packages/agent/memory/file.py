"""File memory: claude-code memdir style.

- MEMORY.md is the index (one pointer per line)
- Each memory is a separate .md file with YAML frontmatter (name/description/type)
- Recall is description-weighted keyword scoring; staleness is surfaced via mtime.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.memory.types import MEMORY_TYPES, Memory


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` frontmatter block (key: value lines) from the body."""
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
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


class FileMemoryStore:
    def __init__(self, directory: Path, index_name: str = "MEMORY.md") -> None:
        self.directory = Path(directory)
        self.index_name = index_name

    @property
    def index_path(self) -> Path:
        return self.directory / self.index_name

    def _file_for(self, key: str) -> Path:
        safe = key.strip().replace("/", "_").replace("\\", "_").replace(":", "_")
        return self.directory / f"{safe}.md"

    def _read(self, path: Path) -> Memory | None:
        """Read one memory file into a :class:`Memory` (frontmatter + body)."""
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        return Memory(
            name=meta.get("name") or path.stem,
            description=meta.get("description", ""),
            type=meta.get("type", ""),
            content=body,
            path=str(path),
            mtime_ms=path.stat().st_mtime * 1000,
        )

    def _write(self, path: Path, memory: Memory) -> None:
        lines = ["---", f"name: {memory.name}", f"description: {memory.description}"]
        if memory.type in MEMORY_TYPES:
            lines.append(f"type: {memory.type}")
        lines.append("---")
        lines.append("")
        lines.append(memory.content)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def load(self, key: str) -> Memory | None:
        return await asyncio.to_thread(self._read, self._file_for(key))

    async def save(
        self, key: str, content: str, description: str = "", type_: str = ""
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._file_for(key)
        memory = Memory(name=key, description=description, type=type_, content=content)
        await asyncio.to_thread(self._write, path, memory)
        await self._index(key)

    async def _index(self, key: str) -> None:
        """Append a pointer line to the index (if not already present)."""

        def _update() -> None:
            index = self.index_path
            existing = index.read_text(encoding="utf-8") if index.is_file() else ""
            if f"({key}.md)" not in existing:
                index.write_text(existing + f"- [{key}]({key}.md)\n", encoding="utf-8")

        await asyncio.to_thread(_update)

    async def list(self) -> list[Memory]:
        return await asyncio.to_thread(self._scan)

    async def search(self, query: str, limit: int = 5) -> list[Memory]:
        """Description-weighted keyword recall: name/description matches rank above body matches."""
        memories = await asyncio.to_thread(self._scan)
        q = query.lower()
        scored: list[tuple[int, Memory]] = []
        for m in memories:
            score = 0
            if q in m.name.lower():
                score += 3
            if q in m.description.lower():
                score += 2
            if q in m.content.lower():
                score += 1
            if score:
                scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def _scan(self) -> list[Memory]:
        """Read every memory file (skipping the index) into Memory records."""
        if not self.directory.is_dir():
            return []
        memories = []
        for f in sorted(self.directory.glob("*.md")):
            if f.name == self.index_name:
                continue
            memory = self._read(f)
            if memory is not None:
                memories.append(memory)
        return memories
