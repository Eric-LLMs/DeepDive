"""Semantic recall: pgvector cosine similarity."""
from core.ports.vector import VectorStorePort

from rag.recall.base import Recaller
from rag.types import SearchHit


class VectorRecaller(Recaller):
    name = "vector"

    def __init__(self, vector_store: VectorStorePort) -> None:
        self.vector_store = vector_store

    async def recall(
        self,
        query: str,
        query_embedding: list[float] | None,
        top_k: int,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        if query_embedding is None:
            return []
        hits = await self.vector_store.search(query_embedding, top_k=top_k, filters=filters)
        return [
            SearchHit(
                id=h["id"],
                text=h["text"],
                score=h["score"],
                meta=h.get("meta"),
                source=self.name,
            )
            for h in hits
        ]
