"""Tests for session-scoped toolkit generation (chat transcript → Cloud Drive artifacts).

Covers the pure session_source helpers, ``DriveService.save_artifact``, the
``/toolkit/generate`` session-mode router branch, and the worker's session job branch. The
latter two use fake sessions / queue / pipeline so no real DB, Redis, or model service is
needed; only the workspace temp-dir is real (``tmp_path``).
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.application.drive_service import RAG_NOT_STARTED, READY
from core.config import settings
from core.infrastructure.jobs import TOOLKIT_GENERATE
from core.infrastructure.storage import object_key
from fastapi import HTTPException

from apps.api.routers.jobs import _confined_folder_path, generate_toolkit
from apps.api.schemas import ToolkitGenerateRequest
from apps.api.tools.toolkit.session_source import (
    SESSION_SRC_DIR,
    artifact_plan,
    build_transcript,
    cleanup_stale_sources,
    sanitize_name,
)
from tests._drive_fakes import make_drive

# ── session_source: pure transcript / artifact helpers ──

def test_sanitize_name_replaces_illegal_chars_and_caps_length():
    assert sanitize_name("My Session") == "My Session"
    assert sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_name("  spaced   out  ") == "spaced out"
    assert sanitize_name("") == "session"
    assert sanitize_name(None) == "session"
    assert sanitize_name("x" * 200)[:80] == "x" * 80
    assert len(sanitize_name("x" * 200)) == 80


def test_build_transcript_filters_tool_system_and_empty():
    msgs = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "tool", "content": "huge raw tool output"},
        {"role": "system", "content": "system noise"},
        {"role": "user", "content": ""},
        {"role": "user", "content": "   "},
    ]
    out = build_transcript("My Session", msgs)
    assert out.startswith("# My Session")
    assert "**User:** Hi" in out
    assert "**Assistant:** Hello" in out
    assert "tool output" not in out
    assert "system noise" not in out


def test_build_transcript_without_title_skips_heading():
    out = build_transcript(None, [{"role": "user", "content": "Hi"}])
    assert not out.startswith("#")
    assert "**User:** Hi" in out


def test_build_transcript_empty_raises():
    with pytest.raises(ValueError, match="no messages"):
        build_transcript("T", [{"role": "tool", "content": "x"}, {"role": "system", "content": "y"}])


def test_artifact_plan_maps_extensions_to_names():
    mind = artifact_plan("mindmap", "My Session")
    assert mind == {".mmd": ("My Session_mindmap.mmd", "text/plain")}
    summ = artifact_plan("summary", "My Session")
    assert summ == {".md": ("My Session_summary.md", "text/markdown")}
    slides = artifact_plan("slides", "My Session")
    assert set(slides) == {".md", ".pptx"}
    assert slides[".md"] == ("My Session_slides.md", "text/markdown")
    assert slides[".pptx"] == (
        "My Session_slides.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


def test_artifact_plan_sanitizes_title():
    plan = artifact_plan("mindmap", "bad:name/*?")
    assert plan[".mmd"][0].startswith("bad_name___")


def test_cleanup_stale_sources_removes_old_only(tmp_path):
    src = tmp_path / SESSION_SRC_DIR
    src.mkdir(parents=True)
    fresh = src / "fresh.md"
    fresh.write_text("x", encoding="utf-8")
    stale = src / "stale.md"
    stale.write_text("x", encoding="utf-8")
    old = time.time() - 25 * 3600  # > 24h
    os.utime(stale, (old, old))

    assert cleanup_stale_sources(tmp_path) == 1
    assert fresh.exists()
    assert not stale.exists()


def test_cleanup_stale_sources_missing_dir_is_noop(tmp_path):
    assert cleanup_stale_sources(tmp_path) == 0


# ── DriveService.save_artifact ──

async def test_save_artifact_writes_storage_object_and_asset(tmp_path):
    drive = make_drive(tmp_path)
    user = uuid.uuid4()
    content = b"mindmap bytes"
    asset = await drive.save_artifact(
        user, "map.mmd", "text/plain", content, folder_path="notes"
    )

    assert asset.name == "map.mmd"
    assert asset.folder_path == "notes"
    assert asset.file_status == READY
    assert asset.rag_status == RAG_NOT_STARTED
    assert asset.object_sha256 == hashlib.sha256(content).hexdigest()

    # Physical bytes landed under the sharded storage key; object row is ref-counted.
    key = object_key(hashlib.sha256(content).hexdigest())
    assert (tmp_path / key).read_bytes() == content
    assert drive.objects.rows[asset.object_sha256].ref_count == 1


async def test_save_artifact_dedupes_colliding_name(tmp_path):
    drive = make_drive(tmp_path)
    user = uuid.uuid4()
    a = await drive.save_artifact(user, "map.mmd", "text/plain", b"aa", folder_path="notes")
    b = await drive.save_artifact(user, "map.mmd", "text/plain", b"bb", folder_path="notes")
    assert a.name == "map.mmd"
    assert b.name == "map(1).mmd"
    assert {x.name for x in drive.assets.rows.values()} == {"map.mmd", "map(1).mmd"}


async def test_save_artifact_defaults_to_personal_root(tmp_path):
    drive = make_drive(tmp_path)
    asset = await drive.save_artifact(uuid.uuid4(), "n.md", "text/markdown", b"x")
    assert asset.workspace_id is None
    assert asset.folder_path is None


# ── /toolkit/generate session-mode router branch ──

def test_confined_folder_path_validates_segments():
    assert _confined_folder_path(None) is None
    assert _confined_folder_path("") is None
    assert _confined_folder_path("   ") is None
    assert _confined_folder_path("notes") == "notes"
    assert _confined_folder_path("a/b/c") == "a/b/c"
    for bad in ("../x", "a/../b", "/abs", "trail/"):
        with pytest.raises(HTTPException) as exc:
            _confined_folder_path(bad)
        assert exc.value.status_code == 400


class _FakeSessionRow:
    def __init__(self, user_id, title=None):
        self.user_id = user_id
        self.title = title


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def get(self, model, ident):
        return self._row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSessionLocal:
    def __init__(self, row):
        self._row = row

    def __call__(self):
        return _FakeSession(self._row)


class _FakeQueue:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, type_, payload, user_id=None):
        self.enqueued.append((type_, payload, user_id))
        return uuid.uuid4()


async def test_toolkit_generate_session_mode_enqueues(monkeypatch):
    owner = uuid.uuid4()
    monkeypatch.setattr(
        "apps.api.routers.jobs.SessionLocal",
        _FakeSessionLocal(_FakeSessionRow(owner)),
    )
    queue = _FakeQueue()
    body = ToolkitGenerateRequest(
        tool="mindmap", session_id=uuid.uuid4(), folder_path="notes"
    )
    res = await generate_toolkit(body, queue=queue, user=SimpleNamespace(user_id=owner))
    assert "job_id" in res

    (type_, payload, uid) = queue.enqueued[0]
    assert type_ == TOOLKIT_GENERATE
    assert payload["tool"] == "mindmap"
    assert payload["session_id"] == str(body.session_id)
    assert payload["folder_path"] == "notes"
    assert uid == owner


async def test_toolkit_generate_session_mode_ownership_404(monkeypatch):
    owner = uuid.uuid4()
    monkeypatch.setattr(
        "apps.api.routers.jobs.SessionLocal",
        _FakeSessionLocal(_FakeSessionRow(owner)),
    )
    queue = _FakeQueue()
    body = ToolkitGenerateRequest(tool="slides", session_id=uuid.uuid4())
    user = SimpleNamespace(user_id=uuid.uuid4())  # not the session owner
    with pytest.raises(HTTPException) as exc:
        await generate_toolkit(body, queue=queue, user=user)
    assert exc.value.status_code == 404
    assert queue.enqueued == []


async def test_toolkit_generate_session_mode_missing_session_404(monkeypatch):
    monkeypatch.setattr(
        "apps.api.routers.jobs.SessionLocal", _FakeSessionLocal(None)
    )
    queue = _FakeQueue()
    body = ToolkitGenerateRequest(tool="summary", session_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await generate_toolkit(body, queue=queue, user=SimpleNamespace(user_id=uuid.uuid4()))
    assert exc.value.status_code == 404
    assert queue.enqueued == []


async def test_toolkit_generate_rejects_mixed_modes():
    queue = _FakeQueue()
    body = ToolkitGenerateRequest(tool="mindmap", paths=["doc.md"], session_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await generate_toolkit(body, queue=queue, user=SimpleNamespace(user_id=uuid.uuid4()))
    assert exc.value.status_code == 400
    assert queue.enqueued == []


# ── worker session branch (_generate_from_session) ──

class _FakeJobRow:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeJobStore:
    def __init__(self, row):
        self._row = row

    async def get(self, job_id):
        return self._row


class _FakePipeline:
    """Writes one artifact per mapped extension so the worker's plan filter is exercised."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.called_with = None

    async def run(self, paths, output_dir=None, **params):
        self.called_with = list(paths)
        outs = []
        for ext in (".md", ".pptx", ".mmd"):
            p = self.workspace / f"artifact{len(outs)}{ext}"
            p.write_bytes(f"{ext} bytes".encode())
            outs.append(str(p))
        return SimpleNamespace(files=outs, summary="done")


