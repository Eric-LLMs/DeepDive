"""Incremental-reindex trigger: update_content flags whether content actually changed."""
import hashlib
from uuid import UUID, uuid4

from core.application.drive_service import DriveError

from tests._drive_fakes import make_drive


async def _make_text_asset(svc, user, content, name="note.md"):
    """Upload a READY text asset via the chunked path."""
    sha = hashlib.sha256(content).hexdigest()
    res = await svc.init_upload(user, sha, len(content), name, None, "text/markdown", None)
    asset_id = UUID(res["asset_id"])
    await svc.store_chunk(user, asset_id, 0, content)
    done = await svc.complete_upload(user, asset_id)
    assert done["asset"]["file_status"] == "READY"
    return asset_id


async def test_rewrite_flags_content_changed(tmp_path):
    svc = make_drive(tmp_path)
    user = uuid4()
    asset_id = await _make_text_asset(svc, user, b"v1 of the note")

    res = await svc.update_content(user, asset_id, "v2 of the note")
    assert res["content_changed"] is True
    assert res["asset"]["object_sha256"] != hashlib.sha256(b"v1 of the note").hexdigest()
    # Storage now holds the new bytes.
    from core.infrastructure.storage import object_key

    new_sha = res["asset"]["object_sha256"]
    assert await svc.storage.get(object_key(new_sha)) == b"v2 of the note"
    # The asset resets to NOT_STARTED — its previous import is now stale.
    assert res["asset"]["rag_status"] == "NOT_STARTED"


async def test_identical_rewrite_is_no_op(tmp_path):
    svc = make_drive(tmp_path)
    user = uuid4()
    content = b"unchanging note"
    asset_id = await _make_text_asset(svc, user, content)

    res = await svc.update_content(user, asset_id, content.decode())
    assert res["content_changed"] is False
    assert res["asset"]["object_sha256"] == hashlib.sha256(content).hexdigest()
    assert res["asset"]["rag_status"] == "NOT_STARTED"


async def test_non_text_asset_rejected(tmp_path):
    svc = make_drive(tmp_path)
    user = uuid4()
    asset_id = await _make_text_asset(svc, user, b"some binary-ish", name="f.bin")
    # Make it a genuine binary asset (the helper defaults to text/markdown).
    asset = svc.assets.rows[asset_id]
    asset.mime_type = "application/octet-stream"

    # A READY binary asset is not a text file → update_content refuses.
    try:
        await svc.update_content(user, asset_id, "text")
        assert False, "expected DriveError"
    except DriveError as e:
        assert e.status_code == 415
