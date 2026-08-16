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

import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

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
            summary = await llm.complete(
                "Summarize this conversation into a concise paragraph capturing the user's "
                "goals and key points.\n\n" + transcript,
                "You are a memory summarizer. Output only the summary.",
            )

        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is not None:
            sess.closed_at = datetime.now(timezone.utc)
            sess.summary = summary
        await session.commit()
        return {"embedded": embedded, "summary": summary}


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


async def list_sessions(session_factory, user_id: UUID) -> list[dict]:
    """Return a user's sessions (newest first) as ``[{id, created_at, summary}]``."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SessionModel)
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.created_at.desc())
            )
        ).scalars().all()
        return [
            {
                "id": str(s.id),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "summary": s.summary,
            }
            for s in rows
        ]


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


async def create_session(session_factory, user_id: UUID) -> UUID:
    """Create a session row and return its id."""
    async with session_factory() as session:
        row = SessionModel(user_id=user_id)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id
