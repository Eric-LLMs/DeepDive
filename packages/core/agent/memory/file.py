"""File memory: claude-code memdir style.

- MEMORY.md is the index (one pointer per line)
- Each memory is a separate .md file (may contain frontmatter)
"""
import asyncio
from pathlib import Path


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

    async def load(self, key: str) -> str | None:
        path = self._file_for(key)
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    async def save(self, key: str, content: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._file_for(key).write_text, content, encoding="utf-8")
        await self._index(key)

    async def _index(self, key: str) -> None:
        """Append a pointer line to the index (if not already present)."""

        def _update() -> None:
            index = self.index_path
            existing = index.read_text(encoding="utf-8") if index.is_file() else ""
            if f"({key}.md)" not in existing:
                index.write_text(existing + f"- [{key}]({key}.md)\n", encoding="utf-8")

        await asyncio.to_thread(_update)

    async def search(self, query: str, limit: int = 5) -> list[str]:
        """Naive keyword retrieval: scan the .md files under the directory and return matching snippets."""
        q = query.lower()
        hits: list[str] = []

        def _scan() -> None:
            if not self.directory.is_dir():
                return
            for f in sorted(self.directory.glob("*.md")):
                if f.name == self.index_name:
                    continue
                text = f.read_text(encoding="utf-8")
                if q in text.lower():
                    hits.append(text[:500])

        await asyncio.to_thread(_scan)
        return hits[:limit]
