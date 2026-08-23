"""Cloud-drive application services: upload, dedup, ref-counted delete, sharing.

DriveService owns the cross-cutting rules:
- instant upload (秒传) when the SHA-256 already exists in the global object store;
- chunked upload state (resume) + assembly + full-hash verification on complete;
- soft-delete of logical assets with atomic ref_count decrement and CAS physical retire;
- owner / workspace-member / ACL visibility and write checks.

Repositories are thin data access; :class:`~core.infrastructure.storage.Storage` is the
physical byte store. The worker's ``asset_ingest`` job consumes the READY asset afterwards.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from math import ceil
from uuid import UUID

from core.config import settings
from core.infrastructure.drive_repositories import (
    RepositoryConflict,
    SqlActivityRepository,
    SqlAssetAclRepository,
    SqlAssetRepository,
    SqlChunkRepository,
    SqlFolderRepository,
    SqlGlobalObjectRepository,
    SqlUploadSessionRepository,
    SqlUserRepository,
    SqlWorkspaceRepository,
)
from core.infrastructure.storage import Storage, get_storage, object_key

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Files treated as editable text notes (markdown / plain text / code / data).
_TEXT_EXT_RE = re.compile(
    r"\.(txt|md|markdown|text|log|json|csv|yaml|yml|toml|ini|xml|html|py|js|ts|jsx|tsx|c|h|cpp|hpp|java|go|rs|sh|bat|sql)$",
    re.IGNORECASE,
)

# File lifecycle.
UPLOADING = "UPLOADING"
PROCESSING = "PROCESSING"
READY = "READY"
DELETED = "DELETED"

# RAG ingest lifecycle.
RAG_PENDING = "PENDING"
RAG_INDEXED = "INDEXED"

# Trash retention: items older than this are permanently purged (lazily, on list_trash).
TRASH_RETENTION_DAYS = 30


class DriveError(Exception):
    """Domain error mapped to an HTTP response by the API layer."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class DriveService:
    def __init__(
        self,
        session_factory,
        storage: Storage | None = None,
        objects=None,
        assets=None,
        uploads=None,
        acl=None,
        chunks=None,
        workspaces=None,
        folders=None,
        users=None,
        logs=None,
    ) -> None:
        self.storage = storage or get_storage()
        self.objects = objects or SqlGlobalObjectRepository(session_factory)
        self.assets = assets or SqlAssetRepository(session_factory)
        self.uploads = uploads or SqlUploadSessionRepository(session_factory)
        self.acl = acl or SqlAssetAclRepository(session_factory)
        self.chunks = chunks or SqlChunkRepository(session_factory)
        self.workspaces = workspaces or SqlWorkspaceRepository(session_factory)
        self.folders = folders or SqlFolderRepository(session_factory)
        self.users = users or SqlUserRepository(session_factory)
        self.logs = logs or SqlActivityRepository(session_factory)

    # ── Access control ──────────────────────────────────────────────────────────

    async def ensure_asset_readable(self, user_id: UUID, asset_id: UUID):
        asset = await self.assets.get_active(asset_id)
        if asset is None:
            raise DriveError("asset not found", 404)
        if not await self._can_read(user_id, asset):
            raise DriveError("no access to asset", 403)
        return asset

    async def ensure_asset_writable(self, user_id: UUID, asset_id: UUID):
        asset = await self.assets.get_active(asset_id)
        if asset is None:
            raise DriveError("asset not found", 404)
        if not await self._can_write(user_id, asset):
            raise DriveError("no write access to asset", 403)
        return asset

    async def ensure_workspace_member(self, user_id: UUID, workspace_id: UUID):
        ws = await self.workspaces.get(workspace_id)
        if ws is None:
            raise DriveError("workspace not found", 404)
        if ws.owner_id != user_id and not await self.workspaces.member_role(workspace_id, user_id):
            raise DriveError("not a workspace member", 403)
        return ws

    async def ensure_workspace_owner(self, user_id: UUID, workspace_id: UUID):
        ws = await self.workspaces.get(workspace_id)
        if ws is None:
            raise DriveError("workspace not found", 404)
        if ws.owner_id != user_id:
            raise DriveError("workspace owner required", 403)
        return ws

    async def ensure_workspace_manager(self, user_id: UUID, workspace_id: UUID):
        """Owner or ``admin`` role: may manage members and view the audit log."""
        ws = await self.workspaces.get(workspace_id)
        if ws is None:
            raise DriveError("workspace not found", 404)
        if ws.owner_id != user_id and await self.workspaces.member_role(workspace_id, user_id) != "admin":
            raise DriveError("workspace admin or owner required", 403)
        return ws

    async def _can_read(self, user_id: UUID, asset) -> bool:
        if asset.user_id == user_id:
            return True
        if asset.workspace_id and await self.workspaces.member_role(asset.workspace_id, user_id):
            return True
        return await self.acl.permission_for(asset.id, user_id) is not None

    async def _can_write(self, user_id: UUID, asset) -> bool:
        if asset.user_id == user_id:
            return True
        if asset.workspace_id:
            role = await self.workspaces.member_role(asset.workspace_id, user_id)
            if role in ("owner", "admin", "editor"):
                return True
        return await self.acl.permission_for(asset.id, user_id) == "write"

    # ── Audit trail ─────────────────────────────────────────────────────────────

    async def _log(
        self,
        actor_id: UUID | None,
        workspace_id: UUID | None,
        action: str,
        target_type: str,
        target_id,
        target_name: str | None,
        detail: str = "",
        *,
        actor_username: str | None = None,
    ) -> None:
        """Record one drive mutation in the workspace audit trail.

        Resolves the actor's username lazily (unless a system entry passes it explicitly).
        The log is best-effort: a failure here must never break the underlying operation.
        """
        try:
            if actor_username is None:
                user = await self.users.get(actor_id)
                actor_username = user.username if user else None
            await self.logs.add(
                actor_user_id=actor_id,
                actor_username=actor_username,
                workspace_id=workspace_id,
                action=action,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                target_name=target_name,
                detail=detail or None,
            )
        except Exception:
            pass

    @staticmethod
    def _parse_iso(value: str, field: str) -> datetime:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise DriveError(f"invalid {field} timestamp: {value}", 400)
        if dt.tzinfo is None:  # date-only ("2026-08-01") means UTC midnight
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    # ── Upload lifecycle ────────────────────────────────────────────────────────

    async def init_upload(
        self,
        user_id: UUID,
        sha256: str,
        size: int,
        name: str,
        folder_path: str | None,
        mime_type: str | None,
        workspace_id: UUID | None = None,
    ) -> dict:
        if not _SHA256_RE.match(sha256):
            raise DriveError("invalid sha256", 400)
        if size < 0:
            raise DriveError("invalid size", 400)
        if workspace_id is not None:
            await self.ensure_workspace_member(user_id, workspace_id)

        # Folders and files share one name space per directory — auto-suffix busy names.
        name = await self._unique_name(user_id, workspace_id, folder_path, name)

        existing = await self.objects.get(sha256)
        if existing is not None:
            # 秒传: physical bytes already present — bump ref_count, mark asset READY.
            await self.objects.upsert_and_increment(
                sha256, existing.size, existing.storage_key, existing.mime_type
            )
            asset = await self.assets.create(
                user_id,
                name,
                workspace_id=workspace_id,
                folder_path=folder_path,
                mime_type=mime_type,
                size=existing.size,
                object_sha256=sha256,
                file_status=READY,
                rag_status=RAG_PENDING,
            )
            await self._log(
                user_id, workspace_id, "file.create", "file", asset.id, name,
                f"uploaded (dedup) to {folder_path or 'root'}",
            )
            return {"status": "instant", "dedup": True, "asset": self._asset_dict(asset)}

        chunk_size = settings.drive_chunk_size
        num_chunks = max(1, ceil(size / chunk_size)) if size > 0 else 1
        asset = await self.assets.create(
            user_id,
            name,
            workspace_id=workspace_id,
            folder_path=folder_path,
            mime_type=mime_type,
            size=size,
            file_status=UPLOADING,
            rag_status=RAG_PENDING,
        )
        await self._log(
            user_id, workspace_id, "file.create", "file", asset.id, name,
            f"upload started ({size} bytes) to {folder_path or 'root'}",
        )
        session = await self.uploads.create(
            user_id, asset.id, sha256.lower(), size, chunk_size, num_chunks
        )
        return {
            "status": "uploading",
            "asset_id": str(asset.id),
            "session_id": str(session.id),
            "chunk_size": chunk_size,
            "num_chunks": num_chunks,
            "received": [],
        }

    async def chunk_status(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        session = await self.uploads.get_by_asset(asset.id)
        if session is None:
            raise DriveError("no upload session for asset", 404)
        received = [i for i, ok in enumerate(session.received_chunks or []) if ok]
        missing = [
            i for i in range(session.num_chunks) if i not in received
        ]
        return {
            "session_id": str(session.id),
            "received": received,
            "missing": missing,
            "chunk_size": session.chunk_size,
            "num_chunks": session.num_chunks,
        }

    async def store_chunk(self, user_id: UUID, asset_id: UUID, index: int, data: bytes) -> None:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        session = await self.uploads.get_by_asset(asset.id)
        if session is None or session.status in ("completed", "aborted"):
            raise DriveError("upload session not open", 409)
        if not 0 <= index < session.num_chunks:
            raise DriveError(f"chunk index out of range: {index}", 400)
        if len(data) > session.chunk_size:
            raise DriveError("chunk larger than chunk_size", 400)

        path = self.storage.upload_chunk_path(session.id, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        await self.uploads.mark_chunk(session.id, index)

    async def complete_upload(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        session = await self.uploads.get_by_asset(asset.id)
        if session is None:
            raise DriveError("no upload session for asset", 404)

        # Atomic guard: only the first complete proceeds; the second sees status=completed.
        if not await self.uploads.complete(session.id):
            current = await self.assets.get_active(asset_id)
            return {"asset": self._asset_dict(current) if current else None}

        data = self._assemble(session)
        digest = hashlib.sha256(data).hexdigest()
        if digest != session.sha256:
            await self.uploads.set_status(session.id, "failed")
            raise DriveError(
                "sha256 mismatch: client re-uploads the bad chunk and completes again", 400
            )

        storage_key = object_key(digest)
        await self.storage.put(storage_key, data)
        await self.objects.upsert_and_increment(
            digest, len(data), storage_key, asset.mime_type
        )
        await self.assets.set_object(asset.id, digest)
        ready = await self.assets.set_status(asset.id, file_status=READY, rag_status=RAG_PENDING)
        return {"asset": self._asset_dict(ready) if ready else None}

    def _assemble(self, session) -> bytes:
        received = [bool(x) for x in (session.received_chunks or [])]
        if len(received) != session.num_chunks or not all(received):
            raise DriveError("incomplete upload; missing chunks", 400)
        parts = []
        for i in range(session.num_chunks):
            parts.append(
                self.storage.upload_chunk_path(session.id, i).read_bytes()
            )
        data = b"".join(parts)
        if len(data) != session.size:
            raise DriveError(
                f"assembled size {len(data)} != expected {session.size}", 400
            )
        return data

    async def abort_upload(self, user_id: UUID, asset_id: UUID) -> None:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        session = await self.uploads.get_by_asset(asset.id)
        if session is not None and session.status not in ("completed", "aborted"):
            await self.uploads.set_status(session.id, "aborted")

    # ── File metadata / delete / download ───────────────────────────────────────

    async def list_files(self, user_id: UUID) -> list[dict]:
        return [self._asset_dict(a) for a in await self.assets.list_visible(user_id)]

    async def get_file(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.ensure_asset_readable(user_id, asset_id)
        return self._asset_dict(asset)

    async def rename_file(
        self, user_id: UUID, asset_id: UUID, name: str | None = None, folder_path: str | None = None
    ) -> dict:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        old_name = asset.name
        old_folder = asset.folder_path
        new_name = name if name is not None else old_name
        new_folder = self._validate_folder_path(folder_path) if folder_path is not None else old_folder
        if new_name != old_name or new_folder != old_folder:
            # Auto-suffix if the target directory already holds a folder/file with this name.
            new_name = await self._unique_name(user_id, asset.workspace_id, new_folder, new_name)
        updated = await self.assets.update(asset.id, name=new_name, folder_path=new_folder)
        changes = []
        if new_name != old_name:
            changes.append(f"name: {old_name} -> {new_name}")
        if new_folder != old_folder:
            changes.append(f"folder: {old_folder or '(root)'} -> {new_folder or '(root)'}")
        await self._log(
            user_id, asset.workspace_id, "file.rename", "file", asset.id,
            updated.name if updated else old_name, "; ".join(changes),
        )
        return self._asset_dict(updated) if updated else self._asset_dict(asset)

    async def delete_asset(self, user_id: UUID, asset_id: UUID) -> dict:
        """Move an asset to the trash (soft delete). Physical bytes + ref_count are kept.

        Permanent removal happens only via ``purge_trash`` / ``empty_trash`` or the lazy
        retention sweep, so a trashed file can still be restored.
        """
        asset = await self.ensure_asset_writable(user_id, asset_id)
        deleted = await self.assets.soft_delete(asset.id)
        if deleted is None:
            raise DriveError("asset not found", 404)
        await self._log(
            user_id, asset.workspace_id, "file.delete", "file", asset.id, asset.name,
            "moved to trash",
        )
        return {"deleted": True, "physical_removed": False}

    async def _purge_asset(self, asset) -> dict:
        """Permanently remove an asset and its share of the physical object.

        The asset row must be dropped BEFORE the global object row: ``assets.object_sha256``
        is a foreign key to ``global_objects``, so retiring the object while the asset still
        points at it would violate the FK. Order: decrement → drop the asset row → retire the
        object at 0 (CAS keeps it if a concurrent upload re-incremented).
        """
        removed_storage_key = None
        if asset.object_sha256:
            new_count = await self.objects.decrement(asset.object_sha256)
            await self.assets.hard_delete(asset.id)
            if new_count is not None and new_count <= 0:
                removed_storage_key = await self.objects.delete_if_zero(asset.object_sha256)
        else:
            await self.assets.hard_delete(asset.id)
        if removed_storage_key:
            await self.storage.delete(removed_storage_key)
        return {"purged": True, "physical_removed": removed_storage_key is not None}

    async def download(self, user_id: UUID, asset_id: UUID) -> tuple[str, str, bytes | None]:
        asset = await self.ensure_asset_readable(user_id, asset_id)
        if asset.file_status != READY or not asset.object_sha256:
            raise DriveError("asset not ready for download", 409)
        data = await self.storage.get(object_key(asset.object_sha256))
        return asset.mime_type or "application/octet-stream", asset.name, data

    # ── Text notes (read / in-place update) ───────────────────────────────────

    @staticmethod
    def _is_text_asset(asset) -> bool:
        mime = (asset.mime_type or "").lower()
        if mime.startswith("text/"):
            return True
        return bool(_TEXT_EXT_RE.search(asset.name or ""))

    async def read_text(self, user_id: UUID, asset_id: UUID) -> str:
        """Return the UTF-8 text content of a note (.md/.txt/…)."""
        asset = await self.ensure_asset_readable(user_id, asset_id)
        if asset.file_status != READY or not asset.object_sha256:
            raise DriveError("asset not ready", 409)
        if not self._is_text_asset(asset):
            raise DriveError("asset is not a text file", 415)
        data = await self.storage.get(object_key(asset.object_sha256))
        if data is None:
            raise DriveError("object bytes missing", 404)
        return data.decode("utf-8", errors="replace")

    async def update_content(self, user_id: UUID, asset_id: UUID, content: str) -> dict:
        """Overwrite a note's text in place and re-point it at a deduplicated object.

        Mirrors :meth:`complete_upload` for the byte-store half (put + ref-count),
        then retires the old object when its ref_count drops to zero. The router
        re-enqueues ``ASSET_INGEST`` so RAG chunks are rebuilt for the new text.
        """
        asset = await self.ensure_asset_writable(user_id, asset_id)
        if not self._is_text_asset(asset):
            raise DriveError("asset is not a text file", 415)

        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()

        if asset.object_sha256 == digest:
            # Content unchanged — nothing to do, keep the asset as-is.
            await self._log(
                user_id, asset.workspace_id, "file.update", "file", asset.id, asset.name,
                "content updated (unchanged)",
            )
            return self._asset_dict(asset)

        storage_key = object_key(digest)
        await self.storage.put(storage_key, data)
        await self.objects.upsert_and_increment(
            digest, len(data), storage_key, asset.mime_type or "text/plain"
        )

        updated = await self.assets.set_content_meta(
            asset.id, digest, len(data), asset.mime_type or "text/plain"
        )
        # FK order: repoint the asset to the new object before retiring the old one.
        if asset.object_sha256 and asset.object_sha256 != digest:
            new_count = await self.objects.decrement(asset.object_sha256)
            if new_count is not None and new_count <= 0:
                removed_key = await self.objects.delete_if_zero(asset.object_sha256)
                if removed_key:
                    await self.storage.delete(removed_key)
        await self.assets.set_status(asset.id, file_status=READY, rag_status=RAG_PENDING)
        await self._log(
            user_id, asset.workspace_id, "file.update", "file", asset.id, asset.name,
            "content updated",
        )
        return self._asset_dict(updated)

    # ── Workspaces ──────────────────────────────────────────────────────────────

    async def create_workspace(self, user_id: UUID, name: str) -> dict:
        ws = await self.workspaces.create(user_id, name)
        await self._log(
            user_id, ws.id, "workspace.create", "workspace", ws.id, ws.name,
            "workspace created",
        )
        return {"id": str(ws.id), "name": ws.name, "owner_id": str(ws.owner_id)}

    async def list_workspaces(self, user_id: UUID) -> list[dict]:
        # Include the requesting user's role so the web console can gray out
        # write/manage actions the user is not allowed to perform.
        result = []
        for ws in await self.workspaces.list_for_user(user_id):
            role = (
                "owner"
                if ws.owner_id == user_id
                else (await self.workspaces.member_role(ws.id, user_id)) or "viewer"
            )
            ident = await self._user_identity(ws.owner_id)
            result.append(
                {
                    "id": str(ws.id), "name": ws.name, "owner_id": str(ws.owner_id),
                    "role": role, "owner_username": ident["username"],
                    "owner_display_name": ident["display_name"],
                }
            )
        return result

    async def rename_workspace(self, user_id: UUID, workspace_id: UUID, name: str) -> dict:
        ws = await self.ensure_workspace_owner(user_id, workspace_id)
        old = ws.name
        updated = await self.workspaces.rename(workspace_id, name)
        await self._log(
            user_id, workspace_id, "workspace.rename", "workspace", workspace_id,
            updated.name if updated else name, f"{old} -> {name}",
        )
        return {"id": str(workspace_id), "name": updated.name if updated else name,
                "owner_id": str(ws.owner_id)}

    async def delete_workspace(self, user_id: UUID, workspace_id: UUID) -> dict:
        ws = await self.ensure_workspace_owner(user_id, workspace_id)
        # Trash every asset (bytes kept so they can be restored), detach them from the
        # workspace so its row can be dropped, then delete it (members + folders cascade).
        for asset in await self.assets.list_by_workspace(workspace_id):
            await self.assets.soft_delete(asset.id)
        await self.assets.nullify_workspace(workspace_id)
        await self.workspaces.delete(workspace_id)
        # No FK on workspace_activity.workspace_id, so this entry survives the deletion.
        await self._log(
            user_id, workspace_id, "workspace.delete", "workspace", workspace_id, ws.name,
            "workspace deleted; files moved to trash",
        )
        return {"deleted": True, "workspace_id": str(ws.id)}

    async def list_workspace_members(self, user_id: UUID, workspace_id: UUID) -> list[dict]:
        await self.ensure_workspace_member(user_id, workspace_id)
        members = []
        for m in await self.workspaces.list_members(workspace_id):
            ident = await self._user_identity(m.user_id)
            members.append(
                {"user_id": str(m.user_id), "role": m.role, **ident}
            )
        return members

    async def _user_identity(self, user_id: UUID) -> dict:
        """Resolve a user's username + display name (used in member/owner listings)."""
        u = await self.users.get(user_id)
        if u is None:
            return {"username": None, "display_name": None}
        return {
            "username": getattr(u, "username", None),
            "display_name": getattr(u, "display_name", None),
        }

    async def search_users(self, q: str, limit: int = 10) -> list[dict]:
        """Find users by username or user-id, for adding them to a workspace."""
        q = (q or "").strip()
        if not q:
            return []
        return await self.users.search(q, max(1, min(limit, 50)))

    async def list_workspace_activity(
        self,
        user_id: UUID,
        workspace_id: UUID,
        q: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Page the workspace audit trail (admin or owner only), newest first."""
        await self.ensure_workspace_manager(user_id, workspace_id)
        start_dt = self._parse_iso(start, "start") if start else None
        end_dt = self._parse_iso(end, "end") if end else None
        total, rows = await self.logs.list(
            workspace_id,
            q=q or None,
            start=start_dt,
            end=end_dt,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        return {"total": total, "items": [self._activity_dict(r) for r in rows]}

    # Valid workspace member roles. The owner is not a member row (it lives on
    # workspaces.owner_id); admin/editor/viewer are the assignable roles.
    _MEMBER_ROLES = {"admin", "editor", "viewer"}

    async def _is_owner(self, user_id: UUID, workspace_id: UUID) -> bool:
        ws = await self.workspaces.get(workspace_id)
        return ws is not None and ws.owner_id == user_id

    async def _assert_can_assign_role(self, user_id: UUID, workspace_id: UUID, role: str, member_user_id: UUID | None = None) -> None:
        """Only the owner may grant the admin role or change/remove an existing admin.

        An admin can add members and edit editor/viewer roles, but must never create
        another admin or modify an existing one.
        """
        if await self._is_owner(user_id, workspace_id):
            return
        if role == "admin":
            raise DriveError("only the workspace owner can assign the admin role", 403)
        if member_user_id is not None:
            current = await self.workspaces.member_role(workspace_id, member_user_id)
            if current == "admin":
                raise DriveError("only the workspace owner can manage admin members", 403)

    async def add_workspace_member(
        self, user_id: UUID, workspace_id: UUID, member_user_id: UUID, role: str
    ) -> dict:
        await self.ensure_workspace_manager(user_id, workspace_id)
        if role not in self._MEMBER_ROLES:
            raise DriveError(f"invalid role '{role}'", 400)
        await self._assert_can_assign_role(user_id, workspace_id, role)
        if await self.workspaces.member_role(workspace_id, member_user_id):
            raise DriveError("user is already a member", 409)
        await self.workspaces.add_member(workspace_id, member_user_id, role)
        member = await self.users.get(member_user_id)
        await self._log(
            user_id, workspace_id, "member.add", "member", member_user_id,
            member.username if member else str(member_user_id), f"role: {role}",
        )
        return {"user_id": str(member_user_id), "role": role}

    async def update_workspace_member(
        self, user_id: UUID, workspace_id: UUID, member_user_id: UUID, role: str
    ) -> dict:
        await self.ensure_workspace_manager(user_id, workspace_id)
        if role not in self._MEMBER_ROLES:
            raise DriveError(f"invalid role '{role}'", 400)
        old_role = await self.workspaces.member_role(workspace_id, member_user_id)
        if old_role is None:
            raise DriveError("member not found", 404)
        await self._assert_can_assign_role(user_id, workspace_id, role, member_user_id)
        await self.workspaces.update_member(workspace_id, member_user_id, role)
        member = await self.users.get(member_user_id)
        await self._log(
            user_id, workspace_id, "member.update", "member", member_user_id,
            member.username if member else str(member_user_id),
            f"role: {old_role} -> {role}",
        )
        return {"user_id": str(member_user_id), "role": role}

    async def remove_workspace_member(
        self, user_id: UUID, workspace_id: UUID, member_user_id: UUID
    ) -> dict:
        await self.ensure_workspace_manager(user_id, workspace_id)
        old_role = await self.workspaces.member_role(workspace_id, member_user_id)
        if old_role is None:
            raise DriveError("member not found", 404)
        await self._assert_can_assign_role(user_id, workspace_id, "editor", member_user_id)
        await self.workspaces.remove_member(workspace_id, member_user_id)
        member = await self.users.get(member_user_id)
        await self._log(
            user_id, workspace_id, "member.remove", "member", member_user_id,
            member.username if member else str(member_user_id), "removed from workspace",
        )
        return {"removed": True}

    # ── Folders ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_folder_path(path: str | None) -> str | None:
        """Clean a folder path: allow nested segments, reject ``..`` and edge slashes."""
        if path is None:
            return None
        if any(seg == ".." for seg in path.split("/")):
            raise DriveError("invalid path segment '..'", 400)
        if path.startswith("/") or path.endswith("/"):
            raise DriveError("folder path must not start or end with '/'", 400)
        return path

    async def _name_taken(
        self, user_id: UUID, workspace_id: UUID | None, parent_path: str | None, name: str
    ) -> bool:
        """True if a folder or file the user could see occupies ``parent_path/name``.

        Folders and files share one namespace per directory (the UI merges them into a
        single tree), so a folder "docs" and a file "docs" in the same parent are ambiguous.
        Personal (My Drive) entries are scoped to ``user_id`` — another user's same-named
        folder is not a clash.
        """
        full = f"{parent_path}/{name}" if parent_path else name
        if await self.folders.get_by_path(user_id, workspace_id, full) is not None:
            return True
        if await self.assets.get_by_path(user_id, workspace_id, parent_path, name) is not None:
            return True
        return False

    async def _unique_name(
        self, user_id: UUID, workspace_id: UUID | None, parent_path: str | None, name: str
    ) -> str:
        """Return ``name``, or the first ``name (n)`` that no folder/file at that spot uses.

        Mirrors desktop copy semantics ("untitled.txt" → "untitled (1).txt") so creating,
        moving or renaming into a busy directory never fails — the requested name wins and
        duplicates get a numeric suffix. The caller surfaces the final name to the user.
        """
        if not await self._name_taken(user_id, workspace_id, parent_path, name):
            return name
        for n in range(1, 1000):
            candidate = f"{name} ({n})"
            if not await self._name_taken(user_id, workspace_id, parent_path, candidate):
                return candidate
        raise DriveError("could not find a free name in that folder", 409)

    async def _ensure_folder_manageable(self, user_id: UUID, folder) -> None:
        """Folder rename/delete needs editor-or-owner rights (workspace) or ownership (My Drive).

        The workspace owner is not a member row, so owner status is checked explicitly.
        """
        if folder.workspace_id is not None:
            ws = await self.workspaces.get(folder.workspace_id)
            if ws is not None and ws.owner_id == user_id:
                return
            role = await self.workspaces.member_role(folder.workspace_id, user_id)
            if role not in ("owner", "admin", "editor"):
                raise DriveError("no write access to folder", 403)
        elif folder.user_id != user_id:
            raise DriveError("no access to folder", 403)

    async def create_folder(
        self,
        user_id: UUID,
        workspace_id: UUID | None,
        parent_path: str | None,
        name: str,
    ) -> dict:
        name = (name or "").strip()
        if not name or "/" in name:
            raise DriveError("folder name must be a single non-empty name", 400)
        if workspace_id is not None:
            await self.ensure_workspace_member(user_id, workspace_id)
        parent_path = self._validate_folder_path(parent_path)
        # Folders and files share one name space per directory — auto-suffix busy names.
        name = await self._unique_name(user_id, workspace_id, parent_path, name)
        full = f"{parent_path}/{name}" if parent_path else name
        try:
            folder = await self.folders.create(user_id, workspace_id, full)
        except RepositoryConflict:
            raise DriveError("folder already exists", 409)
        await self._log(
            user_id, workspace_id, "folder.create", "folder", folder.id, full,
            f"parent: {parent_path or '(root)'}",
        )
        return self._folder_dict(folder)

    async def list_folders(self, user_id: UUID) -> list[dict]:
        return [self._folder_dict(f) for f in await self.folders.list_visible(user_id)]

    async def rename_folder(self, user_id: UUID, folder_id: UUID, new_name: str) -> dict:
        new_name = (new_name or "").strip()
        if not new_name or "/" in new_name:
            raise DriveError("folder name must be a single non-empty name", 400)
        folder = await self.folders.get(folder_id)
        if folder is None:
            raise DriveError("folder not found", 404)
        await self._ensure_folder_manageable(user_id, folder)
        parent = folder.path.rsplit("/", 1)[0] if "/" in folder.path else ""
        old_path = folder.path
        # Auto-suffix if the renamed folder would collide with a folder/file at that spot.
        new_name = await self._unique_name(user_id, folder.workspace_id, parent, new_name)
        new_path = f"{parent}/{new_name}" if parent else new_name
        if old_path != new_path:
            await self.folders.move_subtree(user_id, folder.workspace_id, old_path, new_path)
            await self.assets.move_subtree(user_id, folder.workspace_id, old_path, new_path)
            await self._log(
                user_id, folder.workspace_id, "folder.rename", "folder", folder_id,
                new_path, f"{old_path} -> {new_path}",
            )
        folder = await self.folders.get(folder_id)
        return self._folder_dict(folder) if folder else {"id": str(folder_id), "path": new_path}

    async def move_folder(self, user_id: UUID, folder_id: UUID, parent_path: str | None) -> dict:
        """Move a folder (and its whole subtree) under a new parent in the same scope.

        ``parent_path`` is the destination folder ('' / None = My Drive root). The
        folder keeps its name, so only its path prefix changes. Moving into itself or
        a descendant is refused (would create a cycle); a busy destination name is
        auto-suffixed with ``(n)`` rather than failing.
        """
        folder = await self.folders.get(folder_id)
        if folder is None:
            raise DriveError("folder not found", 404)
        await self._ensure_folder_manageable(user_id, folder)
        parent = self._validate_folder_path(parent_path) or ""
        name = folder.path.rsplit("/", 1)[-1]
        old_path = folder.path
        # Auto-suffix if a folder/file with this name already sits at the destination.
        name = await self._unique_name(user_id, folder.workspace_id, parent, name)
        new_path = f"{parent}/{name}" if parent else name
        if new_path == old_path:
            return self._folder_dict(folder)
        if new_path.startswith(old_path + "/"):
            raise DriveError("cannot move a folder into itself or a descendant", 409)
        await self.folders.move_subtree(user_id, folder.workspace_id, old_path, new_path)
        await self.assets.move_subtree(user_id, folder.workspace_id, old_path, new_path)
        await self._log(
            user_id, folder.workspace_id, "folder.move", "folder", folder_id,
            new_path, f"{old_path} -> {new_path}",
        )
        folder = await self.folders.get(folder_id)
        return self._folder_dict(folder) if folder else {"id": str(folder_id), "path": new_path}

    async def delete_folder(self, user_id: UUID, folder_id: UUID) -> dict:
        """Trash every file under the folder, then remove the folder rows (subtree)."""
        folder = await self.folders.get(folder_id)
        if folder is None:
            raise DriveError("folder not found", 404)
        await self._ensure_folder_manageable(user_id, folder)
        await self.assets.trash_subtree(user_id, folder.workspace_id, folder.path)
        await self.folders.delete_subtree(user_id, folder.workspace_id, folder.path)
        await self._log(
            user_id, folder.workspace_id, "folder.delete", "folder", folder.id,
            folder.path, "folder removed; files moved to trash",
        )
        return {"deleted": True, "folder_id": str(folder.id)}

    async def move_file(
        self,
        user_id: UUID,
        asset_id: UUID,
        workspace_id: UUID | None,
        folder_path: str | None,
    ) -> dict:
        """Move a file to any location: another workspace or My Drive, any folder (None = root)."""
        asset = await self.ensure_asset_writable(user_id, asset_id)
        folder_path = self._validate_folder_path(folder_path)
        if workspace_id is not None:
            await self.ensure_workspace_member(user_id, workspace_id)
        # Auto-suffix if the destination already holds a folder/file with this name
        # (skip when the move is a no-op, so we don't rename the file into itself).
        if workspace_id != asset.workspace_id or folder_path != asset.folder_path:
            name = await self._unique_name(user_id, workspace_id, folder_path, asset.name)
        else:
            name = asset.name
        moved = await self.assets.move(asset_id, workspace_id, folder_path)
        if moved is None:
            raise DriveError("asset not found", 404)
        if name != asset.name:
            moved = await self.assets.update(asset_id, name=name) or moved
        dest = "My Drive"
        if workspace_id is not None:
            ws = await self.workspaces.get(workspace_id)
            dest = ws.name if ws else str(workspace_id)
        await self._log(
            user_id, workspace_id, "file.move", "file", asset_id, moved.name,
            f"moved to {dest} / {folder_path or '(root)'}",
        )
        return self._asset_dict(moved)

    # ── Trash ───────────────────────────────────────────────────────────────────

    async def list_trash(self, user_id: UUID) -> list[dict]:
        """Lazy retention sweep: permanently purge items older than the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
        active = []
        for a in await self.assets.list_trash(user_id):
            if a.deleted_at is not None and a.deleted_at < cutoff:
                await self._purge_asset(a)
                await self._log(
                    None, a.workspace_id, "file.purge", "file", a.id, a.name,
                    "retention: permanently deleted after 30 days", actor_username="system",
                )
            else:
                active.append(a)
        return [self._asset_dict(a) for a in active]

    async def restore_trash(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.assets.get_including_trash(asset_id)
        if asset is None:
            raise DriveError("asset not found", 404)
        if asset.deleted_at is None:
            raise DriveError("asset is not in the trash", 409)
        if not await self._can_write(user_id, asset):
            raise DriveError("no write access to asset", 403)
        # If the original workspace is gone or no longer accessible, fall back to My Drive.
        ws_id = asset.workspace_id
        if ws_id is not None:
            ws = await self.workspaces.get(ws_id)
            if ws is None or (
                ws.owner_id != user_id and not await self.workspaces.member_role(ws_id, user_id)
            ):
                ws_id = None
        detail = "restored from trash"
        if ws_id != asset.workspace_id:
            await self.assets.move(asset.id, ws_id, asset.folder_path)
            detail = "restored from trash to My Drive (original workspace gone)"
        restored = await self.assets.restore(asset.id)
        await self._log(
            user_id, ws_id, "file.restore", "file", asset.id, asset.name, detail,
        )
        return self._asset_dict(restored) if restored else self._asset_dict(asset)

    async def purge_trash(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.assets.get_including_trash(asset_id)
        if asset is None:
            raise DriveError("asset not found", 404)
        if asset.deleted_at is None:
            raise DriveError("asset is not in the trash", 409)
        if not await self._can_write(user_id, asset):
            raise DriveError("no write access to asset", 403)
        await self._log(
            user_id, asset.workspace_id, "file.purge", "file", asset.id, asset.name,
            "permanently deleted",
        )
        return await self._purge_asset(asset)

    async def empty_trash(self, user_id: UUID) -> dict:
        count = 0
        for a in await self.assets.list_trash(user_id):
            if await self._can_write(user_id, a):
                await self._purge_asset(a)
                count += 1
        if count:
            await self._log(
                user_id, None, "trash.empty", "file", None, f"{count} files",
                "trash emptied (permanently deleted)",
            )
        return {"purged": count}

    # ── Sharing (asset ACL) ─────────────────────────────────────────────────────

    async def share_asset(
        self, user_id: UUID, asset_id: UUID, grantee_user_id: UUID | None, permission: str
    ) -> dict:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        if permission not in ("read", "write"):
            raise DriveError("permission must be 'read' or 'write'", 400)
        await self.acl.grant(asset_id, grantee_user_id, permission)
        await self._log(
            user_id, asset.workspace_id, "file.share", "file", asset_id, asset.name,
            f"grant {str(grantee_user_id) if grantee_user_id else 'public'}: {permission}",
        )
        return {"asset_id": str(asset_id), "grantee_user_id": str(grantee_user_id)
                if grantee_user_id else None, "permission": permission}

    async def unshare_asset(
        self, user_id: UUID, asset_id: UUID, grantee_user_id: UUID | None
    ) -> dict:
        asset = await self.ensure_asset_writable(user_id, asset_id)
        await self.acl.revoke(asset_id, grantee_user_id)
        await self._log(
            user_id, asset.workspace_id, "file.unshare", "file", asset_id, asset.name,
            f"revoked {str(grantee_user_id) if grantee_user_id else 'public'}",
        )
        return {"removed": True}

    async def list_asset_shares(self, user_id: UUID, asset_id: UUID) -> list[dict]:
        await self.ensure_asset_readable(user_id, asset_id)
        return [
            {
                "grantee_user_id": str(x.grantee_user_id) if x.grantee_user_id else None,
                "permission": x.permission,
            }
            for x in await self.acl.list_for_asset(asset_id)
        ]

    async def ingest_status(self, user_id: UUID, asset_id: UUID) -> dict:
        asset = await self.ensure_asset_readable(user_id, asset_id)
        return {
            "asset_id": str(asset.id),
            "file_status": asset.file_status,
            "rag_status": asset.rag_status,
        }

    # ── Serialization ───────────────────────────────────────────────────────────

    @staticmethod
    def _activity_dict(row) -> dict:
        return {
            "id": str(row.id),
            "workspace_id": str(row.workspace_id) if row.workspace_id else None,
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "actor_username": row.actor_username,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "target_name": row.target_name,
            "detail": row.detail,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _folder_dict(folder) -> dict:
        return {
            "id": str(folder.id),
            "user_id": str(folder.user_id),
            "workspace_id": str(folder.workspace_id) if folder.workspace_id else None,
            "name": folder.path.rsplit("/", 1)[-1],
            "path": folder.path,
            "created_at": folder.created_at.isoformat() if folder.created_at else None,
            "updated_at": folder.updated_at.isoformat() if folder.updated_at else None,
        }

    @staticmethod
    def _asset_dict(asset) -> dict:
        return {
            "id": str(asset.id),
            "user_id": str(asset.user_id),
            "workspace_id": str(asset.workspace_id) if asset.workspace_id else None,
            "object_sha256": asset.object_sha256,
            "name": asset.name,
            "folder_path": asset.folder_path,
            "mime_type": asset.mime_type,
            "size": asset.size,
            "file_status": asset.file_status,
            "rag_status": asset.rag_status,
            "created_at": asset.created_at.isoformat() if asset.created_at else None,
            "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
            "deleted_at": asset.deleted_at.isoformat() if asset.deleted_at else None,
        }
