"""Cloud-drive HTTP routes: workspaces, file upload lifecycle, download, sharing, RAG status.

All routes require a logged-in user (``require_user``); access to individual assets goes
through the visibility checks in :mod:`api.permissions`.
"""
from __future__ import annotations

import urllib.parse
from uuid import UUID

from api.auth import AuthUser, require_user
from api.deps import get_drive_service, get_task_queue
from api.permissions import (
    require_file_read,
    require_file_write,
    require_workspace_member,
    require_workspace_owner,
)
from api.schemas_drive import (
    ContentUpdate,
    FileRename,
    FolderCreate,
    FolderMoveRequest,
    FolderRename,
    InitUploadRequest,
    MemberAdd,
    MemberUpdate,
    MoveRequest,
    ShareRequest,
    WorkspaceCreate,
    WorkspaceRename,
)
from core.application.drive_service import DriveService
from core.infrastructure.jobs import ASSET_INGEST
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

router = APIRouter()
workspaces = APIRouter(prefix="/workspaces", tags=["workspaces"])
files = APIRouter(prefix="/files", tags=["files"])
folders = APIRouter(prefix="/folders", tags=["folders"])
trash = APIRouter(prefix="/trash", tags=["trash"])
users = APIRouter(prefix="/users", tags=["users"])


def _disposition(name: str) -> str:
    quoted = urllib.parse.quote(name)
    return f"attachment; filename*=UTF-8''{quoted}"


# ── Workspaces ───────────────────────────────────────────────────────────────────

