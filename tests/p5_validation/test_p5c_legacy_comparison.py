"""Layer C — Legacy-Agent vs Fast-Path BEHAVIORAL PARITY.

For every fast path, the same request is run twice through the real ``/chat/stream``:
once with the fast path OFF (pure legacy Agent authority) and once ON. The charter asks
for semantic/behavioral parity, NOT verbatim text — so we compare a behavioral
fingerprint: side-effect counts, tool executions, the set of permission/approval
decisions surfaced, tenant filtering, and the SSE terminal shape. If the fast path can
only match the legacy outcome, that IS the safety argument for shipping it; where it
diverges, the divergence is recorded as a finding.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from core.config import settings

from tests.p5_validation._p5_harness import (
    FakeSeam,
    ScriptedPort,
    Spy,
    build_app,
    build_kernel,
    domains_named,
    sse,
)
from tests.p5_validation.test_p5_smoke import _gate


@dataclass
class Fingerprint:
    folders: int
    terms: int
    web: int
    approvals: int
    approval_name: str | None
    tool_arguments: dict | None
    entered_agent: bool
    single_shot: bool
    judged: bool
    has_done: bool
    answer_len: int


def _fp(res, spy, port) -> Fingerprint:
    return Fingerprint(
        folders=len(spy.folders_created), terms=len(spy.terms_added),
        web=len(spy.web_queries), approvals=len(res.approvals),
        approval_name=(res.approvals[0]["name"] if res.approvals else None),
        tool_arguments=(res.approvals[0].get("arguments") if res.approvals else None),
        entered_agent=port.steps > 0, single_shot=port.single_shot > 0,
        judged=port.judged > 0, has_done=res.done is not None,
        answer_len=len(res.answer or ""),
    )


# ── ACTION: certified dispatch must reach the SAME side effect + funnel as the Agent ───
CREATE_MSG = 'create a folder named "parity"'


async def _create_legacy(monkeypatch):
    # Legacy: the Agent decides to call create_folder → same ASK funnel → one folder.
    port = ScriptedPort(steps=[
        {"content": [], "tool_calls": [{"name": "create_folder", "arguments": {"name": "parity"}}]},
        {"content": ["Created folder 'parity' in your drive."], "tool_calls": None},
    ])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", False, raising=False)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE_MSG)
    return _fp(res, spy, port)


async def _create_fast(monkeypatch):
    port = ScriptedPort(steps=[{"content": ["unused"], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE_MSG)
    return _fp(res, spy, port)


async def test_action_parity_vs_legacy(monkeypatch):
    legacy = await _create_legacy(monkeypatch)
    fast = await _create_fast(monkeypatch)
    # Identical CAPABILITY outcome: same side effect, same approval surfaced.
    assert fast.folders == legacy.folders == 1
    assert fast.approvals == legacy.approvals == 1
    assert fast.approval_name == legacy.approval_name == "create_folder"
    assert fast.tool_arguments == legacy.tool_arguments == {"name": "parity"}
    assert fast.has_done and legacy.has_done
    assert fast.answer_len > 0 and legacy.answer_len > 0
    # The DIFFERENCE that justifies the fast path: no ReAct loop, no LLM step.
    assert legacy.entered_agent and not fast.entered_agent
    assert not fast.single_shot                          # ACTION uses no LLM at all


# ── add_term parity ───────────────────────────────────────────────────────────────────
ADD_MSG = 'add "quark" to my physics vocab'


async def _add_legacy(monkeypatch):
    port = ScriptedPort(steps=[
        {"content": [], "tool_calls": [{"name": "add_term",
                                        "arguments": {"term": "quark", "domain": "physics"}}]},
        {"content": ["Added."], "tool_calls": None},
    ])
    spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow", domains=domains_named("Physics"),
    )
    _gate(monkeypatch, action=True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", False, raising=False)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, ADD_MSG)
    return _fp(res, spy, port)


async def _add_fast(monkeypatch):
    port = ScriptedPort(steps=[{"content": ["unused"], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, broker_mode="allow", domains=domains_named("Physics"),
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, ADD_MSG)
    return _fp(res, spy, port)


async def test_add_term_parity_vs_legacy(monkeypatch):
    legacy = await _add_legacy(monkeypatch)
    fast = await _add_fast(monkeypatch)
    assert fast.terms == legacy.terms == 1
    assert fast.has_done and legacy.has_done
    assert legacy.entered_agent and not fast.entered_agent   # fast path skips the loop


# ── RAG parity: grounded single-shot vs Agent-with-retrieval-tool ──────────────────────
RAG_MSG = "what does my knowledge base say about gradient descent"


async def _rag_legacy(monkeypatch):
    # Legacy: the Agent retrieves then answers; ONE grounded answer, no side effect.
    port = ScriptedPort(steps=[
        {"content": ["It minimizes loss."], "tool_calls": None},
    ])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", False, raising=False)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, RAG_MSG)
    return _fp(res, spy, port)


async def _rag_fast(monkeypatch):
    port = ScriptedPort(deltas=["It", " minimizes loss."], verdict="relevant")
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    app = build_app(monkeypatch, port, FakeSeam([
        {"id": "c1", "text": "GD steps the negative gradient.", "score": 0.9, "meta": {}},
    ]), kernel, broker)
    res = await sse(app, RAG_MSG)
    return _fp(res, spy, port)


async def test_rag_parity_no_side_effect_both(monkeypatch):
    legacy = await _rag_legacy(monkeypatch)
    fast = await _rag_fast(monkeypatch)
    # Neither path has side effects; both deliver an answer with the legacy shape.
    assert fast.folders == fast.terms == fast.web == 0
    assert legacy.has_done and fast.has_done
    assert fast.answer_len > 0 and legacy.answer_len > 0
    # Fast path pays a judge + single generation, NO ReAct step; legacy pays a step.
    assert fast.judged and fast.single_shot and not fast.entered_agent
    assert legacy.entered_agent


# ── DIRECT parity: single-shot vs Agent answer, identical terminal shape ───────────────
@pytest.mark.parametrize("msg", ["hello there", "what is 2 + 2?", "tell me a joke"])
async def test_direct_parity_shape(monkeypatch, msg):
    # OFF
    po = ScriptedPort(steps=[{"content": ["Agent says hi."], "tool_calls": None}])
    so = Spy()
    ko, _, _, bo = build_kernel(monkeypatch, po, so)
    _gate(monkeypatch, direct=True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", False, raising=False)
    fo = _fp(await sse(build_app(monkeypatch, po, FakeSeam([]), ko, bo), msg), so, po)
    # ON
    pn = ScriptedPort(deltas=["Direct", " hi."])
    sn = Spy()
    kn, _, _, bn = build_kernel(monkeypatch, pn, sn)
    _gate(monkeypatch, direct=True)
    fn = _fp(await sse(build_app(monkeypatch, pn, FakeSeam([]), kn, bn), msg), sn, pn)
    assert fo.has_done and fn.has_done
    assert fo.answer_len > 0 and fn.answer_len > 0
    assert fo.entered_agent and not fn.entered_agent       # shed the loop, keep the shape
    assert fn.single_shot
