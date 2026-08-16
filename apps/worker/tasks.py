"""Worker task functions.

Each function is registered by name in ``WorkerSettings.functions`` and mirrors the
enrichment work the gateway used to do in-process. arq calls every task as
``task(ctx, job_id, payload)``; the job row in PostgreSQL is the source of truth, so each
task drives its own status transitions via :class:`JobStore`.
"""
import asyncio
from pathlib import Path
from uuid import UUID

from core.config import settings
from core.infrastructure import media
from core.infrastructure.jobs import JobStore
from core.infrastructure.memory import finalize_session
from core.infrastructure.repositories import SqlSentenceRepository


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
