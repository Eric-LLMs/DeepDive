"""P0 intent_funnel package tests: contracts + the moved orchestration's seams.

P0 discipline (docs/temp.md): the move must be provably structure-only — these
tests pin the DARK behavior (route returns the SAME object), the adapter mapping
between the legacy QIR/bind shapes and the node contracts, and the gate rules
that keep the funnel from overriding L0.
"""
import types

import pytest

from core.application.chat import intent_funnel
from core.application.chat.intent_funnel import contract
from core.application.chat.intent_funnel.funnel import qir_live, route
from core.application.chat.understanding import Signal, TurnRequirements
from core.config import settings


# ── route(): dark launch stays byte-identical ────────────────────────────────────


async def test_route_dark_returns_same_requirements_object(monkeypatch):
    # Default config: chat gates are off -> route must be identity, no copies.
    monkeypatch.setattr(settings, "chat_qir_enabled", False)
    req = TurnRequirements()
    ctx = types.SimpleNamespace(body=types.SimpleNamespace(message="create a folder x"))
    out = await route(ctx, deps=object(), requirements=req)
    assert out is req


async def test_route_delegates_when_gate_live(monkeypatch):
    monkeypatch.setattr(intent_funnel.funnel, "qir_live", lambda r, d, c: True)

    sentinel = TurnRequirements(needs_action=Signal.HIGH)

    async def fake_stage(ctx, deps, requirements):
        return sentinel

    monkeypatch.setattr(intent_funnel.funnel, "run_intent_stage", fake_stage)
    out = await route(object(), deps=object(), requirements=TurnRequirements())
    assert out is sentinel


async def test_route_skips_stage_when_gate_dark(monkeypatch):
    monkeypatch.setattr(intent_funnel.funnel, "qir_live", lambda r, d, c: False)

    async def boom_stage(ctx, deps, requirements):  # must never run
        raise AssertionError("gate closed but stage executed")

    monkeypatch.setattr(intent_funnel.funnel, "run_intent_stage", boom_stage)
    req = TurnRequirements()
    assert await route(object(), deps=object(), requirements=req) is req


# ── qir_live(): the gate rules moved verbatim ────────────────────────────────────


def _live_ctx(message="create a folder"):
    return types.SimpleNamespace(body=types.SimpleNamespace(message=message))


def _all_switches_on(monkeypatch):
    monkeypatch.setattr(settings, "chat_qir_enabled", True)
    monkeypatch.setattr(settings, "chat_fast_paths_enabled", True)
    monkeypatch.setattr(settings, "chat_action_fast_path_enabled", True)


def test_gate_requires_every_switch(monkeypatch):
    _all_switches_on(monkeypatch)
    assert qir_live(TurnRequirements(), deps=object(), ctx=_live_ctx()) is True
    # every single switch turning off kills it
    for flag in ("chat_qir_enabled", "chat_fast_paths_enabled",
                 "chat_action_fast_path_enabled"):
        monkeypatch.setattr(settings, flag, False)
        assert qir_live(TurnRequirements(), deps=object(), ctx=_live_ctx()) is False
        monkeypatch.setattr(settings, flag, True)
    # deps=None (control plane not wired) is dark
    assert qir_live(TurnRequirements(), deps=None, ctx=_live_ctx()) is False


def test_gate_never_overrides_l0_or_demanding_turns(monkeypatch):
    _all_switches_on(monkeypatch)
    certified = TurnRequirements(requested_action={"tool": "t", "args": None})
    assert qir_live(certified, deps=object(), ctx=_live_ctx()) is False
    webby = TurnRequirements(needs_web=Signal.HIGH)
    assert qir_live(webby, deps=object(), ctx=_live_ctx()) is False
    memory = TurnRequirements(needs_memory=True)
    assert qir_live(memory, deps=object(), ctx=_live_ctx()) is False
    research = TurnRequirements()
    ctx = _live_ctx()
    ctx.research_turn = True
    assert qir_live(research, deps=object(), ctx=ctx) is False


def test_gate_refuses_non_pure_user_text(monkeypatch):
    _all_switches_on(monkeypatch)
    assert qir_live(
        TurnRequirements(), deps=object(), ctx=_live_ctx("[Attached: report.pdf] summarize"),
    ) is False


# ── contract adapters: legacy shapes map without information loss ────────────────


def test_bound_arguments_adapter_maps_legacy_return():
    complete = contract.BoundArguments.of({"name": "资料"})
    assert complete.state == contract.BIND_COMPLETE
    assert complete.is_complete and not complete.is_missing
    assert complete.args == {"name": "资料"}
    missing = contract.BoundArguments.of(None)
    assert missing.state == contract.BIND_MISSING
    assert missing.is_missing and not missing.is_complete
    assert missing.args is None


def test_intent_verdict_adapter_carries_routing_metadata_only():
    from core.application.chat.qir.types import RouteResult

    v = contract.IntentVerdict.from_qir(
        RouteResult(capability_id="cap-create-folder", registry_version="qir1-abc")
    )
    assert (v.capability_id, v.registry_version, v.stage) == (
        "cap-create-folder", "qir1-abc", "semantic+decision",
    )
    # 8.8: a verdict never carries execution authority
    fields = {f.name for f in v.__dataclass_fields__.values()}
    assert fields == {"capability_id", "registry_version", "stage"}


def test_reason_code_constants_avoid_bare_ambiguous():
    # 8.10: prefixed codes only — the legacy Confidence.AMBIGUOUS stays untouched.
    assert contract.MATCH_AMBIGUOUS == "MATCH_AMBIGUOUS"
    assert contract.MATCH_AMBIGUOUS != "AMBIGUOUS"
    m = contract.MatchResult(state=contract.MATCH_AMBIGUOUS, candidates=("a", "b"))
    assert m.capability_id is None  # ambiguous: Matcher never picks (8.1)


def test_contracts_are_frozen():
    for cls in (
        contract.MatchResult, contract.Candidate, contract.RecallResult,
        contract.JudgeVerdict, contract.DecisionResult, contract.BoundArguments,
        contract.IntentVerdict, contract.AgentFallback,
    ):
        assert cls.__dataclass_params__.frozen, cls.__name__


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
