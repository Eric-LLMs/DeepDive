"""Memory store abstraction: supports file / vector / database and other implementations.

Modeled after claude-code memdir (file) + openclaw layered memory (session / disk / vector).
Only a minimal interface is defined here; concrete implementations are swapped per layer.
"""
from typing import Protocol


class MemoryStore(Protocol):
    async def load(self, key: str) -> str | None:
        """Load a memory entry."""
        ...

    async def save(self, key: str, content: str) -> None:
        """Write (overwrite) a memory entry."""
        ...

    async def search(self, query: str, limit: int = 5) -> list[str]:
        """Retrieve memories by relevance, returns content snippets."""
        ...
