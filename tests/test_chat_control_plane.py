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


# ── Phase 4 LOCAL_RAG policy mapping ───────────────────────────────────────────────

def _rag_req(**over):
    kw = {"needs_private": Signal.HIGH, "confidence": Confidence.HIGH}
    kw.update(over)
    return TurnRequirements(**kw)


def _all_on():
    return PolicyContext(
        fast_paths_enabled=True, direct_fast_path_enabled=True,
        viewer_fast_path_enabled=True, retrieval_fast_path_enabled=True,
    )


def test_retrieval_kind_maps_under_retrieval_gate_only():
    plan = build_execution_plan(_rag_req(), _all_on())
    assert plan.kind is PlanKind.LOCAL_RAG and plan.requires_retrieval is True
    # Everything else ON but the retrieval gate closed → still AGENT (per-kind gating).
    no_rag = PolicyContext(
        fast_paths_enabled=True, direct_fast_path_enabled=True, viewer_fast_path_enabled=True,
    )
    assert build_execution_plan(_rag_req(), no_rag).kind is PlanKind.AGENT


def test_private_plus_other_demand_stays_agent_fail_closed():
    # A co-occurring web demand never mixes public with private on the fast path —
    # the Agent arbitrates. Same for viewer/action/memory co-demands.
    for over in (
        {"needs_web": Signal.HIGH},
        {"needs_viewer": Signal.HIGH},
        {"needs_action": Signal.HIGH},
        {"needs_memory": True},
    ):
        assert build_execution_plan(_rag_req(**over), _all_on()).kind is PlanKind.AGENT, over


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
        "web": "what is the latest news",
        "memory": "do you remember what we discussed earlier",
        "long": "x" * 500,
    }
    for name, msg in cases.items():
        req = resolve_requirements(_ctx(message=msg), msg)
        assert req.confidence is not Confidence.HIGH, name
    # Phase 4 note: "summarize my document" IS now certified HIGH — but as a SOLE
    # private demand it can only map to LOCAL_RAG (own gate). The Phase 2 guarantee it
    # pinned stays pinned: a private demand is NEVER DIRECT-eligible.
    from core.application.chat.execution_plan import _is_direct_eligible

    priv = "summarize my document"
    req = resolve_requirements(_ctx(message=priv), priv)
    assert req.needs_private is Signal.HIGH and req.confidence is Confidence.HIGH
    assert not _is_direct_eligible(req)
    # …and with the retrieval gate closed it still lands on the Agent.
    direct_only = PolicyContext(fast_paths_enabled=True, direct_fast_path_enabled=True)
    assert build_execution_plan(req, direct_only).kind is PlanKind.AGENT


def test_l0_hard_context_facts_win():
    # A plain message is still not a direct turn when a viewer/attach/research is bound.
    assert resolve_requirements(_ctx(message="explain", viewer={"status": "injected"}), "explain").confidence is not Confidence.HIGH
    assert resolve_requirements(_ctx(message="explain", attach={"kind": "asset", "asset_id": "1"}), "explain").confidence is not Confidence.HIGH
    req = resolve_requirements(_ctx(message="continue", research=True, handoff={"kind": "research"}), "continue")
    assert req.needs_action is Signal.HIGH and req.confidence is Confidence.LOW


# ── Phase 4 L0: private-corpus question certification ──────────────────────────────

def test_l0_private_question_is_high_for_local_rag():
    # A lexical private demand with no other capability is certified HIGH — the
    # policy maps it to LOCAL_RAG only when the Phase 4 gate is on.
    msg = "what does my knowledge base say about gradient descent"
    req = resolve_requirements(_ctx(message=msg), msg)
    assert req.needs_private is Signal.HIGH and req.confidence is Confidence.HIGH
    assert build_execution_plan(req, _all_on()).kind is PlanKind.LOCAL_RAG


def test_l0_attach_and_mixed_private_demands_are_not_high():
    # Attach = read_document (precise tool read), NOT semantic recall — stays uncritical.
    msg = "what does my document say about x"
    att = _ctx(message=msg, attach={"kind": "asset", "asset_id": "1"})
    req = resolve_requirements(att, msg)
    assert req.confidence is not Confidence.HIGH and req.needs_private is Signal.HIGH
    # Private + time-sensitive mixes private with public — Agent arbitrates.
    mixed = "what does my knowledge base say about today's news"
    req = resolve_requirements(_ctx(message=mixed), mixed)
    assert req.confidence is not Confidence.HIGH
    # Private + memory: recall authority stays with MemoryService (Agent).
    mem = "do you remember what my knowledge base says about x"
    req = resolve_requirements(_ctx(message=mem), mem)
    assert req.confidence is not Confidence.HIGH


# ── orchestrator registry ─────────────────────────────────────────────────────────

def test_orchestrator_resolves_agent_plan_and_executor():
    from core.application.chat.executors.agent import AgentExecutor
    from core.application.chat.executors.direct import DirectExecutor
    from core.application.chat.executors.retrieval import RetrievalExecutor
    from core.application.chat.turn_orchestrator import TurnOrchestrator

    ctx = _ctx(message="hi")
    orch = TurnOrchestrator(deps=None)  # dark path: resolution touches no deps
    import asyncio
    plan = asyncio.run(orch.resolve_plan(ctx))  # master gate off → AGENT (async since QIR stage 1)
    assert plan.kind is PlanKind.AGENT
    assert isinstance(orch.executor_for(plan), AgentExecutor)
    # DIRECT / VIEWER / LOCAL_RAG are now registered branches...
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.DIRECT)), DirectExecutor)
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.LOCAL_RAG)), RetrievalExecutor)
    # ...and Phase 5 registered ACTION + COMPOSITE alongside them.
    from core.application.chat.executors.action import ActionExecutor
    from core.application.chat.executors.composite import CompositeExecutor
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.ACTION)), ActionExecutor)
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.COMPOSITE)), CompositeExecutor)
    # Fail-Closed is STRUCTURAL: no WEB executor is registered anywhere, so a private
    # turn's EscalateToAgent hand-back can only land on the Agent — the control plane
    # physically cannot route LOCAL_RAG -> WEB, and the Agent's own web use stays a
    # visible agent-level decision exactly as before Phase 4. WEB is also the example
    # of an unmapped kind: executor_for must degrade it to AGENT, never raise.
    assert isinstance(orch.executor_for(ExecutionPlan(kind=PlanKind.WEB)), AgentExecutor)
