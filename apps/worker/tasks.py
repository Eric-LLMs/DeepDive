"""Worker task functions.

Each function is registered by name in ``WorkerSettings.functions`` and mirrors the
enrichment work the gateway used to do in-process. arq calls every task as
``task(ctx, job_id, payload)``; the job row in PostgreSQL is the source of truth, so each
task drives its own status transitions via :class:`JobStore`.
"""
import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select, update

logger = logging.getLogger(__name__)

from api.agent_factory import (  # shared kernel + drive (api + worker)
    get_agent_kernel,
    get_drive_service,
)
from core.application.drive_service import READY, DriveError, DriveService
from core.config import settings
from core.infrastructure import media
from core.infrastructure.db import (
    ChunkModel,
    MessageModel,
    SessionEventModel,
    SessionLocal,
    SessionModel,
)
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
from core.infrastructure.jobs import SESSION_FINALIZE, JobStore, TaskQueue
from core.infrastructure.memory import (
    SessionMemoryStore,
    finalize_session,
    load_session_detail,
)
from core.infrastructure.repositories import (
    SqlArticleRepository,
    SqlSentenceRepository,
)
from core.infrastructure.storage import get_storage, object_key
from core.logger import reset_log_context, set_log_context
from rag.query_cache import bump_corpus_version

from apps.worker.rag_images import save_images, scan_embedded_images

# Image-attribution sentinels inserted by ``extract_document_text(page_markers=True)`` (PDF
# → ``[[PAGE:n]]``, DOCX → ``[[PARA:n]]``). The annotator below strips them from stored
# chunk content and records the page/paragraph span + image_ids on ``meta``.
_PAGE_MARKER = re.compile(r"\[\[PAGE:(\d+)\]\]")
_PARA_MARKER = re.compile(r"\[\[PARA:(\d+)\]\]")
_MARKER_STRIP = re.compile(r"\[\[PAGE:\d+\]\]|\[\[PARA:\d+\]\]")


def _record_dead_letter(job_id, attempt: int, error: str) -> None:
    """Best-effort dead-letter marker: one JSONL line on the audit log.

    There is no ``job_events`` table, so a terminal failure is appended to
    ``settings.audit_log_path`` (the same best-effort pattern as the agent audit trail).
    """
    payload = {
        "event": "job_dead_letter",
        "job_id": str(job_id),
        "attempt": attempt,
        "error": error,
    }
    path = settings.audit_log_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        logger.warning("job_dead_letter_write_failed job_id=%s", job_id)


async def _run(ctx, job_id: str, work) -> dict:
    """Mark the job running, execute ``work``, and record the terminal state.

    FAILED is only written on the FINAL failure (``attempt >= worker_max_tries``); a
    non-terminal failure flips the job back to RUNNING with a "retrying" note so PG never
    shows a false FAILED while arq is still retrying. The dead-letter marker is recorded on
    terminal failure.
    """
    store: JobStore = ctx["job_store"]
    uid = UUID(job_id)
    await store.mark_running(uid)
    attempt = int(ctx.get("job_try") or 1)
    max_tries = settings.worker_max_tries
    terminal = max_tries <= 1 or attempt >= max_tries
    # Tag every log line this job emits with the job id. arq runs each job in a fresh task,
    # so the ContextVar set here never bleeds across jobs; the reset keeps the tag from
    # surviving if the task is ever reused (tests, nested arq).
    log_tokens = set_log_context(request_id=f"job:{job_id}")
    try:
        try:
            result = await work
        except asyncio.CancelledError:
            # arq cancels jobs past job_timeout (CancelledError is a BaseException, so a
            # bare ``except Exception`` would swallow nothing — the job row would stay
            # "running" forever). Record the honest terminal state before re-raising.
            if terminal:
                await store.mark_failed(uid, "job cancelled: worker job timeout exceeded")
                _record_dead_letter(uid, attempt, "job cancelled: worker job timeout exceeded")
            else:
                await store.mark_running(uid, error=f"attempt {attempt} failed: job cancelled — retrying")
            raise
        except Exception as exc:
            if terminal:
                await store.mark_failed(uid, str(exc))
                _record_dead_letter(uid, attempt, str(exc))
            else:
                await store.mark_running(uid, error=f"attempt {attempt} failed: {exc} — retrying")
            raise
        await store.mark_succeeded(uid, result)
        return result
    finally:
        reset_log_context(log_tokens)


