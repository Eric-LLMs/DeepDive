"""P0 intent_funnel package tests: contracts + the orchestration's seams.

The 2026-09-26 live-table ruling deleted the legacy QIR lane: these tests pin
the DARK behavior (route returns the SAME object), the gate rules that keep the
funnel from overriding L0, and the node contracts the single-hop chain runs on.
"""
import types

import pytest
from core.application.chat import intent_funnel
from core.application.chat.intent_funnel import contract
from core.application.chat.intent_funnel.funnel import funnel_live, route
from core.application.chat.understanding import Signal, TurnRequirements
from core.config import settings

# ── route(): dark launch stays byte-identical ────────────────────────────────────


async def test_route_dark_returns_same_requirements_object(monkeypatch):
    # Default config: the funnel master gate is off -> route must be identity.
    monkeypatch.setattr(settings, "chat_funnel_enabled", False)
    req = TurnRequirements()
    ctx = types.SimpleNamespace(body=types.SimpleNamespace(message="create a folder x"))
    out = await route(ctx, deps=object(), requirements=req)
    assert out is req


async def test_route_delegates_when_gate_live(monkeypatch):
    monkeypatch.setattr(intent_funnel.funnel, "funnel_live", lambda r, d, c: True)

    sentinel = TurnRequirements(needs_action=Signal.HIGH)

    async def fake_cascade(ctx, deps, requirements):
        return sentinel

    monkeypatch.setattr(intent_funnel.funnel, "_cascade", fake_cascade)
    out = await route(object(), deps=object(), requirements=TurnRequirements())
    assert out is sentinel


async def test_route_skips_cascade_when_gate_dark(monkeypatch):
    monkeypatch.setattr(intent_funnel.funnel, "funnel_live", lambda r, d, c: False)

    async def boom_cascade(ctx, deps, requirements):  # must never run
        raise AssertionError("gate closed but cascade executed")

    monkeypatch.setattr(intent_funnel.funnel, "_cascade", boom_cascade)
    req = TurnRequirements()
    assert await route(object(), deps=object(), requirements=req) is req


async def test_route_never_touches_an_l0_certified_turn(monkeypatch):
    # Migration compat boundary: a turn L0 already certified exits before the
    # gate is even consulted.
    monkeypatch.setattr(intent_funnel.funnel, "funnel_live",
                        lambda r, d, c: pytest.fail("gate consulted on L0 turn"))
    req = TurnRequirements(requested_action={"tool": "t", "args": {}})
    assert await route(object(), deps=object(), requirements=req) is req


# ── funnel_live(): the gate rules ────────────────────────────────────────────────


def _live_ctx(message="create a folder"):
    return types.SimpleNamespace(body=types.SimpleNamespace(message=message))


def _all_switches_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_funnel_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)


def test_gate_requires_every_switch(monkeypatch):
    _all_switches_on(monkeypatch)
    assert funnel_live(TurnRequirements(), deps=object(), ctx=_live_ctx()) is True
    # every single switch turning off kills it
    for flag in ("chat_funnel_enabled", "chat_fast_paths_enabled",
                 "chat_action_fast_path_enabled"):
        monkeypatch.setattr(settings, flag, False)
        assert funnel_live(TurnRequirements(), deps=object(), ctx=_live_ctx()) is False
        monkeypatch.setattr(settings, flag, True)
    # deps=None (control plane not wired) is dark
    assert funnel_live(TurnRequirements(), deps=None, ctx=_live_ctx()) is False


def test_gate_defers_demanding_turns_to_guardrails(monkeypatch):
    _all_switches_on(monkeypatch)
    webby = TurnRequirements(needs_web=Signal.HIGH)
    assert funnel_live(webby, deps=object(), ctx=_live_ctx()) is False
    memory = TurnRequirements(needs_memory=True)
    assert funnel_live(memory, deps=object(), ctx=_live_ctx()) is False
    research = TurnRequirements()
    ctx = _live_ctx()
    ctx.research_turn = True
    assert funnel_live(research, deps=object(), ctx=ctx) is False
    handoff = TurnRequirements()
    ctx2 = _live_ctx()
    ctx2.effective_handoff = "some-mode"
    assert funnel_live(handoff, deps=object(), ctx=ctx2) is False


def test_gate_refuses_non_pure_user_text(monkeypatch):
    _all_switches_on(monkeypatch)
    assert funnel_live(
        TurnRequirements(), deps=object(), ctx=_live_ctx("[Attached: report.pdf] summarize"),
    ) is False


# ── contracts ────────────────────────────────────────────────────────────────────


def test_bound_arguments_adapter_maps_legacy_return():
    complete = contract.BoundArguments.of({"name": "资料"})
    assert complete.state == contract.BIND_COMPLETE
    assert complete.is_complete and not complete.is_missing
    assert complete.args == {"name": "资料"}
    missing = contract.BoundArguments.of(None)
    assert missing.state == contract.BIND_MISSING
    assert missing.is_missing and not missing.is_complete
    assert missing.args is None


def test_reason_code_constants_avoid_bare_ambiguous():
    # 8.10: prefixed codes only — the legacy Confidence.AMBIGUOUS stays untouched.
    assert contract.MATCH_AMBIGUOUS == "MATCH_AMBIGUOUS"
    assert contract.MATCH_AMBIGUOUS != "AMBIGUOUS"
    m = contract.MatchResult(state=contract.MATCH_AMBIGUOUS, candidates=("a", "b"))
    assert m.capability_id is None  # ambiguous: Matcher never picks (8.1)


def test_contracts_are_frozen():
    for cls in (
        contract.MatchResult, contract.Candidate, contract.RecallResult,
        contract.ToolIntentVerdict, contract.BoundArguments, contract.TurnFacts,
    ):
        assert cls.__dataclass_params__.frozen, cls.__name__
    # chain ruling 2026-09-24: the Decision node is gone from the contract, and
    # with it the second-hop recheck entry point.
    assert not hasattr(contract, "DecisionResult")
    from core.application.chat.intent_funnel import tool_intent
    assert not hasattr(tool_intent, "recheck")
    # naming ruling 2026-09-24: "Model A" was a placeholder and Judge/Decision
    # were historical — the responsibility name (ToolIntentModel) is the ONLY
    # vocabulary allowed in the contract and package namespaces.
    bad = ("judge", "model_a", "modela", "decision")
    assert not [n for n in dir(contract) if any(b in n.lower() for b in bad)]
    assert not [n for n in dir(tool_intent) if any(b in n.lower() for b in bad)]
    # live-table ruling 2026-09-26: the QIR adapter lane is deleted outright —
    # no IntentVerdict/AgentFallback survivors, the funnel certifies by
    # returning an ACTION TurnRequirements or the fail-open original.
    assert not hasattr(contract, "IntentVerdict")
    assert not hasattr(contract, "AgentFallback")


async def test_orchestrator_calls_funnel_once_with_requirements(monkeypatch):
    """The P0 shape: orchestrator keeps the lifecycle, funnel owns the routing."""
    from core.application.chat.turn_orchestrator import TurnOrchestrator

    calls = []

    async def fake_route(ctx, *, deps, requirements):
        calls.append(requirements)
        return requirements

    monkeypatch.setattr(intent_funnel, "route", fake_route)
    orch = TurnOrchestrator(deps=None)
    ctx = types.SimpleNamespace(
        body=types.SimpleNamespace(message="hello"),
        agent_context=None,
    )
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    await orch.resolve_plan(ctx, deps=None)
    assert len(calls) == 1
    assert isinstance(calls[0], TurnRequirements)
