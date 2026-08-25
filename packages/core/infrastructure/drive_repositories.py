"""SQLAlchemy repositories for the cloud drive (objects/assets/workspaces/uploads/ACL).

Each method opens its own session via the injected ``session_factory`` and commits. The
concurrency-critical operations — object upsert with ref_count, ref_count decrement, and
the ref_count-0 physical-delete CAS — are single statements, so they are atomic regardless
of transaction grouping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import Text, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from core.infrastructure.db import (
    AssetAclModel,
    AssetModel,
    ChunkModel,
    FolderModel,
    GlobalObjectModel,
    UploadSessionModel,
    UserModel,
    WorkspaceActivityModel,
    WorkspaceMemberModel,
    WorkspaceModel,
)
from core.infrastructure.visibility import asset_visible_expr, folder_visible_expr


class RepositoryConflict(Exception):
    """A unique constraint was violated (e.g. the folder path already exists)."""


class SqlGlobalObjectRepository:
    """Physical objects: one row per unique SHA-256, shared across users (ref-counted)."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def get(self, sha256: str) -> GlobalObjectModel | None:
        async with self.session_factory() as session:
            return await session.get(GlobalObjectModel, sha256)

    async def upsert_and_increment(
        self, sha256: str, size: int, storage_key: str, mime_type: str | None
    ) -> GlobalObjectModel:
        """Create the object (ref_count=1) or bump its ref_count atomically.

        Two users uploading the same digest concurrently both get ref_count+1; the row is
        created exactly once. Returns the authoritative row.
        """
        async with self.session_factory() as session:
            stmt = (
                pg_insert(GlobalObjectModel)
                .values(
                    sha256=sha256,
                    size=size,
                    storage_key=storage_key,
                    mime_type=mime_type,
                    ref_count=1,
                )
                .on_conflict_do_update(
                    index_elements=[GlobalObjectModel.sha256],
                    set_={"ref_count": GlobalObjectModel.ref_count + 1},
                )
                .returning(GlobalObjectModel)
            )
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return row

    async def decrement(self, sha256: str) -> int | None:
        """Decrement ref_count, returning the new value (None if the row was already gone)."""
        async with self.session_factory() as session:
            row = await session.execute(
                update(GlobalObjectModel)
                .where(GlobalObjectModel.sha256 == sha256, GlobalObjectModel.ref_count > 0)
                .values(ref_count=GlobalObjectModel.ref_count - 1)
                .returning(GlobalObjectModel.ref_count)
            )
            await session.commit()
            return row.scalar_one_or_none()

    async def delete_if_zero(self, sha256: str) -> str | None:
        """Retire the row iff ref_count reached 0; returns its storage_key for physical delete.

        If a concurrent upload re-increments between the decrement and this statement, the
        DELETE matches 0 rows and the file is retained.
        """
        async with self.session_factory() as session:
            row = await session.execute(
                delete(GlobalObjectModel)
                .where(GlobalObjectModel.sha256 == sha256, GlobalObjectModel.ref_count == 0)
                .returning(GlobalObjectModel.storage_key)
            )
            await session.commit()
            return row.scalar_one_or_none()


