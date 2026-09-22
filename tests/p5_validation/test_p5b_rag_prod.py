"""Layer B — RAG (Phase 4) through the real /chat/stream + the source-policy fence.

These prove the Fail-Closed / honesty contracts the charter names:

  * a relevant recall answers grounded on the shared seam (one judge, one generation,
    the Agent is never entered);
  * empty recall / an irrelevant verdict / a seam error escalate to the Agent, and the
    honest-retrieval note rides ONLY a branch that actually searched the corpus;
  * the escalated Agent stays fenced by the ORIGINAL request's source policy — a
    ``private_only`` turn's ``web_search`` is HARD-DENIED by the sandbox (beating both
    ``grant`` and ALLOW rules), so a retrieval failure never silently widens to the web.
"""
from __future__ import annotations

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
from tests.p5_validation.test_p5_smoke import _gate

HIT = [{"id": "c1", "text": "Gradient descent steps along the negative gradient.",
        "score": 0.9, "meta": {}}]
PRIV_Q = "what does my knowledge base say about gradient descent"
AGENT_STEP = {"content": ["Agent fallback."], "tool_calls": None}


async def test_rag_relevant_answers_grounded_no_agent(monkeypatch):
    port = ScriptedPort(deltas=["It", " minimizes"], verdict="relevant")
    spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    seam = FakeSeam(HIT)
    app = build_app(monkeypatch, port, seam, kernel, broker)
    res = await sse(app, PRIV_Q)
    assert res.answer == "It minimizes"
    assert port.judged == 1 and port.single_shot == 1 and port.steps == 0
    # tenant isolation: the recall is filtered to the requesting user.
    assert seam.calls and seam.calls[0][2] == {"user_id": str(USER)}


async def test_rag_empty_recall_escalates_with_honest_note(monkeypatch):
    port = ScriptedPort(steps=[AGENT_STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    res = await sse(app, PRIV_Q)
    assert port.judged == 0 and port.steps == 1
    note = port.requests[-1][-1]["content"]
    assert "Private retrieval note" in note            # honesty rides a searched branch
    assert res.answer == "Agent fallback."


async def test_rag_irrelevant_verdict_escalates(monkeypatch):
    port = ScriptedPort(steps=[AGENT_STEP], verdict="irrelevant"); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    app = build_app(monkeypatch, port, FakeSeam(HIT), kernel, broker)
    res = await sse(app, PRIV_Q)
    assert port.judged == 1 and port.single_shot == 0 and port.steps == 1
    assert res.answer == "Agent fallback."


async def test_rag_seam_error_escalates_fail_closed(monkeypatch):
    port = ScriptedPort(steps=[AGENT_STEP]); spy = Spy()
    kernel, _, _, broker = build_kernel(monkeypatch, port, spy)
    _gate(monkeypatch, retrieval=True)
    app = build_app(monkeypatch, port, FakeSeam([], raise_exc=True), kernel, broker)
    res = await sse(app, PRIV_Q)
    assert port.steps == 1                             # retrieval outage → Agent, not a crash
    assert res.types[-1] == "done"
    assert res.answer == "Agent fallback."


async def test_escalated_turn_is_fenced_by_original_private_only_policy(monkeypatch):
    """THE crown-jewel fence (commit b3c0311): a private_only turn whose retrieval is
    empty escalates to the Agent, but the ORIGINAL policy keeps HARD-DENYing NETWORK —
    so the Agent's web_search never executes, even with WRITE/NETWORK granted."""
    port = ScriptedPort(steps=[
        {"content": [], "tool_calls": [{"name": "web_search",
                                        "arguments": {"query": "gradient descent"}}]},
        AGENT_STEP,
    ])
    spy = Spy()
    # Grant NETWORK to prove the fence BEATS grant; seam empty forces escalation.
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, grant=[ToolPermission.NETWORK, ToolPermission.WRITE],
    )
    _gate(monkeypatch, retrieval=True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True, raising=False)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    msg = "answer only from my knowledge base, no web: what is gradient descent"
    res = await sse(app, msg)
    assert spy.web_queries == []                        # provider.search NEVER called
    assert port.steps >= 1                              # the Agent did take the turn
    assert res.types[-1] == "done"


async def test_non_private_turn_web_search_is_not_fenced(monkeypatch):
    """Contrast: WITHOUT a private_only/first policy the same web_search runs (proves the
    fence is policy-driven, not a blanket network ban)."""
    port = ScriptedPort(steps=[
        {"content": [], "tool_calls": [{"name": "web_search",
                                        "arguments": {"query": "quantum error correction"}}]},
        AGENT_STEP,
    ])
    spy = Spy()
    kernel, _, _, broker = build_kernel(
        monkeypatch, port, spy, grant=[ToolPermission.NETWORK],
    )
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True, raising=False)
    app = build_app(monkeypatch, port, FakeSeam([]), kernel, broker)
    await sse(app, "search the web for quantum error correction")
    assert spy.web_queries == ["quantum error correction"]   # unfenced ⇒ provider ran
