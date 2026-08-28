"""Enrichment / media jobs: enqueue TTS, image fetch, media generation, explanation, and
poll job state. Streaming TTS synthesizes directly in-process over SSE.

Every endpoint requires an authenticated user: jobs are bound to a caller so ``/jobs/{id}``
can scope reads to the job's owner (admin bypass). ``/media/generate`` confines its
video/subtitle paths to the workspace so a caller cannot point the worker at arbitrary
server files.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from api.auth import AuthUser, require_user
from api.deps import get_task_queue
from api.schemas import (
    ExplainRequest,
    ImageFetchRequest,
    MediaGenerateRequest,
    ToolkitGenerateRequest,
    TTSRequest,
)
from core.application.drive_service import DriveError, DriveService
from core.config import settings
from core.infrastructure.db import SessionLocal, SessionModel
from core.infrastructure.jobs import (
    EXPLAIN,
    GENERATE_MEDIA,
    IMAGE_FETCH,
    TOOLKIT_GENERATE,
    TTS,
    TaskQueue,
)
from core.infrastructure.tts import TTSClient
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

router = APIRouter(tags=["jobs"])


def _confined_folder_path(path: str | None) -> str | None:
    """Return ``path`` if it is a safe Cloud Drive folder path, else raise 400.

    Mirrors :meth:`DriveService._validate_folder_path`: a folder path is ``None`` (drive
    root) or a ``parent/child`` string with no ``..`` and no leading/trailing slash. An empty
    string is normalized to ``None`` (root).
    """
    if path is None:
        return None
    path = path.strip()
    if not path:
        return None
    if any(seg == ".." for seg in path.split("/")):
        raise HTTPException(status_code=400, detail="folder_path: invalid segment '..'")
    if path.startswith("/") or path.endswith("/"):
        raise HTTPException(status_code=400, detail="folder_path: must not start or end with '/'")
    return path


def _confined_path(path: str, *, field: str) -> str:
    """Return ``path`` only if it resolves inside ``settings.workspace_dir``.

    The worker reads these paths directly off the server filesystem (apps/worker/tasks.py
    _build_media), so an unconfined value would let a caller make the worker open any server
    file and render it into a returned PPT/PDF. Absolute escapes and ``..`` are rejected here.
    """
    root = Path(settings.workspace_dir).expanduser().resolve()
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field}: invalid path")
    if not resolved.is_relative_to(root):
        raise HTTPException(
            status_code=400, detail=f"{field}: path must be inside the workspace"
        )
    return str(resolved)


@router.post("/image-fetch")
async def fetch_images(
    body: ImageFetchRequest,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    job_id = await queue.enqueue(
        IMAGE_FETCH,
        {
            "word": body.word,
            "definition": body.definition,
            "context": body.context,
            "regenerate": body.regenerate,
        },
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.post("/media/generate")
async def generate_media(
    body: MediaGenerateRequest,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    """Enqueue PPT/PDF generation from a local video (subtitles + keyframes).

    Both paths are confined to the workspace so the worker only ever reads files the
    operator staged there — never arbitrary server paths.
    """
    video_path = _confined_path(body.video_path, field="video_path")
    subtitle_path = (
        _confined_path(body.subtitle_path, field="subtitle_path")
        if body.subtitle_path
        else None
    )
    job_id = await queue.enqueue(
        GENERATE_MEDIA,
        {
            "video_path": video_path,
            "subtitle_path": subtitle_path,
            "format": body.format,
            "title": body.title,
        },
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.post("/toolkit/generate")
async def generate_toolkit(
    body: ToolkitGenerateRequest,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    """Enqueue slides / mindmap / summary generation.

    Three mutually-exclusive modes:

    - **file mode** (``paths``): generate from workspace files. Every path (and the optional
      output dir) is confined to the workspace by :func:`_confined_path`, so the worker only
      ever reads files staged in the workspace — never arbitrary server paths.
    - **session mode** (``session_id``): generate from a chat session's conversation and save
      the artifacts into the caller's Cloud Drive (``folder_path`` or the drive root).
    - **cloud-file mode** (``file_ids``): generate from the caller's Cloud Drive files; every
      id is ownership-checked here so a caller cannot name another user's file. Artifacts are
      saved back into the caller's Cloud Drive (``folder_path`` or the drive root).
    """
    if body.session_id is not None:
        if body.paths or body.file_ids:
            raise HTTPException(
                status_code=400, detail="cannot combine session_id with paths or file_ids"
            )
        async with SessionLocal() as session:
            sess = await session.get(SessionModel, body.session_id)
        if sess is None or sess.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="session not found")
        job_id = await queue.enqueue(
            TOOLKIT_GENERATE,
            {
                "tool": body.tool,
                "session_id": str(body.session_id),
                "folder_path": _confined_folder_path(body.folder_path),
                "name": body.name,
                "prompt": body.prompt,
            },
            user_id=user.user_id,
        )
        return {"job_id": str(job_id)}

    if body.file_ids:
        if body.paths:
            raise HTTPException(
                status_code=400, detail="cannot combine file_ids with paths"
            )
        max_mb = settings.toolkit_max_file_bytes // (1024 * 1024)
        drive = DriveService(SessionLocal)
        over = []
        for fid in body.file_ids:
            try:
                asset = await drive.ensure_asset_readable(user.user_id, fid)
            except DriveError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=f"file not readable: {fid} ({exc.message})",
                ) from exc
            if getattr(asset, "size", 0) > settings.toolkit_max_file_bytes:
                over.append(asset.name)
        if over:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"file too large (max {max_mb} MB): {', '.join(over)}"
                ),
            )
        job_id = await queue.enqueue(
            TOOLKIT_GENERATE,
            {
                "tool": body.tool,
                "file_ids": [str(f) for f in body.file_ids],
                "folder_path": _confined_folder_path(body.folder_path),
                "name": body.name,
                "prompt": body.prompt,
            },
            user_id=user.user_id,
        )
        return {"job_id": str(job_id)}

    if not body.paths:
        raise HTTPException(status_code=400, detail="paths: at least one file required")
    paths = [_confined_path(p, field="paths") for p in body.paths]
    over = [Path(p).name for p in paths if Path(p).stat().st_size > settings.toolkit_max_file_bytes]
    if over:
        max_mb = settings.toolkit_max_file_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=400,
            detail=f"file too large (max {max_mb} MB): {', '.join(over)}",
        )
    output_dir = (
        _confined_path(body.output_dir, field="output_dir") if body.output_dir else None
    )
    job_id = await queue.enqueue(
        TOOLKIT_GENERATE,
        {
            "tool": body.tool,
            "paths": paths,
            "output_dir": output_dir,
            "name": body.name,
            "prompt": body.prompt,
        },
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.get("/toolkit/prompts")
async def toolkit_prompts(_: AuthUser = Depends(require_user)) -> dict:
    """Return the default system prompts for the toolkit tools.

    The desktop client shows these as the light-gray placeholder in the generation dialog;
    a user-supplied prompt is appended to the tool's default (see the toolkit pipeline).
    """
    from api.tools.toolkit.prompts import SYSTEM_PROMPTS

    return dict(SYSTEM_PROMPTS)


@router.get("/toolkit/config")
async def toolkit_config(_: AuthUser = Depends(require_user)) -> dict:
    """Surface the toolkit generation limits.

    The desktop client shows these (e.g. the per-file size cap, the supported formats) as
    hints in the picker / generate dialog so a user learns about a limit before submitting,
    not from a failed job.
    """
    from core.infrastructure.ingest import supported_extensions

    return {
        "max_file_bytes": settings.toolkit_max_file_bytes,
        "max_input_tokens": settings.toolkit_max_input_tokens,
        "supported_extensions": sorted(supported_extensions()),
    }


@router.post("/tts")
async def synthesize_audio(
    body: TTSRequest,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    job_id = await queue.enqueue(TTS, {"text": body.text}, user_id=user.user_id)
    return {"job_id": str(job_id)}


@router.post("/tts/stream")
async def synthesize_audio_stream(
    body: TTSRequest,
    user: AuthUser = Depends(require_user),
):
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
async def explain(
    body: ExplainRequest,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    job_id = await queue.enqueue(
        EXPLAIN, {"term": body.term, "context": body.context}, user_id=user.user_id
    )
    return {"job_id": str(job_id)}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: UUID,
    queue: TaskQueue = Depends(get_task_queue),
    user: AuthUser = Depends(require_user),
):
    """Return the state of an async enrichment job (single source of truth: the jobs table).

    Jobs are owner-scoped: a non-admin may only read their own jobs (404 otherwise, so a
    foreign UUID is indistinguishable from a missing one).
    """
    state = await queue.get(job_id)
    if state["status"] != "unknown":
        owner = state.get("user_id")
        is_admin = user.role.role_id == "admin"
        if not is_admin and owner is not None and owner != str(user.user_id):
            raise HTTPException(status_code=404, detail="job not found")
    return state
