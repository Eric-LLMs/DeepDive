"""Control-plane contracts: the dark-launch guarantee + Phase 2 DIRECT gating.

The router-level SSE compatibility is already covered end-to-end by
test_viewer_chat (TestClient + fake kernel asserts frame shapes). What is pinned
here is the pure policy + understanding surface:

* ``TurnRequirements`` defaults to an abstain;
* the master switch (not the signals) keeps routing on AGENT when off — the whole
  control plane is behavior-identical to the agent while ``fast_paths_enabled`` is
  False, regardless of confidence;
* Phase 2: a short, pure, zero-demand turn maps to DIRECT ONLY under both gates;
  any capability demand, memory flag, or a closed gate routes to AGENT;
* L0: the in-process signal engine sets confidence correctly from cheap facts;
* the orchestrator's registry resolves the right branch and degrades unmapped kinds
  to the agent branch, never hard-failing.
"""
from __future__ import annotations

from types import SimpleNamespace

from core.application.chat.execution_plan import (
    ExecutionPlan,
    PlanKind,
    PolicyContext,
    build_execution_plan,
)
from core.application.chat.understanding import (
    Confidence,
    Signal,
    TurnRequirements,
    resolve_requirements,
)


def test_default_requirements_abstain():
    req = TurnRequirements()
    assert req.confidence is Confidence.ABSTAIN
    assert req.needs_private is Signal.LOW
    assert req.needs_memory is False


def test_master_switch_keeps_everything_on_agent():
    # Neutral (abstain) and fully-determined HIGH inputs BOTH map to AGENT while the
    # master switch is off — the dark launch must be behavior-identical to the agent.
    neutral = build_execution_plan(TurnRequirements(), PolicyContext())
    assert neutral.kind is PlanKind.AGENT

    strong = TurnRequirements(
        needs_private=Signal.LOW, needs_web=Signal.LOW,
        needs_viewer=Signal.LOW, needs_action=Signal.LOW,
        confidence=Confidence.HIGH,
    )
    # fast paths enabled but the DIRECT gate closed → still AGENT (per-kind gating).
    assert build_execution_plan(strong, PolicyContext(fast_paths_enabled=True)).kind is PlanKind.AGENT


def test_direct_kind_maps_only_under_both_gates():
    clean = TurnRequirements(confidence=Confidence.HIGH)  # all needs default LOW
    both_on = PolicyContext(fast_paths_enabled=True, direct_fast_path_enabled=True)
    assert build_execution_plan(clean, both_on).kind is PlanKind.DIRECT
    # A demand present disqualifies DIRECT even with both gates on.
    demanding = TurnRequirements(
        confidence=Confidence.HIGH, needs_web=Signal.HIGH,
    )
    assert build_execution_plan(demanding, both_on).kind is PlanKind.AGENT


# ── Phase 3 VIEWER policy mapping ────────────────────────────────────────────────

def _viewer_req(**over):
    kw = {"needs_viewer": Signal.HIGH, "confidence": Confidence.HIGH}
    kw.update(over)
    return TurnRequirements(**kw)


def test_viewer_kind_maps_under_viewer_gate_only():
    all_on = PolicyContext(
        fast_paths_enabled=True, direct_fast_path_enabled=True, viewer_fast_path_enabled=True,
    )
    plan = build_execution_plan(_viewer_req(), all_on)
    assert plan.kind is PlanKind.VIEWER and plan.requires_viewer is True
    # Viewer gate OFF → a viewer-demand turn (needs_viewer HIGH) is not DIRECT either,
    # so it falls through to AGENT.
    only_direct = PolicyContext(fast_paths_enabled=True, direct_fast_path_enabled=True)
    assert build_execution_plan(_viewer_req(), only_direct).kind is PlanKind.AGENT


def test_viewer_demand_with_other_capability_stays_agent():
    all_on = PolicyContext(
        fast_paths_enabled=True, direct_fast_path_enabled=True, viewer_fast_path_enabled=True,
    )
    # The viewer must be the SOLE demand — a co-occurring private/web/memory need keeps
    # the turn on the Agent (which owns read_document / rag / web / recall).
    for over in (
        {"needs_private": Signal.HIGH},
        {"needs_web": Signal.HIGH},
        {"needs_action": Signal.HIGH},
        {"needs_memory": True},
    ):
        assert build_execution_plan(_viewer_req(**over), all_on).kind is PlanKind.AGENT, over


