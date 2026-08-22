"""MemoryService: dual-track memory orchestration + memory-as-tools.

Two tracks feed recall:
- **Long-term file memory** — the claude-code memdir (``data/memory/*.md`` + ``MEMORY.md``
  index), keyword-scored by :class:`~agent.memory.file.FileMemoryStore`.
- **Session memory** — PostgreSQL ``pgvector`` (semantic) + ``tsvector`` (keyword) recall
  fused by RRF (see ``core.infrastructure.memory_retrieval``); tsvector-only when the
  embedding service is offline.

At session start ``begin_session()`` loads the ``MEMORY.md`` brief (first ``top_lines``
lines) for injection. ``memory_search``/``memory_save`` expose recall/write as tools;
``memory_save`` is READ-classified with guardrails (name/content/type), so the agent can
persist notes to the local memdir without breaking the session's READ-only posture.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent.decisions import ToolExecution, text_block
from agent.memory.base import MemoryStore
from agent.memory.retrieval import MemoryHit, RRFMemoryRetriever
from agent.memory.types import MEMORY_TYPES, Memory
from agent.tool_permissions import ToolPermission
from agent.tools import ToolDefinition, ToolOutput, define_tool

# kebab-case memory key: lowercase letters/digits/hyphens.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class MemoryService:
    def __init__(
        self,
        file_store: MemoryStore,
        retriever: RRFMemoryRetriever,
        *,
        memory_md_path: Path | None = None,
        top_lines: int = 200,
        note_max_chars: int = 4000,
    ) -> None:
        self._file_store = file_store
        self._retriever = retriever
        self._memory_md = memory_md_path
        self._top_lines = top_lines
        self._note_max_chars = note_max_chars
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

    # ── write (guardrailed; the session stays READ-only, this memdir write is exempt) ──
    async def save(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        type_: str = "",
        confirmed: bool = False,
    ) -> Memory:
        """Commit a note to the long-term file memory with guardrails.

        ``confirmed`` is accepted for a future human-confirmation gate but no longer
        required: ``memory_save`` is READ-classified (it writes the local memdir only), so
        the agent can persist notes without weakening the sandbox's READ-only default.
        """
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"memory name must be kebab-case ([a-z][a-z0-9-]*), got {name!r}")
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        if len(content) > self._note_max_chars:
            raise ValueError(
                f"memory content exceeds the {self._note_max_chars}-character limit"
            )
        if type_ and type_ not in MEMORY_TYPES:
            raise ValueError(f"memory type must be one of {MEMORY_TYPES}, got {type_!r}")
        await self._file_store.save(name, content, description=description, type_=type_)
        saved = await self._file_store.load(name)
        if saved is None:  # pragma: no cover - just written, must exist
            raise RuntimeError(f"memory save reported success but {name!r} is not readable")
        return saved


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
    """The builtin ``memory_save`` tool: commit a guardrailed note to long-term memory."""

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
            "Save a note to long-term memory. The name must be kebab-case and the content "
            "is length-capped; the write is confined to the local memory directory."
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
        permission={ToolPermission.READ},
    )
