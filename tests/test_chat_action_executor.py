"""Phase 5A ActionExecutor: staging + the side-effect boundary isolation.

Constraint 1 pinning, BOTH directions:

  pre-execution failure (schema / seam-not-wired / ActionPreflightFailure)
      ⇒ EscalateToAgent, the seam either untouched or provably non-executing;
  post-entry failure (any other exception out of run_tool)
      ⇒ state UNKNOWN ⇒ ONE honest terminal message + normal done, the Agent is
      NEVER entered (a blind replay could duplicate the side effect);
  decided denial (ok:False) ⇒ terminal honest message, no escalation.

Every case asserts the seam call count — the executor must call run_tool EXACTLY
once when it calls it at all. Event shape (content → internal done) and the
user+assistant persistence are DIRECT-identical; no LLM call happens on this branch.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.application.chat.actions import ActionPreflightFailure
from core.application.chat.execution_plan import ExecutionPlan, PlanKind
from core.application.chat.executors.action import ActionExecutor
from core.application.chat.executors.base import ChatDeps, EscalateToAgent, TurnRequest


class _SM:
    def __init__(self):
        self.rows = []

    async def append_message(self, role, content):
        self.rows.append((role, content))


def _req(action, run_tool):
    ctx = SimpleNamespace(
        user_text="新建文件夹\"x\"", session_memory=_SM(), history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "noted"},
        ],
    )
    deps = ChatDeps(
        session_factory=None, queue=None, drive=None, agent=None, llm=None,
        embedder=None, viewer=None, new_approval_bridge=None, persist_turn_meta=None,
        log_usage=None, resolve_research=None, run_tool=run_tool,
    )
    plan = ExecutionPlan(kind=PlanKind.ACTION, action=action)
    return TurnRequest(ctx=ctx, deps=deps, plan=plan)


GOOD = {"tool": "create_folder", "args": {"name": "x"}}


async def _drain(req):
    events = []
    async for evt in ActionExecutor().stream(req, progress_sink=lambda e: None):
        events.append(evt)
    return events


async def test_success_emits_deterministic_confirmation_without_llm():
    calls = []

    async def run_tool(tool, args, ctx):
        calls.append((tool, args))
        return {"ok": True, "output": "Created folder 'x'."}

    req = _req(GOOD, run_tool)
    events = await _drain(req)
    assert calls == [("create_folder", {"name": "x"})]
    assert [e["type"] for e in events] == ["content", "done"]
    assert events[0]["data"] == "Created folder 'x'."
    done = events[1]["data"]
    assert done["answer"] == "Created folder 'x'." and done["usage"] == {} and done["error"] is None
    assert done["messages"][:2] == [
        {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"},
    ]
    assert req.ctx.session_memory.rows == [
        ("user", "新建文件夹\"x\""), ("assistant", "Created folder 'x'."),
    ]


async def test_malformed_action_escalates_and_never_touches_the_seam():
    calls = []

    async def run_tool(*a):
        calls.append(a)
        return {"ok": True}

    req = _req({"tool": "rm_rf", "args": {}}, run_tool)
    with pytest.raises(EscalateToAgent, match="schema"):
        await _drain(req)
    assert calls == []  # pre-execution failure: the seam was NEVER entered


async def test_seam_not_wired_terminates_as_integrity_never_escalates():
    # C2 (frozen boundary 2): a missing seam is an internal wiring fault, not a
    # user-input problem — the Agent must never serve as the recovery channel.
    req = _req(GOOD, None)
    events = await _drain(req)
    assert [e["type"] for e in events] == ["content", "done"]
    assert "not available" in events[0]["data"].lower()


async def test_preflight_failure_escalates_once_side_effect_free():
    hits = []

    async def run_tool(tool, args, ctx):
        hits.append(1)
        raise ActionPreflightFailure("preflight: domain not found")

    req = _req(GOOD, run_tool)
    with pytest.raises(EscalateToAgent, match="preflight"):
        await _drain(req)
    assert len(hits) == 1  # entered once, proven non-executing — escalation is legal


async def test_state_unknown_terminates_honestly_never_escalates():
    hits = []

    async def run_tool(tool, args, ctx):
        hits.append(1)
        raise RuntimeError("connection reset mid-write")

    req = _req(GOOD, run_tool)
    events = await _drain(req)  # NO EscalateToAgent — the Agent must not replay a write
    assert len(hits) == 1       # called exactly once — no duplicate side effect
    assert [e["type"] for e in events] == ["content", "done"]
    data = events[1]["data"]
    assert data["error"] is None and data["answer"]
    assert "could not be confirmed" in data["answer"].lower()
    # The user row + the honest assistant row are persisted like any answer.
    assert [r[0] for r in req.ctx.session_memory.rows] == ["user", "assistant"]


async def test_binding_integrity_marker_terminates_never_escalates():
    # Stage-2 (routing-layer) C2: resolve_plan stamps a binding_integrity marker;
    # the executor must issue the decided terminal BEFORE the schema gate (such an
    # action carries no args) and never hand the system fault to the Agent.
    hits = []

    async def run_tool(tool, args, ctx):
        hits.append(1)
        return {"ok": True}

    action = {"tool": "create_folder", "args": None, "binding_integrity": "no binding"}
    req = _req(action, run_tool)
    events = await _drain(req)
    assert hits == []                                       # seam never entered
    assert [e["type"] for e in events] == ["content", "done"]
    assert "not available" in events[0]["data"].lower()


async def test_decided_denial_is_terminal_not_an_escalation():
    async def run_tool(tool, args, ctx):
        return {"ok": False, "reason": "approval denied by the user"}

    events = await _drain(_req(GOOD, run_tool))
    assert events[0]["type"] == "content"
    assert "approval denied" in events[0]["data"]
    assert events[1]["type"] == "done" and events[1]["data"]["error"] is None


async def test_run_mirrors_stream_shape():
    async def run_tool(tool, args, ctx):
        return {"ok": True, "output": "Done."}

    req = _req(GOOD, run_tool)
    result = await ActionExecutor().run(req)
    assert result.final_answer == "Done." and result.usage == {} and result.error is None
    assert result.messages[-1] == {"role": "assistant", "content": "Done."}
    assert result.messages[-2] == {"role": "user", "content": "新建文件夹\"x\""}