# Serialize asset_ingest per asset. Upload auto-enqueue (complete_upload), the manual
# cloud-drive "Import to Knowledge" button, and admin reindex can all enqueue the same
# asset; two jobs racing their delete-by-asset + incremental insert would delete each
# other's parent chunks mid-flight and hit ``chunks_parent_chunk_id_fkey``.
_asset_ingest_locks: dict[str, asyncio.Lock] = {}
_asset_ingest_locks_guard = asyncio.Lock()


async def _asset_ingest_lock(asset_id: str) -> asyncio.Lock:
    """Return the per-asset ingest lock, creating it on first use."""
    async with _asset_ingest_locks_guard:
        lock = _asset_ingest_locks.get(asset_id)
        if lock is None:
            lock = asyncio.Lock()
            _asset_ingest_locks[asset_id] = lock
        return lock


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


async def toolkit_generate(ctx, job_id: str, payload: dict) -> dict:
    """Generate slides / mindmap / summary (async job path).

    Two modes:

    - **file mode**: generate from workspace files. The payload paths are already confined
      to the workspace by the router; the pipeline re-checks.
    - **session mode** (``payload["session_id"]``): generate from a chat session's
      conversation and write the artifacts into the caller's Cloud Drive
      (``payload["folder_path"]`` or the drive root).
    """

    async def work() -> dict:
        if payload.get("session_id"):
            return await _generate_from_session(ctx, job_id, payload)
        if payload.get("file_ids") or payload.get("paths"):
            return await _generate_from_files(ctx, job_id, payload)
        raise RuntimeError("no sources to generate from")

    return await _run(ctx, job_id, work())


async def _generate_from_session(ctx, job_id: str, payload: dict) -> dict:
    """Session mode: read the chat transcript, generate, and persist artifacts to the drive.

    The job's owner comes from the job row (never from client input); the session's owner is
    re-checked as defense in depth. A temp transcript is written inside the workspace — the
    pipeline's ``_resolve`` refuses sources outside it — and always deleted afterwards.
    """
    from apps.api.tools.toolkit import pipeline_for
    from apps.api.tools.toolkit.session_source import (
        SESSION_SRC_DIR,
        artifact_plan,
        build_transcript,
        sanitize_name,
    )

    job_row = await ctx["job_store"].get(UUID(job_id))
    if job_row is None or job_row.user_id is None:
        raise RuntimeError("job has no owner")
    user_id = job_row.user_id

    session_id = UUID(payload["session_id"])
    async with SessionLocal() as session:
        sess = await session.get(SessionModel, session_id)
    if sess is None or sess.user_id != user_id:
        raise RuntimeError("session not found")

    title = payload.get("name") or sess.title or "session"
    detail = await load_session_detail(SessionLocal, session_id)
    transcript = build_transcript(title, detail["messages"])

    src_dir = Path(settings.workspace_dir) / SESSION_SRC_DIR
    src_dir.mkdir(parents=True, exist_ok=True)
    tmp_source = src_dir / f"{sanitize_name(title)}_{job_id[:8]}.md"
    try:
        tmp_source.write_text(transcript, encoding="utf-8")
        result = await pipeline_for(payload["tool"], ctx["llm"]).run(
            [str(tmp_source)], prompt=payload.get("prompt")
        )
        plan = artifact_plan(payload["tool"], title)
        drive = DriveService(SessionLocal)
        assets = []
        for path in result.files:
            entry = plan.get(Path(path).suffix.lower())
            if entry is None:
                continue
            name, mime = entry
            asset = await drive.save_artifact(
                user_id,
                name,
                mime,
                Path(path).read_bytes(),
                folder_path=payload.get("folder_path"),
            )
            assets.append(
                {
                    "asset_id": str(asset.id),
                    "name": asset.name,
                    "folder_path": asset.folder_path,
                }
            )
        return {"tool": payload["tool"], "assets": assets, "summary": result.summary}
    finally:
        try:
            tmp_source.unlink()
        except OSError:
            pass


