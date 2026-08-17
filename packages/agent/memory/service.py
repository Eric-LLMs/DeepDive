"""MemoryService: dual-track memory orchestration + memory-as-tools.

Two tracks feed recall:
- **Long-term file memory** — the claude-code memdir (``data/memory/*.md`` + ``MEMORY.md``
  index), keyword-scored by :class:`~agent.memory.file.FileMemoryStore`.
- **Session memory** — PostgreSQL ``pgvector`` (semantic) + ``tsvector`` (keyword) recall
  fused by RRF (see ``core.infrastructure.memory_retrieval``); tsvector-only when the
  embedding service is offline.

At session start ``begin_session()`` loads the ``MEMORY.md`` brief (first ``top_lines``
lines) for injection. ``memory_search``/``memory_save`` expose recall/write as tools;
``memory_save`` enforces a human-confirmation gate (unconfirmed writes are denied).
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.decisions import ToolExecution, text_block
from agent.memory.base import MemoryStore
from agent.memory.retrieval import MemoryHit, RRFMemoryRetriever
from agent.memory.types import Memory
from agent.tool_permissions import ToolPermission
from agent.tools import ToolDefinition, ToolOutput, define_tool


class MemoryService:
    def __init__(
        self,
        file_store: MemoryStore,
        retriever: RRFMemoryRetriever,
        *,
        memory_md_path: Path | None = None,
        top_lines: int = 200,
    ) -> None:
        self._file_store = file_store
        self._retriever = retriever
        self._memory_md = memory_md_path
        self._top_lines = top_lines
        self._brief = ""

    # ── session lifecycle ──
    def begin_session(self) -> None:
        """Load the ``MEMORY.md`` short-term brief (first ``top_lines`` lines)."""
        brief: list[str] = []
        if self._memory_md is not None and self._memory_md.is_file():
            try:
                brief = self._memory_md.read_text(encoding="utf-8").splitlines()[: self._top_lines]
            except OSError:
                brief = []
        self._brief = "\n".join(brief)

    def session_brief(self) -> str:
        """The injected short-term memory brief (empty when no MEMORY.md exists)."""
        return self._brief

    # ── recall ──
    async def recall(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        """RRF-fused session-memory recall (tsvector + pgvector, tsvector fallback)."""
        return await self._retriever.search(query, top_k)

    async def recall_file(self, query: str, limit: int = 3) -> list[MemoryHit]:
        """Keyword recall over the long-term file memory."""
        memories = await self._file_store.search(query, limit=limit)
        return [
            MemoryHit(
                key=mem.name,
                content=mem.content,
                description=mem.description,
                type=mem.type,
                source="file",
            )
            for mem in memories
        ]

    async def recall_all(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        """Session (RRF) + file memory combined for prompt injection / tool results."""
        session_hits = await self.recall(query, top_k)
        file_hits = await self.recall_file(query, limit=3)
        return session_hits + file_hits

    # ── write (human-confirmation gate) ──
    async def save(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        type_: str = "",
        confirmed: bool = False,
    ) -> Memory:
        if not confirmed:
            raise PermissionError(
                "memory_save requires human confirmation before committing to long-term memory"
            )
        return await self._file_store.save(name, content, description=description, type_=type_)


def memory_search_tool(service: MemoryService) -> ToolDefinition:
    """The builtin ``memory_search`` tool: recall session + file memory for context."""

    async def execute(args: dict, exec: ToolExecution) -> list[dict]:
        hits = await service.recall_all(args["query"], args.get("top_k", 5))
        return [h.to_dict() for h in hits]

    return define_tool(
        name="memory_search",
        description=(
            "Search the user's memory — past conversations and archived notes — for "
            "context relevant to the query. Returns snippets with scores."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The memory query."},
                "top_k": {"type": "integer", "description": "Maximum results."},
            },
            "required": ["query"],
        },
        output=ToolOutput(
            schema={"type": "array"},
            render=lambda args, value: [
                text_block(json.dumps(value, ensure_ascii=False, default=str))
            ],
        ),
        execute=execute,
        permission={ToolPermission.READ},
        is_concurrency_safe=True,
    )


def memory_save_tool(service: MemoryService) -> ToolDefinition:
    """The builtin ``memory_save`` tool: commit a note to long-term memory (gated)."""

    async def execute(args: dict, exec: ToolExecution) -> dict:
        memory = await service.save(
            args["name"],
            args["content"],
            description=args.get("description", ""),
            type_=args.get("type", ""),
        )
        return {"saved": memory.name}

    return define_tool(
        name="memory_save",
        description=(
            "Save a note to long-term memory. Requires human confirmation; without it the "
            "write is denied."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Memory key (kebab-case)."},
                "content": {"type": "string", "description": "The note body."},
                "description": {"type": "string", "description": "One-line summary."},
                "type": {
                    "type": "string",
                    "description": "user | feedback | project | reference",
                },
            },
            "required": ["name", "content"],
        },
        output=ToolOutput(
            schema={"type": "object"},
            render=lambda args, value: [text_block(json.dumps(value, ensure_ascii=False))],
        ),
        execute=execute,
        destructive=True,
        permission={ToolPermission.WRITE},
    )
