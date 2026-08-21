"""Request/response models for the cloud-drive router."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class InitUploadRequest(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size: int = Field(ge=0)
    name: str
    folder_path: str | None = None
    mime_type: str | None = None
    workspace_id: UUID | None = None


class WorkspaceCreate(BaseModel):
    name: str


class WorkspaceRename(BaseModel):
    name: str


class MemberAdd(BaseModel):
    user_id: UUID
    role: str = "viewer"  # 'admin' | 'editor' | 'viewer'


class MemberUpdate(BaseModel):
    role: str


class ShareRequest(BaseModel):
    grantee_user_id: UUID | None = None  # None = public link
    permission: str = "read"  # 'read' | 'write'


class FileRename(BaseModel):
    name: str | None = None
    folder_path: str | None = None


class FolderCreate(BaseModel):
    name: str
    parent_path: str | None = None  # folder this one is created inside (''/None = root)
    workspace_id: UUID | None = None  # None = My Drive


class FolderRename(BaseModel):
    name: str


class MoveRequest(BaseModel):
    workspace_id: UUID | None = None  # None = My Drive
    folder_path: str | None  # required; null = workspace root