async def _generate_from_files(ctx, job_id: str, payload: dict) -> dict:
    """Generate from workspace files and/or Cloud Drive files.

    ``paths`` are already router-confined absolute paths inside the workspace. When at least
    one ``file_ids`` is present (cloud-file or mixed mode), every cloud file is ownership-
    checked again here (defense in depth) and its raw bytes downloaded into temp files inside
    the workspace — keeping the original extension so text/PDF/doc extraction works — then all
    sources are merged into a single pipeline run and the artifacts are saved back into the
    caller's Cloud Drive (``assets``). With only ``paths`` the legacy workspace-only behavior
    is kept: write into ``output_dir`` and return the local ``files``.
    """
    from apps.api.tools.toolkit import pipeline_for
    from apps.api.tools.toolkit.session_source import (
        SESSION_SRC_DIR,
        artifact_plan,
        sanitize_name,
    )

    sources: list[str] = list(payload.get("paths") or [])
    tmp_files: list[Path] = []
    drive_mode = bool(payload.get("file_ids"))

    if drive_mode:
        job_row = await ctx["job_store"].get(UUID(job_id))
        if job_row is None or job_row.user_id is None:
            raise RuntimeError("job has no owner")
        user_id = job_row.user_id

        drive = DriveService(SessionLocal)
        src_dir = Path(settings.workspace_dir) / SESSION_SRC_DIR
        src_dir.mkdir(parents=True, exist_ok=True)
        first_name: str | None = None
        for fid in payload["file_ids"]:
            _mime, name, data = await drive.download(user_id, UUID(fid))
            if not data:
                raise RuntimeError(f"file has no readable content: {name}")
            if first_name is None:
                first_name = Path(name).stem or name
            ext = Path(name).suffix.lower()
            tmp = src_dir / f"{sanitize_name(name)}_{job_id[:8]}{ext}"
            tmp.write_bytes(data)
            tmp_files.append(tmp)
            sources.append(str(tmp))

    if not sources:
        raise RuntimeError("no sources to generate from")

    try:
        result = await pipeline_for(payload["tool"], ctx["llm"]).run(
            sources,
            output_dir=payload.get("output_dir"),
            prompt=payload.get("prompt"),
        )
        if not drive_mode:
            return {"files": result.files, "summary": result.summary}
        plan = artifact_plan(payload["tool"], payload.get("name") or first_name or "files")
        assets = []
        for path in result.files:
            entry = plan.get(Path(path).suffix.lower())
            if entry is None:
                continue
            name, mime = entry
            asset = await drive.save_artifact(
                user_id,
                name,
                mime,
                Path(path).read_bytes(),
                folder_path=payload.get("folder_path"),
            )
            assets.append(
                {
                    "asset_id": str(asset.id),
                    "name": asset.name,
                    "folder_path": asset.folder_path,
                }
            )
        return {"tool": payload["tool"], "assets": assets, "summary": result.summary}
    finally:
        for path in tmp_files:
            try:
                path.unlink()
            except OSError:
                pass


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
        #
        # When the document embeds images (PDF/DOCX), each image is persisted as a
        # cloud-drive asset (source-bound to this PDF/DOCX, deduped on re-ingest) and the
        # extracted text is chunked with page/paragraph markers so every chunk's ``meta``
        # carries the image_ids of the pages it covers — the agent then sees those ids in
        # ``rag_search`` results and reads them with the ``vision`` tool.
        await assets.set_status(asset_id, rag_status="CHUNKING")
        scans = await asyncio.to_thread(scan_embedded_images, data, asset.name)
        if scans:
            drive = DriveService(ctx["session_factory"])
            page_images = await save_images(
                scans, asset.name, asset.user_id, asset.workspace_id, asset.id, drive
            )
            text = await extract_document_text(
                data, asset.name, ctx["llm"], page_markers=True
            )

            state = {"page": None, "para": None}  # running anchor for markerless middle blocks

            def on_split(chunks):
                for c in chunks:
                    pages = [int(m) for m in _PAGE_MARKER.findall(c.content_en)]
                    paras = [int(m) for m in _PARA_MARKER.findall(c.content_en)]
                    c.content_en = _MARKER_STRIP.sub("", c.content_en)
                    if pages:  # union: [running page] ∪ every page marker inside the chunk
                        cover = sorted(set(([state["page"]] if state["page"] else []) + pages))
                        state["page"] = pages[-1]
                        meta_key = "pages"
                    elif paras:
                        cover = sorted(set(([state["para"]] if state["para"] else []) + paras))
                        state["para"] = paras[-1]
                        meta_key = "paras"
                    elif state["page"] is not None:  # no marker → still on the running page
                        cover, meta_key = [state["page"]], "pages"
                    elif state["para"] is not None:
                        cover, meta_key = [state["para"]], "paras"
                    else:
                        cover, meta_key = [], "pages"
                    if cover:
                        c.meta[meta_key] = cover
                        ids: list[str] = []
                        for anchor in cover:  # deduped union across every covered page/para
                            for i in page_images.get(anchor, []):
                                if i not in ids:
                                    ids.append(i)
                        c.meta["image_ids"] = ids

            chunks = await build_chunks(
                text, cfg, doc_title=asset.name, llm=ctx["llm"], on_split=on_split
            )
        else:
            text = await extract_document_text(data, asset.name, ctx["llm"])
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
        # The corpus changed → invalidate the Redis query cache (best-effort).
        await bump_corpus_version(ctx["redis"])
        return {"chunks": inserted}

    # Serialize per asset (see _asset_ingest_lock): without this, concurrent jobs for the
    # same asset interleave their delete-by-asset + incremental insert and can delete each
    # other's parent chunks mid-flight (chunks_parent_chunk_id_fkey).
    lock = await _asset_ingest_lock(str(asset_id))
    async with lock:
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
        await bump_corpus_version(ctx["redis"])  # corpus changed → drop stale query-cache hits
        return {"chunks": total}

    return await _run(ctx, job_id, work())