@workspaces.post("", status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.create_workspace(user.user_id, body.name)


@workspaces.get("")
async def list_workspaces(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"workspaces": await drive.list_workspaces(user.user_id)}


@workspaces.patch("/{workspace_id}")
async def rename_workspace(
    workspace_id: UUID,
    body: WorkspaceRename,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_workspace_owner(drive, user.user_id, workspace_id)
    return await drive.rename_workspace(user.user_id, workspace_id, body.name)


@workspaces.delete("/{workspace_id}")
async def delete_workspace(
    workspace_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_workspace_owner(drive, user.user_id, workspace_id)
    return await drive.delete_workspace(user.user_id, workspace_id)


@workspaces.get("/{workspace_id}/members")
async def list_members(
    workspace_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_workspace_member(drive, user.user_id, workspace_id)
    return {"members": await drive.list_workspace_members(user.user_id, workspace_id)}


@workspaces.post("/{workspace_id}/members", status_code=201)
async def add_member(
    workspace_id: UUID,
    body: MemberAdd,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.add_workspace_member(
        user.user_id, workspace_id, body.user_id, body.role
    )


@workspaces.patch("/{workspace_id}/members/{member_id}")
async def update_member(
    workspace_id: UUID,
    member_id: UUID,
    body: MemberUpdate,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.update_workspace_member(
        user.user_id, workspace_id, member_id, body.role
    )


@workspaces.delete("/{workspace_id}/members/{member_id}")
async def remove_member(
    workspace_id: UUID,
    member_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.remove_workspace_member(user.user_id, workspace_id, member_id)


@workspaces.get("/{workspace_id}/activity")
async def list_activity(
    workspace_id: UUID,
    q: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Page the workspace audit trail; filters: fuzzy ``q`` (actor/target), ``start``/``end``."""
    await require_workspace_member(drive, user.user_id, workspace_id)
    return await drive.list_workspace_activity(
        user.user_id, workspace_id, q, start, end, limit, offset
    )


# ── User lookup (for adding members) ─────────────────────────────────────────────

@users.get("/search")
async def search_users(
    q: str,
    limit: int = 10,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Find active users by username or user-id so members can be added by name."""
    return {"users": await drive.search_users(q, limit)}


# ── File upload lifecycle ────────────────────────────────────────────────────────

@files.post("/init-upload")
async def init_upload(
    body: InitUploadRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.init_upload(
        user.user_id,
        body.sha256,
        body.size,
        body.name,
        body.folder_path,
        body.mime_type,
        body.workspace_id,
    )


@files.get("")
async def list_files(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"files": await drive.list_files(user.user_id)}


@files.get("/{asset_id}")
async def get_file(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_file_read(drive, user.user_id, asset_id)
    return await drive.get_file(user.user_id, asset_id)


@files.put("/{asset_id}/chunks/{index}")
async def put_chunk(
    asset_id: UUID,
    index: int,
    request: Request,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    data = await request.body()
    await drive.store_chunk(user.user_id, asset_id, index, data)
    return {"ok": True, "index": index}


@files.get("/{asset_id}/chunks")
async def get_chunk_status(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.chunk_status(user.user_id, asset_id)


@files.post("/{asset_id}/complete")
async def complete_upload(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    result = await drive.complete_upload(user.user_id, asset_id)
    # No auto-import: the file lands at rag_status=NOT_STARTED and the user decides when
    # to push it into the query repository via the cloud-drive "Import to Knowledge" button.
    return result


@files.post("/{asset_id}/abort")
async def abort_upload(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await drive.abort_upload(user.user_id, asset_id)
    return {"aborted": True}


@files.get("/{asset_id}/download")
async def download(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_file_read(drive, user.user_id, asset_id)
    mime, name, data = await drive.download(user.user_id, asset_id)
    if data is None:
        return Response(status_code=404)
    return StreamingResponse(
        _stream(data),
        media_type=mime,
        headers={"Content-Disposition": _disposition(name), "Content-Length": str(len(data))},
    )


@files.get("/{asset_id}/content")
async def read_content(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Return a text note's content (.md/.txt/…) as JSON."""
    await require_file_read(drive, user.user_id, asset_id)
    return {"content": await drive.read_text(user.user_id, asset_id)}


@files.put("/{asset_id}/content")
async def update_content(
    asset_id: UUID,
    body: ContentUpdate,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
    queue=Depends(get_task_queue),
):
    """Overwrite a text note's content.

    A content change is the incremental-reindex trigger: the asset resets to
    rag_status=NOT_STARTED (its previous import is now stale) and ``asset_ingest`` is
    auto-enqueued so the query repository stays fresh. An identical rewrite (no-op) is
    left untouched.
    """
    await require_file_write(drive, user.user_id, asset_id)
    result = await drive.update_content(user.user_id, asset_id, body.content)
    asset = result["asset"]
    response: dict = {"asset": asset, "content_changed": result["content_changed"]}
    if result["content_changed"]:
        job_id = await queue.enqueue(
            ASSET_INGEST,
            {"asset_id": asset["id"], "user_id": str(user.user_id)},
            user_id=user.user_id,
        )
        # Mark pending only after the enqueue succeeded (a failed enqueue leaves
        # NOT_STARTED so the user can retry) — mirrors the manual import-rag flow.
        await drive.mark_rag_pending(asset_id)
        response["job_id"] = str(job_id)
        response["rag_status"] = "queued"
    return response


def _stream(data: bytes):
    chunk = 256 * 1024
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


@files.patch("/{asset_id}")
async def rename_file(
    asset_id: UUID,
    body: FileRename,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_file_write(drive, user.user_id, asset_id)
    return await drive.rename_file(user.user_id, asset_id, body.name, body.folder_path)


@files.delete("/{asset_id}")
async def delete_file(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    await require_file_write(drive, user.user_id, asset_id)
    return await drive.delete_asset(user.user_id, asset_id)


@files.post("/{asset_id}/move")
async def move_file(
    asset_id: UUID,
    body: MoveRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.move_file(
        user.user_id, asset_id, body.workspace_id, body.folder_path
    )


# ── Sharing (asset ACL) ──────────────────────────────────────────────────────────

@files.post("/{asset_id}/share")
async def share_file(
    asset_id: UUID,
    body: ShareRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.share_asset(
        user.user_id, asset_id, body.grantee_user_id, body.permission
    )


@files.delete("/{asset_id}/share/{grantee}")
async def unshare_file(
    asset_id: UUID,
    grantee: str,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    grantee_id = None if grantee == "public" else UUID(grantee)
    return await drive.unshare_asset(user.user_id, asset_id, grantee_id)


@files.get("/{asset_id}/shares")
async def list_shares(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"shares": await drive.list_asset_shares(user.user_id, asset_id)}


@files.get("/{asset_id}/ingest-status")
async def ingest_status(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.ingest_status(user.user_id, asset_id)


@files.post("/{asset_id}/import-rag")
async def import_rag(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
    queue=Depends(get_task_queue),
):
    """(Re)push a READY asset into the RAG query repository.

    Import is manual-only: uploads never auto-enqueue. This is the entry the cloud-drive
    "Import to Knowledge" button calls. Rebuilding a chunk set is idempotent
    (delete-by-asset + bulk insert), so calling it on an already-indexed file is safe.
    """
    await require_file_read(drive, user.user_id, asset_id)
    asset = await drive.get_file(user.user_id, asset_id)
    if asset.get("file_status") != "READY" or not asset.get("object_sha256"):
        raise HTTPException(status_code=400, detail="asset is not ready for ingestion")
    job_id = await queue.enqueue(
        ASSET_INGEST,
        {"asset_id": asset["id"], "user_id": str(user.user_id)},
        user_id=user.user_id,
    )
    # Mark pending only after the enqueue succeeded (a failed enqueue leaves NOT_STARTED
    # so the user can retry) — the frontend polls WORKING rag states, so this flips the
    # button to "Queued…"/"Processing…" instead of reverting to "Import to Knowledge".
    await drive.mark_rag_pending(asset_id)
    return {"job_id": str(job_id), "rag_status": "queued"}


# ── Folders ───────────────────────────────────────────────────────────────────────

@folders.get("")
async def list_folders(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"folders": await drive.list_folders(user.user_id)}


@folders.post("", status_code=201)
async def create_folder(
    body: FolderCreate,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.create_folder(
        user.user_id, body.workspace_id, body.parent_path, body.name
    )


@folders.patch("/{folder_id}")
async def rename_folder(
    folder_id: UUID,
    body: FolderRename,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.rename_folder(user.user_id, folder_id, body.name)


@folders.post("/{folder_id}/move")
async def move_folder(
    folder_id: UUID,
    body: FolderMoveRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.move_folder(user.user_id, folder_id, body.parent_path)


@folders.delete("/{folder_id}")
async def delete_folder(
    folder_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.delete_folder(user.user_id, folder_id)


# ── Trash ─────────────────────────────────────────────────────────────────────────

@trash.get("")
async def list_trash(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return {"files": await drive.list_trash(user.user_id)}


@trash.post("/{asset_id}/restore")
async def restore_trash(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.restore_trash(user.user_id, asset_id)


@trash.delete("/{asset_id}")
async def purge_trash(
    asset_id: UUID,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.purge_trash(user.user_id, asset_id)


@trash.delete("")
async def empty_trash(
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    return await drive.empty_trash(user.user_id)
