"""Source-policy semantics (Phase 4 clarification): fencing comes from the ORIGINAL
request, never from the fact that a fast path failed.

    RAG failure => escalate to Agent
    NOT
    RAG failure => globally forbid Web

The escalated Agent's tools are fenced by a turn-level SOURCE POLICY mapped from the
user's own words:

* explicit private-only ("only use my knowledge base" / 别联网) -> ``private_only``;
* a private-first turn the LOCAL_RAG path declined without an explicit external
  permission -> ``private_first`` (default: disclose the corpus gap honestly, stay
  on private sources — web is NOT silently widened in);
* explicit permission ("if nothing, you can search the web") or a mixed private+web
  request -> no fence: the normal permission/approval funnel governs, attribution
  instructions still ride the escalation note.

The fence is enforced where the Agent ALREADY enforces permissions (Sandbox, the
turn-context funnel the research handoff also uses) — no second RAG, no kernel rewrite.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.engine.context import _TURN_CTX as TURN_CTX
from agent.engine.context import AgentTurn
from agent.security.sandbox import Sandbox, SandboxDecision
from agent.tools.definition import ToolDefinition
from agent.tools.tool_permissions import ToolPermission
from core.application.chat.context import ChatTurnContext
from core.application.chat.execution_plan import (
    PlanKind,
    PolicyContext,
    build_execution_plan,
)
from core.application.chat.understanding import resolve_requirements

from tests.test_chat_retrieval_e2e import (
    HIT,
    PRIVATE_Q,
    FakePort,
    FakeSeam,
    _gates,
    _harness,
    _stream,
)

ALL_ON = PolicyContext(
    fast_paths_enabled=True, direct_fast_path_enabled=True,
    viewer_fast_path_enabled=True, retrieval_fast_path_enabled=True,
)

EXT_OK_Q = PRIVATE_Q + ", if nothing you can search the web"
ONLY_Q = "only use my knowledge base to answer what gradient descent is"
MIXED_Q = "what does my knowledge base say about gd and the latest news"


def _ctx(message: str) -> ChatTurnContext:
    return ChatTurnContext(
        body=SimpleNamespace(message=message, attach=None),
        user=None, user_id="u", guest_token=None, log_user=None, tier="free",
        notice=None, model=None, base_url=None, api_key=None, business_name=None,
        credential_id=None, user_text=message, owned_asset_id=None, inline_image=None,
        viewer_assembly=None, research_turn=False, effective_handoff=None,
    )


def _facts(message: str):
    return resolve_requirements(_ctx(message), message)


# ── L0 source facts (narrow by design: a false positive fences the Agent) ────────────

@pytest.mark.parametrize("msg", [
    ONLY_Q,
    "answer from my notes only, no web",
    "只用我的知识库回答梯度下降",
    "只查我的知识库里关于RAG的说法",
    "不要联网,查我的笔记里怎么说",
])
def test_private_only_detection(msg):
    assert _facts(msg).private_only is True


@pytest.mark.parametrize("msg", [
    PRIVATE_Q,                       # ordinary private: NOT an explicit restriction
    "summarize my document",
    "what is the latest news",
    MIXED_Q,                         # contradiction -> abstain, keep the funnel
])
def test_private_only_not_overfired(msg):
    assert _facts(msg).private_only is False


@pytest.mark.parametrize("msg", [
    EXT_OK_Q,
    "我知识库里没有的话可以搜网络",
])
def test_external_permission_detected(msg):
    assert _facts(msg).external_ok is True


def test_external_permission_not_overfired():
    assert _facts(PRIVATE_Q).external_ok is False
    assert _facts("search the web for latest ai news").external_ok is False


# ── policy mapping: source policy rides the plan, independent of execution ───────────

def test_source_policy_states():
    assert build_execution_plan(_facts(PRIVATE_Q), ALL_ON).source_policy == "private_first"
    assert build_execution_plan(_facts(ONLY_Q), ALL_ON).source_policy == "private_only"
    # Explicit permission -> no fence on the eventual escalation (funnel governs).
    assert build_execution_plan(_facts(EXT_OK_Q), ALL_ON).source_policy is None
    # Mixed private+web never enters the fast path AND keeps the normal funnel.
    plan = build_execution_plan(_facts(MIXED_Q), ALL_ON)
    assert plan.kind is PlanKind.AGENT and plan.source_policy is None


def test_dark_launch_carries_no_source_policy(monkeypatch):
    # Master gate OFF -> the neutral requirement set fences nothing (behavior
    # byte-identical to the legacy agent).
    req = _facts(ONLY_Q)
    dark = PolicyContext(fast_paths_enabled=False)
    assert build_execution_plan(req, dark).source_policy == "private_only"  # facts ride
    # ...but the orchestrator only SINKS under the live control plane — see the sink
    # test below; the dark plan resolves from NEUTRAL requirements (private_only False).
    assert build_execution_plan(type(req)(), dark).source_policy is None


# ── orchestrator sink ────────────────────────────────────────────────────────────────

def _orchestrator():
    from core.application.chat.turn_orchestrator import TurnOrchestrator
    return TurnOrchestrator(deps=None)


def test_sink_only_when_control_plane_live(monkeypatch):
    import core.config as cfg
    monkeypatch.setattr(cfg.settings, "chat_fast_paths_enabled", True, raising=False)
    monkeypatch.setattr(cfg.settings, "chat_retrieval_fast_path_enabled", True, raising=False)
    ctx = _ctx(PRIVATE_Q)
    _orchestrator().resolve_plan(ctx)
    assert (ctx.agent_context or {}).get("source_policy") == "private_first"

    monkeypatch.setattr(cfg.settings, "chat_fast_paths_enabled", False, raising=False)
    ctx2 = _ctx(PRIVATE_Q)
    _orchestrator().resolve_plan(ctx2)
    assert not (ctx2.agent_context or {}).get("source_policy")


def test_explicit_permission_sink_nothing(monkeypatch):
    import core.config as cfg
    monkeypatch.setattr(cfg.settings, "chat_fast_paths_enabled", True, raising=False)
    monkeypatch.setattr(cfg.settings, "chat_retrieval_fast_path_enabled", True, raising=False)
    ctx = _ctx(EXT_OK_Q)
    _orchestrator().resolve_plan(ctx)
    assert not (ctx.agent_context or {}).get("source_policy")


# ── sandbox enforcement (the existing turn-context funnel) ───────────────────────────

def _tool(name: str, props: dict) -> ToolDefinition:
    return ToolDefinition(
        name=name, description="", parameters={"properties": props},
        output=None, execute=None,
    )


WEB = _tool("web_search", {"query": {"type": "string"}, "url": {"type": "string"}})
KB = _tool("rag_search", {"query": {"type": "string"}})


def _bound(context):
    """Bind a turn with the given sink context; returns the contextvar token."""
    return TURN_CTX.set(AgentTurn(user_msg="q", context=context))


def test_private_policies_hard_deny_network_even_with_grant():
    for policy in ("private_only", "private_first"):
        token = _bound({"source_policy": policy})
        try:
            sb = Sandbox()
            sb.grant(ToolPermission.NETWORK)
            from agent.security.sandbox import SandboxRule
            sb.add_rule(SandboxRule(ToolPermission.NETWORK, SandboxDecision.ALLOW))
            # Hard deny: neither a session grant nor an ALLOW rule widens it back.
            assert sb.check(WEB, {}) is SandboxDecision.DENY, policy
            # Private tools are untouched.
            assert sb.check(KB, {}) is SandboxDecision.ALLOW, policy
        finally:
            TURN_CTX.reset(token)


def test_unfenced_turn_keeps_the_approval_funnel():
    token = _bound({"handoff": None})
    try:
        sb = Sandbox()
        assert sb.check(WEB, {}) is SandboxDecision.ASK   # may use web if approved
        sb.grant(ToolPermission.NETWORK)
        assert sb.check(WEB, {}) is SandboxDecision.ALLOW
    finally:
        TURN_CTX.reset(token)


def test_research_grant_unaffected_by_the_new_funnel():
    token = _bound({"handoff": {"kind": "research", "project_id": "t"}})
    try:
        assert Sandbox().check(WEB, {}) is SandboxDecision.ALLOW  # durable research grant
    finally:
        TURN_CTX.reset(token)


# ── e2e through the real /chat/stream router ─────────────────────────────────────────

class _BoomSeam(FakeSeam):
    async def retrieve(self, query, top_k=5, filters=None):
        self.calls.append((query, top_k, filters))
        raise RuntimeError("embedding service down")


async def test_ordinary_private_empty_escalation_fenced_and_honest(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app)
    assert agent.agent_stream_calls == 1
    assert agent.contexts[0]["source_policy"] == "private_first"
    text = agent.user_texts[0]
    assert text.startswith("[Private retrieval note")
    assert "no sufficient evidence" in text            # honest disclosure demanded
    assert "NEVER present general knowledge or web content" in text  # no misattribution
    assert "restricted to private sources" in text     # web ban stated (and enforced)
    assert port.generated == 0


async def test_retrieval_failure_escalation_fenced(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), _BoomSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app)
    assert agent.contexts[0]["source_policy"] == "private_first"
    assert "restricted to private sources" in agent.user_texts[0]


async def test_explicit_kb_only_escalation_fenced_strictest(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app, message=ONLY_Q)
    assert agent.agent_stream_calls == 1
    assert agent.contexts[0]["source_policy"] == "private_only"
    assert "restricted to private sources" in agent.user_texts[0]


async def test_explicit_permission_escalates_without_fence_but_with_honesty(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam([])
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app, message=EXT_OK_Q)
    # The Agent may widen to the web (the funnel, not this code, decides)...
    assert not (agent.contexts[0] or {}).get("source_policy")
    # ...but still must not misattribute: the disclosure note rides, without a web ban.
    text = agent.user_texts[0]
    assert text.startswith("[Private retrieval note")
    assert "NEVER present general knowledge or web content" in text
    assert "restricted to private sources" not in text


async def test_mixed_private_web_goes_straight_to_agent_unfenced(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    await _stream(app, message=MIXED_Q)
    assert agent.agent_stream_calls == 1
    assert seam.calls == []  # mixed demands never touch the fast path at all
    assert not (agent.contexts[0] or {}).get("source_policy")


async def test_successful_fast_path_unchanged_under_policy(monkeypatch):
    _gates(monkeypatch, fast=True, retrieval=True)
    port, seam = FakePort(), FakeSeam(HIT)
    app, agent = _harness(monkeypatch, port, seam)
    events, done = await _stream(app)
    assert events == ["content", "content", "done"]
    assert done["answer"] == "It steps"
    assert agent.agent_stream_calls == 0 and port.generated == 1
