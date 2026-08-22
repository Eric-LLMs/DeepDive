"""Worker task functions.

Each function is registered by name in ``WorkerSettings.functions`` and mirrors the
enrichment work the gateway used to do in-process. arq calls every task as
``task(ctx, job_id, payload)``; the job row in PostgreSQL is the source of truth, so each
task drives its own status transitions via :class:`JobStore`.
"""
import asyncio
import time
from pathlib import Path
from uuid import UUID

from core.application.drive_service import READY
from core.config import settings
from core.infrastructure import media
from core.infrastructure.db import SessionEventModel
from core.infrastructure.drive_repositories import (
    SqlAssetRepository,
    SqlChunkRepository,
)
from core.infrastructure.ingest import extract_text, split_chunks
from core.infrastructure.jobs import JobStore
from core.infrastructure.memory import finalize_session
from core.infrastructure.repositories import SqlSentenceRepository
from core.infrastructure.storage import get_storage, object_key


async def _run(ctx, job_id: str, work) -> dict:
    """Mark the job running, execute ``work``, and record the terminal state."""
    store: JobStore = ctx["job_store"]
    uid = UUID(job_id)
    await store.mark_running(uid)
    try:
        result = await work
    except Exception as exc:  # noqa: BLE001 - record failure then re-raise for arq retries/logging
        await store.mark_failed(uid, str(exc))
        raise
    await store.mark_succeeded(uid, result)
    return result


async def tts(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        path = await ctx["tts"].synthesize(payload["text"])
        if not path:
            raise RuntimeError("TTS synthesis failed")
        return {"url": "/audio/" + Path(path).name}

    return await _run(ctx, job_id, work())


async def image_fetch(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        paths = await ctx["images"].fetch(
            payload.get("word", ""),
            payload.get("definition", ""),
            payload.get("context", ""),
            payload.get("regenerate", False),
        )
        return {"image_paths": paths}

    return await _run(ctx, job_id, work())


async def explain(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        return await ctx["llm"].explain_term(payload["term"], payload.get("context", ""))

    return await _run(ctx, job_id, work())


async def generate_definition(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        definition = await ctx["llm"].generate_definition(payload["term"])
        return {"definition": definition}

    return await _run(ctx, job_id, work())


async def analyze_syntax(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        analysis = await ctx["llm"].analyze_syntax(payload["sentence"])
        return {"analysis": analysis}

    return await _run(ctx, job_id, work())


async def index_sentences(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        domain_id = UUID(payload["domain_id"])
        async with ctx["session_factory"]() as session:
            repo = SqlSentenceRepository(session)
            sentences = await repo.list_by_domain(domain_id)
            if not sentences:
                return {"indexed": 0}
            embeddings = await ctx["embedder"].embed([s.content_en for s in sentences])
            for sentence, embedding in zip(sentences, embeddings):
                await repo.set_embedding(sentence.id, embedding)
            return {"indexed": len(sentences)}

    return await _run(ctx, job_id, work())


async def session_finalize(ctx, job_id: str, payload: dict) -> dict:
    async def work() -> dict:
        session_id = UUID(payload["session_id"])
        return await finalize_session(
            ctx["session_factory"], ctx["embedder"], ctx["llm"], session_id
        )

    return await _run(ctx, job_id, work())


async def prune_session_events(ctx) -> dict:
    """Daily retention: delete session_events older than the retention window.

    arq cron jobs run as ``(ctx)`` (no job_id/payload) and are not user-facing, so no
    ``jobs`` row is created. Only the audit log is swept; messages (the recall corpus) and
    sessions (summaries) are deliberately kept.
    """
    cutoff = time.time() - settings.session_events_retention_days * 86400
    async with ctx["session_factory"]() as session:
        result = await session.execute(
            SessionEventModel.__table__.delete().where(SessionEventModel.timestamp < cutoff)
        )
        await session.commit()
        return {"deleted": result.rowcount}


def _build_media(payload: dict) -> dict:
    """Blocking body of generate_media: subtitle parse → keyframes → PPT/PDF."""
    video_path = payload["video_path"]
    subtitle_path = payload.get("subtitle_path")
    fmt = payload.get("format", "pptx")
    title = payload.get("title") or Path(video_path).stem

    out_dir = Path(settings.media_output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cues = media.parse_subtitles(subtitle_path) if subtitle_path else []
    timestamps = [c.start_ms for c in cues]
    frames = media.extract_keyframes(video_path, timestamps, out_dir / "frames")

    slides = [(frame, cues[i].text if i < len(cues) else "") for i, frame in enumerate(frames)]

    if fmt == "pdf":
        out = out_dir / f"{title}.pdf"
        media.build_pdf(slides, out, title)
    else:
        out = out_dir / f"{title}.pptx"
        media.build_pptx(slides, out, title)
    return {"path": str(out)}


async def generate_media(ctx, job_id: str, payload: dict) -> dict:
    """Generate a PPT or PDF "book" from a local video (subtitles + keyframes)."""

    async def work() -> dict:
        return await asyncio.to_thread(_build_media, payload)

    return await _run(ctx, job_id, work())


async def asset_ingest(ctx, job_id: str, payload: dict) -> dict:
    """Parse, chunk, and embed a READY asset into the RAG corpus.

    rag_status transitions: PARSING → CHUNKING → EMBEDDING → INDEXED (FAILED on error).
    Chunks are fully rebuilt per asset (delete-by-asset + bulk insert), so re-ingesting
    after a failure is idempotent.
    """
    asset_id = UUID(payload["asset_id"])
    assets = SqlAssetRepository(ctx["session_factory"])

    async def work() -> dict:
        asset = await assets.get(asset_id)
        if asset is None or asset.file_status != READY or not asset.object_sha256:
            raise RuntimeError("asset not ready for ingest")
        await assets.set_status(asset_id, rag_status="PARSING")

        data = await get_storage().get(object_key(asset.object_sha256))
        if data is None:
            raise RuntimeError("object bytes missing from storage")

        text = extract_text(data, asset.name)
        await assets.set_status(asset_id, rag_status="CHUNKING")
        chunks = split_chunks(text)

        await assets.set_status(asset_id, rag_status="EMBEDDING")
        embeddings: list[list[float]] = []
        for i in range(0, len(chunks), settings.embed_batch_size):
            batch = chunks[i : i + settings.embed_batch_size]
            embeddings.extend(await ctx["embedder"].embed(batch))

        chunks_repo = SqlChunkRepository(ctx["session_factory"])
        await chunks_repo.delete_by_asset(asset_id)
        await chunks_repo.bulk_insert(
            asset_id,
            asset.user_id,
            asset.workspace_id,
            [
                (chunk, None, {"asset_id": str(asset_id), "seq": idx}, emb)
                for idx, (chunk, emb) in enumerate(zip(chunks, embeddings))
            ],
        )
        await assets.set_status(asset_id, rag_status="INDEXED")
        return {"chunks": len(chunks)}

    try:
        return await _run(ctx, job_id, work())
    except Exception:
        # Keep the asset's rag_status in a terminal FAILED state so the UI can surface it
        # even though the job row itself carries the failure detail.
        await assets.set_status(asset_id, rag_status="FAILED")
        raise