def _default_chat_pairs(messages) -> list[dict]:
    """Group a message list into Q&A pairs: each user message starts a pair, the assistant
    messages until the next user message are its answer (merging multi-turn follow-ups).

    Each pair records ``indices`` — the message index (0-based position in ``messages``)
    of the user line it answers — so the caller can track per-message coverage.
    """
    pairs: list[dict] = []
    current: dict | None = None
    for i, m in enumerate(messages):
        if m.role == "user":
            if current is not None:
                pairs.append(current)
            current = {"question": m.text, "answer": "", "indices": [i]}
        elif m.role == "assistant" and current is not None:
            current["answer"] = (
                (current["answer"] + "\n" + m.text).strip() if current["answer"] else m.text
            )
    if current is not None:
        pairs.append(current)
    return pairs


def _normalize_pair_indices(pair: dict, roles: list[str]) -> list[int]:
    """Keep only message indexes that really are user lines; drop out-of-range / dups."""
    raw = pair.get("indices")
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[int] = set()
    out: list[int] = []
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= len(roles) or roles[i] != "user" or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


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

    The LLM reads the transcript and returns a JSON array of
    ``{"question", "answer", "indices"}`` where ``indices`` are the transcript line
    indexes the entry covers (the user-question line, plus any follow-up user lines
    merged in). Entries missing ``indices`` fall back to the remaining user lines in
    transcript order; any failure (unparseable output, model error) degrades to
    ``_default_chat_pairs`` — organize or fall back, never fail the import job.
    """
    transcript = "\n".join(f"{i}: {m.role}: {m.text[:400]}" for i, m in enumerate(messages))
    try:
        out = await llm.complete(
            "Here is a chat log where each line is '<index>: <role>: <text>'.\n"
            "Group the conversation into Q&A entries: merge all turns that belong to the "
            "same user question (including follow-ups and clarifications) into one entry, "
            "and separate different questions into different entries.\n"
            "For each entry, \"indices\" must list the line indexes it covers — the user "
            "question line plus any follow-up user lines that are part of that same "
            "question.\n"
            "Reply with ONLY a JSON array, no code fences:\n"
            '[{"question": "...", "answer": "...", "indices": [i, j, ...]}, ...]\n\n' + transcript,
            "You are a conversation organiser. Output only JSON.",
        )
        data = json.loads(_strip_code_fence(out))
        roles = [m.role for m in messages]
        pairs: list[dict] = []
        for it in data:
            if not isinstance(it, dict) or not (it.get("question") or it.get("answer")):
                continue
            pairs.append(
                {
                    "question": str(it.get("question", "")).strip(),
                    "answer": str(it.get("answer", "")).strip(),
                    "indices": _normalize_pair_indices(it, roles),
                }
            )
        if pairs:
            # Fill any pair the LLM left without indices from the remaining user lines so
            # every pair still carries an anchor for per-message "✓ Imported" coverage (an
            # older model may return {question, answer} without indices).
            claimed: set[int] = set()
            for p in pairs:
                claimed.update(p["indices"])
            for p in pairs:
                if p["indices"]:
                    continue
                for i, role in enumerate(roles):
                    if role == "user" and i not in claimed:
                        p["indices"] = [i]
                        claimed.add(i)
                        break
            return pairs
    except Exception as exc:  # noqa: BLE001 - degrade to default grouping
        logger.warning("chat session LLM segmentation failed, using default grouping: %s", exc)
    return _default_chat_pairs(messages)


async def _chat_delete_legacy_session_chunks(
    session_factory, user_id: UUID, session_id: str
) -> None:
    """Drop pre-flag whole-session chunk keys (``<session_id>:<i>`` and the bare
    ``<session_id>``) so a legacy session converts to the per-message flag model on its next
    import without duplicating content. A no-op for flag-era sessions (those write per-message
    keys under the first covered user-message id, which this predicate never matches).
    """
    prefix = f"{session_id}:"
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ChunkModel.source_id).where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user_id,
                    or_(
                        ChunkModel.source_id.startswith(prefix),
                        ChunkModel.source_id == session_id,
                    ),
                )
            )
        ).all()
    ids = [sid for (sid,) in rows if sid]
    if ids:
        await SqlChunkRepository(session_factory).delete_by_source("chat", ids)


def _pair_covered_ids(pair: dict, messages) -> list[str]:
    """User-message ids a Q&A entry answers (from its transcript line indexes)."""
    cov: list[str] = []
    for i in pair.get("indices") or []:
        if i < len(messages) and messages[i].role == "user":
            cid = str(messages[i].id)
            if cid not in cov:
                cov.append(cid)
    return cov


def _pair_image_ids(pair: dict, messages) -> list[str]:
    """Cloud-drive screenshot assets owned by the user messages a Q&A entry covers.

    Returns the owned ``chat/temp`` asset ids (one per covered user message with a screenshot).
    The caller mirrors each into a stable ``RAG/images`` copy and puts the *copy* ids into the
    chunk's ``meta.image_ids`` so retrieval returns them for the vision tool; the stable copy is
    a separate asset row that survives the session/message delete cascade (the ``chat/temp``
    original dies with its chat).
    """
    ids: list[str] = []
    for i in pair.get("indices") or []:
        if i < len(messages) and messages[i].role == "user":
            aid = messages[i].attach_asset_id
            if aid is not None and str(aid) not in ids:
                ids.append(str(aid))
    return ids


def _pair_spans(pairs: list[dict], messages) -> list[tuple[int, int]]:
    """Transcript ``[start, end)`` index spans each Q&A entry covers.

    ``start`` is the pair's first question line; ``end`` is the first user line *after* the
    pair's last covered line (or the end of the transcript) — i.e. the question plus its
    merged assistant answer, never a trailing question with no answer. Spans are disjoint, so
    flagging a span never double-flags.
    """
    user_indexes = [i for i, m in enumerate(messages) if m.role == "user"]
    spans: list[tuple[int, int]] = []
    for pair in pairs:
        idx = sorted(pair.get("indices") or [])
        start = idx[0]
        end = next((i for i in user_indexes if i > idx[-1]), len(messages))
        spans.append((start, end))
    return spans


def _span_imported(messages, span: tuple[int, int], flagged: dict[str, bool]) -> bool:
    """True when every user/assistant message in a span already carries ``imported_rag``.

    The whole span must be flagged (not just the question): a regenerated answer creates a
    fresh assistant message id, so an untouched imported pair is skipped while a pair whose
    answer was regenerated re-imports.
    """
    start, end = span
    return all(
        messages[j].role not in ("user", "assistant") or flagged.get(str(messages[j].id), False)
        for j in range(start, end)
    )


async def chat_session_import(ctx, job_id: str, payload: dict) -> dict:
    """Import a whole chat session into the query repository as Q&A chunks.

    The LLM (falling back to a per-turn default) groups the conversation into Q&A entries:
    the same question asked over multiple turns is merged into one entry, distinct
    questions become separate entries. Each entry is chunked + embedded with
    ``source_type='chat'`` + ``source_id=<first covered user-message id>`` (a stable key, so
    positional drift after message deletes / regroupings never collides with old chunks).

    Imports are **incremental and flag-driven**: a pair is only embedded when its span (the
    question plus its merged answer) is not yet fully ``imported_rag``-flagged — re-importing
    a session therefore never re-embeds content that is already in the repo, only newly
    appended or regenerated pairs. Pre-flag legacy whole-session keys (``<session_id>:<i>``)
    are purged once so a legacy session converts cleanly without duplicating content. On
    success the imported pairs' messages get their ``imported_rag`` flag set, which drives the
    per-message "✓ Imported" state on the client straight from the message rows — stable
    across message deletes, because each message carries its own flag.
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

        # Fast path: every chat message is already imported → there is nothing to embed, so
        # skip the LLM segment call entirely. That call is the slow part (minutes when the
        # model gateway is slow or down), and a re-import of an already-imported session must
        # resolve instantly instead of leaving the job "running" while the client polls.
        if messages and all(
            m.imported_rag for m in messages if m.role in ("user", "assistant")
        ):
            return {"chunks": 0, "groups": 0}

        pairs = await _segment_chat(messages, ctx["llm"])
        pairs = [p for p in pairs if p["answer"]]
        if not pairs:
            return {"chunks": 0, "groups": 0}

        # One-time conversion: drop legacy whole-session keys so this session moves to the
        # per-message model without leaving duplicate content behind. No-op for flag-era
        # sessions (which never write positional keys).
        await _chat_delete_legacy_session_chunks(ctx["session_factory"], user_id, str(session_id))

        spans = _pair_spans(pairs, messages)
        flagged = {str(m.id): m.imported_rag for m in messages}

        total = 0
        flag_ids: set[str] = set()
        rag_copy: dict[str, str] = {}  # chat/temp asset id → stable RAG/images copy id
        chunks_repo = SqlChunkRepository(ctx["session_factory"])
        drive = DriveService(ctx["session_factory"])
        for pair, (start, end) in zip(pairs, spans):
            if _span_imported(messages, (start, end), flagged):
                continue  # already in the repo — never re-embed
            covered = _pair_covered_ids(pair, messages)
            image_ids = _pair_image_ids(pair, messages)
            # Each owned screenshot stays in chat/temp AND gets a stable RAG/images copy sharing
            # the same object bytes (so emptying chat/temp never loses a repo-referenced image).
            # The chunk meta references the copy; best-effort, falling back to the owned asset.
            rag_image_ids = []
            for aid in image_ids:
                if aid not in rag_copy:
                    try:
                        rag_copy[aid] = str(
                            (await drive.copy_to_folder(user_id, UUID(aid), "RAG/images")).id
                        )
                    except DriveError:
                        rag_copy[aid] = aid  # degraded: reference the owned chat/temp copy
                rag_image_ids.append(rag_copy[aid])
            key = covered[0] if covered else f"{session_id}:{start}"
            text = f"{pair['question']}\n\n{pair['answer']}"
            title = pair["question"].strip()[:60] or f"Chat Q{start + 1}"
            chunks = await build_chunks(text, cfg, doc_title=title, llm=ctx["llm"])
            for c in chunks:
                c.meta = {
                    **c.meta,
                    "title": title,
                    "kind": "qa",
                    "session_id": str(session_id),
                    "covered": covered,
                }
                # A Q&A entry that covers a message owning a screenshot keeps that image: it
                # rides the chunk meta so retrieval returns it for the vision tool; the stable
                # RAG/images copy is what survives the later session/message delete.
                if rag_image_ids:
                    c.meta["image_ids"] = rag_image_ids
            # Idempotent: replace any chunk already keyed by this question (e.g. a prior
            # single-pair import, or a re-import after the answer was regenerated).
            await chunks_repo.delete_by_source("chat", [key])
            res = await _write_query_repo_chunks(
                ctx,
                chunks=chunks,
                user_id=user_id,
                source_type="chat",
                source_id=key,
            )
            total += res["chunks"]
            for j in range(start, end):
                if messages[j].role in ("user", "assistant"):
                    flag_ids.add(str(messages[j].id))

        if flag_ids:
            async with ctx["session_factory"]() as session:
                await session.execute(
                    update(MessageModel)
                    .where(MessageModel.id.in_(list(flag_ids)))
                    .values(imported_rag=True)
                )
                await session.commit()

        await bump_corpus_version(ctx["redis"])  # corpus changed → drop stale query-cache hits
        return {"chunks": total, "groups": len(pairs)}

    return await _run(ctx, job_id, work())


