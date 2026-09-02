"""Research task API: read-only task monitor + atomic create + idempotent Promote.

Tasks are the unit of the chat-driven Research workflow: a user hits ``+ Research`` in the
desktop chat, the client POSTs ``/research/tasks`` once, and the server creates the task
folder (``materials/`` / ``outputs/`` / ``task_spec.json`` / ``session_history.json``) and
copies the selected cloud-drive materials atomically in that single request. The console is
read-only for everything else: stage transitions, gate overrides, scratch writes, and new
artifact versions are driven *only* by the agent through the six research tools under the
``deep_research`` skill.

Tenancy: every path derives from ``user.user_id`` (a client-supplied owner is never
trusted). Task/asset ids are resolved by :class:`ResearchService` against the owner's
scratch root, so a ``..`` / absolute segment escapes with ``ValueError`` (→ 404), and a
material that the caller cannot read surfaces ``DriveError`` 403/404 from ``drive.download``.
Promote carries the stable idempotency key ``research:{task_id}:{artifact_id}:{version}``
so a duplicate POST is a safe no-op.
"""
from __future__ import annotations

import logging
import uuid

from api.auth import AuthUser, require_user
from api.deps import get_drive_service
from api.schemas_research import TaskCreateRequest
from core.application.drive_service import DriveError, DriveService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.memory import create_session
from fastapi import APIRouter, Depends, HTTPException
from plugins.research.plugin import ResearchService

router = APIRouter(prefix="/research", tags=["research"])

logger = logging.getLogger(__name__)


def _service(drive: DriveService) -> ResearchService:
    # Request-local construction: ResearchService is a thin, file-backed service, so building
    # one per request is cheap and keeps no process-wide mutable state. The DriveService
    # singleton is reused via ``get_drive_service``.
    return ResearchService(drive, settings.research_scratch_dir)


def _http_error(exc: Exception) -> HTTPException:
    """Map a domain error to the right HTTP status: 404 for missing/traversal, else 403/409."""
    if isinstance(exc, DriveError):
        return HTTPException(status_code=exc.status_code, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _not_found(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


async def _make_task_session(user_id: uuid.UUID, title: str) -> str | None:
    """Create the task's dedicated chat session row (one task, one session).

    Best-effort: a DB hiccup leaves the task created without a session (``None``); the client
    then opens a fresh chat whose first message binds it via the research handoff.
    """
    try:
        return str(await create_session(SessionLocal, user_id, title=title))
    except Exception:  # noqa: BLE001 — the task itself is still valid without a session
        logger.exception("research: failed to create the task's chat session")
        return None


@router.get("/tasks")
async def list_tasks(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"tasks": _service(drive).list_tasks(user.user_id)}


@router.post("/tasks", status_code=201)
async def create_task(
    body: TaskCreateRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    try:
        created = await _service(drive).create_task(
            user.user_id,
            title=body.title,
            description=body.description,
            parent_folder_path=body.parent_folder_path,
            material_asset_ids=body.material_asset_ids,
        )
        # Bind the task's dedicated chat session at creation (1:1). The session is a different
        # kind than a normal chat: it is hidden from the Sessions sidebar (the bound_session_ids
        # filter), opened silently when the user selects the task, and a typed run instruction
        # in it drives the task. Reusing the same session on every open never forks a new one.
        session_id = await _make_task_session(user.user_id, created["name"])
        if session_id:
            _service(drive).bind_session(user.user_id, created["task_id"], session_id)
            created["session_id"] = session_id
        return created
    except Exception as exc:
        raise _http_error(exc)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    try:
        return await _service(drive).get_task_status(user.user_id, task_id)
    except ValueError as exc:
        raise _not_found(exc)


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Cascade-delete a research task: cloud task folder → Trash, scratch state removed.

    409 Conflict when the task is RUNNING or its report is already indexed by RAG; 404 for a
    missing task / owner traversal. ``delete_task`` raises ``ValueError`` for all three and the
    message discriminates 409 (guard) from 404 (not found).
    """
    try:
        return await _service(drive).delete_task(user.user_id, task_id)
    except ValueError as exc:
        if "currently running" in str(exc) or "Knowledge Base" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc))
        raise _not_found(exc)
    except DriveError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def get_artifact(
    task_id: str,
    artifact_id: str,
    version: int | None = None,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    try:
        return _service(drive).read_artifact(
            user.user_id, task_id, artifact_id=artifact_id, version=version
        )
    except ValueError as exc:
        raise _not_found(exc)


@router.post("/tasks/{task_id}/artifacts/{artifact_id}/promote", status_code=201)
async def promote_artifact(
    task_id: str,
    artifact_id: str,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    service = _service(drive)
    try:
        version = service.read_artifact(
            user.user_id, task_id, artifact_id=artifact_id
        )["version"]
    except ValueError as exc:
        raise _not_found(exc)
    key = f"research:{task_id}:{artifact_id}:{version}"
    try:
        return await service.promote_to_drive(
            user.user_id,
            task_id,
            artifact_id=artifact_id,
            promote_idempotency_key=key,
        )
    except (ValueError, DriveError) as exc:
        raise _not_found(exc)