class SqlAssetRepository:
    """Logical files: one row per user/workspace pointing at a physical object."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def create(
        self,
        user_id: UUID,
        name: str,
        *,
        workspace_id: UUID | None = None,
        folder_path: str | None = None,
        mime_type: str | None = None,
        size: int | None = None,
        object_sha256: str | None = None,
        file_status: str = "uploading",
        rag_status: str = "pending",
    ) -> AssetModel:
        async with self.session_factory() as session:
            obj = AssetModel(
                user_id=user_id,
                workspace_id=workspace_id,
                object_sha256=object_sha256,
                name=name,
                folder_path=folder_path,
                mime_type=mime_type,
                size=size,
                file_status=file_status,
                rag_status=rag_status,
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return obj

    async def get(self, asset_id: UUID) -> AssetModel | None:
        async with self.session_factory() as session:
            return await session.get(AssetModel, asset_id)

    async def get_active(self, asset_id: UUID) -> AssetModel | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(AssetModel).where(
                        AssetModel.id == asset_id, AssetModel.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()

    async def get_by_path(
        self, user_id: UUID, workspace_id: UUID | None, folder_path: str | None, name: str
    ) -> AssetModel | None:
        """Find an active asset at ``folder_path/name`` that ``user_id`` could see there.

        A workspace scope matches any row in that workspace (all members share it); a
        personal scope (workspace_id None = My Drive) matches only the user's own rows.
        ``folder_path`` None/empty means the scope's root. Used to dedupe names that
        would otherwise clash between files and folders in the same directory.
        """
        async with self.session_factory() as session:
            conditions = [
                self._scope(workspace_id),
                AssetModel.folder_path.is_(None)
                if folder_path in (None, "")
                else AssetModel.folder_path == folder_path,
                AssetModel.name == name,
                AssetModel.deleted_at.is_(None),
            ]
            if workspace_id is None:  # personal (My Drive) rows belong to one user
                conditions.append(AssetModel.user_id == user_id)
            return (
                await session.execute(
                    select(AssetModel).where(*conditions)
                )
            ).scalar_one_or_none()

    async def list_visible(self, user_id: UUID) -> list[AssetModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AssetModel)
                    .where(AssetModel.deleted_at.is_(None), asset_visible_expr(user_id))
                    .order_by(AssetModel.created_at.desc())
                )
            ).scalars().all()
            return list(rows)

    async def list_by_workspace(self, workspace_id: UUID) -> list[AssetModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AssetModel).where(
                        AssetModel.workspace_id == workspace_id, AssetModel.deleted_at.is_(None)
                    )
                )
            ).scalars().all()
            return list(rows)

    async def soft_delete(self, asset_id: UUID) -> AssetModel | None:
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None or obj.deleted_at is not None:
                return obj
            obj.file_status = "DELETED"
            obj.deleted_at = datetime.now(timezone.utc)
            await session.commit()
            return obj

    async def set_status(
        self,
        asset_id: UUID,
        *,
        file_status: str | None = None,
        rag_status: str | None = None,
    ) -> AssetModel | None:
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            if file_status is not None:
                obj.file_status = file_status
            if rag_status is not None:
                obj.rag_status = rag_status
            await session.commit()
            return obj

    async def set_object(self, asset_id: UUID, object_sha256: str) -> AssetModel | None:
        """Link an asset to its physical object once the upload has been verified."""
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            obj.object_sha256 = object_sha256
            await session.commit()
            return obj

    async def set_content_meta(
        self, asset_id: UUID, object_sha256: str, size: int, mime_type: str | None
    ) -> AssetModel | None:
        """Repoint an asset to a new physical object after an in-place content update."""
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            obj.object_sha256 = object_sha256
            obj.size = size
            if mime_type is not None:
                obj.mime_type = mime_type
            await session.commit()
            return obj

    async def update(
        self,
        asset_id: UUID,
        *,
        name: str | None = None,
        folder_path: str | None = None,
    ) -> AssetModel | None:
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            if name is not None:
                obj.name = name
            if folder_path is not None:
                obj.folder_path = folder_path
            await session.commit()
            return obj

    # ── Move (explicit workspace + folder_path, including null = root) ──

    @staticmethod
    def _scope(workspace_id: UUID | None):
        return (
            AssetModel.workspace_id.is_(None)
            if workspace_id is None
            else AssetModel.workspace_id == workspace_id
        )

    async def move(
        self, asset_id: UUID, workspace_id: UUID | None, folder_path: str | None
    ) -> AssetModel | None:
        """Relocate an asset to a workspace (or My Drive) and folder path (None = root).

        Unlike :meth:`update` this sets ``folder_path`` explicitly even when None, so a
        file can be moved back to a workspace root.
        """
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            obj.workspace_id = workspace_id
            obj.folder_path = folder_path
            await session.commit()
            return obj

    async def move_subtree(
        self, user_id: UUID, workspace_id: UUID | None, old_path: str, new_path: str
    ) -> None:
        """Rewrite every asset under ``old_path`` to the matching path under ``new_path``.

        Personal (My Drive) rows are scoped to ``user_id`` so one user's move never
        rewrites another user's same-named folders.
        """
        async with self.session_factory() as session:
            conditions = [
                self._scope(workspace_id),
                or_(
                    AssetModel.folder_path == old_path,
                    AssetModel.folder_path.like(old_path + "/%"),
                ),
            ]
            if workspace_id is None:
                conditions.append(AssetModel.user_id == user_id)
            await session.execute(
                update(AssetModel)
                .where(*conditions)
                .values(
                    folder_path=func.concat(
                        new_path, func.substr(AssetModel.folder_path, len(old_path) + 1)
                    )
                )
            )
            await session.commit()

    async def trash_subtree(self, user_id: UUID, workspace_id: UUID | None, path: str) -> None:
        """Soft-delete (send to trash) every non-deleted asset under ``path``."""
        async with self.session_factory() as session:
            conditions = [
                self._scope(workspace_id),
                or_(
                    AssetModel.folder_path == path,
                    AssetModel.folder_path.like(path + "/%"),
                ),
                AssetModel.deleted_at.is_(None),
            ]
            if workspace_id is None:
                conditions.append(AssetModel.user_id == user_id)
            await session.execute(
                update(AssetModel)
                .where(*conditions)
                .values(file_status="DELETED", deleted_at=datetime.now(timezone.utc))
            )
            await session.commit()

    # ── Trash (soft-deleted assets retained for a while, physical bytes kept) ──

    async def list_trash(self, user_id: UUID) -> list[AssetModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AssetModel)
                    .where(
                        AssetModel.deleted_at.is_not(None),
                        asset_visible_expr(user_id),
                    )
                    .order_by(AssetModel.deleted_at.desc())
                )
            ).scalars().all()
            return list(rows)

    async def get_including_trash(self, asset_id: UUID) -> AssetModel | None:
        async with self.session_factory() as session:
            return await session.get(AssetModel, asset_id)

    async def restore(self, asset_id: UUID) -> AssetModel | None:
        """Pull an asset back out of the trash; returns to READY."""
        async with self.session_factory() as session:
            obj = await session.get(AssetModel, asset_id)
            if obj is None:
                return None
            obj.deleted_at = None
            obj.file_status = "READY"
            await session.commit()
            return obj

    async def hard_delete(self, asset_id: UUID) -> None:
        """Permanently remove the logical row (physical bytes handled by the service)."""
        async with self.session_factory() as session:
            await session.execute(delete(AssetModel).where(AssetModel.id == asset_id))
            await session.commit()

    async def nullify_workspace(self, workspace_id: UUID) -> None:
        """Detach every asset from a workspace (used before deleting the workspace row)."""
        async with self.session_factory() as session:
            await session.execute(
                update(AssetModel)
                .where(AssetModel.workspace_id == workspace_id)
                .values(workspace_id=None)
            )
            await session.commit()


class SqlAssetAclRepository:
    """Asset-level sharing: a grantee (or public, grantee NULL) with a read/write permission."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def grant(
        self, asset_id: UUID, grantee_user_id: UUID | None, permission: str
    ) -> None:
        async with self.session_factory() as session:
            cond = (
                AssetAclModel.grantee_user_id.is_(None)
                if grantee_user_id is None
                else AssetAclModel.grantee_user_id == grantee_user_id
            )
            row = (
                await session.execute(
                    select(AssetAclModel).where(AssetAclModel.asset_id == asset_id, cond)
                )
            ).scalar_one_or_none()
            if row:
                row.permission = permission
            else:
                session.add(
                    AssetAclModel(
                        asset_id=asset_id, grantee_user_id=grantee_user_id, permission=permission
                    )
                )
            await session.commit()

    async def revoke(self, asset_id: UUID, grantee_user_id: UUID | None) -> None:
        async with self.session_factory() as session:
            cond = (
                AssetAclModel.grantee_user_id.is_(None)
                if grantee_user_id is None
                else AssetAclModel.grantee_user_id == grantee_user_id
            )
            await session.execute(
                delete(AssetAclModel).where(AssetAclModel.asset_id == asset_id, cond)
            )
            await session.commit()

    async def list_for_asset(self, asset_id: UUID) -> list[AssetAclModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AssetAclModel).where(AssetAclModel.asset_id == asset_id)
                )
            ).scalars().all()
            return list(rows)

    async def permission_for(self, asset_id: UUID, user_id: UUID) -> str | None:
        """Best effective permission: write > read, user grant > public grant."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(AssetAclModel).where(AssetAclModel.asset_id == asset_id)
                )
            ).scalars().all()
        user = {r.permission for r in rows if r.grantee_user_id == user_id}
        public = {r.permission for r in rows if r.grantee_user_id is None}
        if "write" in user or "write" in public:
            return "write"
        if user or public:
            return "read"
        return None


class SqlUploadSessionRepository:
    """Chunked-upload state: which chunks are received, for resume + assembly."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def create(
        self,
        user_id: UUID,
        asset_id: UUID,
        sha256: str,
        size: int,
        chunk_size: int,
        num_chunks: int,
    ) -> UploadSessionModel:
        async with self.session_factory() as session:
            obj = UploadSessionModel(
                user_id=user_id,
                asset_id=asset_id,
                sha256=sha256,
                size=size,
                chunk_size=chunk_size,
                num_chunks=num_chunks,
                received_chunks=[False] * num_chunks,
                status="pending",
            )
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return obj

    async def get_by_asset(self, asset_id: UUID) -> UploadSessionModel | None:
        async with self.session_factory() as session:
            return (
                await session.execute(
                    select(UploadSessionModel)
                    .where(UploadSessionModel.asset_id == asset_id)
                    .order_by(UploadSessionModel.created_at.desc())
                )
            ).scalars().first()

    async def mark_chunk(self, session_id: UUID, index: int) -> UploadSessionModel | None:
        async with self.session_factory() as session:
            obj = await session.get(UploadSessionModel, session_id)
            if obj is None:
                return None
            received = list(obj.received_chunks or [])
            while len(received) <= index:
                received.append(False)
            received[index] = True
            obj.received_chunks = received
            obj.status = "uploading"
            await session.commit()
            return obj

    async def complete(self, session_id: UUID) -> bool:
        """Atomically flip pending/uploading → completed. False when already terminal.

        Guards against two concurrent ``complete`` calls double-counting an object.
        """
        async with self.session_factory() as session:
            row = await session.execute(
                update(UploadSessionModel)
                .where(
                    UploadSessionModel.id == session_id,
                    UploadSessionModel.status.in_(("pending", "uploading")),
                )
                .values(status="completed")
                .returning(UploadSessionModel.id)
            )
            await session.commit()
            return row.scalar_one_or_none() is not None

    async def set_status(self, session_id: UUID, status: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(UploadSessionModel)
                .where(UploadSessionModel.id == session_id)
                .values(status=status)
            )
            await session.commit()