async def run_agent_turn(ctx, job_id: str, payload: dict) -> dict:
    """Run one agent turn for a user/session in the background (cron / scheduled activity).

    Reuses the shared :class:`AgentKernel` composition (:mod:`api.agent_factory`), so a
    scheduled turn gets the exact same prompt assembly, recall, approvals, telemetry, and
    budget guard as an interactive one — no second, drift-prone kernel construction in the
    worker. The answer lands in the session like a normal chat message and
    ``session_finalize`` is deferred exactly as in the interactive path.

    Payload: ``user_id``, ``session_id``, ``message`` (+ optional ``model`` / ``base_url`` /
    ``api_key`` to pin an LLM channel, mirroring the chat endpoint).
    """
    async def work() -> dict:
        user_id = UUID(payload["user_id"])
        session_id = UUID(payload["session_id"])
        message = payload["message"]
        log_tokens = set_log_context(
            user_id=str(user_id), session_id=str(session_id), request_id=f"job:{job_id}"
        )
        try:
            session_memory = SessionMemoryStore(
                ctx["session_factory"], ctx["embedder"], ctx["llm"], session_id, user_id
            )
            history = await session_memory.load_messages()
            result = await get_agent_kernel().run(
                message,
                history,
                session_memory=session_memory,
                model=payload.get("model"),
                base_url=payload.get("base_url"),
                api_key=payload.get("api_key"),
            )
            # run() already closed session_memory (flushed events); defer the expensive
            # embed + summary work to session_finalize, like the interactive chat path.
            await TaskQueue(ctx["redis"], ctx["job_store"]).enqueue(
                SESSION_FINALIZE, {"session_id": str(session_id)}
            )
            return {
                "final_answer": result.final_answer,
                "steps": len(result.messages),
                "usage": result.usage,
                "cost_usd": result.cost_usd,
            }
        finally:
            reset_log_context(log_tokens)

    return await _run(ctx, job_id, work())


