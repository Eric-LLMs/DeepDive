"""Workspace audit trail: every drive mutation logs who / what / when, and the
trail is listable with actor/target search, date bounds, and pagination."""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from tests._drive_fakes import make_drive


async def _upload(svc, user, content, name="f.txt", workspace_id=None, folder_path=None):
    """Full chunked upload of ``content`` into a scope, returning the asset dict."""
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(
        user, sha, len(content), name, folder_path, "text/plain", workspace_id
    )
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    return (await svc.complete_upload(user, asset_id))["asset"]


async def _act(svc, user, ws_id, **kw):
    r = await svc.list_workspace_activity(user, ws_id, **kw)
    return r["total"], r["items"]


async def test_file_ops_logged(tmp_path):
    svc = make_drive(tmp_path, users={uuid4(): "alice"})
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])

    f = await _upload(svc, a, b"hello", name="a.txt", workspace_id=ws_id)
    await svc.rename_file(a, UUID(f["id"]), name="b.txt")
    await svc.move_file(a, UUID(f["id"]), ws_id, "Docs")
    await svc.delete_asset(a, UUID(f["id"]))
    await svc.restore_trash(a, UUID(f["id"]))
    await svc.delete_asset(a, UUID(f["id"]))
    await svc.purge_trash(a, UUID(f["id"]))

    total, items = await _act(svc, a, ws_id)
    assert total == 8  # workspace.create + 7 file ops
    actions = [i["action"] for i in items]
    assert actions == ["file.purge", "file.delete", "file.restore", "file.delete",
                        "file.move", "file.rename", "file.create", "workspace.create"]
    for i in items:
        assert i["actor_user_id"] == str(a)
        assert i["target_name"]
    assert items[1]["detail"] == "moved to trash"
    assert "name: a.txt -> b.txt" in items[5]["detail"]


async def test_personal_ops_do_not_leak_into_workspace_log(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await _upload(svc, a, b"x", name="ws.txt", workspace_id=ws_id)
    await _upload(svc, a, b"y", name="p.txt")  # personal (workspace_id None)

    # Workspace log only shows the workspace-scoped upload (+ workspace.create).
    total, items = await _act(svc, a, ws_id)
    assert total == 2
    assert {i["target_name"] for i in items} == {"Team", "ws.txt"}
    # The personal upload IS recorded (complete trail), under workspace_id None.
    personal = [r for r in svc.logs.rows if r.workspace_id is None]
    assert [r.target_name for r in personal] == ["p.txt"]


async def test_folder_ops_logged(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])

    d = await svc.create_folder(a, ws_id, None, "English")
    await svc.create_folder(a, ws_id, "English", "Vocab")
    await svc.rename_folder(a, UUID(d["id"]), "Lang")
    await svc.delete_folder(a, UUID(d["id"]))

    total, items = await _act(svc, a, ws_id)
    actions = [i["action"] for i in items]
    assert actions == ["folder.delete", "folder.rename", "folder.create", "folder.create",
                        "workspace.create"]
    assert items[0]["target_name"] == "Lang"  # renamed before deletion
    assert items[1]["detail"] == "English -> Lang"


async def test_member_ops_logged_with_username(tmp_path):
    a, b = uuid4(), uuid4()
    svc = make_drive(tmp_path, users={a: "alice", b: "bob"})
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])

    await svc.add_workspace_member(a, ws_id, b, "editor")
    await svc.update_workspace_member(a, ws_id, b, "viewer")
    await svc.remove_workspace_member(a, ws_id, b)

    total, items = await _act(svc, a, ws_id)
    member_actions = [i for i in items if i["action"].startswith("member.")]
    assert [i["action"] for i in member_actions] == ["member.remove", "member.update", "member.add"]
    for i in member_actions:
        assert i["target_name"] == "bob"
        assert i["actor_username"] == "alice"
    assert member_actions[1]["detail"] == "role: editor -> viewer"


async def test_workspace_lifecycle_logged(tmp_path):
    svc = make_drive(tmp_path, users={uuid4(): "alice"})
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    await svc.rename_workspace(a, UUID(ws["id"]), "Team 2")
    await svc.delete_workspace(a, UUID(ws["id"]))

    # The workspace row is gone, but its audit trail survives (no FK).
    rows = svc.logs.rows
    assert all(r.workspace_id == UUID(ws["id"]) for r in rows)
    assert [(r.action, r.detail) for r in rows] == [
        ("workspace.create", "workspace created"),
        ("workspace.rename", "Team -> Team 2"),
        ("workspace.delete", "workspace deleted; files moved to trash"),
    ]


