"""Phase 5B e2e through /chat/stream: COMPOSITE routing + the fence-inheritance
contract (constraint 2) + the parallel-input fan-out proof.

The viewer body rides the real router (same injection path as Phase 3) while the
private-recall branch runs through the Phase-4 seam double — one turn, two independent
inputs, ONE final generation. Escalations re-enter the Agent with:

* the SAME ``source_policy`` the plan resolved from the ORIGINAL request (private_first
  / private_only) — sunk into the agent context before dispatch, verified on the wire
  through the harness's recorded contexts;
* the honest-disclosure note (this branch DID search the corpus — unlike the ACTION
  branch's byte-clean hand-off, prepending here is the point);
* and, for the private_only case, the fence is then pinned at the enforcement layer:
  a Sandbox bound with the Agent's received context HARD-DENIES web_search even with
  a NETWORK grant + ALLOW rule (the constraint-2 end-to-end proof).
"""
from __future__ import annotations

import json
from uuid import uuid4

import httpx
from agent.engine.context import _TURN_CTX as TURN_CTX
from agent.engine.context import AgentTurn
from agent.security.sandbox import Sandbox, SandboxDecision, SandboxRule
from agent.tools.tool_permissions import ToolPermission
from api.routers import chat as chat_mod
from core.config import settings

from tests.test_chat_retrieval_e2e import (
    HIT,
    USER,
    FakePort,
    FakeSeam,
    _harness,
)
from tests.test_chat_source_policy import WEB
from tests.test_chat_viewer_e2e import VIEWER_BODY

COMPOSITE_MSG = "这一页讲的内容，我的知识库里还有哪些相关资料"
COMPOSITE_ONLY_MSG = "只用我的知识库回答，这一页提到的概念还有哪些相关资料"


def _gates(monkeypatch, *, fast=True, composite=True):
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", fast, raising=False)
    monkeypatch.setattr(settings, "chat_direct_fast_path_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chat_viewer_fast_path_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_retrieval_fast_path_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chat_composite_fast_path_enabled", composite, raising=False)


async def _stream_viewer(app, message=COMPOSITE_MSG):
    """The Phase-4 _stream plus the viewer body (real assembly on the router path)."""
    transport = httpx.ASGITransport(app=app)
    events, done = [], None
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client, \
            client.stream("POST", "/chat/stream", json={
                "message": message, "session_id": str(uuid4()), "viewer": VIEWER_BODY,
            }) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            evt = json.loads(line[5:].strip())
            events.append(evt.get("type"))
            if evt.get("type") == "done":
                done = evt["data"]
    return events, done


async def test_viewer_plus_private_routes_composite_one_generation(monkeypatch):
    _gates(monkeypatch)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream_viewer(app)
    assert events == ["content", "content", "done"]
    assert done["answer"] == "It steps"
    assert agent.agent_stream_calls == 0
    assert len(seam.calls) == 1 and seam.calls[0][2] == {"user_id": str(USER)}
    # Constraint 4 on the wire: judge (chat) and generation (chat_stream) counted apart.
    assert port.judged == 1 and port.generated == 1


async def test_fanout_inputs_run_once_and_independently(monkeypatch):
    """Parallel-input proof: one recall call + one renderer call, no second pass."""
    _gates(monkeypatch)
    port, seam = FakePort(), FakeSeam(HIT)
    app, _agent = _harness(monkeypatch, port, seam)
    renders = []
    real_render = chat_mod._VIEWER_DEPS.render_reference

    def spy(blocks):
        renders.append(len(list(blocks)))
        return real_render(blocks)

    monkeypatch.setattr(chat_mod, "_VIEWER_DEPS",
                        chat_mod.ViewerDeps(**{
                            **{k: getattr(chat_mod._VIEWER_DEPS, k)
                               for k in chat_mod._VIEWER_DEPS.__dataclass_fields__
                               if k != "render_reference"},
                            "render_reference": spy,
                        }))
    await _stream_viewer(app)
    assert renders == [1]                       # blocks rendered exactly once
    assert len(seam.calls) == 1                 # corpus recalled exactly once
    assert port.judged == 1 and port.generated == 1  # gate once, generate once


async def test_empty_recall_escalates_under_inherited_private_first(monkeypatch):
    _gates(monkeypatch)
    port, seam = FakePort(), FakeSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream_viewer(app)
    assert done["answer"] == "agent" and agent.agent_stream_calls == 1
    assert port.generated == 0                  # pre-commit: fast path emitted nothing
    ctx = agent.contexts[0] or {}
    assert ctx.get("source_policy") == "private_first"   # constraint 2: inherited fence
    assert events == ["done"]


async def test_private_only_composite_escalation_keeps_the_sandbox_hard_deny(monkeypatch):
    """End-to-end fence: the ORIGINAL 'knowledge base only' request still fences the
    escalated Agent — its received context, rebound as a turn, HARD-DENIES web_search
    even with a NETWORK grant and an ALLOW rule."""
    _gates(monkeypatch)
    port, seam = FakePort(), FakeSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    await _stream_viewer(app, message=COMPOSITE_ONLY_MSG)
    received = agent.contexts[0] or {}
    assert received.get("source_policy") == "private_only"
    assert "restricted to private sources" in agent.user_texts[0]
    token = TURN_CTX.set(AgentTurn(user_msg=COMPOSITE_ONLY_MSG, context=received))
    try:
        sb = Sandbox()
        sb.grant(ToolPermission.NETWORK)
        sb.add_rule(SandboxRule(ToolPermission.NETWORK, SandboxDecision.ALLOW))
        assert sb.check(WEB, {}) is SandboxDecision.DENY   # the fence survived the swap
    finally:
        TURN_CTX.reset(token)


async def test_nonrelevant_verdict_escalates_with_honest_note(monkeypatch):
    _gates(monkeypatch)
    port, seam = FakePort(verdict="irrelevant"), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream_viewer(app)
    assert agent.agent_stream_calls == 1 and port.generated == 0
    text = agent.user_texts[0]
    assert text.startswith("[Private retrieval note")  # corpus WAS searched → note rides
    assert text.endswith(COMPOSITE_MSG)                # original question intact below it
    assert (agent.contexts[0] or {}).get("source_policy") == "private_first"


async def test_composite_gate_off_routes_viewer_or_agent_not_composite(monkeypatch):
    """Dark per-kind gate: viewer alone would answer — but this turn demands the corpus
    too, so it lands on the Agent untouched (Phase 5B must not disturb Phase 3/4)."""
    _gates(monkeypatch, composite=False)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream_viewer(app)
    assert agent.agent_stream_calls == 1 and seam.calls == []


async def test_master_gate_off_dark_launch_untouched(monkeypatch):
    _gates(monkeypatch, fast=False)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream_viewer(app)
    assert agent.agent_stream_calls == 1 and seam.calls == [] and port.judged == 0
