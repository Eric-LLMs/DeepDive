"""Viewer Context Provider — chat-router wiring (S1).

Helper-level coverage of the router seam (the /chat and /chat/stream entry points share
these): permission checks own the drive here (``viewer_context`` stays pure), forged asset
ids drop the identity but never 403 the request, ``too_large``/``unavailable`` abort
before the agent runs, the user message is NEVER prefixed with viewer content, and the
per-turn metadata lands under dedicated ``meta["viewer"]`` / ``meta["viewer_citations"]``
keys that cannot collide with ``meta["retrieval"]``.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from api.auth import AuthUser
from api.routers import chat as chat_mod
from api.schemas import ChatRequest, ViewerPayload, ViewerSelection
from api.viewer_context import build_viewer_blocks
from core.application.drive_service import DriveError

AID = uuid4()
USER = uuid4()


class _Drive:
    """ensure_asset_readable passes only for ids in ``readable``."""

    def __init__(self, readable=()):
        self.readable = {str(r) for r in readable}
        self.calls: list[tuple] = []

    async def ensure_asset_readable(self, user_id, asset_id):
        self.calls.append((user_id, str(asset_id)))
        if str(asset_id) not in self.readable:
            raise DriveError("no access", status_code=403)


def _body(message="这段在讲什么", **viewer_kw) -> ChatRequest:
    base = dict(name="paper.pdf", kind="pdf", provenance="cloud", asset_id=AID,
                focus_text="Attention is a mechanism.", page=7)
    base.update(viewer_kw)
    return ChatRequest(message=message, viewer=ViewerPayload(**base))


# ── _asset_readable: never raises, always a boolean ───────────────────────────

async def test_asset_readable_matrix():
    d = _Drive(readable={AID})
    assert await chat_mod._asset_readable(d, USER, AID) is True
    assert await chat_mod._asset_readable(d, USER, uuid4()) is False   # DriveError
    assert await chat_mod._asset_readable(d, USER, "not-a-uuid") is False  # ValueError
    assert await chat_mod._asset_readable(d, USER, None) is False
    assert await chat_mod._asset_readable(d, None, AID) is False       # guest without id


# ── _build_viewer_assembly ─────────────────────────────────────────────────────

async def test_assembly_none_without_viewer():
    body = ChatRequest(message="hi")
    assert await chat_mod._build_viewer_assembly(body, _Drive(), USER) is None


async def test_focus_injected_and_user_message_untouched():
    body = _body()
    a = await chat_mod._build_viewer_assembly(body, _Drive(readable={AID}), USER)
    assert a["mode"] == "focus" and a["status"] == "injected"
    assert [b.tag for b in a["blocks"]] == ["V1"]
    assert a["blocks"][0].asset_id == str(AID)
    # CRITICAL boundary: the router NEVER splices viewer content into the user message.
    assert body.message == "这段在讲什么"


async def test_forged_asset_200_drops_identity_keeps_user_text():
    body = _body(focus_text=None, page=None, mode="none", follow=False,
                 selections=[ViewerSelection(kind="text", text="MY SELECTION")])
    a = await chat_mod._build_viewer_assembly(body, _Drive(readable=set()), USER)
    assert a["mode"] == "none" and a["status"] == "injected"  # request continues (200)
    assert "unauthorized_asset" in a["rejected"]
    assert a["blocks"][0].asset_id is None and a["blocks"][0].text == "MY SELECTION"


async def test_frame_permissions_checked_per_image_asset():
    ok, bad = uuid4(), uuid4()
    d = _Drive(readable={AID, ok})
    body = _body(selections=[
        ViewerSelection(kind="frame", image_asset_id=ok, locator={"t_ms": 1000}),
        ViewerSelection(kind="frame", image_asset_id=bad, locator={"t_ms": 2000}),
    ])
    a = await chat_mod._build_viewer_assembly(body, d, USER)
    kinds = [b.kind for b in a["blocks"]]
    # bad frame dropped; the readable frame rides as P0 ahead of the FOCUS page block
    assert kinds == ["frame", "page"]
    assert any(r.startswith(f"unauthorized_frame:{bad}") for r in a["rejected"])
    # each unique image id checked once, plus the viewer's own asset
    assert sorted(c[1] for c in d.calls) == sorted({str(AID), str(ok), str(bad)})


# ── _viewer_abort: honest short-circuit, no downgrade ─────────────────────────

def test_viewer_abort_only_for_short_circuit_statuses():
    v = _body().viewer
    injected = build_viewer_blocks(v, "这段在讲什么")
    assert chat_mod._viewer_abort(injected) is None
    assert chat_mod._viewer_abort(None) is None
    # char hint alone (no full_text) proves the budget — not the transport cap — decides
    tl = build_viewer_blocks(
        ViewerPayload(name="big.pdf", kind="pdf", asset_id=AID,
                      full_chars=400_000, full_trusted=True),
        "总结全文")
    ab = chat_mod._viewer_abort(tl)
    assert ab == {"mode": "full", "status": "too_large", "rejected": []}
    un = build_viewer_blocks(
        ViewerPayload(name="p.pdf", asset_id=AID, full_text="partial",
                      full_chars=7, full_trusted=False),
        "总结全文")
    assert chat_mod._viewer_abort(un)["status"] == "unavailable"


# ── _viewer_post_turn: dedicated meta keys, citation validation ───────────────

async def test_post_turn_persists_dedicated_keys(monkeypatch):
    saved: list[tuple] = []

    async def fake_persist(message_id, key, value):
        saved.append((message_id, key, value))

    monkeypatch.setattr(chat_mod, "_persist_turn_meta", fake_persist)
    body = _body()
    a = build_viewer_blocks(body.viewer, body.message)
    payload = await chat_mod._viewer_post_turn(
        a, body, "as [V1] says … and also [V9]", [], "uid-1", "aid-1")
    keys = {(m, k) for m, k, _ in saved}
    assert keys == {("uid-1", "viewer"), ("aid-1", "viewer_citations")}
    snapshot = saved[0][2]
    assert snapshot["asset_id"] == str(AID) and snapshot["injected"][0]["tag"] == "V1"
    citations = saved[1][2]
    assert set(citations["map"]) == {"V1"}
    assert set(citations["cited"]) == {"V1"}
    assert citations["invalid"] == ["V9"]
    assert payload["citations"]["V1"]["locator"] == {"page": 7}
    # Non-interference: retrieval meta is a different key written elsewhere.
    assert all(k != "retrieval" for _, k, _ in saved)


async def test_post_turn_skips_non_injected(monkeypatch):
    calls = []
    monkeypatch.setattr(chat_mod, "_persist_turn_meta",
                        lambda *a: calls.append(a))
    # follow=False keeps this a true ``none`` — a followed pdf viewer would now stub.
    v = ViewerPayload(name="x.pdf", asset_id=AID, follow=False)  # no focus text → no blocks
    a = build_viewer_blocks(v, "今天天气怎么样")
    assert a["status"] == "none"
    assert await chat_mod._viewer_post_turn(a, ChatRequest(message="今天天气怎么样", viewer=v),
                                            "sunny", [], "u", "x") is None
    assert calls == []


async def test_post_turn_tolerates_missing_ids(monkeypatch):
    saved: list[tuple] = []

    async def fake_persist(message_id, key, value):
        saved.append((message_id, key))

    monkeypatch.setattr(chat_mod, "_persist_turn_meta", fake_persist)
    body = _body()
    a = build_viewer_blocks(body.viewer, body.message)
    p = await chat_mod._viewer_post_turn(a, body, "[V1]", [], None, None)
    assert saved == [] and p["status"] == "injected"  # done frame still carries citations


# ── stub trace: tool-driven reads recorded on the assistant row ───────────────

async def test_post_turn_stub_trace_captures_failed_reads(monkeypatch):
    saved: list[tuple] = []

    async def fake_persist(message_id, key, value):
        saved.append((message_id, key, value))

    monkeypatch.setattr(chat_mod, "_persist_turn_meta", fake_persist)
    v = ViewerPayload(name="paper.pdf", kind="pdf", provenance="cloud", asset_id=AID,
                      page=3, focus_text=None)
    # "今天天气怎么样" matches no regex → NONE; followed readable pdf → stub
    a = build_viewer_blocks(v, "今天天气怎么样")
    assert a["status"] == "stub" and a["stub"]["asset_id"] == str(AID)

    messages = [
        {"role": "user", "content": "今天天气怎么样"},
        {"role": "assistant", "content": "", "tool_calls": [
            # FAILED call (string args, bad pages) — must still be traced
            {"id": "c1", "name": "read_document",
             "arguments": json.dumps({"asset_id": str(AID), "pages": "0"})},
            # call against a different asset — must be ignored
            {"id": "c2", "name": "read_document",
             "arguments": json.dumps({"asset_id": str(uuid4())})},
            # successful full-document read — dict args shape, pages None
            {"id": "c3", "name": "read_document",
             "arguments": {"asset_id": str(AID)}},
        ]},
    ]
    p = await chat_mod._viewer_post_turn(
        a, ChatRequest(message="今天天气怎么样", viewer=v), "answer", messages,
        "uid-9", "aid-9")
    assert p == {"mode": "none", "status": "stub", "asset_id": str(AID),
                 "current_page": 3,
                 "reads": [{"tool_call_id": "c1", "pages": "0"},
                           {"tool_call_id": "c3", "pages": None}]}
    assert saved == [("aid-9", "viewer", p)]  # assistant row only; user snapshot is injected-only


# ── streaming E2E: /chat/stream stub routing + failed-read trace in done ─────

import httpx  # noqa: E402
from agent.security.approvals import MemoryApprovalBroker  # noqa: E402
from api.auth import require_user_optional  # noqa: E402
from api.deps import get_drive_service, get_task_queue  # noqa: E402
from api.routers.chat import router as chat_router  # noqa: E402
from core.infrastructure.db import UserRoleModel  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from tests._memory_v2_fakes import Db, FakeSession, Llm  # noqa: E402


class _HttpSession(FakeSession):
    async def get(self, model, pk):
        return None


class _Agent:
    """Stands in for get_agent(): records the sunk context, emits one tool round trip."""

    def __init__(self, done_payload):
        self.done_payload = done_payload
        self.contexts: list[dict | None] = []

    async def run_stream(self, user_text, history, **kw):
        self.contexts.append(kw.get("context"))
        yield {"type": "content", "data": "checking the page…"}
        yield {"type": "done", "data": self.done_payload}


async def test_stream_stub_enters_agent_and_traces_failed_read(monkeypatch, tmp_path):
    sid = uuid4()
    db = Db(rows=[])
    monkeypatch.setattr(chat_mod, "SessionLocal", lambda: _HttpSession(db))
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
    saved: list[tuple] = []

    async def _persist(message_id, key, value):
        saved.append((message_id, key, value))

    monkeypatch.setattr(chat_mod, "_persist_turn_meta", _persist)

    async def _usage(*a, **k):
        return None

    monkeypatch.setattr(chat_mod, "_log_usage", _usage)
    monkeypatch.setattr(chat_mod, "get_approval_bridge",
                        lambda: SimpleNamespace(broker=MemoryApprovalBroker()))

    messages = [
        {"role": "user", "content": "今天天气怎么样"},
        # assistant tried a page-scoped read with an invalid spec — the tool FAILED;
        # the trace contract records attempted calls, not just successful ones.
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "read_document",
             "arguments": json.dumps({"asset_id": str(AID), "pages": "0"})},
        ]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "Error: pages must be 1-based, got '0'"},
    ]
    agent = _Agent({"answer": "读到了。", "messages": messages, "usage": None})
    monkeypatch.setattr(chat_mod, "get_agent", lambda: agent)

    app = FastAPI()
    app.include_router(chat_router)
    app.dependency_overrides[require_user_optional] = lambda: AuthUser(
        user_id=USER, username="alice", display_name=None,
        role=UserRoleModel(role_id="user", role_name="User"), token_id=uuid4())
    app.dependency_overrides[get_task_queue] = lambda: SimpleNamespace(
        enqueue=lambda *a, **k: asyncio.sleep(0))
    app.dependency_overrides[get_drive_service] = lambda: _Drive(readable={AID})

    body = {
        "message": "今天天气怎么样",
        "session_id": str(sid),
        "viewer": {"name": "paper.pdf", "kind": "pdf", "provenance": "cloud",
                   "asset_id": str(AID), "page": 3, "follow": True},
    }
    transport = httpx.ASGITransport(app=app)
    done = None
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("POST", "/chat/stream", json=body) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                evt = json.loads(line[5:].strip())
                if evt.get("type") == "viewer":
                    raise AssertionError("stub must not emit a viewer abort frame")
                if evt.get("type") == "done":
                    done = evt["data"]
    assert done is not None
    # 1) the stub assembly was sunk into the agent context (Access Context was in the prompt)
    assert agent.contexts and agent.contexts[0]["viewer"]["status"] == "stub"
    # 2) the done frame carries the trace; the FAILED tool call is in `reads`
    vp = done["viewer"]
    assert vp["status"] == "stub" and vp["asset_id"] == str(AID)
    assert vp["current_page"] == 3
    assert vp["reads"] == [{"tool_call_id": "c1", "pages": "0"}]
