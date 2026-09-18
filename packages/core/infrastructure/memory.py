"""Session memory: persist chat messages + session events to PostgreSQL.

Chat session-memory v2 — client Live State is authoritative, SQL is the durable fallback:

- **Normal turn (hot path): zero SQL reads.** The client uploads ``context_state``
  (the summary it holds + the inclusive watermark ``through_message_id``) plus its
  ``tail``; the server only assembles ``[summary → leading system] + tail + new
  message``. ``sessions.compaction`` is a durable backup serving recovery,
  compaction, and reconcile — never the normal turn.
- **Compaction is a rare event.** Below threshold: 0 extra compaction LLM calls
  (normal chat inference is untouched). Above threshold: after the dual persistence
  barrier (client reports no pending mutations AND the server-side per-session write
  queue has fully flushed), the server reads the fold range from SQL and produces
  ONE full re-fold from RAW messages into a 5-section single-layer structured
  summary — never a summary-of-summary. On any failure the context is left
  untouched (no silent trim); the turn defers with a visible ``compaction_deferred``
  reason and the next turn retries.
- **Per-turn persistence** goes through a per-session async write queue
  (:class:`_SessionWriteQueue`): batch ``INSERT ... RETURNING`` at flush time, 2
  retries, ``persist_failed`` recorded — a failed write never touches the client's
  Live State.
- **Boundaries** are expressed purely as ``message_id + created_at`` (there is no
  sequence column). ``through_message_id`` is INCLUSIVE: the summary fully covers
  the session head through that message; the first message after it starts the tail.

Explicit trade-off (deliberate, documented per the design):
    The full re-fold (every compaction rebuilds the summary from raw SQL messages,
    refusing summary-of-summary) carries a token cost on the COMPACTING turn that
    grows linearly with the fold range — O(total session length) per fold. This is
    the accepted price of eliminating generational memory decay entirely. The cost
    is paid only at low-frequency compaction events; normal turns make 0 extra
    compaction LLM calls. Raw messages are permanent in SQL, so the summary can be
    regenerated losslessly at any time.

Retrieval is vector recall over a user's messages (cross-session) via pgvector cosine distance;
long-term file memory (memdir) remains a separate layer (see ``agent.memory``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, or_, select, tuple_

logger = logging.getLogger(__name__)

from core.config import settings
from core.infrastructure.db import (
    AssetModel,
    MessageModel,
    SessionEventModel,
    SessionModel,
    UserModel,
)
from core.infrastructure.vector import TEIEmbedder

# ─────────────────────────────────────────────────────────────────────────────
# Compaction checkpoint (sessions.compaction JSONB, migration 0018)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CompactionState:
    """Parsed ``sessions.compaction`` checkpoint.

    Invariant: ``summary`` fully covers every model-facing message from the session
    head through ``through_message_id`` INCLUSIVE. Summary and watermark are written
    by ONE conditional UPDATE (revision CAS) — a partial state is impossible.
    """

    revision: int
    through_message_id: UUID | None
    through_created_at: datetime | None
    summary: str | None
    summary_chars: int
    last_compaction_at: str | None
    fold_count: int

    def to_payload(self) -> dict:
        return {
            "revision": self.revision,
            "through_message_id": str(self.through_message_id) if self.through_message_id else None,
            "through_created_at": self.through_created_at.isoformat() if self.through_created_at else None,
            "summary": self.summary,
            "summary_chars": self.summary_chars,
            "last_compaction_at": self.last_compaction_at,
            "fold_count": self.fold_count,
        }


async def load_compaction(session_factory, session_id: UUID) -> CompactionState | None:
    """Read the durable checkpoint (recovery / compaction / reconcile paths ONLY)."""
    async with session_factory() as session:
        raw = (
            await session.execute(
                select(SessionModel.compaction).where(SessionModel.id == session_id)
            )
        ).scalar_one_or_none()
    if not raw:
        return None
    try:
        return CompactionState(
            revision=int(raw.get("revision") or 0),
            through_message_id=(
                UUID(str(raw["through_message_id"])) if raw.get("through_message_id") else None
            ),
            through_created_at=(
                datetime.fromisoformat(raw["through_created_at"])
                if raw.get("through_created_at")
                else None
            ),
            summary=raw.get("summary"),
            summary_chars=int(raw.get("summary_chars") or 0),
            last_compaction_at=raw.get("last_compaction_at"),
            fold_count=int(raw.get("fold_count") or 0),
        )
    except Exception:  # noqa: BLE001 - a corrupt checkpoint degrades to "no checkpoint"
        logger.warning("corrupt sessions.compaction checkpoint for %s; ignoring", session_id)
        return None


async def save_compaction(
    session_factory,
    session_id: UUID,
    state: CompactionState,
    *,
    expected_revision: int | None,
) -> bool:
    """CAS-write the checkpoint. Returns ``False`` when the revision moved under us.

    ``expected_revision=None`` means "first fold" (row must still have no
    checkpoint). Summary and watermark land in the SAME UPDATE — no partial state.
    The UI sidebar column ``sessions.summary`` is refreshed from the summary head in
    the same statement (a pure copy; it NEVER feeds the chat hot path).
    """
    from sqlalchemy import text as sql_text

    payload = _json_dumps(state.to_payload())
    side = (state.summary or "")[:400] or None
    async with session_factory() as session:
        if expected_revision is None:
            result = await session.execute(
                sql_text(
                    "UPDATE sessions SET compaction = CAST(:payload AS jsonb), summary = :side "
                    "WHERE id = :sid AND compaction IS NULL"
                ),
                {"payload": payload, "side": side, "sid": str(session_id)},
            )
        else:
            result = await session.execute(
                sql_text(
                    "UPDATE sessions SET compaction = CAST(:payload AS jsonb), summary = :side "
                    "WHERE id = :sid AND (compaction->>'revision')::int = :exp_rev"
                ),
                {"payload": payload, "side": side, "sid": str(session_id), "exp_rev": expected_revision},
            )
        await session.commit()
        return result.rowcount == 1


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# Per-session async write queue (persistence fallback; flush = barrier primitive)
# ─────────────────────────────────────────────────────────────────────────────

class _SessionWriteQueue:
    """Pending message writes for one session, flushed as ONE batch INSERT.

    Same event loop only (the API/worker process each own one loop; the dict is
    keyed by session). ``flush`` retries twice; on final failure the items stay
    pending (nothing is lost) and the caller decides how to surface it. A retry
    after a connection drop could duplicate a row that actually committed —
    accepted: the transcript is an append log and reconcile (client-authoritative)
    is the convergence mechanism.
    """

    def __init__(self) -> None:
        self._pending: list[dict] = []
        self._lock = asyncio.Lock()

    def enqueue(self, item: dict) -> None:
        self._pending.append(item)

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    async def flush(self, session_factory) -> list[dict]:
        """Write all pending items; return the written rows ``{id, created_at, role}``.

        Raises after 2 retries — pending items are kept for the next flush.
        """
        async with self._lock:
            if not self._pending:
                return []
            items = list(self._pending)
            values = [
                {
                    "user_id": it["user_id"],
                    "session_id": it["session_id"],
                    "role": it["role"],
                    "text": it["text"],
                    "attach_asset_id": it.get("attach_asset_id"),
                }
                for it in items
            ]
            last_error: Exception | None = None
            for _attempt in range(3):  # first try + 2 retries
                try:
                    async with session_factory() as session:
                        rows = (
                            await session.execute(
                                insert(MessageModel)
                                .values(values)
                                .returning(MessageModel.id, MessageModel.created_at, MessageModel.role)
                            )
                        ).all()
                        await session.commit()
                    self._pending = self._pending[len(items):]
                    return [
                        {
                            "message_id": str(r.id),
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "role": r.role,
                        }
                        for r in rows
                    ]
                except Exception as exc:  # noqa: BLE001 - transient DB errors retry; then surface
                    last_error = exc
                    await asyncio.sleep(0.2 * (_attempt + 1))
            raise last_error  # type: ignore[misc]


_write_queues: dict[UUID, _SessionWriteQueue] = {}


def _get_write_queue(session_id: UUID) -> _SessionWriteQueue:
    queue = _write_queues.get(session_id)
    if queue is None:
        queue = _write_queues[session_id] = _SessionWriteQueue()
    return queue


async def flush_session_writes(session_factory, session_id: UUID) -> list[dict]:
    """Persistence-barrier primitive: drive all pending writes for ``session_id`` to SQL.

    Raises on persistent failure — the caller must treat that as "SQL does not
    reflect the current state" and refuse to read the fold range (compaction
    defers with ``persist_barrier_failed``).
    """
    queue = _get_write_queue(session_id)
    written = await queue.flush(session_factory)
    if not queue.pending:
        _write_queues.pop(session_id, None)
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Structured fold (full re-fold from raw messages — never summary-of-summary)
# ─────────────────────────────────────────────────────────────────────────────

_FOLD_SYSTEM = (
    "You are a conversation-memory compactor. Compress the transcript into EXACTLY these "
    "five markdown sections, preserving every durable fact (no invented content):\n"
    "## Primary intent\nWhat the user is trying to accomplish overall.\n"
    "## Decisions & conclusions\nChoices made and their rationale; answers given.\n"
    "## Key facts & entities\nNames, paths, IDs, numbers, configuration values, dates.\n"
    "## Files & assets touched\nWorkspace files, documents, images discussed or modified.\n"
    "## Open tasks & questions\nUnfinished work and unresolved questions, including any "
    "verbatim user constraints (quote them exactly).\n"
    "Output only the five sections."
)


async def _fold_summary(
    llm,
    transcript: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Produce the 5-section single-layer structured summary, capped by settings."""
    raw = await llm.complete(transcript, _FOLD_SYSTEM, model=model, base_url=base_url, api_key=api_key)
    cap = settings.compaction_summary_max_chars
    if cap > 0 and len(raw) > cap:
        raw = raw[: cap - 1].rstrip() + "…"
    return raw


