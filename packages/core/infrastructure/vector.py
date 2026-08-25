"""Embedding via the TEI (Text Embeddings Inference) service + pgvector vector store."""
import asyncio
import random
import uuid
from uuid import UUID

import httpx
from sqlalchemy import and_, or_, select

from core.config import settings
from core.infrastructure.db import AssetModel, ChunkModel
from core.infrastructure.visibility import chunk_visible_expr


class TEIEmbedder:
    """Client for a TEI service serving BGE-M3 (``POST /embed``).

    The embedding model runs in a separate container; this client only POSTs texts and
    returns vectors, so the API never loads the (heavy) model and model updates don't
    require an API restart.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 5.0) -> None:
        self.base_url = base_url or settings.embedding_base_url
        # Local TEI container answers in well under a second for a short query; fail fast
        # instead of stalling a chat turn when the embedding service is down (503/hang).
        # Batch embed (session finalize, sentence indexing) runs in the worker and passes a
        # longer timeout — a batch of long messages can exceed the fast-fail budget.
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Worker concurrency (10) × concurrent ingest batches can exceed TEI's
        # --max-concurrent-requests, so 429 (and transient 5xx) is a normal "slow down"
        # condition, not an outage — retry with exponential backoff. Connection errors still
        # fail fast so a genuinely dead embedding service stalls nothing.
        for attempt in range(4):
            try:
                resp = await self._client.post(
                    "/embed", json={"inputs": texts, "normalize": True}
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status != 429 and not (500 <= status < 600):
                    raise
                if attempt == 3:
                    raise
                await asyncio.sleep(0.5 * (2**attempt) + random.uniform(0, 0.2))


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
                    asset_id=uuid.UUID(m["asset_id"]),
                    user_id=uuid.UUID(m["user_id"]) if m.get("user_id") else None,
                    workspace_id=uuid.UUID(m["workspace_id"]) if m.get("workspace_id") else None,
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
                # Parent chunks are context only; recall surfaces leaves (parent_expand
                # widens a hit to its parent's text).
                .where(ChunkModel.chunk_kind == "leaf")
                .order_by(ChunkModel.embedding.cosine_distance(query_embedding))
                .limit(top_k)
            )
            # Tenant isolation: same predicate as keyword recall — owner / workspace / ACL
            # over READY assets. ``user_id`` None (guest) resolves to public-link assets.
            # ``domain_id`` (P1) narrows to one domain; both join assets.
            #
            # LEFT JOIN so non-file chunks (learning / chat, ``asset_id`` NULL) survive the
            # join; the READY / domain predicates apply only to file chunks while the
            # owner/workspace/ACL visibility (via ``chunk_visible_expr``, which checks
            # ``chunks.user_id`` directly) keeps working for both — a non-file chunk is
            # visible to its ``user_id`` owner.
            if filters and ("user_id" in filters or filters.get("domain_id")):
                stmt = stmt.outerjoin(AssetModel, AssetModel.id == ChunkModel.asset_id)
                if "user_id" in filters:
                    stmt = stmt.where(
                        or_(
                            ChunkModel.asset_id.is_(None),
                            AssetModel.file_status == "READY",
                        ),
                        chunk_visible_expr(filters["user_id"]),
                    )
                if filters.get("domain_id"):
                    stmt = stmt.where(
                        and_(
                            ChunkModel.asset_id.is_not(None),
                            AssetModel.domain_id == UUID(str(filters["domain_id"])),
                        )
                    )
            rows = (await session.execute(stmt)).all()
            return [
                {"id": str(r.id), "text": r.content_en, "score": float(r.score), "meta": r.meta}
                for r in rows
            ]
