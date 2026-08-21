"""Keyword recall: tsvector full-text search (websearch_to_tsquery + ts_rank)."""
from sqlalchemy import text

from core.infrastructure.visibility import asset_visibility_sql
from rag.recall.base import Recaller
from rag.types import SearchHit


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
        params: dict = {"q": query, "limit": top_k}
        where = "to_tsvector('english', c.content_en) @@ websearch_to_tsquery('english', :q)"
        # Tenant isolation: without a ``user_id`` filter the recall returns every asset's
        # chunks (admin/global mode). With ``user_id`` present (possibly None for a guest)
        # apply the owner / workspace / ACL visibility predicate, scoped to READY assets.
        if filters and "user_id" in filters:
            params["uid"] = filters["user_id"]
            where += (
                " AND a.file_status = 'READY'"
                f" AND ({asset_visibility_sql(filters['user_id'], 'c')})"
            )

        sql = f"""
            SELECT c.id, c.content_en, c.meta,
                   ts_rank(to_tsvector('english', c.content_en),
                           websearch_to_tsquery('english', :q)) AS score
            FROM chunks c
            JOIN assets a ON a.id = c.asset_id
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