def test_viewer_ground_eligible_rules():
    from core.application.chat.understanding import _viewer_ground_eligible

    b = lambda kind, img=None: SimpleNamespace(kind=kind, image_asset_id=img)
    assert _viewer_ground_eligible({"status": "injected", "blocks": [b("selection")]}) is True
    assert _viewer_ground_eligible({"status": "injected", "blocks": [b("page")]}) is True
    # image/roi/frame → needs the vision tool → NOT eligible (media path stays on Agent).
    assert _viewer_ground_eligible({"status": "injected", "blocks": [b("frame", img="9")]}) is False
    # stub (document open, nothing injected) → NOT eligible (Open != Inject).
    assert _viewer_ground_eligible({"status": "stub", "blocks": []}) is False
    assert _viewer_ground_eligible({"status": "injected", "blocks": []}) is False
    assert _viewer_ground_eligible(None) is False


def test_l0_viewer_injected_text_is_high_but_stub_is_not():
    # HIGH only for an already-injected TEXT turn with the viewer as the sole demand.
    inj = _ctx(message="what does this say", viewer={
        "status": "injected",
        "blocks": [SimpleNamespace(kind="selection", image_asset_id=None)],
    })
    assert resolve_requirements(inj, "what does this say").confidence is Confidence.HIGH
    # A stub (readable doc open, no selection) must NOT be HIGH → stays on the Agent.
    stub = _ctx(message="summarize the document", viewer={"status": "stub", "blocks": []})
    assert resolve_requirements(stub, "summarize the document").confidence is not Confidence.HIGH


def test_reason_is_traceable():
    assert "agent" in build_execution_plan(TurnRequirements(), PolicyContext()).reason


# ── L0 signal engine ────────────────────────────────────────────────────────────

def _ctx(*, message, attach=None, viewer=None, research=False, handoff=None):
    from core.application.chat.context import ChatTurnContext
    return ChatTurnContext(
        body=SimpleNamespace(message=message, attach=attach),
        user=None, user_id="u", guest_token=None, log_user=None, tier="free",
        notice=None, model=None, base_url=None, api_key=None, business_name=None,
        credential_id=None, user_text=message, owned_asset_id=None, inline_image=None,
        viewer_assembly=viewer, research_turn=research, effective_handoff=handoff,
    )


def test_l0_short_pure_turn_is_high():
    req = resolve_requirements(_ctx(message="hello there"), "hello there")
    assert req.confidence is Confidence.HIGH
    assert req.needs_private is Signal.LOW and req.needs_memory is False


def test_l0_capability_and_memory_demands_are_not_high():
    cases = {
        "private": "summarize my document",
        "web": "what is the latest news",
        "memory": "do you remember what we discussed earlier",
        "long": "x" * 500,
    }
    for name, msg in cases.items():
        req = resolve_requirements(_ctx(message=msg), msg)
        assert req.confidence is not Confidence.HIGH, name


def test_l0_hard_context_facts_win():
    # A plain message is still not a direct turn when a viewer/attach/research is bound.
    assert resolve_requirements(_ctx(message="explain", viewer={"status": "injected"}), "explain").confidence is not Confidence.HIGH
    assert resolve_requirements(_ctx(message="explain", attach={"kind": "asset", "asset_id": "1"}), "explain").confidence is not Confidence.HIGH
    req = resolve_requirements(_ctx(message="continue", research=True, handoff={"kind": "research"}), "continue")
    assert req.needs_action is Signal.HIGH and req.confidence is Confidence.LOW


# ── orchestrator registry ─────────────────────────────────────────────────────────

def test_orchestrator_resolves_agent_plan_and_executor():
    from core.application.chat.executors.agent import AgentExecutor
    from core.application.chat.executors.direct import DirectExecutor
    from core.application.chat.turn_orchestrator import TurnOrchestrator

    ctx = _ctx(message="hi")
    orch = TurnOrchestrator(deps=None)  # dark path: resolution touches no deps
    plan = orch.resolve_plan(ctx)  # master gate off by default → AGENT
    assert plan.kind is PlanKind.AGENT
    assert isinstance(orch.executor_for(plan), AgentExecutor)
    # DIRECT is now a registered branch...
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.DIRECT)), DirectExecutor)
    # ...while a still-unmapped kind degrades to the agent branch, never raising.
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.COMPOSITE)), AgentExecutor)
