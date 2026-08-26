"""RAG pipeline console (admin): config editor, test/trace, chunk preview, golden-set
eval, reindex, and query-repository management. Plus the public ``/rag/feedback`` collector.
"""
from __future__ import annotations

from uuid import UUID

from api.auth import AuthAdmin, AuthUser, require_admin, require_user
from api.deps import _embedder, _retriever, get_task_queue, llm
from api.schemas import (
    RagChunkPreviewRequest,
    RagConfigUpdateRequest,
    RagEvalRequest,
    RagFeedbackRequest,
    RagTestRequest,
)
from core.config import settings
from core.infrastructure.db import AssetModel, ChunkModel, RagFeedbackModel, SessionLocal, UserModel
from fastapi import APIRouter, Depends, HTTPException, Request
from rag.query_cache import bump_corpus_version
from sqlalchemy import select

router = APIRouter(tags=["rag-admin"])


def _rag_pipeline():
    """Build the RAG pipeline from the currently stored config + the env-seeded deps."""
    from core.infrastructure.vector import PgVectorStore
    from rag import build_pipeline
    from rag.config_store import current_config

    return build_pipeline(
        embedder=_embedder(),
        vector_store=PgVectorStore(SessionLocal),
        session_factory=SessionLocal,
        llm=llm,
        settings=settings,
        config=current_config(),
    )


@router.get("/admin/rag/config")
async def get_rag_config(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Read the stored pipeline config (or the env-seeded defaults)."""
    from rag.config_store import load_config
    from rag.pipeline.registry import registry

    cfg = await load_config(SessionLocal)
    return {"config": cfg.to_dict(), "available_nodes": registry.metadata()}


@router.post("/admin/rag/config")
async def update_rag_config(
    body: RagConfigUpdateRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Validate + persist a full pipeline config blob; invalidates the retriever cache."""
    from rag.config_store import RagPipelineConfig, save_config

    cfg = RagPipelineConfig.from_dict(body.config or {})
    errors = await save_config(SessionLocal, cfg)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    _retriever.cache_clear()
    return {"ok": True}


@router.post("/admin/rag/test")
async def rag_test(body: RagTestRequest, admin: AuthAdmin = Depends(require_admin)) -> dict:
    """Run the configured pipeline and return hits + the per-node trace."""
    from rag import RetrievalUnavailable

    # The admin console never sets the per-request user contextvar (only /chat does), so
    # get_request_user_id() would be None here — and recallers treat user_id=None as a
    # tenant that sees nothing. Scope the test to the admin account's own user row (the
    # account the admin imports content under) so isolation matches the product; a
    # token-only admin with no matching user falls back to an unfiltered global view.
    async with SessionLocal() as session:
        admin_user = (
            await session.execute(select(UserModel).where(UserModel.username == admin.username))
        ).scalar_one_or_none()
    filters: dict = {}
    if admin_user is not None:
        filters["user_id"] = admin_user.id
    if body.domain_id:
        filters["domain_id"] = body.domain_id

    pipe = _rag_pipeline()
    try:
        result = await pipe.trace(body.query or "", body.top_k, filters)
    except RetrievalUnavailable as exc:
        return {"ok": False, "error": str(exc), "trace": [], "hits": []}
    return {
        "ok": True,
        "hits": result["hits"],
        "trace": [
            {"name": t.name, "status": t.status, "ms": t.ms, "out": t.out}
            for t in result["trace"]
        ],
        "errors": result["errors"],
    }


@router.post("/admin/rag/chunk-preview")
async def rag_chunk_preview(
    body: RagChunkPreviewRequest, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Preview chunking + (optionally) CJK segmentation / context prefixes, no DB writes."""
    from core.infrastructure.ingest import Chunk, contextualize_chunks, split_chunks
    from rag.query.cjk import segment

    texts = split_chunks(
        body.text, body.chunk_chars, body.overlap, body.strategy
    )
    chunks = [Chunk(content_en=t) for t in texts]
    if body.cjk:
        for c in chunks:
            c.content_search = segment(c.content_en)
    if body.contextual:
        chunks = await contextualize_chunks(chunks, "preview", llm)
    return {
        "chunks": [
            {
                "text": c.content_en,
                "search": c.content_search,
                "context": (c.meta or {}).get("context"),
            }
            for c in chunks
        ]
    }


@router.post("/admin/rag/eval")
async def rag_eval(body: RagEvalRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Run the golden-set regression and return the metric table."""
    from rag.eval import run_golden_set

    report = await run_golden_set(_rag_pipeline, body.golden_path)
    return report.to_dict()


@router.post("/admin/rag/reindex")
async def rag_reindex(
    request: Request, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Re-ingest every READY asset under the current chunking/enrichment config."""
    async with SessionLocal() as session:
        ready = (
            await session.execute(
                select(AssetModel).where(
                    AssetModel.file_status == "READY",
                    AssetModel.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    task_queue = get_task_queue(request)
    for asset in ready:
        await task_queue.enqueue("asset_ingest", {"asset_id": str(asset.id)})
    # Reindex changes the whole corpus: bump the version so the Redis query cache stops
    # serving pre-reindex hits immediately (the key embeds the corpus version).
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        await bump_corpus_version(redis)
    return {"queued": len(ready)}


@router.get("/admin/rag/repository")
async def rag_repository(_: AuthAdmin = Depends(require_admin)) -> dict:
    """List non-file query-repository chunks (learning / chat sources) for the admin console."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ChunkModel)
                .where(ChunkModel.source_type != "file")
                .order_by(ChunkModel.id)
                .limit(500)
            )
        ).scalars().all()
    return {
        "items": [
            {
                "id": str(r.id),
                "source_type": r.source_type,
                "source_id": r.source_id,
                "title": (r.meta or {}).get("title") or "",
                "user_id": str(r.user_id) if r.user_id else None,
                "created_at": None,
            }
            for r in rows
        ]
    }


@router.delete("/admin/rag/repository/{chunk_id}")
async def rag_repository_delete(
    chunk_id: UUID, _: AuthAdmin = Depends(require_admin)
) -> dict:
    """Delete one non-file query-repository chunk."""
    async with SessionLocal() as session:
        row = await session.get(ChunkModel, chunk_id)
        if row is None or row.source_type == "file":
            raise HTTPException(status_code=404, detail="chunk not found")
        await session.delete(row)
        await session.commit()
    return {"status": "ok"}


@router.post("/rag/feedback")
async def rag_feedback(
    body: RagFeedbackRequest, user: AuthUser = Depends(require_user)
) -> dict:
    """Persist 👍/👎 retrieval feedback (query → chunks → rating → reason).

    Each row snapshots the query, the retrieved hits (ids + scores), and the rating so the
    corpus becomes a golden dataset for future fine-tuning / eval without re-running
    retrieval.
    """
    hits = [
        {str(k): v for k, v in (h or {}).items() if k in ("id", "score", "text")}
        for h in body.hits
    ]
    async with SessionLocal() as session:
        session.add(
            RagFeedbackModel(
                user_id=user.user_id,
                query=body.query,
                rating=body.rating,
                reason=body.reason or None,
                hits=hits,
                filters={"user_id": str(user.user_id)},
            )
        )
        await session.commit()
    return {"ok": True, "recorded": len(hits)}