async def test_add_duplicate_member_conflict(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await svc.add_workspace_member(a, ws_id, b, "editor")
    with pytest.raises(DriveError) as e:
        await svc.add_workspace_member(a, ws_id, b, "viewer")
    assert e.value.status_code == 409


async def test_list_filters_actor_target_and_dates(tmp_path):
    a, b = uuid4(), uuid4()
    svc = make_drive(tmp_path, users={a: "alice", b: "bob"})
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await svc.add_workspace_member(a, ws_id, b, "editor")
    await _upload(svc, a, b"z", name="report.pdf", workspace_id=ws_id)
    await svc.create_folder(a, ws_id, None, "Archive")

    total, _ = await _act(svc, a, ws_id, q="alice")
    assert total >= 3
    total, items = await _act(svc, a, ws_id, q="report")
    assert total == 1
    assert items[0]["action"] == "file.create"
    total, _ = await _act(svc, a, ws_id, q=str(a)[:8])
    assert total >= 3

    total, _ = await _act(svc, a, ws_id, start="2000-01-01", end="2000-12-31")
    assert total == 0


async def test_list_pagination(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    for i in range(5):
        await _upload(svc, a, f"content-{i}".encode(), name=f"f{i}.txt", workspace_id=ws_id)

    total, page1 = await _act(svc, a, ws_id, limit=2, offset=0)
    _, page2 = await _act(svc, a, ws_id, limit=2, offset=2)
    _, page3 = await _act(svc, a, ws_id, limit=2, offset=4)
    assert total == 6  # 1 workspace.create + 5 file.create
    assert len(page1) == 2 and len(page2) == 2 and len(page3) == 2
    names1 = {i["target_name"] for i in page1}
    names2 = {i["target_name"] for i in page2}
    names3 = {i["target_name"] for i in page3}
    assert not (names1 & names2) and not (names2 & names3)


async def test_activity_requires_membership(tmp_path):
    svc = make_drive(tmp_path)
    a, outsider = uuid4(), uuid4()
    ws = await svc.create_workspace(a, "Team")
    with pytest.raises(DriveError) as e:
        await svc.list_workspace_activity(outsider, UUID(ws["id"]))
    assert e.value.status_code == 403


async def test_share_unshare_logged(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    f = await _upload(svc, a, b"data", name="s.txt")
    await svc.share_asset(a, UUID(f["id"]), b, "read")
    await svc.unshare_asset(a, UUID(f["id"]), b)

    actions = [(r.action, r.detail) for r in svc.logs.rows]
    assert actions == [("file.create", "upload started (4 bytes) to root"),
                       ("file.share", f"grant {b}: read"),
                       ("file.unshare", f"revoked {b}")]


async def test_search_users_by_name_and_id(tmp_path):
    a, b = uuid4(), uuid4()
    svc = make_drive(tmp_path, users={a: "alice", b: "bob"})

    by_name = await svc.search_users("ali")
    assert {u["username"] for u in by_name} == {"alice"}
    by_id = await svc.search_users(str(a)[:10])
    assert {u["username"] for u in by_id} == {"alice"}
    assert await svc.search_users("") == []


async def test_list_workspaces_carries_role(tmp_path):
    a, b = uuid4(), uuid4()
    svc = make_drive(tmp_path)
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])

    # b is not yet a member -> not listed at all
    assert svc.list_workspaces and [w["id"] for w in await svc.list_workspaces(b)] == []

    await svc.add_workspace_member(a, ws_id, b, "editor")
    by_a = {w["id"]: w["role"] for w in await svc.list_workspaces(a)}
    by_b = {w["id"]: w["role"] for w in await svc.list_workspaces(b)}
    assert by_a[str(ws_id)] == "owner"
    assert by_b[str(ws_id)] == "editor"

    await svc.update_workspace_member(a, ws_id, b, "viewer")
    by_b = {w["id"]: w["role"] for w in await svc.list_workspaces(b)}
    assert by_b[str(ws_id)] == "viewer"


async def test_member_uploads_visible_to_owner_and_peers(tmp_path):
    """Regression: the owner is not a workspace_members row, so the visibility
    predicate previously hid member uploads from the owner (and the owner's per-file
    checks rejected them). Ownership must count as a workspace membership channel."""
    a, b, c = uuid4(), uuid4(), uuid4()
    svc = make_drive(tmp_path)
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await svc.add_workspace_member(a, ws_id, b, "editor")
    await svc.add_workspace_member(a, ws_id, c, "viewer")

    fb = await _upload(svc, b, b"from-b", name="b.txt", workspace_id=ws_id, folder_path="test")
    fa = await _upload(svc, a, b"from-a", name="a.txt", workspace_id=ws_id, folder_path="test")

    # Owner, editor and viewer all see both files inside the workspace.
    for who in (a, b, c):
        names = {f["name"] for f in await svc.list_files(who)}
        assert names == {"a.txt", "b.txt"}, f"user {who} should see both files"

    # Owner and viewer can read the member's file.
    await svc.get_file(a, UUID(fb["id"]))
    await svc.get_file(c, UUID(fb["id"]))

    # Owner keeps write rights on the member's file.
    await svc.rename_file(a, UUID(fb["id"]), name="b-renamed.txt")
    assert {f["name"] for f in await svc.list_files(b)} == {"a.txt", "b-renamed.txt"}

    # An outsider sees neither.
    assert await svc.list_files(uuid4()) == []


async def test_admin_role_matrix(tmp_path):
    """Admin = editor file access + manage (logs, members), but workspace rename/delete
    and granting/modifying admin members are owner-only."""
    a, adm, ed, vi = uuid4(), uuid4(), uuid4(), uuid4()
    svc = make_drive(tmp_path)
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await svc.add_workspace_member(a, ws_id, adm, "admin")
    await svc.add_workspace_member(a, ws_id, ed, "editor")
    await svc.add_workspace_member(a, ws_id, vi, "viewer")

    # list_workspaces carries the admin role for the member.
    roles = {w["id"]: w["role"] for w in await svc.list_workspaces(adm)}
    assert roles[str(ws_id)] == "admin"

    # Admin: can add editor/viewer, edit roles, remove members, read the log.
    x = uuid4()
    await svc.add_workspace_member(adm, ws_id, x, "editor")
    await svc.update_workspace_member(adm, ws_id, x, "viewer")
    await svc.remove_workspace_member(adm, ws_id, x)
    total, _ = await _act(svc, adm, ws_id)
    assert total >= 4  # workspace.create + member.add/update/remove

    # Admin: full file/folder write.
    await svc.create_folder(adm, ws_id, None, "sub")
    f = await _upload(svc, adm, b"x", name="f.txt", workspace_id=ws_id, folder_path="sub")
    assert f["name"] == "f.txt"

    # Admin: cannot rename or delete the workspace.
    with pytest.raises(DriveError) as e:
        await svc.rename_workspace(adm, ws_id, "Nope")
    assert e.value.status_code == 403
    with pytest.raises(DriveError) as e:
        await svc.delete_workspace(adm, ws_id)
    assert e.value.status_code == 403

    # Admin: cannot grant admin, nor demote/remove an existing admin.
    y = uuid4()
    with pytest.raises(DriveError) as e:
        await svc.add_workspace_member(adm, ws_id, y, "admin")
    assert e.value.status_code == 403
    with pytest.raises(DriveError) as e:
        await svc.update_workspace_member(adm, ws_id, ed, "admin")
    assert e.value.status_code == 403
    await svc.add_workspace_member(a, ws_id, y, "admin")  # owner promotes y
    with pytest.raises(DriveError) as e:
        await svc.update_workspace_member(adm, ws_id, y, "viewer")
    assert e.value.status_code == 403
    with pytest.raises(DriveError) as e:
        await svc.remove_workspace_member(adm, ws_id, y)
    assert e.value.status_code == 403
    await svc.update_workspace_member(a, ws_id, y, "editor")  # owner can demote

    # Editor / viewer: cannot manage members or read the log.
    for who in (ed, vi):
        with pytest.raises(DriveError) as e:
            await svc.add_workspace_member(who, ws_id, uuid4(), "viewer")
        assert e.value.status_code == 403
        with pytest.raises(DriveError) as e:
            await svc.list_workspace_activity(who, ws_id)
        assert e.value.status_code == 403

    # Unknown role value is rejected.
    with pytest.raises(DriveError) as e:
        await svc.add_workspace_member(a, ws_id, uuid4(), "superadmin")
    assert e.value.status_code == 400
