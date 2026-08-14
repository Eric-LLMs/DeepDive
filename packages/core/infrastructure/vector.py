"""Embedding via the TEI (Text Embeddings Inference) service + pgvector vector store."""
import uuid

import httpx
from sqlalchemy import select

from core.config import settings
from core.infrastructure.db import ChunkModel


class TEIEmbedder:
    """Client for a TEI service serving BGE-M3 (``POST /embed``).

    The embedding model runs in a separate container; this client only POSTs texts and
    returns vectors, so the API never loads the (heavy) model and model updates don't
    require an API restart.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url or settings.embedding_base_url
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post("/embed", json={"inputs": texts, "normalize": True})
        resp.raise_for_status()
        return resp.json()


class PgVectorStore:
    """pgvector vector store implementation (semantic retrieval)."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def upsert(
        self, ids: list[str], texts: list[str], embeddings: list[list[float]], meta: list[dict]
    ) -> None:
        async with self.session_factory() as session:
            for id_, text, emb, m in zip(ids, texts, embeddings, meta):
                obj = ChunkModel(
                    id=uuid.UUID(id_),
                    material_id=uuid.UUID(m["material_id"]),
                    seq=m.get("seq", 0),
                    content_en=text,
                    content_cn=m.get("content_cn"),
                    meta=m,
                    embedding=emb,
                )
                await session.merge(obj)  # upsert by primary key
            await session.commit()

    async def search(
        self, query_embedding: list[float], top_k: int = 5, filters: dict | None = None
    ) -> list[dict]:
        async with self.session_factory() as session:
            stmt = (
                select(
                    ChunkModel.id,
                    ChunkModel.content_en,
                    ChunkModel.meta,
                    (1 - ChunkModel.embedding.cosine_distance(query_embedding)).label("score"),
                )
                .where(ChunkModel.embedding.is_not(None))
                .order_by(ChunkModel.embedding.cosine_distance(query_embedding))
                .limit(top_k)
            )
            if filters and filters.get("material_id"):
                stmt = stmt.where(ChunkModel.material_id == filters["material_id"])
            rows = (await session.execute(stmt)).all()
            return [
                {"id": str(r.id), "text": r.content_en, "score": float(r.score), "meta": r.meta}
                for r in rows
            ]
