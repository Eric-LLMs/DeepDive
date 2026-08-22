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


def _parse_int(value: str | None, *, default: int) -> int:
    """Parse a frontmatter integer, falling back to ``default`` on garbage."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


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
            importance=_parse_int(meta.get("importance"), default=5),
            status=meta.get("status", "active"),
            supersedes=meta.get("supersedes", ""),
            content=body,
            path=str(path),
            mtime_ms=path.stat().st_mtime * 1000,
        )

    def _write(self, path: Path, memory: Memory) -> None:
        lines = ["---", f"name: {memory.name}", f"description: {memory.description}"]
        if memory.type in MEMORY_TYPES:
            lines.append(f"type: {memory.type}")
        lines.append(f"importance: {memory.importance}")
        if memory.status != "active":
            lines.append(f"status: {memory.status}")
        if memory.supersedes:
            lines.append(f"supersedes: {memory.supersedes}")
        lines.append("---")
        lines.append("")
        lines.append(memory.content)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def load(self, key: str) -> Memory | None:
        return await asyncio.to_thread(self._read, self._file_for(key))

    async def save(
        self,
        key: str,
        content: str,
        description: str = "",
        type_: str = "",
        importance: int = 5,
        supersedes: str = "",
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._file_for(key)
        memory = Memory(
            name=key,
            description=description,
            type=type_,
            importance=importance,
            supersedes=supersedes,
            content=content,
        )
        await asyncio.to_thread(self._write, path, memory)
        await self._index(key)

    async def mark_superseded(self, key: str) -> None:
        """Mark an existing memory ``status: superseded`` and drop it from the index.

        The file stays on disk as an audit trail, but ``list``/``search``/the index no
        longer surface it, so a superseded value can never resurface from stale reads.
        """

        def _update() -> None:
            path = self._file_for(key)
            if not path.is_file():
                return
            memory = self._read(path)
            if memory is None or memory.status == "superseded":
                return
            memory.status = "superseded"
            self._write(path, memory)

        await asyncio.to_thread(_update)
        await self._unindex(key)

    async def _unindex(self, key: str) -> None:
        """Remove a memory's pointer line from the index (idempotent)."""

        def _update() -> None:
            index = self.index_path
            if not index.is_file():
                return
            needle = f"({key}.md)"
            kept = [
                line
                for line in index.read_text(encoding="utf-8").splitlines()
                if needle not in line
            ]
            index.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

        await asyncio.to_thread(_update)

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
        """Keyword recall weighted by importance.

        Name/description matches rank above body matches, then the keyword score is
        multiplied by ``importance`` (1–10) so curated, high-salience memories surface
        ahead of incidental ones (OpenClaw-style ``relevance × importance``).
        Superseded entries are excluded entirely.
        """
        memories = await asyncio.to_thread(self._scan)
        q = query.lower()
        scored: list[tuple[int, Memory]] = []
        for m in memories:
            if m.status == "superseded":
                continue
            points = 0
            if q in m.name.lower():
                points += 3
            if q in m.description.lower():
                points += 2
            if q in m.content.lower():
                points += 1
            if points:
                scored.append((points * m.importance, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def _scan(self) -> list[Memory]:
        """Read every active memory file (skipping the index and superseded entries)."""
        if not self.directory.is_dir():
            return []
        memories = []
        for f in sorted(self.directory.glob("*.md")):
            if f.name == self.index_name:
                continue
            memory = self._read(f)
            if memory is not None and memory.status != "superseded":
                memories.append(memory)
        return memories
