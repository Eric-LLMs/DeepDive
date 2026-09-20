"""Viewer Context Provider — chat-router wiring (S1).

Helper-level coverage of the router seam (the /chat and /chat/stream entry points share
these): permission checks own the drive here (``viewer_context`` stays pure), forged asset
ids drop the identity but never 403 the request, ``too_large``/``unavailable`` abort
before the agent runs, the user message is NEVER prefixed with viewer content, and the
per-turn metadata lands under dedicated ``meta["viewer"]`` / ``meta["viewer_citations"]``
keys that cannot collide with ``meta["retrieval"]``.
"""
from uuid import uuid4

import pytest
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
        a, body, "as [V1] says … and also [V9]", "uid-1", "aid-1")
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
    v = ViewerPayload(name="x.pdf", asset_id=AID)  # no focus text → blocks empty
    a = build_viewer_blocks(v, "今天天气怎么样")
    assert await chat_mod._viewer_post_turn(a, ChatRequest(message="今天天气怎么样", viewer=v),
                                            "sunny", "u", "x") is None
    assert calls == []


async def test_post_turn_tolerates_missing_ids(monkeypatch):
    saved: list[tuple] = []

    async def fake_persist(message_id, key, value):
        saved.append((message_id, key))

    monkeypatch.setattr(chat_mod, "_persist_turn_meta", fake_persist)
    body = _body()
    a = build_viewer_blocks(body.viewer, body.message)
    p = await chat_mod._viewer_post_turn(a, body, "[V1]", None, None)
    assert saved == [] and p["status"] == "injected"  # done frame still carries citations
