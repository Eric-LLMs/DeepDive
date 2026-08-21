"""Instant-upload (秒传) dedup: a second asset with the same SHA-256 shares the physical object."""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from core.infrastructure.storage import object_key
from tests._drive_fakes import make_drive


async def _upload(svc, user, content, name="f.txt"):
    """Full chunked upload of ``content``, returning the asset id."""
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(user, sha, len(content), name, None, "text/plain", None)
    assert res["status"] == "uploading"
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    done = await svc.complete_upload(user, asset_id)
    assert done["asset"]["file_status"] == "READY"
    return asset_id


async def test_second_asset_with_same_digest_is_instant(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    content = b"shared learning notes\n" * 100
    sha = hashlib.sha256(content).hexdigest()

    await _upload(svc, a, content, "a.txt")

    # Second user, same bytes → instant, no chunk upload.
    res = await svc.init_upload(b, sha, len(content), "b.txt", None, "text/plain", None)
    assert res["status"] == "instant"
    assert res["dedup"] is True
    assert res["asset"]["object_sha256"] == sha

    # One physical object, ref_count == 2, both assets point at it.
    obj = svc.objects.rows[sha]
    assert obj.ref_count == 2
    assert await svc.storage.get(object_key(sha)) == content


async def test_instant_upload_does_not_need_chunk_storage(tmp_path):
    svc = make_drive(tmp_path)
    a, b = uuid4(), uuid4()
    content = b"dedup me"
    await _upload(svc, a, content)
    before = len(svc.uploads.rows)

    res = await svc.init_upload(
        b, hashlib.sha256(content).hexdigest(), len(content), "copy.txt",
        None, "text/plain", None,
    )
    assert res["status"] == "instant"
    # The instant path must not create a new upload session.
    assert len(svc.uploads.rows) == before


async def test_invalid_sha256_rejected(tmp_path):
    svc = make_drive(tmp_path)
    with pytest.raises(DriveError) as e:
        await svc.init_upload(uuid4(), "not-a-digest", 10, "x.txt", None, None, None)
    assert e.value.status_code == 400
