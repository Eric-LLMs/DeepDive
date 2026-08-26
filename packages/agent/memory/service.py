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

from core.config import settings

from agent.engine.decisions import ToolExecution, text_block
from agent.memory.base import MemoryStore
from agent.memory.retrieval import MemoryHit, RRFMemoryRetriever
from agent.memory.types import MEMORY_TYPES, Memory
from agent.tools.definition import ToolDefinition, ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission

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

    # ── session lifecycle ──
    def begin_session(self) -> str:
        """Load and return the ``MEMORY.md`` short-term brief (first ``top_lines`` lines).

        The returned text is per-turn state: the kernel stores it on the current
        :class:`~agent.engine.context.AgentTurn` (``turn.memory_brief``), so concurrent turns never
        share it (the old ``self._brief`` instance field raced across turns).
        """
        brief: list[str] = []
        if self._memory_md is not None and self._memory_md.is_file():
            try:
                brief = self._memory_md.read_text(encoding="utf-8").splitlines()[: self._top_lines]
            except OSError:
                brief = []
        return "\n".join(brief)

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

    def should_recall(self, query: str) -> bool:
        """Cheap lexical prefilter deciding whether proactive recall runs this turn.

        Recall is gated (OpenClaw Lane-2 style) so the expensive RRF query only fires on
        turns that plausibly refer to prior context: a non-empty query that either is short
        (elliptical, e.g. "上次呢" → refers to earlier context) or contains a trigger word
        ("remember", "上次", "之前", ...). Every turn still gets the always-on
        ``MEMORY.md`` brief (Lane-1), so this only skips the deep recall, never the context.
        """
        q = (query or "").strip().lower()
        if not q:
            return False
        if len(q) <= settings.memory_recall_min_len:
            return True
        return any(t in q for t in settings.memory_recall_trigger_words)

    # ── write (guardrailed; the session stays READ-only, this memdir write is exempt) ──
    async def save(
        self,
        name: str,
        content: str,
        *,
        description: str = "",
        type_: str = "",
        importance: int = 5,
        supersedes: str = "",
        confirmed: bool = False,
    ) -> Memory:
        """Commit a note to the long-term file memory with guardrails.

        ``importance`` (1–10) is folded into keyword recall so curated notes surface ahead
        of incidental ones. ``supersedes`` implements supersede-in-place: when it names an
        existing memory, that memory is marked ``superseded`` and dropped from the index and
        recall, so a stale preference can never resurface alongside its replacement.
        ``confirmed`` is accepted for a future human-confirmation gate but no longer
        required: ``memory_save`` is READ-classified (it writes the local memdir only), so
        the agent can persist notes without weakening the sandbox's READ-only default.
        """
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"memory name must be kebab-case ([a-z][a-z0-9-]*), got {name!r}")
        if not isinstance(importance, int) or not 1 <= importance <= 10:
            raise ValueError(f"importance must be an integer 1-10, got {importance!r}")
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        if len(content) > self._note_max_chars:
            raise ValueError(
                f"memory content exceeds the {self._note_max_chars}-character limit"
            )
        if type_ and type_ not in MEMORY_TYPES:
            raise ValueError(f"memory type must be one of {MEMORY_TYPES}, got {type_!r}")
        if supersedes:
            old = await self._file_store.load(supersedes)
            if old is None:
                raise ValueError(f"supersedes targets missing memory {supersedes!r}")
            await self._file_store.mark_superseded(supersedes)
        await self._file_store.save(
            name, content, description=description, type_=type_,
            importance=importance, supersedes=supersedes,
        )
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
            importance=args.get("importance", 5),
            supersedes=args.get("supersedes", ""),
        )
        return {"saved": memory.name}

    return define_tool(
        name="memory_save",
        description=(
            "Save a note to long-term memory. The name must be kebab-case and the content "
            "is length-capped; the write is confined to the local memory directory. "
            "For type='user', write an imperative directive ('Always …', 'Never …', "
            "'Prefer …'). To replace an outdated memory in place, pass its name in "
            "'supersedes' — the old value is marked superseded and never resurfaces."
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
                "importance": {
                    "type": "integer",
                    "description": "Salience 1-10 (default 5); higher ranks higher in recall.",
                },
                "supersedes": {
                    "type": "string",
                    "description": "Name of an existing memory this one replaces (supersede in place).",
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
