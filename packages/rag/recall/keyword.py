"""Keyword recall: tsvector full-text search (websearch_to_tsquery + ts_rank).

Two configs drive the same query:
- English / any non-CJK text: ``to_tsvector('english', content_en)`` (unchanged path).
- Query containing CJK: ``to_tsvector('simple', content_search)`` over jieba-segmented
  text, so Chinese queries match the tokenized index.

Recall always targets leaf chunks (parent chunks are context for ``parent_expand``) and
honors the tenant-isolation predicate plus an optional ``domain_id`` filter.
"""
from core.infrastructure.visibility import asset_visibility_sql
from sqlalchemy import text

from rag.cjk import contains_cjk, segment
from rag.recall.base import Recaller
from rag.types import SearchHit


def _ts_match(query: str) -> tuple[str, str, dict]:
    """Return ``(match_predicate, score_expr, params)`` for the query's language path."""
    if contains_cjk(query):
        seg = segment(query)
        params = {"segq": seg}
        match = (
            "to_tsvector('simple', COALESCE(c.content_search, '')) "
            "@@ plainto_tsquery('simple', :segq)"
        )
        score = (
            "ts_rank(to_tsvector('simple', COALESCE(c.content_search, '')), "
            "plainto_tsquery('simple', :segq))"
        )
        return match, score, params
    params = {"q": query}
    match = "to_tsvector('english', c.content_en) @@ websearch_to_tsquery('english', :q)"
    score = (
        "ts_rank(to_tsvector('english', c.content_en), "
        "websearch_to_tsquery('english', :q))"
    )
    return match, score, params


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
        match, score, match_params = _ts_match(query)
        params: dict = {**match_params, "limit": top_k}
        where = match

        # Tenant isolation: without a ``user_id`` filter the recall returns every asset's
        # chunks (admin/global mode). With ``user_id`` present (possibly None for a guest)
        # apply the owner / workspace / ACL visibility predicate, scoped to READY assets.
        # The LEFT JOIN + `(c.asset_id IS NULL OR a.file_status='READY')` lets non-file
        # chunks (learning / chat, ``asset_id`` NULL) survive; the owner/workspace/ACL
        # predicate in ``asset_visibility_sql`` still matches them by ``c.user_id``.
        if filters and "user_id" in filters:
            params["uid"] = filters["user_id"]
            where += (
                " AND (c.asset_id IS NULL OR a.file_status = 'READY')"
                f" AND ({asset_visibility_sql(filters['user_id'], 'c')})"
            )
        # Optional domain scoping (P1): ``AND assets.domain_id = :domain_id``, file-only
        # (non-file chunks carry no domain column; their owner scoping already applied).
        if filters and filters.get("domain_id"):
            params["domain_id"] = filters["domain_id"]
            where += " AND (c.asset_id IS NOT NULL AND a.domain_id = :domain_id::uuid)"
        # Parent chunks are context only; recall surfaces leaves (parent_expand widens).
        where += " AND c.chunk_kind = 'leaf'"

        sql = f"""
            SELECT c.id, c.content_en, c.meta, {score} AS score
            FROM chunks c
            LEFT JOIN assets a ON a.id = c.asset_id
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
