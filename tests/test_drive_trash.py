"""Trash lifecycle: soft-delete keeps bytes, restore, permanent purge, lazy retention sweep."""
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError, TRASH_RETENTION_DAYS
from core.infrastructure.storage import object_key
from tests._drive_fakes import make_drive


async def _upload(svc, user, content, name="f.txt", workspace_id=None, folder_path=None):
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(
        user, sha, len(content), name, folder_path, "text/plain", workspace_id
    )
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    return (await svc.complete_upload(user, asset_id))["asset"]


async def test_delete_to_trash_keeps_bytes_and_hides_from_list(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    content = b"precious bytes"
    f = await _upload(svc, a, content)
    sha = hashlib.sha256(content).hexdigest()

    r = await svc.delete_asset(a, UUID(f["id"]))
    assert r["deleted"] is True
    assert r["physical_removed"] is False

    assert await svc.list_files(a) == []
    assert [x["id"] for x in await svc.list_trash(a)] == [f["id"]]
    assert svc.objects.rows[sha].ref_count == 1
    assert await svc.storage.exists(object_key(sha)) is True


async def test_restore_to_original_location(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    f = await _upload(svc, a, b"hi", "notes.txt", workspace_id=ws_id, folder_path="Eng")

    await svc.delete_asset(a, UUID(f["id"]))
    restored = await svc.restore_trash(a, UUID(f["id"]))

    assert restored["deleted_at"] is None
    assert restored["workspace_id"] == str(ws_id)
    assert restored["folder_path"] == "Eng"
    assert await svc.list_files(a) != []


async def test_restore_after_workspace_deleted_falls_back_to_my_drive(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    ws = await svc.create_workspace(a, "Team")
    ws_id = UUID(ws["id"])
    f = await _upload(svc, a, b"hi", "notes.txt", workspace_id=ws_id, folder_path="Eng")

    await svc.delete_asset(a, UUID(f["id"]))
    await svc.delete_workspace(a, ws_id)

    restored = await svc.restore_trash(a, UUID(f["id"]))
    assert restored["workspace_id"] is None
    assert restored["folder_path"] == "Eng"


async def test_purge_trash_frees_physical_object(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    content = b"gone soon"
    f = await _upload(svc, a, content)
    sha = hashlib.sha256(content).hexdigest()

    await svc.delete_asset(a, UUID(f["id"]))
    r = await svc.purge_trash(a, UUID(f["id"]))

    assert r["purged"] is True
    assert r["physical_removed"] is True
    assert sha not in svc.objects.rows
    assert await svc.storage.exists(object_key(sha)) is False
    assert await svc.list_trash(a) == []


async def test_purge_requires_trash_membership(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    f = await _upload(svc, a, b"hi")

    # B cannot see or purge A's trash.
    await svc.delete_asset(a, UUID(f["id"]))
    with pytest.raises(DriveError) as e:
        await svc.purge_trash(b, UUID(f["id"]))
    assert e.value.status_code == 403


async def test_empty_trash_purges_everything(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    fa = await _upload(svc, a, b"one", "one.txt")
    fb = await _upload(svc, a, b"two", "two.txt")
    await svc.delete_asset(a, UUID(fa["id"]))
    await svc.delete_asset(a, UUID(fb["id"]))

    r = await svc.empty_trash(a)
    assert r["purged"] == 2
    assert await svc.list_trash(a) == []


async def test_lazy_retention_sweep_purges_expired_items(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    fa = await _upload(svc, a, b"old", "old.txt")
    fb = await _upload(svc, a, b"new", "new.txt")
    await svc.delete_asset(a, UUID(fa["id"]))
    await svc.delete_asset(a, UUID(fb["id"]))

    # Age only the first entry beyond the retention window.
    svc.assets.rows[UUID(fa["id"])].deleted_at = (
        datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS + 1)
    )

    listed = await svc.list_trash(a)
    assert [x["id"] for x in listed] == [fb["id"]]  # expired one auto-purged
    assert UUID(fa["id"]) not in svc.assets.rows
    # Idempotent: a second call returns the same survivors (no double-purge errors).
    assert await svc.list_trash(a) == listed


async def test_restore_idempotency_and_errors(tmp_path):
    svc = make_drive(tmp_path)
    a = uuid4()
    f = await _upload(svc, a, b"hi")

    with pytest.raises(DriveError) as e:
        await svc.restore_trash(a, UUID(f["id"]))  # not in trash yet
    assert e.value.status_code == 409
