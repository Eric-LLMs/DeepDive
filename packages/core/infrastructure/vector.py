"""Embedding implementation (BGE-M3, based on sentence-transformers) + pgvector vector store."""
import json
import uuid

from sqlalchemy import select

from core.config import settings
from core.infrastructure.db import ChunkModel


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.embedding_model
        self._model = None

    def _load(self):
        # Lazy import + load: sentence-transformers pulls in torch (~2GB); only load when embedding
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # sentence-transformers is synchronous; in a real service it should run in a thread pool (anyio.to_thread)
        return self._load().encode(texts, normalize_embeddings=True).tolist()


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