class _FakeDrive:
    def __init__(self):
        self.saved = []

    async def save_artifact(self, user_id, name, mime, content, *, folder_path=None, workspace_id=None):
        asset = SimpleNamespace(id=uuid.uuid4(), name=name, folder_path=folder_path)
        self.saved.append((user_id, name, mime, content, folder_path))
        return asset


async def test_worker_session_branch_generates_and_saves(monkeypatch, tmp_path):
    from apps.worker import tasks as worker_tasks

    owner = uuid.uuid4()
    session_id = uuid.uuid4()
    monkeypatch.setattr(
        "apps.worker.tasks.SessionLocal",
        _FakeSessionLocal(_FakeSessionRow(owner, title="My Session")),
    )
    async def _fake_load_detail(_sf, _sid):
        return {
            "title": "My Session",
            "messages": [
                {"role": "user", "content": "Explain retrieval"},
                {"role": "assistant", "content": "Hybrid retrieval blends scores."},
                {"role": "tool", "content": "noise"},
            ],
        }

    monkeypatch.setattr("apps.worker.tasks.load_session_detail", _fake_load_detail)
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)

    pipeline = _FakePipeline(tmp_path)
    monkeypatch.setattr(
        "apps.api.tools.toolkit.pipeline_for", lambda tool, llm: pipeline
    )
    drive = _FakeDrive()
    monkeypatch.setattr("apps.worker.tasks.DriveService", lambda _sf: drive)

    job_id = uuid.uuid4()
    ctx = {"job_store": _FakeJobStore(_FakeJobRow(owner)), "llm": None}
    result = await worker_tasks._generate_from_session(
        ctx, str(job_id), {"tool": "slides", "session_id": str(session_id), "folder_path": "notes"}
    )

    assert result["tool"] == "slides"
    # Only the extensions in the slides plan (.md + .pptx) are saved; .mmd is filtered out.
    assert len(result["assets"]) == 2
    names = {a["name"] for a in result["assets"]}
    assert names == {"My Session_slides.md", "My Session_slides.pptx"}
    assert all(a["folder_path"] == "notes" for a in result["assets"])
    assert result["summary"] == "done"

    # The transcript was fed through the pipeline as a single workspace temp source.
    assert pipeline.called_with and Path(pipeline.called_with[0]).is_relative_to(
        tmp_path / SESSION_SRC_DIR
    )
    # Every save went to the job owner with the mapped mime + real bytes.
    assert len(drive.saved) == 2
    assert all(s[0] == owner for s in drive.saved)
    # The temp transcript was removed afterwards.
    src_dir = tmp_path / SESSION_SRC_DIR
    assert src_dir.is_dir() and not list(src_dir.glob("*.md"))


async def test_worker_session_branch_rejects_foreign_session(monkeypatch, tmp_path):
    from apps.worker import tasks as worker_tasks

    owner = uuid.uuid4()
    other = uuid.uuid4()
    monkeypatch.setattr(
        "apps.worker.tasks.SessionLocal",
        _FakeSessionLocal(_FakeSessionRow(other)),  # session belongs to another user
    )
    ctx = {"job_store": _FakeJobStore(_FakeJobRow(owner))}
    with pytest.raises(RuntimeError, match="session not found"):
        await worker_tasks._generate_from_session(
            ctx, str(uuid.uuid4()), {"tool": "summary", "session_id": str(uuid.uuid4())}
        )
