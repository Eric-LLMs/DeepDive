"""Boundary tests for the desktop chat-mic endpoint ``POST /stt`` (FunASR SenseVoice sidecar).

Nine scenarios per spec: auth rejection, MIME validation (415 + safe fallback), bounded
size guard (exact max passes, max+1 rejected), the happy path, dynamic MIME pass-through
to the client, and connection/timeout faults both surfacing as 502.

The router runs in a minimal app (no lifespan/DB); ``require_user`` is overridden per test
and :class:`jobs.STTClient` is swapped for a recording fake.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from api.auth import AuthUser, require_user
from api.routers import jobs as jobs_module
from api.routers.jobs import router as jobs_router
from core.infrastructure.db import UserRoleModel
from fastapi import FastAPI
from fastapi.testclient import TestClient

USER = uuid.uuid4()
_FAKE_ROLE = UserRoleModel(role_id="user", role_name="User")
_MAX = 50  # tiny cap so boundary tests stay cheap


class _FakeSTT:
    """Records the call arguments; its failure mode is set per test via ``exc``."""

    calls: list[dict] = []
    exc: Exception | None = None
    text = "hello world"

    def __init__(self, *a, **k):
        pass

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        cls = type(self)
        cls.calls.append(
            {"size": len(audio), "filename": filename, "content_type": content_type}
        )
        if cls.exc is not None:
            raise cls.exc
        return cls.text


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.dependency_overrides[require_user] = lambda: AuthUser(
        user_id=USER, username="alice", display_name=None,
        role=_FAKE_ROLE, token_id=uuid.uuid4(),
    )
    app.include_router(jobs_router)
    monkeypatch.setattr(
        jobs_module,
        "settings",
        SimpleNamespace(
            stt_base_url="http://localhost:18881/v1",
            stt_api_key="not-needed",
            stt_model="sensevoice",
            stt_max_bytes=_MAX,
            stt_timeout_seconds=30.0,
        ),
    )
    _FakeSTT.calls = []
    _FakeSTT.exc = None
    _FakeSTT.text = "hello world"
    monkeypatch.setattr(jobs_module, "STTClient", _FakeSTT)
    return TestClient(app)


def _post(client, content: bytes, content_type: str | None = "audio/webm", filename="voice.webm"):
    part: tuple = (filename, content) if content_type is None else (filename, content, content_type)
    return client.post("/stt", files={"file": part})


# ── 1. auth ─────────────────────────────────────────────────────────────────
def test_unauthenticated_rejected():
    app = FastAPI()
    app.include_router(jobs_router)  # no require_user override → real guard runs
    c = TestClient(app)
    r = c.post("/stt", files={"file": ("v.webm", b"x", "audio/webm")})
    assert r.status_code in (401, 403), r.status_code


# ── 2. MIME validation ──────────────────────────────────────────────────────
def test_non_audio_mime_rejected(client):
    r = _post(client, b"just text", content_type="text/plain")
    assert r.status_code == 415
    assert not _FakeSTT.calls  # rejected before any transcription


def test_missing_content_type_falls_back_to_octet_stream(client):
    # No explicit part type AND no guessable extension — the server sees a truly empty
    # content type and must fall back instead of rejecting.
    r = _post(client, b"abc", content_type=None, filename="voice")
    assert r.status_code == 200, r.text
    assert _FakeSTT.calls[0]["content_type"] == "application/octet-stream"


# ── 3. bounded size guard ───────────────────────────────────────────────────
def test_exact_max_allowed(client):
    r = _post(client, b"x" * _MAX)
    assert r.status_code == 200, r.text
    assert _FakeSTT.calls[0]["size"] == _MAX  # whole body forwarded, not truncated


def test_one_byte_over_max_rejected(client):
    r = _post(client, b"x" * (_MAX + 1))
    assert r.status_code == 413
    assert not _FakeSTT.calls


# ── 4. happy path ───────────────────────────────────────────────────────────
def test_transcription_returns_text(client):
    r = _post(client, b"audio-bytes")
    assert r.status_code == 200
    assert r.json() == {"text": "hello world"}


# ── 5. dynamic MIME pass-through ────────────────────────────────────────────
def test_mime_and_suffix_pass_through(client):
    r = _post(client, b"audio-bytes", content_type="audio/webm;codecs=opus", filename="voice.webm")
    assert r.status_code == 200, r.text
    call = _FakeSTT.calls[0]
    assert call["content_type"] == "audio/webm"  # base type, codec param stripped
    assert call["filename"] == "voice.webm"  # existing extension kept, not duplicated
    r2 = _post(client, b"audio-bytes", content_type="audio/mp4", filename="clip")
    assert r2.status_code == 200, r2.text
    assert _FakeSTT.calls[1]["content_type"] == "audio/mp4"
    assert _FakeSTT.calls[1]["filename"] == "clip.m4a"


# ── 6. sidecar faults → 502 ─────────────────────────────────────────────────
def test_connection_failure_maps_to_502(client):
    _FakeSTT.exc = httpx.ConnectError("connection refused")
    r = _post(client, b"audio-bytes")
    assert r.status_code == 502
    assert "transcription failed" in r.json()["detail"]


def test_timeout_maps_to_502(client):
    _FakeSTT.exc = httpx.ReadTimeout("timed out")
    r = _post(client, b"audio-bytes")
    assert r.status_code == 502
    assert "transcription failed" in r.json()["detail"]
