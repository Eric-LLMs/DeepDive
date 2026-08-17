"""PostgreSQL-backed memory recall channels: tsvector keyword + pgvector semantic.

The keyword channel uses ``to_tsvector('english', text)`` full-text search — a pure SQL
expression over the existing ``messages`` table, so it works with no embedding service at
all. The vector channel uses pgvector cosine distance over ``messages.embedding``. The
agent kernel fuses the two via :class:`agent.memory.retrieval.RRFMemoryRetriever` and
degrades to tsvector-only when the embedding service is offline (never a silent empty).
"""
from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from agent.memory.retrieval import MemoryHit, VectorRetriever
from sqlalchemy import func, select

from core.infrastructure.db import MessageModel
from core.infrastructure.vector import TEIEmbedder

_FTS_CONFIG = "english"


def _tsvector():
    return func.to_tsvector(_FTS_CONFIG, MessageModel.text)


class PgKeywordRecaller:
    """tsvector full-text keyword recall over a user's messages (deterministic, no vectors)."""

    def __init__(
        self,
        session_factory: Callable,
        user_id: UUID | None = None,
        fts_config: str = _FTS_CONFIG,
    ) -> None:
        self._session_factory = session_factory
        self._user_id = user_id
        self._fts_config = fts_config

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        if not query.strip():
            return []
        tsquery = func.plainto_tsquery(self._fts_config, query)
        rank = func.ts_rank_cd(_tsvector(), tsquery).label("score")
        stmt = (
            select(MessageModel, rank)
            .where(_tsvector().op("@@") (tsquery))
            .order_by(rank.desc())
            .limit(top_k)
        )
        if self._user_id is not None:
            stmt = stmt.where(MessageModel.user_id == self._user_id)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [
            MemoryHit(
                key=str(message.id),
                content=message.text,
                score=float(score),
                source="keyword",
                meta={"role": message.role, "session_id": str(message.session_id)},
            )
            for message, score in rows
        ]


class PgVectorRecaller(VectorRetriever):
    """pgvector cosine recall over a user's message embeddings."""

    def __init__(
        self,
        session_factory: Callable,
        embedder: TEIEmbedder,
        user_id: UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._user_id = user_id

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        # Embedding service down → raise so RRFMemoryRetriever falls back to tsvector.
        query_embedding = (await self._embedder.embed([query]))[0]
        score = (1 - MessageModel.embedding.cosine_distance(query_embedding)).label("score")
        stmt = (
            select(MessageModel, score)
            .where(MessageModel.embedding.is_not(None))
            .order_by(MessageModel.embedding.cosine_distance(query_embedding))
            .limit(top_k)
        )
        if self._user_id is not None:
            stmt = stmt.where(MessageModel.user_id == self._user_id)
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return [
            MemoryHit(
                key=str(message.id),
                content=message.text,
                score=float(score),
                source="vector",
                meta={"role": message.role, "session_id": str(message.session_id)},
            )
            for message, score in rows
        ]
