"""Phase 2 end-to-end: with the DIRECT gate on, a short pure turn skips the agent.

Drives the real ``/chat/stream`` router with the control-plane gates enabled and a
fake kernel. A plain "hello" (no viewer / attach / research) must resolve to the
DIRECT plan and be answered by the single-shot port — the agent's ``run_stream`` is
never entered — while the SSE frames keep the exact legacy shape (content deltas +
a terminal done carrying the answer).
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


class _HttpSession(FakeSession):
    async def get(self, model, pk):
        return None


class _Drive:
    async def ensure_asset_readable(self, user_id, asset_id):
        return None


class FakePort:
    """The kernel's reliability-wrapped port used by the DIRECT executor."""

    def __init__(self, deltas):
        self._deltas = list(deltas)
        self.calls = 0

    async def chat_stream(self, request, *, tools=None, model=None, base_url=None,
                          api_key=None, disable_thinking=False):
        self.calls += 1
        assert tools is None  # the direct path never advertises tools
        for d in self._deltas:
            yield {"type": "content", "data": d}
        yield {"type": "usage", "data": {"total_tokens": 3}}


class _Agent:
    """Fake kernel: exposes .loop.llm for DIRECT; run_stream records if AGENT is taken."""

    def __init__(self, port):
        self.loop = SimpleNamespace(llm=port)
        self.agent_stream_calls = 0

    async def run_stream(self, *a, **k):  # pragma: no cover - must NOT be called
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
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client, \
            client.stream("POST", "/chat/stream",
                          json={"message": message, "session_id": str(uuid4())}) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            events.append(evt.get("type"))
            if evt.get("type") == "done":
                done = evt["data"]
    return events, done


@pytest.mark.parametrize("gates", [(True, True), (False, False)],
                         ids=["direct-on", "dark"])
async def test_short_turn_routes_direct_when_gate_on(monkeypatch, gates):
    fast, direct = gates
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    port = FakePort(["Hel", "lo"])
    app, agent = _harness(monkeypatch, port)
    events, done = await _stream(app, "hello")
    assert done is not None
    # The terminal SSE frame is always last; the branch is distinguished below.
    assert events[-1] == "done"

    if fast and direct:
        assert done["answer"] == "Hello"
        # content deltas then the terminal done — legacy frame shape preserved.
        assert events == ["content", "content", "done"]
        # DIRECT taken: the single-shot port ran and the agent's run_stream never did.
        assert port.calls == 1 and agent.agent_stream_calls == 0
    else:
        # Dark launch: the AGENT path answered it, the direct port was never touched.
        assert done["answer"] == "agent"
        assert port.calls == 0 and agent.agent_stream_calls == 1


async def test_capability_turn_stays_on_agent_even_with_gate_on(monkeypatch):
    # "summarize my document" trips the private-retrieval signal → must NOT be direct.
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", True, raising=False)
    port = FakePort(["nope"])
    app, agent = _harness(monkeypatch, port)
    await _stream(app, "summarize my document please")
    assert agent.agent_stream_calls == 1 and port.calls == 0