@dataclass
class CompactionOutcome:
    """Result of :func:`apply_compaction`.

    ``status`` is ``"compacted"`` or ``"deferred"``; on defer the caller keeps the
    client Live State ASSEMBLED-AS-IS (no trim, no watermark move) and surfaces
    ``deferred_reason`` in the done frame. ``context`` is the assembled history to
    feed the agent when compacted (leading system summary + uncovered overflow + kept
    tail); ``None`` on defer (caller falls back to its own assembly).
    """

    status: str
    deferred_reason: str | None = None
    context: list[dict] | None = None
    compaction: dict | None = None  # done-frame payload: {summary, through_message_id, through_created_at, revision}
    audit: dict | None = None
    written: list[dict] = field(default_factory=list)


def needs_compaction(*, summary: str | None, tail: list[dict], new_message: str) -> bool:
    """Threshold check over the ASSEMBLED context (summary + tail + new message).

    Mirrors the old rule: trigger on message count over ``history_max_messages``, or
    on the char budget ``prompt_max_chars`` once past the kept tail size. Below the
    threshold nothing here runs — 0 extra compaction LLM calls.
    """
    total = len(tail) + 1 + (1 if summary else 0)
    if total > settings.history_max_messages:
        return True
    if total > settings.history_keep_messages:
        total_chars = len(summary or "") + sum(len(str(m.get("content") or "")) for m in tail) + len(new_message)
        if total_chars > settings.prompt_max_chars:
            return True
    return False


