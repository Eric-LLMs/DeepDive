"""Phase 1 control-plane contracts: everything maps to AGENT, wire stays legacy.

The router-level SSE compatibility is already covered end-to-end by
test_viewer_chat (TestClient + fake kernel asserts frame shapes). What is pinned
here is the pure policy surface introduced in Phase 1:

* ``TurnRequirements`` defaults to an abstain (no understanding engine yet);
* ``build_execution_plan`` maps EVERY input to AGENT while fast paths are off —
  including a HIGH-confidence requirement (the master switch, not the signals,
  decides Phase 1 routing);
* the orchestrator's plan resolution + executor registry always yield the
  AgentExecutor branch (unmapped kinds fall back, never hard-fail).
"""
from __future__ import annotations

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
)


def test_default_requirements_abstain():
    req = TurnRequirements()
    assert req.confidence is Confidence.ABSTAIN
    assert req.needs_private is Signal.LOW
    assert req.needs_memory is False


def test_phase1_policy_maps_everything_to_agent():
    # Neutral (abstain) and fully-determined HIGH inputs BOTH map to AGENT while
    # the master switch is off — Phase 1 must be behavior-identical to the agent.
    neutral = build_execution_plan(TurnRequirements(), PolicyContext())
    assert neutral.kind is PlanKind.AGENT

    strong = TurnRequirements(
        needs_private=Signal.LOW,
        needs_web=Signal.LOW,
        needs_viewer=Signal.LOW,
        needs_action=Signal.LOW,
        confidence=Confidence.HIGH,
    )
    plan = build_execution_plan(strong, PolicyContext())
    assert plan.kind is PlanKind.AGENT


def test_agent_plan_reason_is_traceable():
    plan = build_execution_plan(TurnRequirements(), PolicyContext())
    assert "phase1" in plan.reason


def test_orchestrator_resolves_agent_plan_and_executor():
    from core.application.chat.context import ChatTurnContext
    from core.application.chat.executors.agent import AgentExecutor
    from core.application.chat.turn_orchestrator import TurnOrchestrator

    ctx = ChatTurnContext(
        body=None, user=None, user_id="u", guest_token=None, log_user=None,
        tier="free", notice=None, model=None, base_url=None, api_key=None,
        business_name=None, credential_id=None, user_text="hi",
        owned_asset_id=None, inline_image=None,
    )
    orch = TurnOrchestrator(deps=None)  # plan resolution touches no deps
    plan = orch.resolve_plan(ctx)
    assert isinstance(plan, ExecutionPlan) and plan.kind is PlanKind.AGENT
    assert isinstance(orch.executor_for(plan), AgentExecutor)
    # An unmapped kind must degrade to the agent branch, never raise.
    fake = ExecutionPlan(kind=PlanKind.DIRECT)
    assert isinstance(orch.executor_for(fake), AgentExecutor)
