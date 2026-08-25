"""Visibility predicate: owner / workspace member / ACL grantee / public share channels."""
import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from core.application.drive_service import DriveError
from core.infrastructure.drive_repositories import SqlChunkRepository
from core.infrastructure.vector import PgVectorStore
from core.infrastructure.visibility import (
    asset_visible_expr,
    asset_visibility_sql,
    chunk_visible_expr,
)
from tests._drive_fakes import make_drive


def test_visibility_sql_contains_all_three_channels():
    sql = asset_visibility_sql(uuid4())
    assert "c.user_id = :uid" in sql
    assert "workspace_members" in sql
    assert "asset_acl" in sql
    assert "grantee_user_id IS NULL" in sql


def test_visible_expr_builds():
    expr = asset_visible_expr(uuid4())
    assert expr is not None


def test_chunk_visible_expr_builds():
    expr = chunk_visible_expr(uuid4())
    assert expr is not None


class _CapturingSession:
    """Fake async session that records the compiled statement it is handed."""

    def __init__(self):
        self.sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.sql = str(stmt.compile(dialect=postgresql.dialect()))
        return self

    def all(self):
        return []


class _CapturingFactory:
    def __init__(self):
        self.session = _CapturingSession()

    def __call__(self):
        return self.session


def test_vector_search_visibility_checks_chunk_owner():
    """Non-file chunks (chat / learning, asset_id NULL) must stay visible to their owner.

    Regression: the vector recaller previously applied the asset-only visibility
    expression over the LEFT JOINed row, which evaluated NULL for non-file chunks and
    dropped them from semantic recall entirely.
    """
    factory = _CapturingFactory()
    store = PgVectorStore(factory)
    asyncio.run(store.search([0.1, 0.2], 5, {"user_id": "u-1"}))
    sql = factory.session.sql
    # chunk-level ownership/workspace — the channels that keep non-file chunks visible
    assert "chunks.user_id" in sql
    assert "chunks.workspace_id" in sql
    # ACL still rides on the chunk's asset_id
    assert "asset_acl" in sql


def test_vector_search_keeps_non_ready_file_guard():
    factory = _CapturingFactory()
    store = PgVectorStore(factory)
    asyncio.run(store.search([0.1, 0.2], 5, {"user_id": "u-1"}))
    sql = factory.session.sql
    assert "chunks.asset_id IS NULL" in sql
    assert "assets.file_status" in sql  # 'READY' is a bound parameter in the compiled SQL


def test_vector_search_without_user_id_has_no_visibility_predicate():
    factory = _CapturingFactory()
    store = PgVectorStore(factory)
    asyncio.run(store.search([0.1, 0.2], 5))
    assert "chunks.user_id" not in factory.session.sql
    assert "asset_acl" not in factory.session.sql


def test_get_parents_by_child_ids_selects_parent_text():
    """parent_expand must return the *parent* chunk's text, not the leaf's.

    Regression: the repo selected content_en/meta from the leaf row while keying by the
    parent id, so siblings were deduped but the hit never widened to the parent text.
    """
    factory = _CapturingFactory()
    repo = SqlChunkRepository(factory)
    asyncio.run(repo.get_parents_by_child_ids([str(uuid4())]))
    sql = factory.session.sql
    assert "JOIN chunks" in sql  # self-joins the parent row
    assert "chunks_1.content_en" in sql  # parent text is what gets returned


async def _owned_file(svc, user, content=b"notes"):
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(user, sha, len(content), "f.txt", None, None, None)
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    await svc.complete_upload(user, asset_id)
    return asset_id


async def test_owner_only_by_default(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    asset_id = await _owned_file(svc, a)

    assert (await svc.ensure_asset_readable(a, asset_id)).id == asset_id
    with pytest.raises(DriveError) as e:
        await svc.ensure_asset_readable(b, asset_id)
    assert e.value.status_code == 403


async def test_acl_grant_opens_read_and_write(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    asset_id = await _owned_file(svc, a)

    await svc.share_asset(a, asset_id, b, "read")
    assert (await svc.ensure_asset_readable(b, asset_id)).id == asset_id
    with pytest.raises(DriveError) as e:
        await svc.ensure_asset_writable(b, asset_id)
    assert e.value.status_code == 403

    await svc.share_asset(a, asset_id, b, "write")
    assert (await svc.ensure_asset_writable(b, asset_id)).id == asset_id


async def test_public_share_readable_by_anyone(tmp_path):
    svc = make_drive(tmp_path)
    a, stranger = uuid4(), uuid4()
    asset_id = await _owned_file(svc, a)

    await svc.share_asset(a, asset_id, None, "read")  # public link
    assert (await svc.ensure_asset_readable(stranger, asset_id)).id == asset_id


async def test_revoke_removes_access(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    asset_id = await _owned_file(svc, a)

    await svc.share_asset(a, asset_id, b, "read")
    await svc.unshare_asset(a, asset_id, b)
    with pytest.raises(DriveError) as e:
        await svc.ensure_asset_readable(b, asset_id)
    assert e.value.status_code == 403


async def test_workspace_member_reads_and_editor_writes(tmp_path):
    svc = make_drive(tmp_path)
    owner, member, stranger = uuid4(), uuid4(), uuid4()
    ws_id = uuid4()
    svc.workspaces.workspaces[ws_id] = _Ws(ws_id, owner)

    content = b"notes"
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(owner, sha, len(content), "f.txt", None, None, ws_id)
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(owner, asset_id, 0, content)
    await svc.complete_upload(owner, asset_id)

    # Viewer: can read, cannot write.
    svc.workspaces.members[(ws_id, member)] = "viewer"
    assert (await svc.ensure_asset_readable(member, asset_id)).id == asset_id
    with pytest.raises(DriveError) as e:
        await svc.ensure_asset_writable(member, asset_id)
    assert e.value.status_code == 403
    # A stranger with no membership is denied entirely.
    with pytest.raises(DriveError) as e2:
        await svc.ensure_asset_readable(stranger, asset_id)
    assert e2.value.status_code == 403

    # Editor: can write.
    svc.workspaces.members[(ws_id, member)] = "editor"
    assert (await svc.ensure_asset_writable(member, asset_id)).id == asset_id


class _Ws:
    def __init__(self, id, owner_id):
        self.id = id
        self.owner_id = owner_id
