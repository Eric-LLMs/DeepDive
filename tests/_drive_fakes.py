"""Hand-rolled fakes for drive tests (no DB), mirroring the tests/test_jobs.py style.

The fakes implement the same method surface as the SQL repositories, so DriveService runs
unchanged against them. Storage is a real :class:`LocalStorage` rooted at pytest tmp_path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from core.infrastructure.db import AssetModel, FolderModel, GlobalObjectModel, UploadSessionModel
from core.infrastructure.drive_repositories import RepositoryConflict
from core.infrastructure.storage import LocalStorage


class FakeObjects:
    def __init__(self) -> None:
        self.rows: dict[str, GlobalObjectModel] = {}

    async def get(self, sha: str) -> GlobalObjectModel | None:
        return self.rows.get(sha)

    async def upsert_and_increment(self, sha, size, storage_key, mime_type):
        obj = self.rows.get(sha)
        if obj:
            obj.ref_count += 1
            return obj
        obj = GlobalObjectModel(
            sha256=sha, size=size, storage_key=storage_key, mime_type=mime_type, ref_count=1
        )
        self.rows[sha] = obj
        return obj

    async def decrement(self, sha: str) -> int | None:
        obj = self.rows.get(sha)
        if obj is None or obj.ref_count <= 0:
            return None
        obj.ref_count -= 1
        return obj.ref_count

    async def delete_if_zero(self, sha: str) -> str | None:
        obj = self.rows.get(sha)
        if obj is None or obj.ref_count != 0:
            return None
        key = obj.storage_key
        del self.rows[sha]
        return key


class FakeAssets:
    def __init__(self, workspaces=None) -> None:
        self.rows: dict[UUID, AssetModel] = {}
        self.workspaces = workspaces

    async def create(self, user_id, name, *, workspace_id=None, folder_path=None,
                     mime_type=None, size=None, object_sha256=None,
                     file_status="uploading", rag_status="pending"):
        obj = AssetModel(
            id=uuid4(), user_id=user_id, name=name, workspace_id=workspace_id,
            folder_path=folder_path, mime_type=mime_type, size=size,
            object_sha256=object_sha256, file_status=file_status, rag_status=rag_status,
        )
        self.rows[obj.id] = obj
        return obj

    async def get(self, asset_id):
        return self.rows.get(asset_id)

    async def get_active(self, asset_id):
        obj = self.rows.get(asset_id)
        return obj if obj is not None and obj.deleted_at is None else None

    async def list_visible(self, user_id):
        # Mirror asset_visible_expr: ownership, plus workspaces the user owns or is a
        # member of (the owner is not a workspace_members row).
        visible_ws = set()
        if self.workspaces:
            visible_ws |= {ws.id for ws in self.workspaces.workspaces.values()
                           if ws.owner_id == user_id}
            visible_ws |= {wid for (wid, uid) in self.workspaces.members if uid == user_id}
        return [
            o for o in self.rows.values()
            if o.deleted_at is None and (o.user_id == user_id or o.workspace_id in visible_ws)
        ]

    async def list_by_workspace(self, workspace_id):
        return [
            o for o in self.rows.values()
            if o.workspace_id == workspace_id and o.deleted_at is None
        ]

    async def soft_delete(self, asset_id):
        obj = self.rows.get(asset_id)
        if obj is None or obj.deleted_at is not None:
            return obj
        obj.file_status = "DELETED"
        obj.deleted_at = datetime.now(timezone.utc)
        return obj

    async def set_status(self, asset_id, *, file_status=None, rag_status=None):
        obj = self.rows.get(asset_id)
        if obj is None:
            return None
        if file_status is not None:
            obj.file_status = file_status
        if rag_status is not None:
            obj.rag_status = rag_status
        return obj

    async def set_object(self, asset_id, object_sha256):
        obj = self.rows.get(asset_id)
        if obj is None:
            return None
        obj.object_sha256 = object_sha256
        return obj

    async def update(self, asset_id, *, name=None, folder_path=None):
        obj = self.rows.get(asset_id)
        if obj is None:
            return None
        if name is not None:
            obj.name = name
        if folder_path is not None:
            obj.folder_path = folder_path
        return obj

    async def move(self, asset_id, workspace_id, folder_path):
        obj = self.rows.get(asset_id)
        if obj is None:
            return None
        obj.workspace_id = workspace_id
        obj.folder_path = folder_path
        return obj

    async def move_subtree(self, workspace_id, old_path, new_path):
        for o in self.rows.values():
            if o.workspace_id != workspace_id:
                continue
            if o.folder_path is None:
                continue
            if o.folder_path == old_path:
                o.folder_path = new_path
            elif o.folder_path.startswith(old_path + "/"):
                o.folder_path = new_path + o.folder_path[len(old_path):]

    async def trash_subtree(self, workspace_id, path):
        for o in self.rows.values():
            if o.workspace_id != workspace_id or o.deleted_at is not None:
                continue
            if o.folder_path is None:
                continue
            if o.folder_path == path or o.folder_path.startswith(path + "/"):
                o.file_status = "DELETED"
                o.deleted_at = datetime.now(timezone.utc)

    async def list_trash(self, user_id):
        return [
            o for o in self.rows.values()
            if o.deleted_at is not None and o.user_id == user_id
        ]

    async def get_including_trash(self, asset_id):
        return self.rows.get(asset_id)

    async def restore(self, asset_id):
        obj = self.rows.get(asset_id)
        if obj is None:
            return None
        obj.deleted_at = None
        obj.file_status = "READY"
        return obj

    async def hard_delete(self, asset_id):
        self.rows.pop(asset_id, None)

    async def nullify_workspace(self, workspace_id):
        for o in self.rows.values():
            if o.workspace_id == workspace_id:
                o.workspace_id = None


class FakeUploads:
    def __init__(self) -> None:
        self.rows: dict[UUID, UploadSessionModel] = {}

    async def create(self, user_id, asset_id, sha256, size, chunk_size, num_chunks):
        obj = UploadSessionModel(
            id=uuid4(), user_id=user_id, asset_id=asset_id, sha256=sha256, size=size,
            chunk_size=chunk_size, num_chunks=num_chunks,
            received_chunks=[False] * num_chunks, status="pending",
        )
        self.rows[obj.id] = obj
        return obj

    async def get_by_asset(self, asset_id):
        for o in self.rows.values():
            if o.asset_id == asset_id:
                return o
        return None

    async def mark_chunk(self, session_id, index):
        obj = self.rows.get(session_id)
        if obj is None:
            return None
        received = list(obj.received_chunks or [])
        while len(received) <= index:
            received.append(False)
        received[index] = True
        obj.received_chunks = received
        obj.status = "uploading"
        return obj

    async def complete(self, session_id):
        obj = self.rows.get(session_id)
        if obj is None or obj.status in ("completed", "failed", "aborted"):
            return False
        obj.status = "completed"
        return True

    async def set_status(self, session_id, status):
        obj = self.rows.get(session_id)
        if obj is not None:
            obj.status = status


class FakeAcl:
    def __init__(self) -> None:
        self.grants: dict[tuple, str] = {}  # (asset_id, grantee) -> permission

    async def grant(self, asset_id, grantee_user_id, permission):
        self.grants[(asset_id, grantee_user_id)] = permission

    async def revoke(self, asset_id, grantee_user_id):
        self.grants.pop((asset_id, grantee_user_id), None)

    async def list_for_asset(self, asset_id):
        return [
            _AclRow(asset_id, g, p) for (a, g), p in self.grants.items() if a == asset_id
        ]

    async def permission_for(self, asset_id, user_id):
        user = {
            p for (a, g), p in self.grants.items()
            if a == asset_id and g == user_id
        }
        public = {
            p for (a, g), p in self.grants.items()
            if a == asset_id and g is None
        }
        if "write" in user or "write" in public:
            return "write"
        if user or public:
            return "read"
        return None


class _AclRow:
    def __init__(self, asset_id, grantee_user_id, permission):
        self.asset_id = asset_id
        self.grantee_user_id = grantee_user_id
        self.permission = permission


class FakeWorkspaces:
    def __init__(self) -> None:
        self.workspaces: dict[UUID, SimpleNamespace] = {}
        self.members: dict[tuple, str] = {}

    async def create(self, user_id, name):
        ws = SimpleNamespace(id=uuid4(), name=name, owner_id=user_id)
        self.workspaces[ws.id] = ws
        return ws

    async def get(self, workspace_id):
        return self.workspaces.get(workspace_id)

    async def list_for_user(self, user_id):
        return [
            ws for ws in self.workspaces.values()
            if ws.owner_id == user_id or (ws.id, user_id) in self.members
        ]

    async def rename(self, workspace_id, name):
        ws = self.workspaces.get(workspace_id)
        if ws is not None:
            ws.name = name
        return ws

    async def delete(self, workspace_id):
        self.workspaces.pop(workspace_id, None)
        self.members = {k: v for k, v in self.members.items() if k[0] != workspace_id}

    async def member_role(self, workspace_id, user_id):
        ws = self.workspaces.get(workspace_id)
        if ws is not None and ws.owner_id == user_id:
            return "owner"
        return self.members.get((workspace_id, user_id))

    async def add_member(self, workspace_id, user_id, role):
        self.members[(workspace_id, user_id)] = role

    async def list_members(self, workspace_id):
        return [
            SimpleNamespace(user_id=uid, role=role)
            for (wid, uid), role in self.members.items() if wid == workspace_id
        ]

    async def update_member(self, workspace_id, user_id, role):
        if (workspace_id, user_id) not in self.members:
            return False
        self.members[(workspace_id, user_id)] = role
        return True

    async def remove_member(self, workspace_id, user_id):
        self.members.pop((workspace_id, user_id), None)


class FakeFolders:
    def __init__(self, workspaces=None) -> None:
        self.rows: dict[UUID, FolderModel] = {}
        self.workspaces = workspaces

    async def create(self, user_id, workspace_id, path):
        segs = path.split("/")
        for i in range(1, len(segs) + 1):
            ancestor = "/".join(segs[:i])
            existing = next(
                (f for f in self.rows.values()
                 if f.workspace_id == workspace_id and f.path == ancestor),
                None,
            )
            if existing is not None:
                if i == len(segs):
                    raise RepositoryConflict("folder already exists")
                continue
            obj = FolderModel(
                id=uuid4(), user_id=user_id, workspace_id=workspace_id, path=ancestor,
                created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            )
            self.rows[obj.id] = obj
        return self.rows[obj.id]

    async def get(self, folder_id):
        return self.rows.get(folder_id)

    async def list_visible(self, user_id):
        # Mirror folder_visible_expr: creator, plus workspaces the user owns or belongs to.
        if self.workspaces is None:
            visible_ws = set()
        else:
            visible_ws = {ws.id for ws in self.workspaces.workspaces.values()
                          if ws.owner_id == user_id}
            visible_ws |= {wid for (wid, uid) in self.workspaces.members if uid == user_id}
        return [
            f for f in self.rows.values()
            if f.user_id == user_id or f.workspace_id in visible_ws
        ]

    async def move_subtree(self, workspace_id, old_path, new_path):
        for f in self.rows.values():
            if f.workspace_id != workspace_id:
                continue
            if f.path == old_path:
                f.path = new_path
            elif f.path.startswith(old_path + "/"):
                f.path = new_path + f.path[len(old_path):]

    async def delete_subtree(self, workspace_id, path):
        for fid, f in list(self.rows.items()):
            if f.workspace_id != workspace_id:
                continue
            if f.path == path or f.path.startswith(path + "/"):
                del self.rows[fid]


class FakeChunks:
    def __init__(self) -> None:
        self.rows = []

    async def delete_by_asset(self, asset_id):
        self.rows = [r for r in self.rows if r[0] != asset_id]

    async def bulk_insert(self, asset_id, user_id, workspace_id, chunks):
        self.rows.extend((asset_id, c) for c in chunks)


class FakeUsers:
    """Resolves user_id -> username; ``users`` maps UUID -> username."""

    def __init__(self, users=None) -> None:
        self.users = users or {}

    async def get(self, user_id):
        name = self.users.get(user_id)
        return SimpleNamespace(username=name) if name else None

    async def search(self, q, limit=10):
        ql = q.lower()
        out = []
        for uid, name in self.users.items():
            if ql in name.lower() or ql in str(uid).lower():
                out.append({"user_id": str(uid), "username": name, "display_name": None})
                if len(out) >= limit:
                    break
        return out


class FakeActivity:
    def __init__(self, users=None) -> None:
        self.rows = []
        self.users = users or {}
        self._clock = datetime.now(timezone.utc)

    async def add(self, *, actor_user_id, actor_username, workspace_id, action,
                  target_type, target_id, target_name, detail):
        # Strictly increasing clock so DESC ordering matches insertion order deterministically.
        self._clock += timedelta(microseconds=1)
        self.rows.append(
            SimpleNamespace(
                id=uuid4(), workspace_id=workspace_id, actor_user_id=actor_user_id,
                actor_username=actor_username, action=action, target_type=target_type,
                target_id=target_id, target_name=target_name, detail=detail,
                created_at=self._clock,
            )
        )

    async def list(self, workspace_id, *, q=None, start=None, end=None, limit=20, offset=0):
        rows = [r for r in self.rows if r.workspace_id == workspace_id]
        if q:
            ql = q.lower()
            rows = [
                r for r in rows
                if (r.actor_username and ql in r.actor_username.lower())
                or (r.target_name and ql in r.target_name.lower())
                or (r.actor_user_id and ql in str(r.actor_user_id).lower())
                or (r.target_id and ql in r.target_id.lower())
            ]
        if start is not None:
            rows = [r for r in rows if r.created_at >= start]
        if end is not None:
            rows = [r for r in rows if r.created_at <= end]
        rows = sorted(rows, key=lambda r: r.created_at, reverse=True)
        return len(rows), rows[offset:offset + limit]


def make_drive(tmp_path, users=None):
    """DriveService wired to fake repos + real LocalStorage under tmp_path.

    ``users`` is an optional ``{UUID: username}`` map that gives the fakes actor/member
    names (real repositories resolve these from the users table).
    """
    from core.application.drive_service import DriveService

    users = users or {}
    workspaces = FakeWorkspaces()
    return DriveService(
        session_factory=None,  # unused by the fakes
        storage=LocalStorage(tmp_path),
        objects=FakeObjects(),
        assets=FakeAssets(workspaces),
        uploads=FakeUploads(),
        acl=FakeAcl(),
        chunks=FakeChunks(),
        workspaces=workspaces,
        folders=FakeFolders(workspaces),
        logs=FakeActivity(users),
        users=FakeUsers(users),
    )
