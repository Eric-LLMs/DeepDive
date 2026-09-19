"""Unit tests for the ``read_document`` chat tool (attached-document content extraction).

Same style as ``test_document_tools.py``: a real :class:`LocalStorage` at tmp_path holds
the asset bytes, and the SQL asset repository is monkeypatched with ``FakeAssets`` so no
database is touched.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from uuid import uuid4

from agent import Context
from agent.engine.decisions import ToolExecution
from agent.engine.runtime import ToolRuntime
from core.infrastructure.storage import LocalStorage, object_key

from apps.api.tools import read_document_tool
from tests._drive_fakes import FakeAssets


def _ctx(tmp_path) -> Context:
    ctx = Context()
    ctx.provide("storage", LocalStorage(tmp_path))
    ctx.provide("session_factory", object)  # unused: the repo is monkeypatched
    return ctx


async def _register(monkeypatch, tmp_path, name: str, data: bytes) -> tuple[ToolRuntime, str]:
    """Store ``data`` as an asset named ``name`` and return (runtime, asset_id)."""
    storage = LocalStorage(tmp_path)
    sha = hashlib.sha256(data).hexdigest()
    await storage.put(object_key(sha), data)
    assets = FakeAssets()
    asset = await assets.create(uuid4(), name, object_sha256=sha)
    monkeypatch.setattr(read_document_tool, "SqlAssetRepository", lambda sf: assets)
    runtime = ToolRuntime()
    read_document_tool.register(runtime, _ctx(tmp_path), llm=None)
    return runtime, str(asset.id)


async def _call(runtime: ToolRuntime, asset_id: str):
    return await runtime.execute(
        ToolExecution("c1", "read_document", {"asset_id": asset_id})
    )


async def test_reads_plain_text(monkeypatch, tmp_path):
    runtime, asset_id = await _register(monkeypatch, tmp_path, "notes.txt", b"hello document")
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "hello document" in str(res.value)


async def test_reads_markdown_and_csv(monkeypatch, tmp_path):
    runtime, a1 = await _register(monkeypatch, tmp_path, "doc.md", b"# Heading\nbody")
    assert "Heading" in str((await _call(runtime, a1)).value)
    runtime, a2 = await _register(monkeypatch, tmp_path, "t.csv", b"a,b\n1,2")
    assert "1,2" in str((await _call(runtime, a2)).value)


async def test_reads_docx(monkeypatch, tmp_path):
    import docx

    buf = BytesIO()
    d = docx.Document()
    d.add_paragraph("docx paragraph one")
    d.save(buf)
    runtime, asset_id = await _register(monkeypatch, tmp_path, "report.docx", buf.getvalue())
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "docx paragraph one" in str(res.value)


async def test_reads_xlsx(monkeypatch, tmp_path):
    from openpyxl import Workbook

    buf = BytesIO()
    wb = Workbook()
    wb.active["A1"] = "quarter"
    wb.active["B1"] = 42
    wb.save(buf)
    runtime, asset_id = await _register(monkeypatch, tmp_path, "book.xlsx", buf.getvalue())
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "quarter" in str(res.value)
    assert "42" in str(res.value)


async def test_reads_pdf(monkeypatch, tmp_path):
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "pdf body text")
    data = doc.tobytes()
    doc.close()
    runtime, asset_id = await _register(monkeypatch, tmp_path, "paper.pdf", data)
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "pdf body text" in str(res.value)


async def test_image_routed_to_vision_hint(monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    runtime, asset_id = await _register(monkeypatch, tmp_path, "shot.png", png)
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "vision" in str(res.value)


async def test_unsupported_type_returns_message(monkeypatch, tmp_path):
    runtime, asset_id = await _register(monkeypatch, tmp_path, "pkg.zip", b"PK\x03\x04junk")
    res = await _call(runtime, asset_id)
    assert res.is_error is False
    assert "Cannot extract text" in str(res.value)


async def test_legacy_xls_rejected_with_resave_hint(monkeypatch, tmp_path):
    runtime, asset_id = await _register(monkeypatch, tmp_path, "old.xls", b"\xd0\xcf\x11\xe0junk")
    res = await _call(runtime, asset_id)
    assert "xlsx" in str(res.value)


async def test_long_document_is_truncated(monkeypatch, tmp_path):
    from apps.api.tools.read_document_tool import MAX_OUTPUT_CHARS

    payload = b"x" * (MAX_OUTPUT_CHARS + 500)
    runtime, asset_id = await _register(monkeypatch, tmp_path, "big.txt", payload)
    res = await _call(runtime, asset_id)
    assert "truncated" in str(res.value)
    assert len(str(res.value)) < MAX_OUTPUT_CHARS + 200


async def test_missing_asset_raises(monkeypatch, tmp_path):
    runtime, _ = await _register(monkeypatch, tmp_path, "a.txt", b"x")
    res = await _call(runtime, str(uuid4()))
    assert res.is_error is True