class SqlWorkspaceRepository:
    """User-owned workspaces + membership (the sharing mechanism)."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def create(self, owner_id: UUID, name: str) -> WorkspaceModel:
        async with self.session_factory() as session:
            obj = WorkspaceModel(owner_id=owner_id, name=name)
            session.add(obj)
            await session.commit()
            await session.refresh(obj)
            return obj

    async def get(self, workspace_id: UUID) -> WorkspaceModel | None:
        async with self.session_factory() as session:
            return await session.get(WorkspaceModel, workspace_id)

    async def list_for_user(self, user_id: UUID) -> list[WorkspaceModel]:
        """Workspaces the user owns or is a member of (deduplicated).

        The two selects run separately: ``union_all`` of two ORM entity selects flattens
        the rows to raw column values in the async driver, losing the model objects.
        """
        async with self.session_factory() as session:
            owned = (
                await session.execute(
                    select(WorkspaceModel).where(WorkspaceModel.owner_id == user_id)
                )
            ).scalars().all()
            joined = (
                await session.execute(
                    select(WorkspaceModel)
                    .join(
                        WorkspaceMemberModel,
                        WorkspaceMemberModel.workspace_id == WorkspaceModel.id,
                    )
                    .where(WorkspaceMemberModel.user_id == user_id)
                )
            ).scalars().all()
            seen: dict[UUID, WorkspaceModel] = {}
            for r in (*owned, *joined):
                seen[r.id] = r
            return list(seen.values())

    async def is_owner(self, workspace_id: UUID, user_id: UUID) -> bool:
        async with self.session_factory() as session:
            obj = await session.get(WorkspaceModel, workspace_id)
            return obj is not None and obj.owner_id == user_id

    async def rename(self, workspace_id: UUID, name: str) -> WorkspaceModel | None:
        async with self.session_factory() as session:
            obj = await session.get(WorkspaceModel, workspace_id)
            if obj is None:
                return None
            obj.name = name
            await session.commit()
            return obj

    async def delete(self, workspace_id: UUID) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(WorkspaceModel).where(WorkspaceModel.id == workspace_id)
            )
            await session.commit()

    async def member_role(self, workspace_id: UUID, user_id: UUID) -> str | None:
        # The owner holds the top role even though they are not a workspace_members
        # row; without this every per-asset check would reject the owner.
        async with self.session_factory() as session:
            ws = await session.get(WorkspaceModel, workspace_id)
            if ws is not None and ws.owner_id == user_id:
                return "owner"
            row = await session.get(WorkspaceMemberModel, (workspace_id, user_id))
            return row.role if row else None

    async def add_member(self, workspace_id: UUID, user_id: UUID, role: str) -> None:
        async with self.session_factory() as session:
            session.add(
                WorkspaceMemberModel(workspace_id=workspace_id, user_id=user_id, role=role)
            )
            await session.commit()

    async def update_member(self, workspace_id: UUID, user_id: UUID, role: str) -> bool:
        async with self.session_factory() as session:
            row = await session.get(WorkspaceMemberModel, (workspace_id, user_id))
            if row is None:
                return False
            row.role = role
            await session.commit()
            return True

    async def remove_member(self, workspace_id: UUID, user_id: UUID) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(WorkspaceMemberModel).where(
                    WorkspaceMemberModel.workspace_id == workspace_id,
                    WorkspaceMemberModel.user_id == user_id,
                )
            )
            await session.commit()

    async def list_members(self, workspace_id: UUID) -> list[WorkspaceMemberModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(WorkspaceMemberModel).where(
                        WorkspaceMemberModel.workspace_id == workspace_id
                    )
                )
            ).scalars().all()
            return list(rows)


class SqlFolderRepository:
    """First-class folders: one row per (workspace | personal drive) folder path."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _scope(workspace_id: UUID | None):
        return (
            FolderModel.workspace_id.is_(None)
            if workspace_id is None
            else FolderModel.workspace_id == workspace_id
        )

    async def create(self, user_id: UUID, workspace_id: UUID | None, path: str) -> FolderModel:
        """Create a folder, upserting any missing ancestor rows so the tree stays complete.

        Personal (My Drive) rows are scoped to ``user_id``: another user's same-named
        folder at the same workspace-NULL path is not a collision for this user.
        """
        async with self.session_factory() as session:
            segs = path.split("/")
            for i in range(1, len(segs) + 1):
                ancestor = "/".join(segs[:i])
                conditions = [self._scope(workspace_id), FolderModel.path == ancestor]
                if workspace_id is None:
                    conditions.append(FolderModel.user_id == user_id)
                existing = (
                    await session.execute(select(FolderModel).where(*conditions))
                ).scalar_one_or_none()
                if existing is not None:
                    if i == len(segs):
                        raise RepositoryConflict("folder already exists")
                    continue
                session.add(FolderModel(user_id=user_id, workspace_id=workspace_id, path=ancestor))
            try:
                await session.commit()
            except IntegrityError as e:  # race with a concurrent create
                await session.rollback()
                raise RepositoryConflict("folder already exists") from e
            fetch_conditions = [self._scope(workspace_id), FolderModel.path == path]
            if workspace_id is None:
                fetch_conditions.append(FolderModel.user_id == user_id)
            obj = (
                await session.execute(select(FolderModel).where(*fetch_conditions))
            ).scalar_one()
            return obj

    async def get(self, folder_id: UUID) -> FolderModel | None:
        async with self.session_factory() as session:
            return await session.get(FolderModel, folder_id)

    async def list_visible(self, user_id: UUID) -> list[FolderModel]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(FolderModel)
                    .where(folder_visible_expr(user_id))
                    .order_by(FolderModel.path)
                )
            ).scalars().all()
            return list(rows)

    async def get_by_path(
        self, user_id: UUID, workspace_id: UUID | None, path: str
    ) -> FolderModel | None:
        """Find a folder row by its exact path, scoped like :meth:`SqlAssetRepository.get_by_path`.

        Workspace rows are shared by members; personal (My Drive, workspace_id None) rows
        belong to ``user_id`` alone, so another user's same-named folder is not a clash.
        """
        async with self.session_factory() as session:
            conditions = [self._scope(workspace_id), FolderModel.path == path]
            if workspace_id is None:
                conditions.append(FolderModel.user_id == user_id)
            return (
                await session.execute(select(FolderModel).where(*conditions))
            ).scalar_one_or_none()

    async def move_subtree(
        self, user_id: UUID, workspace_id: UUID | None, old_path: str, new_path: str
    ) -> None:
        """Rewrite every folder under ``old_path`` to the matching path under ``new_path``.

        Personal (My Drive) rows are scoped to ``user_id`` so one user's move never
        rewrites another user's same-named folders.
        """
        async with self.session_factory() as session:
            conditions = [
                self._scope(workspace_id),
                or_(
                    FolderModel.path == old_path,
                    FolderModel.path.like(old_path + "/%"),
                ),
            ]
            if workspace_id is None:
                conditions.append(FolderModel.user_id == user_id)
            await session.execute(
                update(FolderModel)
                .where(*conditions)
                .values(
                    path=func.concat(new_path, func.substr(FolderModel.path, len(old_path) + 1))
                )
            )
            await session.commit()

    async def delete_subtree(self, user_id: UUID, workspace_id: UUID | None, path: str) -> None:
        async with self.session_factory() as session:
            conditions = [
                self._scope(workspace_id),
                or_(FolderModel.path == path, FolderModel.path.like(path + "/%")),
            ]
            if workspace_id is None:
                conditions.append(FolderModel.user_id == user_id)
            await session.execute(delete(FolderModel).where(*conditions))
            await session.commit()


