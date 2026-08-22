"""Session memory: persist chat messages + session events to PostgreSQL.

Layered memory (hot path vs subconscious consolidation):

- ``append_message`` writes message text synchronously during the session (cheap hot path);
- ``record_event`` buffers session events in memory;
- ``close`` flushes events (the only synchronous work at session end);
- ``finalize_session`` batch-embeds messages (backfilling ``embedding``) and generates a
  session summary — deferred to the ``session_finalize`` worker job.

Retrieval is vector recall over a user's messages (cross-session) via pgvector cosine distance;
long-term file memory (memdir) remains a separate layer (see ``agent.memory``).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)

from core.config import settings
from core.infrastructure.db import MessageModel, SessionEventModel, SessionModel, UserModel
from core.infrastructure.vector import TEIEmbedder


class SessionMemoryStore:
    def __init__(
        self,
        session_factory,
        embedder: TEIEmbedder,
        llm,
        session_id: UUID,
        user_id: UUID,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder
        self.llm = llm
        self.session_id = session_id
        self.user_id = user_id
        self._events: list[tuple[str, dict]] = []

    def record_event(self, type_: str, payload: dict) -> None:
        """Buffer a session event (flushed on :meth:`close`)."""
        self._events.append((type_, payload or {}))

    async def append_message(self, role: str, text: str) -> None:
        """Persist one message (text only; embedding is backfilled on close)."""
        async with self.session_factory() as session:
            session.add(
                MessageModel(
                    user_id=self.user_id, session_id=self.session_id, role=role, text=text
                )
            )
            await session.commit()

    async def close(self) -> None:
        """Flush buffered events (the only synchronous work at session end).

        The expensive consolidation (batch embedding + summary) is deferred to the
        ``session_finalize`` worker job via :func:`finalize_session`.
        """
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

    async def load_messages(self) -> list[dict]:
        """Rebuild the message history for resume."""
        return await load_session_messages(self.session_factory, self.session_id)


async def finalize_session(session_factory, embedder, llm, session_id: UUID) -> dict:
    """Batch-embed messages + generate a summary for a closed session.

    Called by the worker's ``session_finalize`` job; the gateway only flushes events
    synchronously (:meth:`SessionMemoryStore.close`). Writes message embeddings, session
    summary, and ``closed_at`` back to PostgreSQL.
    """
    async with session_factory() as session:
        messages = (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).scalars().all()

        embedded = 0
        to_embed = [m for m in messages if m.embedding is None]
        if to_embed:
            embeddings = await embedder.embed([m.text for m in to_embed])
            for message, embedding in zip(to_embed, embeddings):
                message.embedding = embedding
            embedded = len(to_embed)

        summary = None
        if settings.session_summary_enabled and messages:
            transcript = "\n".join(f"{m.role}: {m.text}" for m in messages)
            try:
                summary = await _summarize_transcript(llm, transcript)
            except Exception:  # noqa: BLE001 - summary is cosmetic; finalize must still finish
                logger.warning("session summary failed (finalize continues): session %s", session_id)
                summary = None

        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is not None:
            sess.closed_at = datetime.now(timezone.utc)
            sess.summary = summary
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
    """LLM-compress a raw transcript into a concise summary paragraph.

    Shared by the deferred ``session_finalize`` job and the synchronous compaction boundary
    (``compact_history``), so both summarize long conversations with the same prompt.
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


async def get_session_summary(session_factory, session_id: UUID) -> str | None:
    """Return a session's existing summary, or ``None`` when none has been generated."""
    async with session_factory() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        return sess.summary if sess is not None else None


async def compact_history(
    history: list[dict],
    *,
    session_factory,
    session_id: UUID,
    session_memory: Any,
    llm,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[dict]:
    """Bound the agent's history window: truncate long histories, compress the overflow.

    When ``history`` exceeds ``settings.history_max_messages`` messages, keep only the most
    recent ``settings.history_keep_messages`` and fold the dropped prefix into a conversation
    summary — the existing session summary when one is present, otherwise a synchronous LLM
    summary of the overflow (same prompt as ``finalize_session``). The summary is injected as
    a leading system message so the agent keeps cross-turn context while the token window
    stays flat. A ``compaction`` event is recorded on ``session_memory`` for auditability.

    If the summary call fails, the overflow is still dropped (deterministic degradation: a
    bounded window is preferable to an unbounded request the provider rejects).
    """
    if len(history) <= settings.history_max_messages:
        return history
    overflow = history[: len(history) - settings.history_keep_messages]
    tail = history[len(history) - settings.history_keep_messages :]

    summary = await get_session_summary(session_factory, session_id)
    if not summary:
        transcript = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in overflow if m.get("content")
        )
        try:
            summary = await _summarize_transcript(
                llm, transcript, model=model, base_url=base_url, api_key=api_key
            )
        except Exception:  # noqa: BLE001 - compaction must not break the chat request
            summary = None

    session_memory.record_event(
        "compaction", {"dropped": len(overflow), "had_summary": bool(summary)}
    )
    if summary:
        return [{"role": "system", "content": f"## Conversation summary\n{summary}"}] + tail
    return tail


async def load_session_messages(session_factory, session_id: UUID) -> list[dict]:
    """Return a session's messages as ``[{"role", "content"}]`` for resume."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).scalars().all()
        return [{"role": m.role, "content": m.text} for m in rows]


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
    """Return a session's title + messages (with ids) for resume.

    Messages carry their ``id`` so the client can delete a single message; the shape of
    :func:`load_session_messages` (used by the agent kernel) is intentionally untouched.
    """
    async with session_factory() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        rows = (
            await session.execute(
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).scalars().all()
        return {
            "title": sess.title if sess is not None else None,
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role,
                    "content": m.text,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ],
        }


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


async def create_session(session_factory, user_id: UUID, title: str | None = None) -> UUID:
    """Create a session row and return its id.

    A fresh session is titled after the first user message (ChatGPT/Gemini style) so the
    sidebar shows a readable name immediately instead of the raw id while the deferred
    finalize job is still running.
    """
    async with session_factory() as session:
        row = SessionModel(user_id=user_id)
        if title:
            row.title = " ".join(title.split())[:40]
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id
