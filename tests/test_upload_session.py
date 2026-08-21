"""Chunked upload: resume state, full-hash verification, and complete idempotency."""
import hashlib
from uuid import UUID, uuid4

import pytest

from core.application.drive_service import DriveError
from core.config import settings
from tests._drive_fakes import make_drive


async def test_resume_reports_received_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "drive_chunk_size", 4)
    svc = make_drive(tmp_path)
    user = uuid4()
    content = b"0123456789"  # 10 bytes → 3 chunks (4/4/2)
    sha = hashlib.sha256(content).hexdigest()

    res = await svc.init_upload(user, sha, len(content), "big.txt", None, None, None)
    assert res["status"] == "uploading"
    assert res["num_chunks"] == 3
    asset_id = UUID(res["asset_id"])

    await svc.store_chunk(user, asset_id, 0, content[0:4])

    status = await svc.chunk_status(user, asset_id)
    assert status["received"] == [0]
    assert status["missing"] == [1, 2]

    # A chunk larger than chunk_size is rejected.
    with pytest.raises(DriveError) as e:
        await svc.store_chunk(user, asset_id, 1, b"0123456789abcdef")
    assert e.value.status_code == 400


async def test_complete_verifies_digest_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "drive_chunk_size", 4)
    svc = make_drive(tmp_path)
    user = uuid4()
    content = b"0123456789"
    sha = hashlib.sha256(content).hexdigest()

    res = await svc.init_upload(user, sha, len(content), "big.txt", None, None, None)
    asset_id = UUID(res["asset_id"])
    for i, part in enumerate((content[0:4], content[4:8], content[8:])):
        await svc.store_chunk(user, asset_id, i, part)

    done = await svc.complete_upload(user, asset_id)
    assert done["asset"]["file_status"] == "READY"

    # Second complete is a no-op and must not double-count the object.
    again = await svc.complete_upload(user, asset_id)
    assert again["asset"]["file_status"] == "READY"
    assert svc.objects.rows[sha].ref_count == 1


async def test_complete_rejects_wrong_digest(tmp_path):
    svc = make_drive(tmp_path)
    user = uuid4()
    content = b"real content"
    wrong_sha = hashlib.sha256(b"different content").hexdigest()

    res = await svc.init_upload(user, wrong_sha, len(content), "f.txt", None, None, None)
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)

    with pytest.raises(DriveError) as e:
        await svc.complete_upload(user, asset_id)
    assert e.value.status_code == 400

    # Session is marked failed; asset stays UPLOADING so the client can retry.
    session = await svc.uploads.get_by_asset(asset_id)
    assert session.status == "failed"
    assert (await svc.assets.get(asset_id)).file_status == "UPLOADING"


async def test_incomplete_upload_rejected_on_complete(tmp_path):
    svc = make_drive(tmp_path)
    user = uuid4()
    content = b"0123456789"
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(user, sha, len(content), "f.txt", None, None, None)
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content[0:4])  # only chunk 0

    with pytest.raises(DriveError) as e:
        await svc.complete_upload(user, asset_id)
    assert e.value.status_code == 400
