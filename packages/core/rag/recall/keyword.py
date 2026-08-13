"""Keyword recall: tsvector full-text search (websearch_to_tsquery + ts_rank)."""
from sqlalchemy import text

from core.rag.recall.base import Recaller
from core.rag.types import SearchHit


class KeywordRecaller(Recaller):
    name = "keyword"

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def recall(
        self,
        query: str,
        query_embedding: list[float] | None,
        top_k: int,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        where = "to_tsvector('english', content_en) @@ websearch_to_tsquery('english', :q)"
        params: dict = {"q": query, "limit": top_k}
        if filters and filters.get("material_id"):
            where += " AND material_id = :material_id"
            params["material_id"] = filters["material_id"]

        sql = f"""
            SELECT id, content_en, meta,
                   ts_rank(to_tsvector('english', content_en),
                           websearch_to_tsquery('english', :q)) AS score
            FROM chunks
            WHERE {where}
            ORDER BY score DESC
            LIMIT :limit
        """
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).all()

        return [
            SearchHit(
                id=str(r.id),
                text=r.content_en,
                score=float(r.score),
                meta=r.meta,
                source=self.name,
            )
            for r in rows
        ]
