"""Phase 4 end-to-end: private-corpus turns route to LOCAL_RAG, and Fail-Closed
escalation hands the SAME stream to the Agent.

Drives the real ``/chat/stream`` router (legacy SSE contract throughout). Three
routing facts are pinned:

* master + RETRIEVAL gate ON, a recalled hit the CRAG judge certifies ``relevant`` →
  the single-shot port answers grounded on [Rn] evidence; the seam ran with the
  tenant filter; ``run_stream`` is never entered;
* empty recall / non-affirmative verdict → the executor escalates BEFORE any event
  and the Agent pump takes over mid-stream (client sees only the Agent's frames —
  the escalation is an internal, pre-commit swap);
* gates OFF → pure dark launch: the seam is never touched, the Agent answers.

No WEB re-route exists anywhere on this path — that is the fail-closed contract.
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
PRIVATE_Q = "what does my knowledge base say about gradient descent"


class _HttpSession(FakeSession):
    async def get(self, model, pk):
        return None


class _Drive:
    async def ensure_asset_readable(self, user_id, asset_id):
        return None


class FakeSeam:
    """The cache-wrapped retrieval seam double — same retrieve() contract as the tool."""

    def __init__(self, hits):
        self.hits = list(hits)
        self.calls: list[tuple] = []

    async def retrieve(self, query, top_k=5, filters=None):
        self.calls.append((query, top_k, filters))
        return self.hits


class FakePort:
    """Reliability-port double: chat() answers the judge; chat_stream() generates."""

    def __init__(self, verdict="relevant", deltas=("It", " steps")):
        self._verdict = verdict
        self._deltas = list(deltas)
        self.judged = 0
        self.generated = 0

    async def chat(self, request, *, tools=None, model=None, base_url=None, api_key=None):
        self.judged += 1
        return {"content": f'{{"verdict": "{self._verdict}"}}', "tool_calls": [], "usage": {}}

    async def chat_stream(self, request, *, tools=None, model=None, base_url=None,
                          api_key=None, disable_thinking=False):
        assert tools is None
        self.generated += 1
        for d in self._deltas:
            yield {"type": "content", "data": d}
        yield {"type": "usage", "data": {"total_tokens": 2}}


class _Agent:
    def __init__(self, port):
        self.loop = SimpleNamespace(llm=port)
        self.agent_stream_calls = 0
        self.user_texts: list[str] = []
        self.contexts: list[dict | None] = []

    async def run_stream(self, user_text, history, **kw):  # AGENT path (fallback / dark)
        self.agent_stream_calls += 1
        self.user_texts.append(user_text)
        self.contexts.append(kw.get("context"))
        yield {"type": "done", "data": {"answer": "agent", "messages": [], "usage": None}}


def _harness(monkeypatch, port, seam):
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _HttpSession(Db(rows=[])))
    monkeypatch.setattr(chat_mod, "llm", Llm())
    monkeypatch.setattr(chat_mod, "_embedder", lambda: None)
    monkeypatch.setattr(chat_mod, "get_retriever", lambda: seam)

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


def _gates(monkeypatch, *, fast, retrieval, direct=False, viewer=False):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_retrieval_fast_path_enabled", retrieval, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", viewer, raising=False)


HIT = [{"id": "c1", "text": "Gradient descent steps along the negative gradient.", "score": 0.9, "meta": {}}]


async def _stream(app, message=PRIVATE_Q):
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


async def test_relevant_recall_answers_grounded_without_agent(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app)
    assert events == ["content", "content", "done"]
    assert done["answer"] == "It steps"
    assert len(seam.calls) == 1 and seam.calls[0][0] == PRIVATE_Q
    assert seam.calls[0][2] == {"user_id": str(USER)}  # tool-identical tenant scoping
    assert port.judged == 1 and port.generated == 1 and agent.agent_stream_calls == 0


@pytest.mark.parametrize("hits,verdict", [([], "relevant"), (HIT, "irrelevant")],
                         ids=["empty-recall", "judge-irrelevant"])
async def test_fail_closed_escalates_to_agent_before_any_event(monkeypatch, hits, verdict):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(verdict=verdict), FakeSeam(hits)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app)
    # Client sees ONLY the Agent's frames — the pre-commit swap is invisible on the wire.
    assert events == ["done"]
    assert done["answer"] == "agent"
    assert agent.agent_stream_calls == 1
    assert port.generated == 0  # the fast path emitted nothing before escalating
    assert port.judged == (0 if not hits else 1)  # empty recall never pays the judge


async def test_dark_launch_never_touches_the_retrieval_seam(monkeypatch):
    _gates(monkeypatch, fast=False, retrieval=True)  # master OFF gates everything
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app)
    assert events == ["done"]
    assert done["answer"] == "agent" and agent.agent_stream_calls == 1
    assert seam.calls == [] and port.judged == 0


async def test_retrieval_gate_off_stays_agent_even_when_recall_would_pass(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=False)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app)
    assert agent.agent_stream_calls == 1 and seam.calls == []