async def research_drive(ctx, job_id: str, payload: dict) -> dict:
    """Run one auto-continue worker turn of a Research OS task run (the T0 chain).

    One job = one :class:`~plugins.research.driver.ResearchRunDriver.auto_turn` execution
    (one ``execution_id``). The driver owns every state transition under the single-flight
    CAS (claim, ledger, ``last_block``, slot release); this task supplies the LLM turn, then
    mirrors the model's answer into the task's session mirror, schedules the *next* auto turn
    when the driver says continue, and publishes the wake-up event so the desktop monitor
    refetches.

    Payload: ``user_id``, ``task_id``, ``run_id``, ``turn_index`` (+ optional ``session_id``
    and ``model`` / ``base_url`` / ``api_key`` pinning the interactive run's LLM channel).
    Never raises for a graded stop (finish / blocked / stalled / cancelled / drop): those are
    honest outcomes recorded in the driver state. Only an unexpected bug escapes (after the
    slot is released) so the job row fails truthfully.
    """
    async def work() -> dict:
        from agent.security.approvals import (
            ApprovalStore,
            get_approval_bridge,
            set_request_approval,
        )
        from core.infrastructure.request_context import set_request_user

        from plugins.research.driver import ResearchRunDriver, RunTurnResult
        from plugins.research.plugin import ResearchService

        user_id = UUID(payload["user_id"])
        set_request_user(user_id)
        task_id = payload["task_id"]
        run_id = payload["run_id"]
        turn_index = int(payload["turn_index"])
        session_id = payload.get("session_id")
        model = payload.get("model")
        base_url = payload.get("base_url")
        api_key = payload.get("api_key")
        # The _run wrapper already tagged the job id; add the research run's owner/task/run so
        # driver and settle log lines carry ``task_id:run_id`` like the interactive turn does.
        log_tokens = set_log_context(
            user_id=str(user_id),
            session_id=str(session_id) if session_id else None,
            task_id=task_id,
            run_id=run_id,
        )

        # The driver mirror (append_session_turn) keys off the DB session id; the task keeps
        # its own task-local session_history.json projection. The session must be bound to this
        # task (chat binds it when the user started the run) for the mirror to land.
        service = ResearchService(get_drive_service(), settings.research_scratch_dir)
        driver = ResearchRunDriver()

        # History for the model = the real DB conversation (the interactive human turns). We
        # deliberately do NOT pass SessionMemoryStore as the kernel's session_memory: the auto
        # driver prompt is synthetic and would otherwise be persisted as a fake user message in
        # the DB session. The assistant answers still reach the task-local mirror below.
        history: list[dict] = []
        if session_id:
            store = SessionMemoryStore(
                ctx["session_factory"], ctx["embedder"], ctx["llm"],
                UUID(session_id), user_id,
            )
            history = await store.load_messages()

        # Bind an approval store so the bridge never DENYs an ASK'd tool for lack of a bound
        # store; resolutions route through the shared Redis broker (configured at startup).
        set_request_approval(ApprovalStore(get_approval_bridge().broker, user_id=str(user_id)))

        async def run_turn(prompt: str) -> RunTurnResult:
            result = await get_agent_kernel().run(
                prompt,
                history,
                session_memory=None,
                model=model,
                base_url=base_url,
                api_key=api_key,
                context={
                    "handoff": {
                        "kind": "research",
                        "project_id": task_id,
                        "mode": "research_resume",
                    }
                },
                max_steps=settings.research_driver_turn_max_steps,
            )
            return RunTurnResult(
                final_answer=result.final_answer or "",
                cost_usd=float(result.cost_usd or 0.0),
            )

        try:
            outcome = await driver.auto_turn(
                service,
                owner_id=user_id,
                task_id=task_id,
                run_id=run_id,
                turn_index=turn_index,
                run_turn=run_turn,
            )
            return await _settle_research_outcome(
                ctx, service, driver,
                owner_id=user_id,
                task_id=task_id,
                session_id=session_id,
                outcome=outcome,
                channel=(model, base_url, api_key),
            )
        finally:
            set_request_approval(None)
            reset_log_context(log_tokens)

    return await _run(ctx, job_id, work())


