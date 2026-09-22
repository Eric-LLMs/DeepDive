"""Phase 3 end-to-end: a viewer-grounded turn routes to the VIEWER executor (and only
under the viewer gate).

Drives the real ``/chat/stream`` router with a text selection attached (an
already-injected block). Three routing facts are pinned:

* master + VIEWER gate ON → the single-shot port answers it grounded on the [Vn]
  section; the agent's ``run_stream`` is never entered;
* master gate OFF (dark launch) → the Agent answers, byte-identical to Phase 2;
* DIRECT-only gate ON but VIEWER gate OFF → a viewer-demand turn is NOT direct (the
  viewer is a capability), so it still routes to the Agent — proving per-kind gating
  and that "a viewer is open" alone can never hijack the fast path.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from agent.security.approvals import MemoryApprovalBroker
from api.auth import AuthUser, require_user_optional
from api.deps import get_drive_service, get_task_queue
from api.routers import chat as chat_mod
from api.routers.chat import router as chat_router
from core.config import settings
from core.infrastructure.db import UserRoleModel
from fastapi import FastAPI

from tests._memory_v2_fakes import Db, FakeSession, Llm

USER = uuid4()

# An injected text selection (kind="text") → viewer_context assembles it as a
# ``selection`` block, status="injected" — the sole eligible VIEWER fast-path input.
VIEWER_BODY = {
    "name": "report.pdf", "kind": "pdf", "page": 3,
    "selections": [{"kind": "text", "text": "The mitochondria is the powerhouse of the cell."}],
}


class _HttpSession(FakeSession):
    async def get(self, model, pk):
        return None


class _Drive:
    async def ensure_asset_readable(self, user_id, asset_id):
        return None


class FakePort:
    """The kernel's reliability-wrapped port used by the VIEWER (single-shot) executor."""

    def __init__(self, deltas):
        self._deltas = list(deltas)
        self.calls: list[dict] = []

    async def chat_stream(self, request, *, tools=None, model=None, base_url=None,
                          api_key=None, disable_thinking=False):
        self.calls.append({"request": request, "tools": tools})
        assert tools is None  # a grounded turn never dispatches read_document/vision
        for d in self._deltas:
            yield {"type": "content", "data": d}
        yield {"type": "usage", "data": {"total_tokens": 4}}


class _Agent:
    def __init__(self, port):
        self.loop = SimpleNamespace(llm=port)
        self.agent_stream_calls = 0

    async def run_stream(self, *a, **k):  # pragma: no cover - AGENT path
        self.agent_stream_calls += 1
        yield {"type": "done", "data": {"answer": "agent", "messages": [], "usage": None}}


def _harness(monkeypatch, port):
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _HttpSession(Db(rows=[])))
    monkeypatch.setattr(chat_mod, "llm", Llm())
    monkeypatch.setattr(chat_mod, "_embedder", lambda: None)

    async def _route(session, token, role_id):
        return "http://fake", "key", "model", "biz", None
    monkeypatch.setattr(chat_mod, "_resolve_chat_route", _route)

    async def _authz(session, uid, role):
        return "free"
    monkeypatch.setattr(chat_mod, "authorize_usage", _authz)
    monkeypatch.setattr(chat_mod, "_resolve_research_context",
                        lambda *a, **k: (None, None, None, None))

    async def _persist(message_id, key, value):
        return None
    monkeypatch.setattr(chat_mod, "_persist_turn_meta", _persist)

    async def _usage(*a, **k):
        return None
    monkeypatch.setattr(chat_mod, "_log_usage", _usage)
    monkeypatch.setattr(chat_mod, "get_approval_bridge",
                        lambda: SimpleNamespace(broker=MemoryApprovalBroker()))
    agent = _Agent(port)
    monkeypatch.setattr(chat_mod, "get_agent", lambda: agent)

    app = FastAPI()
    app.include_router(chat_router)
    app.dependency_overrides[require_user_optional] = lambda: AuthUser(
        user_id=USER, username="alice", display_name=None,
        role=UserRoleModel(role_id="user", role_name="User"), token_id=uuid4())
    app.dependency_overrides[get_task_queue] = lambda: SimpleNamespace(
        enqueue=lambda *a, **k: asyncio.sleep(0))
    app.dependency_overrides[get_drive_service] = lambda: _Drive()
    return app, agent


async def _stream(app, message):
    transport = httpx.ASGITransport(app=app)
    events, done = [], None
    body = {"message": message, "session_id": str(uuid4()), "viewer": VIEWER_BODY}
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client, \
            client.stream("POST", "/chat/stream", json=body) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            events.append(evt.get("type"))
            if evt.get("type") == "done":
                done = evt["data"]
    return events, done


# (master, direct, viewer) gate triples.
async def test_viewer_turn_routes_viewer_when_gate_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", True, raising=False)
    port = FakePort(["Powerhouse", " of the cell."])
    app, agent = _harness(monkeypatch, port)
    events, done = await _stream(app, "what does the selection say?")
    assert done is not None and events[-1] == "done"
    assert done["answer"] == "Powerhouse of the cell."
    assert events == ["content", "content", "done"]
    assert len(port.calls) == 1 and agent.agent_stream_calls == 0
    # The grounded system prompt carried the injected [V1] block through the shared renderer.
    sent = port.calls[0]["request"][0]
    assert sent["role"] == "system" and "[V1]" in sent["content"]


@pytest.mark.parametrize("gates", [
    (False, False, False),  # dark launch → Agent
    (True, True, False),    # DIRECT-only gate on, VIEWER off → viewer demand stays Agent
], ids=["dark", "direct-only-viewer-off"])
async def test_viewer_turn_stays_on_agent_without_viewer_gate(monkeypatch, gates):
    fast, direct, viewer = gates
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", viewer, raising=False)
    port = FakePort(["nope"])
    app, agent = _harness(monkeypatch, port)
    await _stream(app, "what does the selection say?")
    assert agent.agent_stream_calls == 1 and len(port.calls) == 0