def _snip(content: str) -> str:
    cap = settings.prompt_message_max_chars
    if cap > 0 and len(content) > cap:
        return content[: cap - 1].rstrip() + "…(truncated)"
    return content


async def apply_compaction(
    *,
    session_factory,
    session_id: UUID,
    llm,
    tail: list[dict],
    has_pending_mutations: bool,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    flush_hook: Callable[[], Awaitable[list[dict]]] | None = None,
) -> CompactionOutcome:
    """Threshold-triggered compaction behind the dual persistence barrier.

    Rules (locked by the design — do not soften):

    1. **Dual barrier**: ``has_pending_mutations=true`` (client backlog of unsent
       Edit/Delete) defers with ``client_pending_mutations``; otherwise the
       server-side write queue must FULLY flush first — flush failure defers with
       ``persist_barrier_failed``. Only after both may SQL be read for folding.
       A possibly-stale SQL snapshot is never used for a summary.
    2. **Full re-fold**: the fold input is every raw model-facing row from the
       session head through the boundary (inclusive); the old summary is discarded
       and rebuilt — never rolled. See the module docstring for the O(fold range)
       token trade-off.
    3. **Budget, deterministically**: rows are packed oldest-first within
       ``prompt_max_chars``; the watermark advances only to the LAST covered row;
       uncovered middle rows stay at the top of the tail (monotone convergence,
       zero silent loss).
    4. **Failure never trims**: a fold-LLM or CAS failure leaves the context and
       watermark untouched and defers (``fold_failed`` / ``cas_conflict``); the
       next turn retries. There is no silent overflow-drop degradation.
    5. ``through_message_id`` is INCLUSIVE: the boundary is the last overflow tail
       entry carrying a message id; the tail's first kept message follows it.

    ``tail`` is the CLIENT-declared tail WITHOUT the current user message; each
    entry is ``{message_id|None, role, content}``. The current turn's messages are
    persisted by the normal write path, never here.
    """
    keep = settings.history_keep_messages
    if has_pending_mutations:
        return CompactionOutcome(status="deferred", deferred_reason="client_pending_mutations")
    overflow = tail[:-keep] if len(tail) > keep else []
    tail_items = tail[-keep:] if len(tail) > keep else tail
    if not overflow:
        # Nothing droppable (char-budget trigger with a small count): keep tail as-is.
        return CompactionOutcome(status="deferred", deferred_reason="below_keep")

    # Server-side barrier: force every pending write down to SQL BEFORE reading.
    written: list[dict] = []
    try:
        written = await (flush_hook or (lambda: flush_session_writes(session_factory, session_id)))()
    except Exception:  # noqa: BLE001 - barrier failure: no read, no fold, no watermark move
        logger.warning("persist barrier flush failed for session %s; deferring compaction", session_id)
        return CompactionOutcome(status="deferred", deferred_reason="persist_barrier_failed")

    # Boundary: last overflow entry that carries a client message id.
    boundary_id: UUID | None = None
    for item in reversed(overflow):
        raw = item.get("message_id")
        if raw:
            try:
                boundary_id = UUID(str(raw))
            except ValueError:
                boundary_id = None
            if boundary_id:
                break
    if boundary_id is None:
        return CompactionOutcome(status="deferred", deferred_reason="no_boundary_id")

    # Fold range read from SQL (the ONLY dialogue path that reads messages):
    # head .. boundary INCLUSIVE, model-facing roles only, ordered by (created_at, id).
    async with session_factory() as session:
        brow = (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.id == boundary_id, MessageModel.session_id == session_id)
            )
        ).scalar_one_or_none()
        if brow is None:
            # Boundary row vanished (deleted server-side outside the client's view) —
            # the snapshot is not trustworthy for this fold; defer and let the next
            # turn (after reconcile/recovery) retry with a client tail that matches SQL.
            return CompactionOutcome(status="deferred", deferred_reason="stale_boundary")
        rows = (
            await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.role.in_(("user", "assistant")),
                    tuple_(MessageModel.created_at, MessageModel.id)
                    <= tuple_(brow.created_at, brow.id),
                )
                .order_by(MessageModel.created_at, MessageModel.id)
            )
        ).scalars().all()

    budget = settings.prompt_max_chars
    covered: list = []
    used = 0
    for m in rows:
        size = len(_snip(m.text))
        if covered and used + size > budget:
            break  # watermark stops before this row; it stays at the tail top
        covered.append(m)
        used += size
    if not covered:
        return CompactionOutcome(status="deferred", deferred_reason="fold_failed")

    covered_ids = {m.id for m in covered}
    last = covered[-1]
    transcript = "\n".join(f"{m.role}: {_snip(m.text)}" for m in covered)
    try:
        summary = await _fold_summary(
            llm, transcript, model=model, base_url=base_url, api_key=api_key
        )
    except Exception:  # noqa: BLE001 - fold failure NEVER trims: full defer semantics
        logger.warning("compaction fold LLM call failed for session %s; deferring", session_id)
        return CompactionOutcome(status="deferred", deferred_reason="fold_failed")

    current = await load_compaction(session_factory, session_id)
    expected = current.revision if current is not None else None
    new_state = CompactionState(
        revision=(expected or 0) + 1,
        through_message_id=last.id,
        through_created_at=last.created_at,
        summary=summary,
        summary_chars=len(summary),
        last_compaction_at=datetime.now(UTC).isoformat(),
        fold_count=(current.fold_count + 1) if current else 1,
    )
    ok = await save_compaction(session_factory, session_id, new_state, expected_revision=expected)
    if not ok:
        # Another writer moved the checkpoint mid-fold: reload next turn, do not
        # double-fold from this snapshot.
        return CompactionOutcome(status="deferred", deferred_reason="cas_conflict")

    # Assemble: summary (leading system) + overflow rows the fold did NOT cover + kept tail.
    kept_overflow = [
        {"role": item["role"], "content": item.get("content") or ""}
        for item in overflow
        if not item.get("message_id")
        or _safe_uuid(item["message_id"]) not in covered_ids
    ]
    context = (
        [{"role": "system", "content": "## Conversation summary\n" + summary}]
        + kept_overflow
        + [{"role": item["role"], "content": item.get("content") or ""} for item in tail_items]
    )
    audit = {
        "dropped": len(covered),
        "through_message_id": str(last.id),
        "through_created_at": last.created_at.isoformat() if last.created_at else None,
        "revision": new_state.revision,
        "summary": summary,
    }
    payload = {
        "summary": summary,
        "through_message_id": str(last.id),
        "through_created_at": last.created_at.isoformat() if last.created_at else None,
        "revision": new_state.revision,
    }
    return CompactionOutcome(
        status="compacted", context=context, compaction=payload, audit=audit, written=written
    )


