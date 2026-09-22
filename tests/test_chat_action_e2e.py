"""Phase 5A e2e through the real /chat/stream: ACTION dispatch + the lossless-fallback
consistency contract.

The user-visible invariant these cases pin:

* certified action, gate ON → the tool's deterministic confirmation streams with the
  legacy content→done shape; NO LLM channel is used, the Agent is never entered;
* abstention / preflight failure → the Agent receives the ORIGINAL user text
  BYTE-FOR-BYTE (no attempt markers, no notes — `_escalation_note` only rides branches
  that actually searched the corpus), so the same request behaves exactly like the old
  link with the fast path disabled;
* state-UNKNOWN failure → the Agent is NEVER re-entered (no duplicate side effect) and
  the stream finishes honestly;
* gate OFF → pure dark launch: the seam is never called and the Agent sees an
  untouched request.

Drives the router through the Phase-4 harness; only ``chat_mod._run_tool`` is
substituted (the adapter's runtime.execute classification is covered by the seed-tool
and executor unit tests, and the funnel-binding contract by the direct _run_tool case
at the bottom of this file).
"""
from __future__ import annotations

from types import SimpleNamespace

from api.routers import chat as chat_mod
from core.config import settings

from tests.test_chat_retrieval_e2e import (
    HIT,
    FakePort,
    FakeSeam,
    _harness,
    _stream,
)

USER = None  # harness owns the identity
MSG = 'create a folder named "archive"'


def _gates5(monkeypatch, *, fast=True, action=True, direct=False, retrieval=False):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", direct, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chat_retrieval_fast_path_enabled", retrieval, raising=False)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", action, raising=False)
    monkeypatch.setattr(settings, "chat_composite_fast_path_enabled", False, raising=False)


async def test_certified_action_dispatches_tool_without_any_llm(monkeypatch):
    _gates5(monkeypatch)
    calls = []

    async def fake_run_tool(tool, args, ctx):
        calls.append((tool, args))
        return {"ok": True, "output": "Created folder 'archive' in your drive."}

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app, message=MSG)
    assert calls == [("create_folder", {"name": "archive"})]
    assert done["answer"] == "Created folder 'archive' in your drive."
    assert events == ["content", "done"]
    # No LLM anywhere on this branch, and the Agent loop is never entered.
    assert port.judged == 0 and port.generated == 0 and agent.agent_stream_calls == 0


async def test_preflight_failure_hands_the_agent_the_untouched_original_text(monkeypatch):
    """Lossless fallback: abstention/pre-flight must leave ZERO trace in the replay."""
    from core.application.chat.actions import ActionPreflightFailure

    _gates5(monkeypatch)
    hits = []

    async def fake_run_tool(tool, args, ctx):
        hits.append(tool)
        raise ActionPreflightFailure("preflight: folder name rejected")

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    _, done = await _stream(app, message=MSG)
    assert hits == ["create_folder"]                 # entered once, proven non-executing
    assert agent.agent_stream_calls == 1             # Agent takes over exactly once
    assert agent.user_texts[0] == MSG                # byte-identical: no note, no marker
    assert not (agent.contexts[0] or {}).get("source_policy")  # ACTION fences nothing
    assert done["answer"] == "agent"
    assert port.generated == 0


async def test_state_unknown_terminates_honest_and_never_replays_the_agent(monkeypatch):
    """Constraint 1 (unknown side of the boundary): no blind Agent retry."""
    _gates5(monkeypatch)
    hits = []

    async def fake_run_tool(tool, args, ctx):
        hits.append(tool)
        raise RuntimeError("connection reset mid-write")

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app, message=MSG)
    assert hits == ["create_folder"]                 # seam called EXACTLY once
    assert agent.agent_stream_calls == 0             # Agent NOT re-entered
    assert events == ["content", "done"]
    assert "could not be confirmed" in done["answer"].lower()


async def test_decided_denial_is_terminal_honest_answer(monkeypatch):
    _gates5(monkeypatch)

    async def fake_run_tool(tool, args, ctx):
        return {"ok": False, "reason": "the approval request was declined"}

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    _, done = await _stream(app, message=MSG)
    assert agent.agent_stream_calls == 0
    assert "declined" in done["answer"]  # terminal answer, legacy done shape unchanged


async def test_dark_launch_action_gate_off_never_touches_the_seam(monkeypatch):
    _gates5(monkeypatch, action=False)
    hits = []

    async def fake_run_tool(tool, args, ctx):
        hits.append(tool)
        return {"ok": True, "output": "nope"}

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    _, done = await _stream(app, message=MSG)
    assert hits == [] and agent.agent_stream_calls == 1
    assert agent.user_texts[0] == MSG  # identical behavior to the pre-Phase-5 link
    assert done["answer"] == "agent"


async def test_master_gate_off_is_indistinguishable_from_legacy(monkeypatch):
    _gates5(monkeypatch, fast=False)
    hits = []

    async def fake_run_tool(tool, args, ctx):
        hits.append(tool)
        return {"ok": True}

    monkeypatch.setattr("api.routers.chat._run_tool", fake_run_tool)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app, message=MSG)
    assert hits == [] and port.judged == 0 and port.generated == 0
    assert agent.agent_stream_calls == 1 and agent.user_texts[0] == MSG


async def test_run_tool_binds_agent_so_the_sandbox_funnel_still_sees_the_tool(monkeypatch):
    """Phase-5 bench regression: the synthetic ToolExecution MUST carry ``agent=`` —
    the sandbox guard and the ASK listener resolve the tool definition through
    ``exec.agent.runtime`` (loop.py dispatches the same way). Without it the fast path
    would silently BYPASS the permission/approval funnel the Agent path goes through."""
    captured = {}

    class _RT:
        async def execute(self, execution):
            captured["execution"] = execution
            return SimpleNamespace(is_error=False, value="Created.", error=None)

    agent = SimpleNamespace(runtime=_RT())
    monkeypatch.setattr(chat_mod, "get_agent", lambda: agent)
    ctx = SimpleNamespace(user_text=MSG, agent_context=None)

    out = await chat_mod._run_tool("create_folder", {"name": "archive"}, ctx)
    assert out == {"ok": True, "output": "Created."}
    assert captured["execution"].agent is agent           # guards resolve the tool
    assert captured["execution"].name == "create_folder"
    assert captured["execution"].arguments == {"name": "archive"}