class SqlUserRepository:
    """User identity lookups used by the drive (actor resolution, member-add search)."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def get(self, user_id: UUID) -> UserModel | None:
        async with self.session_factory() as session:
            return await session.get(UserModel, user_id)

    async def search(self, q: str, limit: int = 10) -> list[dict]:
        """Fuzzy-match active users by username or user-id (for adding members)."""
        async with self.session_factory() as session:
            like = f"%{q.lower()}%"
            rows = (
                await session.execute(
                    select(UserModel)
                    .where(
                        UserModel.username.is_not(None),
                        UserModel.is_active.is_(True),
                        or_(
                            func.lower(UserModel.username).like(like),
                            cast(UserModel.id, Text).like(f"%{q.lower()}%"),
                        ),
                    )
                    .order_by(UserModel.username)
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {"user_id": str(u.id), "username": u.username, "display_name": u.display_name}
                for u in rows
            ]


class SqlActivityRepository:
    """Append-only drive audit trail (workspace_activity)."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def add(
        self,
        *,
        actor_user_id: UUID | None,
        actor_username: str | None,
        workspace_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str | None,
        target_name: str | None,
        detail: str | None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                WorkspaceActivityModel(
                    workspace_id=workspace_id,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    target_name=target_name,
                    detail=detail,
                )
            )
            await session.commit()

    async def list(
        self,
        workspace_id: UUID,
        *,
        q: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[int, list[WorkspaceActivityModel]]:
        """Page the audit trail for a workspace, newest first, with optional filters.

        ``q`` fuzzy-matches the actor (username or user-id) and the target name/id, so a
        single search box covers "who" and "what". ``start``/``end`` bound ``created_at``.
        """
        async with self.session_factory() as session:
            cond = [WorkspaceActivityModel.workspace_id == workspace_id]
            if q:
                like = f"%{q.lower()}%"
                cond.append(
                    or_(
                        func.lower(WorkspaceActivityModel.actor_username).like(like),
                        func.lower(WorkspaceActivityModel.target_name).like(like),
                        cast(WorkspaceActivityModel.actor_user_id, Text).like(like),
                        cast(WorkspaceActivityModel.target_id, Text).like(like),
                    )
                )
            if start is not None:
                cond.append(WorkspaceActivityModel.created_at >= start)
            if end is not None:
                cond.append(WorkspaceActivityModel.created_at <= end)
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(WorkspaceActivityModel)
                    .where(*cond)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(WorkspaceActivityModel)
                    .where(*cond)
                    .order_by(WorkspaceActivityModel.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars().all()
            return total, list(rows)


class SqlChunkRepository:
    """RAG chunks; denormalized user/workspace on each chunk for filtered recall."""

    def __init__(self, session_factory: Callable) -> None:
        self.session_factory = session_factory

    async def delete_by_asset(self, asset_id: UUID) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(ChunkModel).where(ChunkModel.asset_id == asset_id)
            )
            await session.commit()

    async def delete_by_source(self, source_type: str, source_ids: list[str]) -> None:
        """Drop a non-file source's chunks so re-importing it is idempotent."""
        if not source_ids:
            return
        async with self.session_factory() as session:
            await session.execute(
                delete(ChunkModel).where(
                    ChunkModel.source_type == source_type,
                    ChunkModel.source_id.in_(source_ids),
                )
            )
            await session.commit()

    async def bulk_insert(
        self,
        asset_id: UUID | None,
        user_id: UUID,
        workspace_id: UUID | None,
        chunks: list[dict],
        source_type: str = "file",
        source_id: str | None = None,
    ) -> None:
        """Insert chunk rows with a running seq.

        Each ``chunks`` dict matches the ChunkModel columns plus ``embedding``:
        ``{content_en, content_cn?, meta?, embedding, chunk_kind?, parent_chunk_id?, id?}``.
        ``id`` defaults to a server-generated UUID; parents pass a client UUID so their
        leaves can reference ``parent_chunk_id`` before insert.

        ``asset_id`` is optional — non-file sources (learning / chat) leave it NULL and
        instead pass ``source_type`` + ``source_id`` (e.g. an article id or a chat Q&A
        pair); the caller still provides the owner ``user_id`` so recall scoping works.
        """
        async with self.session_factory() as session:
            for seq, c in enumerate(chunks):
                parent_id = c.get("parent_chunk_id")
                chunk_id = c.get("id")
                session.add(
                    ChunkModel(
                        id=UUID(chunk_id) if chunk_id else None,
                        asset_id=asset_id,
                        source_type=source_type,
                        source_id=source_id,
                        user_id=user_id,
                        workspace_id=workspace_id,
                        seq=seq,
                        content_en=c["content_en"],
                        content_cn=c.get("content_cn"),
                        meta=c.get("meta") or {},
                        embedding=c["embedding"],
                        chunk_kind=c.get("chunk_kind", "leaf"),
                        parent_chunk_id=UUID(parent_id) if parent_id else None,
                        content_search=c.get("content_search"),
                    )
                )
            await session.commit()

    async def get_parents_by_child_ids(self, child_ids: list[str]) -> dict[str, dict]:
        """Map leaf chunk ids → their parent row ``{id, text, meta}`` (parent_expand).

        Self-joins the parent row so the returned text/meta come from the *parent* chunk,
        not from the leaf that only references it.
        """
        if not child_ids:
            return {}
        parent = aliased(ChunkModel)
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ChunkModel.id,
                        parent.id,
                        parent.content_en,
                        parent.meta,
                    )
                    .join(parent, parent.id == ChunkModel.parent_chunk_id)
                    .where(ChunkModel.id.in_([UUID(c) for c in child_ids]))
                )
            ).all()
        return {
            str(child_id): {"id": str(parent_id), "text": text, "meta": meta}
            for child_id, parent_id, text, meta in rows
        }
