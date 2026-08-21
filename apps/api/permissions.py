"""HTTP layer for drive access control: map DriveService assertions to FastAPI responses."""
from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from core.application.drive_service import DriveError, DriveService


def _http(e: DriveError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.message)


async def require_file_read(drive: DriveService, user_id: UUID, asset_id: UUID):
    try:
        return await drive.ensure_asset_readable(user_id, asset_id)
    except DriveError as e:
        raise _http(e) from e


async def require_file_write(drive: DriveService, user_id: UUID, asset_id: UUID):
    try:
        return await drive.ensure_asset_writable(user_id, asset_id)
    except DriveError as e:
        raise _http(e) from e


async def require_workspace_member(drive: DriveService, user_id: UUID, workspace_id: UUID):
    try:
        return await drive.ensure_workspace_member(user_id, workspace_id)
    except DriveError as e:
        raise _http(e) from e


async def require_workspace_owner(drive: DriveService, user_id: UUID, workspace_id: UUID):
    try:
        return await drive.ensure_workspace_owner(user_id, workspace_id)
    except DriveError as e:
        raise _http(e) from e
