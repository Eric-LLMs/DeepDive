"""Harness smoke: does the REAL /chat/stream + kernel funnel actually work?

A handful of end-to-end probes across every branch the validation suite leans on.
Run first; if these are green, the mass case authoring rests on a proven assembly.
"""
from __future__ import annotations

import pytest
from agent.tools.tool_permissions import ToolPermission
from core.config import settings

from tests.p5_validation._p5_harness import (
    USER,
    FakeSeam,
    ScriptedPort,
    Spy,
    build_app,
    build_kernel,
    sse,
)

CREATE = 'create a folder named "archive"'
PRIVATE_Q = "what does my knowledge base say about gradient descent"
HIT = [{"id": "c1", "text": "Gradient descent steps along the negative gradient.", "score": 0.9, "meta": {}}]


def _gate(monkeypatch, *, action=False, direct=False, viewer=False, retrieval=False, composite=False):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", action, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", viewer, raising=False)
    monkeypatch.setattr(settings, "chat_retrieval_fast_path_enabled", retrieval, raising=False)
    monkeypatch.setattr(settings, "chat_composite_fast_path_enabled", composite, raising=False)


async def test_action_certified_dispatches_real_tool_through_funnel(monkeypatch):
    """5A core: ACTION dispatches create_folder through the REAL sandbox→approval→body
    funnel (WRITE ⇒ ASK), no LLM anywhere, one side effect, legacy content→done shape."""
    port = ScriptedPort()
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="allow")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE)
    assert res.types[0] == "approval-request"      # WRITE not granted ⇒ ASK surfaced
    assert res.types[-2:] == ["content", "done"]
    assert broker.requests                          # shared funnel actually engaged
    assert spy.folders_created == [(str(USER), "archive")]
    assert res.answer == "Created folder 'archive' in your drive."
    assert port.single_shot == 0 and port.steps == 0 and port.judged == 0


async def test_action_denied_at_approval_is_terminal_no_side_effect(monkeypatch):
    port = ScriptedPort()
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy, broker_mode="deny")
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE)
    assert res.approvals
    assert res.types[-2:] == ["content", "done"]
    assert spy.folders_created == []                # decided denial ⇒ nothing executed
    assert "Could not complete" in res.answer
    assert port.steps == 0                           # never re-entered the Agent


async def test_action_preflight_escalates_byte_identical(monkeypatch):
    """DriveError before any write ⇒ 'preflight:' ⇒ escalate. WRITE granted so no ASK
    noise; the Agent must receive the ORIGINAL text byte-for-byte (no honest note)."""
    port = ScriptedPort(steps=[{"content": ["Let me help with that."], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, drive_mode="preflight", grant=[ToolPermission.WRITE],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE)
    assert spy.folders_created == []
    assert port.steps == 1                            # Agent took over exactly once
    agent_user_msg = port.requests[-1][-1]["content"]
    assert agent_user_msg == CREATE                   # zero-pollution escalation
    assert "Private retrieval note" not in agent_user_msg
    assert res.answer == "Let me help with that."


async def test_action_state_unknown_never_replays_the_agent(monkeypatch):
    port = ScriptedPort(steps=[{"content": ["should not run"], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, drive_mode="post-write", grant=[ToolPermission.WRITE],
    )
    _gate(monkeypatch, action=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, CREATE)
    assert len(spy.folders_created) == 1             # write happened, then failed mid-flight
    assert port.steps == 0                            # Agent NOT re-entered (no duplicate folder)
    assert "could not be confirmed" in res.answer.lower()


async def test_direct_answers_single_shot_without_tools(monkeypatch):
    port = ScriptedPort(deltas=["Hi", " there"])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, direct=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, "hello there")
    assert res.types[-2:] == ["content", "done"]
    assert res.answer == "Hi there"
    assert port.single_shot == 1 and port.steps == 0


async def test_rag_relevant_answers_grounded_no_agent(monkeypatch):
    port = ScriptedPort(deltas=["It", " steps"], verdict="relevant")
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    seam = FakeSeam(HIT)
    app = build_app(monkeypatch, port, seam, kernel, broker)
    res = await sse(app, PRIVATE_Q)
    assert res.answer == "It steps"
    assert seam.calls and seam.calls[0][2] == {"user_id": str(USER)}
    assert port.judged == 1 and port.single_shot == 1 and port.steps == 0


async def test_rag_empty_recall_escalates_with_honest_note(monkeypatch):
    port = ScriptedPort(steps=[{"content": ["Agent fallback."], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, PRIVATE_Q)
    assert port.judged == 0                            # empty recall never pays the judge
    assert port.steps == 1                              # Agent took the turn
    agent_user_msg = port.requests[-1][-1]["content"]
    assert "Private retrieval note" in agent_user_msg   # honesty note rides searched branches
    assert res.answer == "Agent fallback."


async def test_complex_turn_stays_on_agent(monkeypatch):
    """A multi-tool demand must never be fast-pathed — straight to the Agent.

    ``create … and add …`` is certified non-actionable by the ACTION extractor (compound
    demand ⇒ abstain) yet carries a HIGH ``needs_action`` lexical signal, so the policy
    hands it to the Agent (confidence LOW). The ACTION gate is OFF-safe here: the folder
    is NOT created by a fast path, the Agent owns the whole turn.
    """
    port = ScriptedPort(steps=[{"content": ["Working on it."], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, action=True, retrieval=True, direct=True)
    app = build_app(monkeypatch, port, FakeSeam(HIT), kernel, broker)
    res = await sse(
        app, "create a folder named 'archive' and add the term 'quantum' to my vocabulary"
    )
    assert res.types[-1] == "done"
    assert port.steps == 1
    assert port.single_shot == 0                       # not hijacked by DIRECT
    assert spy.folders_created == []                   # ACTION did not fire either


@pytest.mark.xfail(
    reason=(
        "VALIDATED FINDING (Phase-2 DIRECT routing gap, NOT a 5A regression): a sequenced "
        "multi-step chain whose verbs are absent from _ACTION_PAT (e.g. 'research X, then "
        "build a deck') is certified all-demand-LOW by L0 and — because _SEQUENCE_PAT is "
        "only applied to COMPOSITE, never DIRECT — is hijacked by the tool-less DIRECT path "
        "when the direct gate is on (single_shot==1, Agent never entered). Direct is OFF by "
        "default so this is not an active prod risk; flagged for the report, not fixed here "
        "(blocking 'then' on DIRECT is a policy call with Phase-2 golden-case regression risk)."
    ),
    strict=True,
)
async def test_sequenced_chain_should_not_be_direct_hijacked(monkeypatch):
    port = ScriptedPort(steps=[{"content": ["Working on it."], "tool_calls": None}])
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, action=True, retrieval=True, direct=True)
    app = build_app(monkeypatch, port, FakeSeam(HIT), kernel, broker)
    await sse(app, "research quantum error correction, then build a slide deck from it")
    # The invariant we WANT: the Agent owns a sequenced chain, not a tool-less single shot.
    assert port.single_shot == 0
    assert port.steps >= 1

