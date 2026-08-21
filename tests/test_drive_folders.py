"""First-class folders: create/nest/rename/delete, file moves across scopes."""
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
    done = await svc.complete_upload(user, asset_id)
    return done["asset"]


async def test_create_and_list_personal_folder(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()

    f = await svc.create_folder(a, None, None, "Vocab")
    assert f["name"] == "Vocab"
    assert f["path"] == "Vocab"
    assert f["workspace_id"] is None

    child = await svc.create_folder(a, None, "Vocab", "Unit1")
    assert child["path"] == "Vocab/Unit1"

    listed = {x["path"] for x in await svc.list_folders(a)}
    assert listed == {"Vocab", "Vocab/Unit1"}


async def test_create_folder_conflict(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    await svc.create_folder(a, None, None, "Vocab")
    with pytest.raises(DriveError) as e:
        await svc.create_folder(a, None, None, "Vocab")
    assert e.value.status_code == 409


async def test_create_folder_rejects_bad_names_and_paths(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()

    with pytest.raises(DriveError) as e:
        await svc.create_folder(a, None, None, "a/b")  # must be a single segment
    assert e.value.status_code == 400

    with pytest.raises(DriveError) as e:
        await svc.create_folder(a, None, "..", "x")
    assert e.value.status_code == 400


async def test_rename_folder_cascades_to_files_and_subfolders(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    await svc.create_folder(a, None, None, "English")
    await svc.create_folder(a, None, "English", "Vocab")

    f = await _upload(svc, a, b"hi", "notes.txt", folder_path="English/Vocab")
    folder = [x for x in await svc.list_folders(a) if x["path"] == "English"][0]

    renamed = await svc.rename_folder(a, UUID(folder["id"]), "Spanish")
    assert renamed["path"] == "Spanish"

    paths = {x["path"] for x in await svc.list_folders(a)}
    assert paths == {"Spanish", "Spanish/Vocab"}

    moved = await svc.get_file(a, UUID(f["id"]))
    assert moved["folder_path"] == "Spanish/Vocab"


async def test_delete_folder_trashes_files(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    await svc.create_folder(a, None, "English", "Vocab")
    f = await _upload(svc, a, b"hi", "notes.txt", folder_path="English/Vocab")
    folder = [x for x in await svc.list_folders(a) if x["path"] == "English"][0]

    await svc.delete_folder(a, UUID(folder["id"]))

    # Folder rows gone, file sits in the trash (not purged).
    assert await svc.list_folders(a) == []
    assert [x["id"] for x in await svc.list_trash(a)] == [f["id"]]
    # No longer visible in the active file list.
    assert await svc.list_files(a) == []


async def test_move_file_to_root_and_cross_workspace(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])

    f = await _upload(svc, a, b"hi", "notes.txt", workspace_id=ws_id, folder_path="Eng")

    # Move deeper then back to root.
    moved = await svc.move_file(a, UUID(f["id"]), ws_id, "Eng/Sub")
    assert moved["folder_path"] == "Eng/Sub"
    moved = await svc.move_file(a, UUID(f["id"]), ws_id, None)
    assert moved["folder_path"] is None
    assert moved["workspace_id"] == str(ws_id)

    # Cross-workspace: B is not a member → 403.
    with pytest.raises(DriveError) as e:
        await svc.move_file(b, UUID(f["id"]), ws_id, None)
    assert e.value.status_code == 403

    # Move into My Drive.
    moved = await svc.move_file(a, UUID(f["id"]), None, "Personal")
    assert moved["workspace_id"] is None
    assert moved["folder_path"] == "Personal"


async def test_workspace_folder_visible_only_to_members(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    await svc.create_folder(a, ws_id, None, "Docs")

    # B is not a member → cannot see the folder.
    assert await svc.list_folders(b) == []

    # After adding B as viewer, the folder shows up but rename/delete are blocked.
    await svc.add_workspace_member(a, ws_id, b, "viewer")
    assert {x["path"] for x in await svc.list_folders(b)} == {"Docs"}

    folder = [x for x in await svc.list_folders(b) if x["path"] == "Docs"][0]
    with pytest.raises(DriveError) as e:
        await svc.rename_folder(b, UUID(folder["id"]), "X")
    assert e.value.status_code == 403


async def test_create_folder_requires_workspace_membership(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    ws = await svc.create_workspace(a, "Team")
    with pytest.raises(DriveError) as e:
        await svc.create_folder(b, UUID(ws["id"]), None, "Docs")
    assert e.value.status_code == 403
