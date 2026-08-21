"""Ref-counted purge: sharing a physical object, purging only the logical asset.

``delete_asset`` now only moves a file to the trash (bytes + ref_count kept); permanent
physical removal happens through ``purge_trash`` / ``empty_trash``. These tests drive that
purge path.
"""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from core.infrastructure.storage import object_key
from tests._drive_fakes import make_drive


async def _shared_pair(svc, content):
    a, b = uuid4(), uuid4()
    sha = hashlib.sha256(content).hexdigest()

    res = await svc.init_upload(a, sha, len(content), "a.txt", None, "text/plain", None)
    asset_a = UUID(res["asset_id"])
    await svc.store_chunk(a, asset_a, 0, content)
    await svc.complete_upload(a, asset_a)

    res2 = await svc.init_upload(b, sha, len(content), "b.txt", None, "text/plain", None)
    assert res2["status"] == "instant"
    asset_b = UUID(res2["asset"]["id"])
    return a, b, sha, asset_a, asset_b


async def test_delete_one_keeps_physical_object_for_other_user(tmp_path):
    svc = make_drive(tmp_path)
    content = b"shared bytes"
    a, b, sha, asset_a, asset_b = await _shared_pair(svc, content)

    # Trash only: delete does NOT touch the physical object or ref_count.
    r = await svc.delete_asset(a, asset_a)
    assert r["deleted"] is True
    assert r["physical_removed"] is False
    assert svc.objects.rows[sha].ref_count == 2
    assert await svc.storage.exists(object_key(sha)) is True

    # The other user still sees and can read it.
    other = await svc.ensure_asset_readable(b, asset_b)
    assert other.id == asset_b

    # Purging one trash entry drops ref_count but keeps the object for user b.
    pr = await svc.purge_trash(a, asset_a)
    assert pr["physical_removed"] is False
    assert svc.objects.rows[sha].ref_count == 1
    assert await svc.storage.exists(object_key(sha)) is True


async def test_purge_last_reference_removes_physical_object(tmp_path):
    svc = make_drive(tmp_path)
    content = b"shared bytes"
    a, b, sha, asset_a, asset_b = await _shared_pair(svc, content)

    await svc.delete_asset(a, asset_a)
    await svc.delete_asset(b, asset_b)

    # Both are in the trash; purging the first keeps bytes for the second.
    assert (await svc.purge_trash(a, asset_a))["physical_removed"] is False
    r = await svc.purge_trash(b, asset_b)
    assert r["physical_removed"] is True
    assert sha not in svc.objects.rows
    assert await svc.storage.exists(object_key(sha)) is False


async def test_concurrent_reincrement_keeps_file(tmp_path):
    """If a concurrent upload re-increments between decrement and the CAS delete, the file is kept."""
    svc = make_drive(tmp_path)
    content = b"shared bytes"
    a, b, sha, asset_a, asset_b = await _shared_pair(svc, content)

    await svc.delete_asset(a, asset_a)
    await svc.delete_asset(b, asset_b)

    # Decrement happens inside purge_trash; simulate a concurrent instant-upload by
    # making the CAS delete see a fresh reference (delete_if_zero then returns None).
    async def _reincrement_delete_if_zero(s):
        svc.objects.rows[s].ref_count = 1  # concurrent upload landed between the two steps
        return None

    svc.objects.delete_if_zero = _reincrement_delete_if_zero  # shadow instance method
    r = await svc.purge_trash(a, asset_a)

    assert r["physical_removed"] is False
    assert sha in svc.objects.rows
    assert await svc.storage.exists(object_key(sha)) is True


async def test_delete_idempotent(tmp_path):
    svc = make_drive(tmp_path)
    content = b"x"
    a = uuid4()
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(a, sha, 1, "a.txt", None, None, None)
    asset_a = UUID(res["asset_id"])
    await svc.store_chunk(a, asset_a, 0, content)
    await svc.complete_upload(a, asset_a)

    await svc.delete_asset(a, asset_a)
    # Deleting again: asset already soft-deleted → 404 (get_active returns None).
    with pytest.raises(DriveError) as e:
        await svc.delete_asset(a, asset_a)
    assert e.value.status_code == 404