async def _settle_research_outcome(
    ctx,
    service,
    driver,
    *,
    owner_id: UUID,
    task_id: str,
    session_id: str | None,
    outcome,
    channel: tuple[str | None, str | None, str | None],
) -> dict:
    """Act on a driver outcome: mirror, schedule the next turn, publish the wake-up event.

    Kept as a plain helper so the mirror/enqueue/publish ordering is testable without arq.
    """
    from core.infrastructure.jobs import RESEARCH_DRIVE

    if outcome.dropped:
        # A stale/duplicate job: nothing changed on disk, nothing to mirror or enqueue.
        return {"action": outcome.action, "reason": outcome.reason, "dropped": True}

    # Mirror the model's answer (and a compact auto-run marker) into the task's session mirror
    # so the desktop research panel shows what each background turn produced.
    if outcome.final_answer and session_id:
        marker = f"[auto-run · turn {outcome.turn_index}] continuing the research task."
        with contextlib.suppress(Exception):
            await service.append_session_turn(
                owner_id, session_id, "user", marker
            )
            await service.append_session_turn(
                owner_id, session_id, "assistant", outcome.final_answer
            )

    model, base_url, api_key = channel
    if outcome.action == "continue":
        try:
            await TaskQueue(ctx["redis"], ctx["job_store"]).enqueue(
                RESEARCH_DRIVE,
                {
                    "user_id": str(owner_id),
                    "task_id": task_id,
                    "run_id": outcome.run_id,
                    "session_id": session_id,
                    "turn_index": outcome.next_turn_index,
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                },
                user_id=owner_id,
            )
        except Exception as exc:
            # The chain cannot continue — release the slot with an honest error record and
            # surface the failure, never leaving active_run held with no job behind it.
            err = driver.abort_run(
                service, owner_id, task_id,
                run_id=outcome.run_id, execution_id=outcome.execution_id,
                reason=f"could not schedule the next auto turn: {exc}",
            )
            with contextlib.suppress(Exception):
                await service.publish_change(owner_id, task_id, kind=err.publish_kind)
            raise
        # A wake-up so the desktop monitor refetches the fresh snapshot + mirrored turn.
        with contextlib.suppress(Exception):
            await service.publish_change(owner_id, task_id, kind=outcome.publish_kind)
        return {
            "action": "continue",
            "turn_index": outcome.turn_index,
            "next_turn_index": outcome.next_turn_index,
            "execution_id": outcome.execution_id,
            "cost_usd": outcome.cumulative_cost_usd,
        }

    # Graded terminal (finished / blocked / stalled / cancelled / error-from-retry-exhaustion).
    with contextlib.suppress(Exception):
        await service.publish_change(owner_id, task_id, kind=outcome.publish_kind)
    return {
        "action": outcome.action,
        "reason": outcome.reason,
        "state": outcome.state.value,
        "turn_index": outcome.turn_index,
        "execution_id": outcome.execution_id,
        "cost_usd": outcome.cumulative_cost_usd,
    }
