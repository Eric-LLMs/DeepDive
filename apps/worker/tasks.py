"""Worker task functions.

Each function is registered by name in ``WorkerSettings.functions`` and mirrors the
enrichment work the gateway used to do in-process. arq calls every task as
``task(ctx, job_id, payload)``; the job row in PostgreSQL is the source of truth, so each
task drives its own status transitions via :class:`JobStore`.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

logger = logging.getLogger(__name__)

from core.application.drive_service import READY
from core.config import settings
from core.infrastructure import media
from core.infrastructure.db import MessageModel, SessionEventModel
from core.infrastructure.drive_repositories import (
    SqlAssetRepository,
    SqlChunkRepository,
)
from core.infrastructure.ingest import (
    Chunk,
    build_chunks,
    extract_document_text,
    write_query_repo_chunks,
)
from core.infrastructure.jobs import JobStore
from core.infrastructure.memory import finalize_session
from core.infrastructure.repositories import (
    SqlArticleRepository,
    SqlSentenceRepository,
)
from core.infrastructure.storage import get_storage, object_key


async def _run(ctx, job_id: str, work) -> dict:
    """Mark the job running, execute ``work``, and record the terminal state."""
    store: JobStore = ctx["job_store"]
    uid = UUID(job_id)
    await store.mark_running(uid)
    try:
        result = await work
    except asyncio.CancelledError:
        # arq cancels jobs past job_timeout (CancelledError is a BaseException, so a
        # bare ``except Exception`` would swallow nothing — the job row would stay
        # "running" forever). Record the honest terminal state before re-raising.
        await store.mark_failed(uid, "job cancelled: worker job timeout exceeded")
        raise
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

        # Runtime pipeline config (admin console) governs chunking / enrichment; config
        # changes take effect on the next re-ingest via /admin/rag/reindex.
        from rag.config_store import load_config

        cfg = await load_config(ctx["session_factory"])

        # PDF runs the async path (body + vision-transcribed tables via the PDF tools);
        # every other extension goes through the sync ``extract_text`` dispatch.
        text = await extract_document_text(data, asset.name, ctx["llm"])
        await assets.set_status(asset_id, rag_status="CHUNKING")
        chunks = await build_chunks(text, cfg, doc_title=asset.name, llm=ctx["llm"])

        await assets.set_status(asset_id, rag_status="EMBEDDING")
        # Drop previous chunks up front, then embed + insert incrementally per batch so a
        # worker timeout preserves whatever already committed — a re-run re-does only the
        # remainder instead of losing the whole document.
        chunks_repo = SqlChunkRepository(ctx["session_factory"])
        await chunks_repo.delete_by_asset(asset_id)

        # Parents are context only: recall searches leaf chunks (``chunk_kind='leaf'``) and
        # parent_expand fetches parents by ID, so parent embeddings are never queried. Skipping
        # them removes the ingest bottleneck — 3600-char parents pushed a 16-row batch to
        # ~14k tokens, which TEI (max-batch-tokens 2048) took ~70s to embed, blowing the
        # embedder timeout. Parents get a zero-vector sentinel to satisfy the NOT NULL column.
        zero = [0.0] * settings.embedding_dim
        parent_rows = [
            {
                "id": c.id,
                "content_en": c.content_en,
                "content_cn": c.content_cn,
                "meta": {**c.meta, "asset_id": str(asset_id)},
                "embedding": zero,
                "chunk_kind": c.chunk_kind,
                "parent_chunk_id": c.parent_chunk_id,
                "content_search": c.content_search,
            }
            for c in chunks
            if c.chunk_kind == "parent"
        ]
        if parent_rows:
            await chunks_repo.bulk_insert(
                asset_id, asset.user_id, asset.workspace_id, parent_rows
            )

        # Leaf chunks embed + insert in batches; parents were inserted first so every
        # ``parent_chunk_id`` reference is already satisfied.
        inserted = len(parent_rows)
        leaves = [c for c in chunks if c.chunk_kind != "parent"]
        for i in range(0, len(leaves), settings.embed_batch_size):
            batch = leaves[i : i + settings.embed_batch_size]
            embeddings = await ctx["embedder"].embed([c.content_en for c in batch])
            rows = [
                {
                    "id": c.id,
                    "content_en": c.content_en,
                    "content_cn": c.content_cn,
                    "meta": {**c.meta, "asset_id": str(asset_id)},
                    "embedding": emb,
                    "chunk_kind": c.chunk_kind,
                    "parent_chunk_id": c.parent_chunk_id,
                    "content_search": c.content_search,
                }
                for c, emb in zip(batch, embeddings)
            ]
            await chunks_repo.bulk_insert(asset_id, asset.user_id, asset.workspace_id, rows)
            inserted += len(batch)
        await assets.set_status(asset_id, rag_status="INDEXED")
        return {"chunks": inserted}

    try:
        return await _run(ctx, job_id, work())
    except asyncio.CancelledError:
        # Cancellation is a BaseException — it skips ``except Exception``. Mark the asset
        # FAILED so the UI doesn't show a forever-stuck CHUNKING/EMBEDDING badge.
        await assets.set_status(asset_id, rag_status="FAILED")
        raise
    except Exception:
        # Keep the asset's rag_status in a terminal FAILED state so the UI can surface it
        # even though the job row itself carries the failure detail.
        await assets.set_status(asset_id, rag_status="FAILED")
        raise


async def _write_query_repo_chunks(
    ctx, *, chunks: list[Chunk], user_id, source_type: str, source_id: str | None
) -> dict:
    """Embed + persist chunks as a non-file query-repo source (shared helper in ingest)."""
    return await write_query_repo_chunks(
        ctx["session_factory"],
        ctx["embedder"],
        chunks=chunks,
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
    )


async def learning_import(ctx, job_id: str, payload: dict) -> dict:
    """Push Learning-Platform content (sentences / articles) into the query repository.

    Each sentence / article is chunked under the runtime pipeline config and stored with
    ``source_type='learning'`` + ``source_id=<id>``. Re-importing the same id is
    idempotent (delete-by-source then rewrite), so the ImportData page can safely re-push.
    """
    user_id = UUID(payload["user_id"])
    kind = payload["kind"]

    async def work() -> dict:
        from rag.config_store import load_config  # lazy: rag is a sibling package

        cfg = await load_config(ctx["session_factory"])
        docs: list[tuple[str, str, str, str]] = []  # (id, title, text, kind)
        async with ctx["session_factory"]() as session:
            if kind == "sentence":
                repo = SqlSentenceRepository(session)
                for sid in payload["ids"]:
                    s = await repo.get(UUID(sid))
                    if s is not None and s.content_en.strip():
                        docs.append((str(sid), s.content_en[:60], s.content_en, "sentence"))
            elif kind == "article":
                repo = SqlArticleRepository(session)
                for aid in payload["ids"]:
                    a = await repo.get(UUID(aid))
                    if a is not None and a.content.strip():
                        docs.append((str(aid), a.title, a.content, "article"))
            else:
                raise ValueError(f"unknown learning import kind: {kind}")

        if not docs:
            return {"chunks": 0}
        chunks_repo = SqlChunkRepository(ctx["session_factory"])
        total = 0
        for sid, title, text, doc_kind in docs:
            await chunks_repo.delete_by_source("learning", [sid])
            chunks = await build_chunks(text, cfg, doc_title=title, llm=ctx["llm"])
            for c in chunks:
                c.meta = {**c.meta, "title": title, "kind": doc_kind}
            res = await _write_query_repo_chunks(
                ctx,
                chunks=chunks,
                user_id=user_id,
                source_type="learning",
                source_id=sid,
            )
            total += res["chunks"]
        return {"chunks": total}

    return await _run(ctx, job_id, work())


def _default_chat_pairs(messages) -> list[dict]:
    """Group a message list into Q&A pairs: each user message starts a pair, the assistant
    messages until the next user message are its answer (merging multi-turn follow-ups)."""
    pairs: list[dict] = []
    current: dict | None = None
    for m in messages:
        if m.role == "user":
            if current is not None:
                pairs.append(current)
            current = {"question": m.text, "answer": ""}
        elif m.role == "assistant" and current is not None:
            current["answer"] = (
                (current["answer"] + "\n" + m.text).strip() if current["answer"] else m.text
            )
    if current is not None:
        pairs.append(current)
    return pairs


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


async def _segment_chat(messages, llm) -> list[dict]:
    """Split a chat into Q&A entries: merge same-question turns, split distinct questions.

    The LLM reads the transcript and returns a JSON array of ``{question, answer}``;
    any failure (unparseable output, model error) degrades to ``_default_chat_pairs`` —
    organize or fall back, never fail the import job.
    """
    transcript = "\n".join(f"{i}: {m.role}: {m.text[:400]}" for i, m in enumerate(messages))
    try:
        out = await llm.complete(
            "Here is a chat log where each line is '<index>: <role>: <text>'.\n"
            "Group the conversation into Q&A entries: merge all turns that belong to the "
            "same user question (including follow-ups and clarifications) into one entry, "
            "and separate different questions into different entries.\n"
            "Reply with ONLY a JSON array, no code fences:\n"
            '[{"question": "...", "answer": "..."}, ...]\n\n' + transcript,
            "You are a conversation organiser. Output only JSON.",
        )
        data = json.loads(_strip_code_fence(out))
        pairs = [
            {"question": str(it.get("question", "")).strip(), "answer": str(it.get("answer", "")).strip()}
            for it in data
            if isinstance(it, dict) and (it.get("question") or it.get("answer"))
        ]
        if pairs:
            return pairs
    except Exception as exc:  # noqa: BLE001 - degrade to default grouping
        logger.warning("chat session LLM segmentation failed, using default grouping: %s", exc)
    return _default_chat_pairs(messages)


async def chat_session_import(ctx, job_id: str, payload: dict) -> dict:
    """Import a whole chat session into the query repository as Q&A chunks.

    The LLM (falling back to a per-turn default) groups the conversation into Q&A entries:
    the same question asked over multiple turns is merged into one entry, distinct
    questions become separate entries, and each entry is chunked + embedded with
    ``source_type='chat'`` + ``source_id=<session_id>`` (idempotent on re-import).
    """
    session_id = UUID(payload["session_id"])
    user_id = UUID(payload["user_id"])

    async def work() -> dict:
        from rag.config_store import load_config  # lazy: rag is a sibling package

        cfg = await load_config(ctx["session_factory"])
        async with ctx["session_factory"]() as session:
            messages = (
                await session.execute(
                    select(MessageModel)
                    .where(MessageModel.session_id == session_id)
                    .order_by(MessageModel.created_at)
                )
            ).scalars().all()

        pairs = await _segment_chat(messages, ctx["llm"])
        pairs = [p for p in pairs if p["answer"]]
        if not pairs:
            return {"chunks": 0, "groups": 0}

        chunks_repo = SqlChunkRepository(ctx["session_factory"])
        await chunks_repo.delete_by_source("chat", [str(session_id)])
        total = 0
        for i, pair in enumerate(pairs):
            text = f"{pair['question']}\n\n{pair['answer']}"
            title = pair["question"].strip()[:60] or f"Chat Q{i + 1}"
            chunks = await build_chunks(text, cfg, doc_title=title, llm=ctx["llm"])
            for c in chunks:
                c.meta = {
                    **c.meta,
                    "title": title,
                    "kind": "session-qa",
                    "session_id": str(session_id),
                    "qid": i,
                }
            res = await _write_query_repo_chunks(
                ctx,
                chunks=chunks,
                user_id=user_id,
                source_type="chat",
                source_id=str(session_id),
            )
            total += res["chunks"]
        return {"chunks": total, "groups": len(pairs)}

    return await _run(ctx, job_id, work())
