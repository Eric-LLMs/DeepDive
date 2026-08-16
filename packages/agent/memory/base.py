"""Memory store abstraction: supports file / vector / database and other implementations.

Modeled after claude-code memdir (file) + openclaw layered memory (session / disk / vector).
Only a minimal interface is defined here; concrete implementations are swapped per layer.
"""
from __future__ import annotations

from typing import Protocol

from agent.memory.types import Memory


class MemoryStore(Protocol):
    async def load(self, key: str) -> Memory | None:
        """Load a memory record by name/key."""
        ...

    async def save(
        self, key: str, content: str, description: str = "", type_: str = ""
    ) -> None:
        """Write (overwrite) a memory record with optional description/type metadata."""
        ...

    async def list(self) -> list[Memory]:
        """Return every memory record."""
        ...

    async def search(self, query: str, limit: int = 5) -> list[Memory]:
        """Retrieve memories by relevance, returning records (most relevant first)."""
        ...
