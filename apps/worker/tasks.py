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

from api.deps import get_agent  # the hardened AgentKernel singleton (worker→api coupling)
from core.application.drive_service import READY
from core.config import settings
from core.infrastructure import media
from core.infrastructure.db import ChunkModel, MessageModel, SessionEventModel
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
from core.infrastructure.memory import SessionMemoryStore, finalize_session
from core.infrastructure.repositories import (
    SqlArticleRepository,
    SqlSentenceRepository,
)
from core.infrastructure.storage import get_storage, object_key
from rag.query_cache import bump_corpus_version


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
            # every pair still carries an anchor for per-message coverage / incremental
            # import (an older model may return {question, answer} without indices).
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


async def _chat_covered_ids(session_factory, user_id: UUID, session_id: str) -> set[str]:
    """All user-message ids already covered by imported chat chunks for a session.

    New-style ``kind='qa'`` chunks record their covered user-message ids in
    ``meta['covered']``; legacy single-pair imports used ``source_id=<user-message id>``
    directly (recognizable by the lack of a ``session_id:`` prefix).
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ChunkModel.source_id, ChunkModel.meta).where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user_id,
                    ChunkModel.meta["session_id"].astext == session_id,
                )
            )
        ).all()
    covered: set[str] = set()
    for source_id, meta in rows:
        if (meta or {}).get("kind") != "qa":
            continue
        cov = (meta or {}).get("covered")
        if isinstance(cov, list) and cov:
            covered.update(str(c) for c in cov)
        elif source_id and ":" not in source_id:
            covered.add(source_id)
    return covered


async def _chat_delete_stale_keys(
    session_factory, user_id: UUID, session_id: str, written: set[str]
) -> None:
    """Drop per-entry chat chunks whose key the current grouping no longer produces.

    The LLM may regroup after an append (e.g. two turns that used to be separate entries
    merge into one), leaving an old per-entry key behind; it would otherwise become a
    stale duplicate. Single-pair chunks (``source_id`` == a user-message id) are untouched.
    """
    prefix = f"{session_id}:"
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ChunkModel.source_id).where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user_id,
                    ChunkModel.meta["session_id"].astext == session_id,
                    ChunkModel.meta["kind"].astext == "qa",
                )
            )
        ).all()
    stale = [sid for (sid,) in rows if sid.startswith(prefix) and sid not in written]
    if stale:
        await SqlChunkRepository(session_factory).delete_by_source("chat", stale)


def _pending_chat_entries(
    pairs: list[dict], covered_by_pair: list[list[str]], existing: set[str]
) -> list[tuple[int, dict, list[str]]]:
    """Which segmentation entries still need importing.

    An entry is skipped when it has no user message to anchor, or when every user message
    it covers is already in the query repo — that is the incremental append behavior:
    re-importing a session only embeds newly asked questions.
    """
    out: list[tuple[int, dict, list[str]]] = []
    for i, (pair, covered) in enumerate(zip(pairs, covered_by_pair)):
        if not covered:
            continue
        if all(cid in existing for cid in covered):
            continue
        out.append((i, pair, covered))
    return out


async def chat_session_import(ctx, job_id: str, payload: dict) -> dict:
    """Import a whole chat session into the query repository as Q&A chunks.

    The LLM (falling back to a per-turn default) groups the conversation into Q&A entries:
    the same question asked over multiple turns is merged into one entry, distinct
    questions become separate entries. Each entry is chunked + embedded with
    ``source_type='chat'`` + ``source_id=<session_id>:<i>`` (per-entry key), so a re-import
    replaces entries independently.

    Imports are incremental: an entry whose covered user messages are all already in the
    query repo is skipped, so re-importing after an append only embeds the newly asked
    questions. Every chunk records the user-message ids it covers (``meta['covered']``),
    which drives the per-message "✓ Imported" state on the client. Legacy whole-session
    chunks (old ``kind='session-qa'``) are converted to this per-message model on the next
    import.
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
        # Convert legacy whole-session imports (old kind="session-qa" keyed on the session
        # id): they have no per-message coverage, so rebuild them under the new model.
        await chunks_repo.delete_by_source("chat", [str(session_id)])

        # Coverage per entry: the user-message ids this Q&A answers (order preserved).
        covered_by_pair: list[list[str]] = []
        for p in pairs:
            cov: list[str] = []
            for i in p.get("indices") or []:
                if i < len(messages) and messages[i].role == "user":
                    cid = str(messages[i].id)
                    if cid not in cov:
                        cov.append(cid)
            covered_by_pair.append(cov)

        existing = await _chat_covered_ids(ctx["session_factory"], user_id, str(session_id))
        written: set[str] = set()
        total = 0
        for i, pair, covered in _pending_chat_entries(pairs, covered_by_pair, existing):
            key = f"{session_id}:{i}"
            text = f"{pair['question']}\n\n{pair['answer']}"
            title = pair["question"].strip()[:60] or f"Chat Q{i + 1}"
            chunks = await build_chunks(text, cfg, doc_title=title, llm=ctx["llm"])
            for c in chunks:
                c.meta = {
                    **c.meta,
                    "title": title,
                    "kind": "qa",
                    "session_id": str(session_id),
                    "covered": covered,
                    "qid": i,
                }
            # Idempotent per-entry: replace any earlier version of this same key.
            await chunks_repo.delete_by_source("chat", [key])
            res = await _write_query_repo_chunks(
                ctx,
                chunks=chunks,
                user_id=user_id,
                source_type="chat",
                source_id=key,
            )
            total += res["chunks"]
            written.add(key)
            existing.update(covered)

        await _chat_delete_stale_keys(ctx["session_factory"], user_id, str(session_id), written)
        await bump_corpus_version(ctx["redis"])  # corpus changed → drop stale query-cache hits
        return {"chunks": total, "groups": len(pairs)}

    return await _run(ctx, job_id, work())


async def run_agent_turn(ctx, job_id: str, payload: dict) -> dict:
    """Run one agent turn for a user/session in the background (cron / scheduled activity).

    Reuses the API's hardened :class:`AgentKernel` singleton, so a scheduled turn gets the
    exact same prompt assembly, recall, approvals, telemetry, and budget guard as an
    interactive one — no second, drift-prone kernel construction in the worker. The answer
    lands in the session like a normal chat message and ``session_finalize`` is deferred
    exactly as in the interactive path.

    Payload: ``user_id``, ``session_id``, ``message`` (+ optional ``model`` / ``base_url`` /
    ``api_key`` to pin an LLM channel, mirroring the chat endpoint).
    """
    async def work() -> dict:
        user_id = UUID(payload["user_id"])
        session_id = UUID(payload["session_id"])
        message = payload["message"]
        session_memory = SessionMemoryStore(
            ctx["session_factory"], ctx["embedder"], ctx["llm"], session_id, user_id
        )
        history = await session_memory.load_messages()
        result = await get_agent().run(
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

    return await _run(ctx, job_id, work())