def _safe_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def summary_block(summary: str | None) -> list[dict]:
    """The Live-State summary as a leading system message (None → no block)."""
    if not summary:
        return []
    return [{"role": "system", "content": "## Conversation summary\n" + summary}]


async def assemble_recovery_history(
    session_factory,
    session_id: UUID,
    llm,
    new_message: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], dict | None, str | None, dict | None]:
    """Client-less recovery mode: rebuild history from the checkpoint + bounded SQL load.

    Used when no client Live State is available — legacy chat clients and the worker
    ``run_agent_turn`` job. Loads only rows STRICTLY after the checkpoint watermark
    (never the old full-table reload), and compacts with the same
    :func:`apply_compaction` if the threshold is crossed — closing the gap where the
    worker path previously grew unbounded. Returns
    ``(history, compaction_payload, deferred_reason, audit_payload)``; the caller
    records ``audit`` as a ``compaction`` session event when present.
    """
    checkpoint = await load_compaction(session_factory, session_id)
    summary = checkpoint.summary if checkpoint is not None else None
    tail = await load_session_messages(
        session_factory,
        session_id,
        after=checkpoint.through_message_id if checkpoint is not None else None,
        with_ids=True,
    )
    history = summary_block(summary) + [
        {"role": m["role"], "content": m["content"]} for m in tail
    ]
    if not needs_compaction(summary=summary, tail=tail, new_message=new_message):
        return history, None, None, None
    outcome = await apply_compaction(
        session_factory=session_factory,
        session_id=session_id,
        llm=llm,
        tail=tail,
        has_pending_mutations=False,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    if outcome.status == "compacted":
        return outcome.context, outcome.compaction, None, outcome.audit
    return history, None, outcome.deferred_reason, None


# ─────────────────────────────────────────────────────────────────────────────
# Store (per-turn handle)
# ─────────────────────────────────────────────────────────────────────────────

class SessionMemoryStore:
    def __init__(
        self,
        session_factory,
        embedder: TEIEmbedder,
        llm,
        session_id: UUID,
        user_id: UUID,
        attach_asset_id: UUID | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder
        self.llm = llm
        self.session_id = session_id
        self.user_id = user_id
        # Cloud-drive asset the current turn's user message OWNS (📷 screenshot). None for
        # referential attaches. Written to MessageModel.attach_asset_id on append_message.
        self._attach_asset_id = attach_asset_id
        self._events: list[tuple[str, dict]] = []
        # Written rows collected by flushes on this store; the router drains them via
        # ``take_written`` to put this turn's message ids in the done frame (no text scan).
        self._written_buffer: list[dict] = []
        # True once a write flush exhausted its retries — surfaced in the done frame as
        # ``persist_failed``; the client's Live State is never affected.
        self.persist_failed = False

    def record_event(self, type_: str, payload: dict) -> None:
        """Buffer a session event (flushed on :meth:`close`)."""
        self._events.append((type_, payload or {}))

    async def append_message(self, role: str, text: str) -> None:
        """Enqueue one message write (non-blocking; batched by the per-session queue).

        The user message of a turn records ``attach_asset_id`` — the cloud-drive asset the
        message OWNS (a 📷 chat screenshot). It is written only for ``role == "user"`` and
        only when the store was built with a value, so assistant/tool messages and
        referential attaches never populate it. Deleting the message/session later cascades a
        soft-delete of that asset (folder-agnostic).

        Persistence is async and lossless-by-retry: the row lands on the next
        :meth:`flush_writes` (every turn end) with 2 retries; a final failure sets
        ``persist_failed`` and keeps the item pending for the following turn.
        """
        queue = _get_write_queue(self.session_id)
        queue.enqueue(
            {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "role": role,
                "text": text,
                "attach_asset_id": self._attach_asset_id if role == "user" else None,
            }
        )

    async def flush_writes(self) -> list[dict]:
        """Drive pending writes to SQL; return written rows ``{message_id, created_at, role}``.

        Never raises — failure is recorded on ``persist_failed`` and the items stay
        queued for the next flush. The done frame awaits this (millisecond task) to
        carry the new messages' ids.
        """
        try:
            rows = await flush_session_writes(self.session_factory, self.session_id)
            self._written_buffer.extend(rows)
            return rows
        except Exception:  # noqa: BLE001 - persistence must never break a chat turn
            logger.warning("message persistence failed for session %s (retry queued)", self.session_id)
            self.persist_failed = True
            return []

    def take_written(self) -> list[dict]:
        """Drain and return every row written through this store so far."""
        rows, self._written_buffer = self._written_buffer, []
        return rows

    async def close(self) -> None:
        """Flush pending message writes + buffered events (idempotent, best-effort).

        The expensive consolidation (batch embedding) is deferred to the
        ``session_finalize`` worker job via :func:`finalize_session`.
        """
        await self.flush_writes()
        async with self.session_factory() as session:
            for seq, (type_, payload) in enumerate(self._events):
                session.add(
                    SessionEventModel(
                        session_id=self.session_id,
                        seq=seq,
                        type=type_,
                        timestamp=time.time(),
                        payload=payload,
                    )
                )
            self._events.clear()
            await session.commit()

    async def search(self, query_embedding: list[float], top_k: int = 5) -> list[dict]:
        """Vector recall over this user's messages (cross-session)."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        MessageModel,
                        (1 - MessageModel.embedding.cosine_distance(query_embedding)).label("score"),
                    )
                    .where(
                        MessageModel.user_id == self.user_id,
                        MessageModel.embedding.is_not(None),
                    )
                    .order_by(MessageModel.embedding.cosine_distance(query_embedding))
                    .limit(top_k)
                )
            ).all()
            return [
                {"id": str(m.id), "role": m.role, "text": m.text, "score": float(score)}
                for m, score in rows
            ]

    async def load_messages(self, after: str | UUID | None = None, with_ids: bool = False) -> list[dict]:
        """Rebuild the message history for resume (bounded: only rows after the watermark)."""
        return await load_session_messages(
            self.session_factory, self.session_id, after=after, with_ids=with_ids
        )


async def insert_plain_message(
    session_factory, user_id: UUID, session_id: UUID, role: str, text: str
) -> None:
    """Insert one chat message row (any role) directly, committing immediately.

    A lightweight twin of :meth:`SessionMemoryStore.append_message` for callers that already
    hold ``user_id`` / ``session_id`` and have no embedder/llm handy. Used to persist the
    deterministic ``system`` gate notes; those rows are display-only and are filtered out of
    model context, session archival, and RAG import everywhere else.
    """
    async with session_factory() as session:
        session.add(
            MessageModel(
                user_id=user_id, session_id=session_id, role=role, text=text
            )
        )
        await session.commit()


async def finalize_session(session_factory, embedder, llm, session_id: UUID) -> dict:
    """Backfill message embeddings for a closed session (+ first-time title/summary).

    Called by the worker's ``session_finalize`` job. Since session-memory v2 the
    conversation summary is owned by the compaction checkpoint
    (``sessions.compaction``) — this job NO LONGER re-summarizes the transcript on
    every call. It keeps: incremental embedding of un-embedded user/assistant rows,
    ``closed_at``, the one-shot LLM title, and a first-time-only sidebar summary
    (generated once when the session has neither a sidebar summary nor a
    checkpoint; compaction refreshes the sidebar column directly thereafter).
    """
    async with session_factory() as session:
        messages = (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).scalars().all()

        # Archival covers real conversation only: deterministic ``system`` notes (e.g. gate
        # review explanations) and ``tool`` rows never enter the embedding corpus or the
        # summary transcript, so recall/summary cannot leak runtime bookkeeping to the model.
        convo = [m for m in messages if m.role in ("user", "assistant")]

        embedded = 0
        to_embed = [m for m in convo if m.embedding is None]
        if to_embed:
            embeddings = await embedder.embed([m.text for m in to_embed])
            for message, embedding in zip(to_embed, embeddings):
                message.embedding = embedding
            embedded = len(to_embed)

        summary = None
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is not None:
            sess.closed_at = datetime.now(UTC)
            # First-time-only bounded sidebar summary (session still uncompacted and
            # never summarized). Idempotent: later finalizes skip; compaction keeps
            # the column fresh from the checkpoint side.
            if (
                settings.session_summary_enabled
                and sess.summary is None
                and not sess.compaction
                and convo
            ):
                transcript = "\n".join(f"{m.role}: {m.text}" for m in convo)
                try:
                    summary = await _summarize_transcript(llm, transcript)
                    sess.summary = summary
                except Exception:  # noqa: BLE001 - summary is cosmetic; finalize must still finish
                    logger.warning("session summary failed (finalize continues): session %s", session_id)
                    summary = None
            # Auto-title once: a short LLM title from the first user message. Idempotent —
            # later finalizes skip because ``sess.title`` is already set. Cosmetic, so a
            # failure degrades to the first words of the first user message.
            if sess.title is None and messages:
                first_user = next((m.text for m in messages if m.role == "user"), "")
                if first_user:
                    try:
                        sess.title = await _summarize_title(llm, first_user[:500])
                    except Exception:  # noqa: BLE001 - title is cosmetic
                        sess.title = None
                    if not sess.title or len(sess.title) > 50:
                        sess.title = first_user.strip()[:40] or None
        await session.commit()
        return {"embedded": embedded, "summary": summary}


async def _summarize_transcript(
    llm,
    transcript: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """LLM-compress a raw transcript into a concise summary paragraph (sidebar only).

    Used ONCE by ``finalize_session`` for first-time sidebar summaries of short,
    never-compacted sessions. The compaction path uses :func:`_fold_summary` (the
    structured 5-section template) — never this prompt.
    """
    return await llm.complete(
        "Summarize this conversation into a concise paragraph capturing the user's "
        "goals and key points.\n\n" + transcript,
        "You are a memory summarizer. Output only the summary.",
        model=model,
        base_url=base_url,
        api_key=api_key,
    )


async def _summarize_title(llm, first_user_text: str) -> str:
    """LLM-shorten a session's first user message into a short display title.

    Used by ``finalize_session`` for auto-naming (like ChatGPT/Gemini). The caller
    validates length and falls back to the first words when this fails.
    """
    return await llm.complete(
        "Summarize into a very short conversation title (50 chars max, 3-7 words), "
        "based on the user's first message. Output only the title.\n\n" + first_user_text,
        "You are a title generator. Output only the title.",
    )


async def load_session_messages(
    session_factory,
    session_id: UUID,
    *,
    after: str | UUID | None = None,
    with_ids: bool = False,
) -> list[dict]:
    """Return a session's *model-facing* messages as ``[{"role", "content"}]``.

    Only ``user``/``assistant`` rows are returned. Deterministic ``system`` notes (gate
    review explanations) and ``tool`` rows are display bookkeeping — they never enter the
    prompt context the agent kernel builds from this history. (The client-facing resume view
    is :func:`load_session_detail`, which keeps every row so ``system`` notes still render.)

    ``after`` is the compaction watermark (INCLUSIVE boundary): recovery/worker paths
    load only the rows STRICTLY after it, ordered by ``(created_at, id)``; an unknown
    id degrades to the full load (nothing is silently skipped). ``with_ids`` also
    returns ``message_id``/``created_at`` so a recovery-mode caller can hand the
    exact same tail shape to :func:`apply_compaction`.
    """
    async with session_factory() as session:
        after_row = None
        if after is not None:
            try:
                after_row = (
                    await session.execute(
                        select(MessageModel)
                        .where(MessageModel.id == UUID(str(after)), MessageModel.session_id == session_id)
                    )
                ).scalar_one_or_none()
            except (ValueError, TypeError):
                after_row = None
        stmt = (
            select(MessageModel)
            .where(
                MessageModel.session_id == session_id,
                MessageModel.role.in_(("user", "assistant")),
            )
            .order_by(MessageModel.created_at, MessageModel.id)
        )
        if after_row is not None:
            stmt = stmt.where(
                tuple_(MessageModel.created_at, MessageModel.id)
                > tuple_(after_row.created_at, after_row.id)
            )
        rows = (await session.execute(stmt)).scalars().all()
        out: list[dict] = []
        for m in rows:
            item: dict = {"role": m.role, "content": m.text}
            if with_ids:
                item["message_id"] = str(m.id)
                item["created_at"] = m.created_at.isoformat() if m.created_at else None
            out.append(item)
        return out


async def append_messages_batch(
    session_factory, user_id: UUID, session_id: UUID, items: list[dict]
) -> list[dict]:
    """Batch-insert ``[{role, text, attach_asset_id?}]`` with ``RETURNING``.

    The write queue's primitive: one INSERT for the whole turn, returning the
    created rows' ``{message_id, created_at, role}`` so the done frame can carry
    real ids without a full-text scan.
    """
    if not items:
        return []
    values = [
        {
            "user_id": user_id,
            "session_id": session_id,
            "role": it["role"],
            "text": it["text"],
            "attach_asset_id": it.get("attach_asset_id"),
        }
        for it in items
    ]
    async with session_factory() as session:
        rows = (
            await session.execute(
                insert(MessageModel)
                .values(values)
                .returning(MessageModel.id, MessageModel.created_at, MessageModel.role)
            )
        ).all()
        await session.commit()
    return [
        {
            "message_id": str(r.id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "role": r.role,
        }
        for r in rows
    ]


async def list_sessions(session_factory, user_id: UUID, q: str | None = None) -> list[dict]:
    """Return a user's sessions (newest first) as ``[{id, created_at, summary, title}]``.

    ``q`` filters to sessions whose title, summary, or any message text contains the
    case-insensitive substring. When ``q`` is set each result also carries ``snippet`` —
    the earliest matching message's text (truncated) — so the client can show where the
    match landed even when it is not in the title.
    """
    async with session_factory() as session:
        stmt = select(SessionModel).where(SessionModel.user_id == user_id)
        if q:
            like = f"%{q}%"
            stmt = (
                stmt.outerjoin(MessageModel, MessageModel.session_id == SessionModel.id)
                .filter(
                    or_(
                        SessionModel.title.ilike(like),
                        SessionModel.summary.ilike(like),
                        MessageModel.text.ilike(like),
                    )
                )
                .distinct()
            )
        rows = (
            (await session.execute(stmt.order_by(SessionModel.created_at.desc()))).scalars().all()
        )
        out = [
            {
                "id": str(s.id),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "summary": s.summary,
                "title": s.title,
                # 0 = chat, 1 = research task session — the sidebar filter drops type 1.
                "type": s.type,
            }
            for s in rows
        ]
        if q:
            snippets = await _first_matching_text(session, [s.id for s in rows], f"%{q}%")
            for s, row in zip(out, rows):
                s["snippet"] = snippets.get(row.id)
        return out


async def _first_matching_text(
    session, session_ids: list[UUID], like: str
) -> dict[UUID, str]:
    """Return the earliest message text containing ``like`` per session (capped at 500)."""
    if not session_ids:
        return {}
    rows = (
        await session.execute(
            select(MessageModel.session_id, MessageModel.text)
            .where(MessageModel.session_id.in_(session_ids), MessageModel.text.ilike(like))
            .order_by(MessageModel.session_id, MessageModel.created_at)
        )
    ).all()
    seen: set = set()
    out: dict[UUID, str] = {}
    for sid, text in rows:
        if sid not in seen:
            seen.add(sid)
            out[sid] = text[:500]
    return out


async def load_session_detail(session_factory, session_id: UUID) -> dict:
    """Return a session's title + messages (with ids) + compaction checkpoint for resume.

    Messages carry their ``id`` so the client can delete a single message, plus the
    per-message ``imported_rag`` flag so the "✓ Imported" button state comes straight from
    the row instead of a separate coverage query. A user message that created a chat
    attachment carries ``attach`` (``{asset_id, name, mime}``) so the client can render the
    image/file inline in the bubble. ``compaction`` is the durable checkpoint inlined for
    Live-State RECOVERY (a client that lost its in-memory state rebuilds ``[summary] +
    [tail]`` from here and returns to zero-read turns) — recovery is the only reason a
    normal path touches this. The shape of :func:`load_session_messages` (used by the
    agent kernel) is intentionally untouched.
    """
    async with session_factory() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        rows = (
            await session.execute(
                select(MessageModel, AssetModel)
                .outerjoin(AssetModel, AssetModel.id == MessageModel.attach_asset_id)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at, MessageModel.id)
            )
        ).all()
        return {
            "title": sess.title if sess is not None else None,
            "compaction": sess.compaction if sess is not None else None,
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role,
                    "content": m.text,
                    "imported_rag": m.imported_rag,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "attach": (
                        {
                            "asset_id": str(asset.id),
                            "name": asset.name,
                            "mime": asset.mime_type,
                        }
                        if asset is not None
                        else None
                    ),
                    # Retrieval-feedback snapshot (assistant messages that ran rag_search):
                    # {hits, queries} so the client re-renders the 👍/👎 row after reopen.
                    "retrieval": (m.meta or {}).get("retrieval") if m.meta else None,
                }
                for m, asset in rows
            ],
        }


async def reconcile_messages(
    session_factory, user_id: UUID, session_id: UUID, tail: list[dict]
) -> dict:
    """Align SQL to the client's authoritative tail (RECONNECT/RECOVERY ONLY).

    Compensation for failed async Edit/Delete syncs and for writes that never landed
    while the client was offline. For rows after the checkpoint boundary:
    client-missing → delete; SQL-missing → insert (``embedding`` NULL, backfilled by
    finalize); text mismatch → rewrite the client's text and clear ``embedding`` for
    re-embed. Ids are validated for OWNERSHIP here (this path reads SQL anyway).
    Returns ``{deleted, inserted, updated}`` counts.
    """
    async with session_factory() as session:
        boundary = None
        raw_checkpoint = (
            await session.execute(
                select(SessionModel.compaction).where(SessionModel.id == session_id)
            )
        ).scalar_one_or_none()
        if raw_checkpoint and raw_checkpoint.get("through_message_id"):
            boundary = (
                await session.execute(
                    select(MessageModel)
                    .where(
                        MessageModel.id == UUID(str(raw_checkpoint["through_message_id"])),
                        MessageModel.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
        stmt = (
            select(MessageModel)
            .where(
                MessageModel.session_id == session_id,
                MessageModel.user_id == user_id,  # ownership predicate: foreign ids cannot align
                MessageModel.role.in_(("user", "assistant")),
            )
            .order_by(MessageModel.created_at, MessageModel.id)
        )
        if boundary is not None:
            stmt = stmt.where(
                tuple_(MessageModel.created_at, MessageModel.id)
                > tuple_(boundary.created_at, boundary.id)
            )
        existing = (await session.execute(stmt)).scalars().all()
        by_id = {m.id: m for m in existing}

        client_ids: set[UUID] = set()
        deleted = inserted = updated = 0
        for item in tail:
            raw = item.get("message_id")
            if not raw:
                # Unpersisted client row → insert it (client is authoritative).
                session.add(
                    MessageModel(
                        user_id=user_id,
                        session_id=session_id,
                        role=item["role"],
                        text=item.get("content") or "",
                    )
                )
                inserted += 1
                continue
            mid = _safe_uuid(raw)
            if mid is None or mid not in by_id:
                # SQL has no such row (write lost) or it belongs to another session —
                # insert under a fresh row; the client id is NOT honored cross-session.
                session.add(
                    MessageModel(
                        user_id=user_id,
                        session_id=session_id,
                        role=item["role"],
                        text=item.get("content") or "",
                    )
                )
                inserted += 1
                continue
            client_ids.add(mid)
            row = by_id[mid]
            if (row.text or "") != (item.get("content") or ""):
                row.text = item.get("content") or ""
                row.embedding = None  # re-embed on next finalize
                updated += 1
        for row in existing:
            if row.id not in client_ids:
                await session.delete(row)
                deleted += 1
        await session.commit()
    return {"deleted": deleted, "inserted": inserted, "updated": updated}


async def ensure_user(session_factory, user_id: UUID | None = None) -> UUID:
    """Return ``user_id`` (creating it if absent), or create an anonymous default user."""
    async with session_factory() as session:
        if user_id is not None:
            row = (
                await session.execute(select(UserModel).where(UserModel.id == user_id))
            ).scalar_one_or_none()
            if row is None:
                session.add(UserModel(id=user_id))
                await session.commit()
            return user_id
        row = UserModel()
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def create_session(
    session_factory, user_id: UUID, title: str | None = None, type: int = 0
) -> UUID:
    """Create a session row and return its id.

    ``type``: 0 = ordinary chat (default), 1 = research task session (hidden from the chat
    sidebar, deleted together with its task).

    A fresh session is titled after the first user message (ChatGPT/Gemini style) so the
    sidebar shows a readable name immediately instead of the raw id while the deferred
    finalize job is still running.
    """
    async with session_factory() as session:
        row = SessionModel(user_id=user_id, type=type)
        if title:
            row.title = " ".join(title.split())[:40]
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def set_session_type(session_factory, session_id: UUID, type: int) -> None:
    """Mark an existing session's type (used when a chat turn binds it to a research task)."""
    async with session_factory() as session:
        row = await session.get(SessionModel, session_id)
        if row is not None and row.type != type:
            row.type = type
            await session.commit()
