"""Enrichment / media jobs: enqueue TTS, image fetch, media generation, explanation, and
poll job state. Streaming TTS synthesizes directly in-process over SSE.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from api.deps import get_task_queue
from api.schemas import ExplainRequest, ImageFetchRequest, MediaGenerateRequest, TTSRequest
from core.infrastructure.jobs import EXPLAIN, GENERATE_MEDIA, IMAGE_FETCH, TTS, TaskQueue
from core.infrastructure.tts import TTSClient
from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["jobs"])


@router.post("/image-fetch")
async def fetch_images(body: ImageFetchRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(
        IMAGE_FETCH,
        {
            "word": body.word,
            "definition": body.definition,
            "context": body.context,
            "regenerate": body.regenerate,
        },
    )
    return {"job_id": str(job_id)}


@router.post("/media/generate")
async def generate_media(body: MediaGenerateRequest, queue: TaskQueue = Depends(get_task_queue)):
    """Enqueue PPT/PDF generation from a local video (subtitles + keyframes)."""
    job_id = await queue.enqueue(
        GENERATE_MEDIA,
        {
            "video_path": body.video_path,
            "subtitle_path": body.subtitle_path,
            "format": body.format,
            "title": body.title,
        },
    )
    return {"job_id": str(job_id)}


@router.post("/tts")
async def synthesize_audio(body: TTSRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(TTS, {"text": body.text})
    return {"job_id": str(job_id)}


@router.post("/tts/stream")
async def synthesize_audio_stream(body: TTSRequest):
    """Stream TTS audio sentence-by-sentence as SSE.

    Each ``segment`` event carries the ``/audio/<file>`` URL of one cached WAV (synthesized
    directly in the API process — the TTS container is reachable over localhost), so the
    client can start playing the first sentence while the rest are still being generated.
    """

    async def gen():
        client = TTSClient()
        idx = 0
        try:
            async for path in client.synthesize_segments(body.text):
                yield {"data": json.dumps({"type": "segment", "index": idx, "url": "/audio/" + Path(path).name}, ensure_ascii=False)}
                idx += 1
        except Exception as exc:  # noqa: BLE001 - report the failure to the client, then end the stream
            yield {"data": json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False)}
            return
        yield {"data": json.dumps({"type": "done", "count": idx}, ensure_ascii=False)}

    return EventSourceResponse(gen())


@router.post("/explain")
async def explain(body: ExplainRequest, queue: TaskQueue = Depends(get_task_queue)):
    job_id = await queue.enqueue(EXPLAIN, {"term": body.term, "context": body.context})
    return {"job_id": str(job_id)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, queue: TaskQueue = Depends(get_task_queue)):
    """Return the state of an async enrichment job (single source of truth: the jobs table)."""
    return await queue.get(job_id)
