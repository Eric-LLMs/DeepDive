"""Cloud-drive Copy / Move — pure filesystem semantics.

Copy: one new logical asset row sharing the source's ``object_sha256`` with the
physical object's ``ref_count`` incremented (no blob re-upload). Move: the row's
logical address (workspace_id / folder_path) is rewritten; bytes never move.
Conflicts in both cases are resolved authoritatively server-side by auto-suffixing
(``a(1).ext``) — no overwrite, no skip, no trash interplay. Folders move (with
their subtree) but never copy.
"""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from core.infrastructure.storage import object_key
from tests._drive_fakes import make_drive

USER = uuid4()
OTHER = uuid4()


async def _file(svc, user, name, content, folder_path=None):
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(user, sha, len(content), name, folder_path, "text/plain", None)
    if res.get("status") == "instant":
        return UUID(res["asset"]["id"]), sha
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    await svc.complete_upload(user, asset_id)
    return asset_id, sha


async def _folder(svc, name, parent=None):
    return await svc.create_folder(USER, None, parent, name)


# ── Copy ────────────────────────────────────────────────────────────────────────


async def test_copy_creates_new_row_shares_object_and_bumps_ref_count(tmp_path):
    svc = make_drive(tmp_path)
    content = b"payload bytes"
    src, sha = await _file(svc, USER, "a.txt", content)
    assert svc.objects.rows[sha].ref_count == 1
    docs = await _folder(svc, "docs")

    result = await svc.copy_file(USER, src, None, docs["path"])

    assert result["renamed"] is False
    assert result["id"] != str(src)
    assert result["name"] == "a.txt"
    assert result["folder_path"] == "docs"
    assert result["object_sha256"] == sha
    assert result["file_status"] == "READY"
    # One physical object, two logical rows, ref_count + 1.
    assert svc.objects.rows[sha].ref_count == 2
    _, _, data_src = await svc.download(USER, src)
    _, _, data_cp = await svc.download(USER, UUID(result["id"]))
    assert data_src == data_cp == content

    # Source untouched.
    src_row = await svc.assets.get_active(src)
    assert src_row.folder_path is None and src_row.name == "a.txt"


async def test_purging_copy_keeps_original_readable(tmp_path):
    svc = make_drive(tmp_path)
    content = b"payload bytes"
    src, sha = await _file(svc, USER, "a.txt", content)
    docs = await _folder(svc, "docs")
    cp = UUID((await svc.copy_file(USER, src, None, docs["path"]))["id"])

    await svc.delete_asset(USER, cp)
    await svc.purge_trash(USER, cp)
    assert svc.objects.rows[sha].ref_count == 1
    assert await svc.storage.exists(object_key(sha)) is True
    _, _, data = await svc.download(USER, src)
    assert data == content


async def test_copy_conflict_auto_renames_on_server(tmp_path):
    svc = make_drive(tmp_path)
    src, sha = await _file(svc, USER, "a.txt", b"payload")

    # Copy onto the source's own spot: the occupied name forces the (n) suffix,
    # decided by the server right before the insert — never by a client pre-check.
    result = await svc.copy_file(USER, src, None, None)

    assert result["renamed"] is True
    assert result["name"] == "a(1).txt"
    assert svc.objects.rows[sha].ref_count == 2


async def test_copy_folder_id_is_not_an_asset(tmp_path):
    """Folders are a separate table — copying a folder id is a 404, by design."""
    svc = make_drive(tmp_path)
    docs = await _folder(svc, "docs")
    with pytest.raises(DriveError) as exc:
        await svc.copy_file(USER, UUID(docs["id"]), None, None)
    assert exc.value.status_code == 404


async def test_copy_foreign_asset_denied(tmp_path):
    svc = make_drive(tmp_path)
    src, _ = await _file(svc, USER, "a.txt", b"payload")
    with pytest.raises(DriveError) as exc:
        await svc.copy_file(OTHER, src, None, None)
    assert exc.value.status_code == 403


async def test_copy_to_foreign_workspace_denied(tmp_path):
    svc = make_drive(tmp_path)
    src, _ = await _file(svc, USER, "a.txt", b"payload")
    ws = await svc.workspaces.create(OTHER, "Team")  # USER is not a member
    with pytest.raises(DriveError) as exc:
        await svc.copy_file(USER, src, ws.id, None)
    assert exc.value.status_code == 403


async def test_copy_to_workspace_member_ok_owned_by_copier(tmp_path):
    svc = make_drive(tmp_path)
    src, _ = await _file(svc, USER, "a.txt", b"payload")
    ws = await svc.workspaces.create(OTHER, "Team")
    await svc.workspaces.add_member(ws.id, USER, "editor")

    result = await svc.copy_file(USER, src, ws.id, None)
    assert result["workspace_id"] == str(ws.id)
    assert result["user_id"] == str(USER)  # the copy is owned by whoever made it


# ── Move ────────────────────────────────────────────────────────────────────────


async def test_move_updates_logical_path_only(tmp_path):
    svc = make_drive(tmp_path)
    src, sha = await _file(svc, USER, "a.txt", b"payload")
    docs = await _folder(svc, "docs")

    moved = await svc.move_file(USER, src, None, docs["path"])

    assert moved["id"] == str(src)  # same logical record, new address
    assert moved["folder_path"] == "docs"
    assert moved["object_sha256"] == sha
    assert svc.objects.rows[sha].ref_count == 1  # bytes never moved


async def test_move_conflict_auto_renames(tmp_path):
    svc = make_drive(tmp_path)
    a, _ = await _file(svc, USER, "x.txt", b"one")
    docs = await _folder(svc, "docs")
    b, _ = await _file(svc, USER, "x.txt", b"two", docs["path"])

    moved = await svc.move_file(USER, a, None, docs["path"])

    assert moved["name"] == "x(1).txt"
    assert moved["id"] == str(a)
    # The pre-existing file is untouched (no overwrite).
    assert (await svc.assets.get_active(b)).name == "x.txt"


async def test_move_noop_same_folder_keeps_name(tmp_path):
    svc = make_drive(tmp_path)
    docs = await _folder(svc, "docs")
    a, _ = await _file(svc, USER, "x.txt", b"one", docs["path"])
    moved = await svc.move_file(USER, a, None, docs["path"])
    assert moved["name"] == "x.txt"  # renaming itself must be avoided


async def test_move_folder_cascades_subtree(tmp_path):
    svc = make_drive(tmp_path)
    docs = await _folder(svc, "docs")
    a, _ = await _file(svc, USER, "a.txt", b"one", docs["path"])
    inner = await _folder(svc, "inner", docs["path"])
    b, _ = await _file(svc, USER, "b.txt", b"two", inner["path"])
    proj = await _folder(svc, "proj")

    moved = await svc.move_folder(USER, UUID(docs["id"]), proj["path"])

    assert moved["path"] == "proj/docs"
    assert (await svc.assets.get_active(a)).folder_path == "proj/docs"
    assert (await svc.assets.get_active(b)).folder_path == "proj/docs/inner"


async def test_move_folder_into_itself_rejected(tmp_path):
    svc = make_drive(tmp_path)
    docs = await _folder(svc, "docs")
    inner = await _folder(svc, "inner", docs["path"])
    with pytest.raises(DriveError) as exc:
        await svc.move_folder(USER, UUID(docs["id"]), inner["path"])
    assert exc.value.status_code == 409
