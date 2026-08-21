"""Visibility predicate: owner / workspace member / ACL grantee / public share channels."""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from core.infrastructure.visibility import asset_visible_expr, asset_visibility_sql
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
