"""ExecutionPlan: the control plane's single execution contract + pure policy.

This module OWNS ``ExecutionPlan`` and the pure mapping function
``build_execution_plan(requirements, policy)``. The mapping is in-memory only — no
I/O, no capability probing, no authorization (those live in Pre-flight and the
executors). ``turn_orchestrator`` is a pure consumer of the result.

Phase 1 policy: fast paths are switched off globally, so every turn resolves to
``AGENT`` and the refactor is behavior-neutral. Later phases extend the mapping
below the ``fast_paths_enabled`` gate, one kind at a time (DIRECT -> VIEWER ->
LOCAL_RAG -> ACTION/COMPOSITE), each behind its own feature switch.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.application.chat.understanding import Confidence, Signal, TurnRequirements


class PlanKind(str, Enum):
    DIRECT = "direct"
    VIEWER = "viewer"
    LOCAL_RAG = "local_rag"
    WEB = "web"
    ACTION = "action"
    COMPOSITE = "composite"
    AGENT = "agent"


@dataclass(frozen=True)
class PolicyContext:
    """Tenant/global switches consumed by the pure policy. Values only — no handles.

    Phase 1: every fast-path gate is False → all traffic maps to AGENT.
    """

    fast_paths_enabled: bool = False       # global master switch
    direct_fast_path_enabled: bool = False  # Phase 2
    viewer_fast_path_enabled: bool = False  # Phase 3
    retrieval_fast_path_enabled: bool = False  # Phase 4
    action_enabled: bool = False           # Phase 5
    web_enabled: bool = False              # later


@dataclass(frozen=True)
class ExecutionPlan:
    """What the orchestrator must do for this turn — the sole scheduling input.

    ``requires_memory`` only marks HYDRATION ELIGIBILITY; the authoritative recall
    decision stays with ``MemoryService.should_recall()``. ``action`` carries the
    normalized Action Request (ACTION kind only); final validation/authz happen in
    Pre-flight, not here. ``reason`` is the decision trace for telemetry.
    """

    kind: PlanKind
    requires_memory: bool = False
    requires_viewer: bool = False
    requires_retrieval: bool = False
    action: dict | None = None
    reason: str = ""


def _agent(reason: str) -> ExecutionPlan:
    return ExecutionPlan(kind=PlanKind.AGENT, requires_memory=True, reason=reason)


def build_execution_plan(
    requirements: TurnRequirements, policy: PolicyContext
) -> ExecutionPlan:
    """Pure mapping: TurnRequirements + policy switches -> ExecutionPlan.

    Static rules (enforced from Phase 2 on):
      * anything not HIGH-confidence, any AMBIGUOUS/ABSTAIN or capability conflict
        -> AGENT (fallback);
      * private retrieval failure must never downgrade to WEB (fail-closed);
      * dynamic multi-step chains never become COMPOSITE — COMPOSITE only aggregates
        independent parallel inputs;
      * side-effect actions with unregistered templates -> AGENT or explicit error.
    """
    if requirements.confidence is not Confidence.HIGH:
        return _agent(f"confidence={requirements.confidence.value} -> agent")
    if not policy.fast_paths_enabled:
        # Control plane ships dark: the legacy full-agent path stays authoritative.
        return _agent("fast paths disabled -> agent")

    # ── Phase 2: DIRECT ───────────────────────────────────────────────────────────
    # A HIGH-confidence, zero-demand, short+pure turn is the direct case. Any
    # capability still HIGH (defensive — L0 gates them, this is the policy guard) or a
    # memory flag disqualifies DIRECT: memory recall authority lives with
    # MemoryService, not this path, so a recall-eligible turn stays on the Agent.
    if policy.direct_fast_path_enabled and _is_direct_eligible(requirements):
        return ExecutionPlan(
            kind=PlanKind.DIRECT, requires_memory=False,
            reason="phase2: short pure turn, no capability demand -> direct",
        )

    # ── Phase 3: VIEWER (grounded over already-injected text blocks) ──────────────
    # Only a turn the L0 engine certified as needing the viewer content, with no other
    # capability demand, maps here. ``requires_viewer`` is a marker for the executor;
    # the STUB (read_document) and image (vision) paths were never certified HIGH, so
    # opening a PDF with nothing injected still falls through to the Agent below.
    if policy.viewer_fast_path_enabled and _is_viewer_eligible(requirements):
        return ExecutionPlan(
            kind=PlanKind.VIEWER, requires_viewer=True, requires_memory=False,
            reason="phase3: viewer content already injected as text -> viewer-grounded",
        )

    return _agent("no enabled fast path matches this requirement set -> agent")


def _is_direct_eligible(requirements: TurnRequirements) -> bool:
    """The DIRECT capability-freeness guard (private/web/viewer/action all LOW, no memory)."""
    return (
        requirements.needs_private is Signal.LOW
        and requirements.needs_web is Signal.LOW
        and requirements.needs_viewer is Signal.LOW
        and requirements.needs_action is Signal.LOW
        and not requirements.needs_memory
    )


def _is_viewer_eligible(requirements: TurnRequirements) -> bool:
    """The VIEWER guard: the viewer is the SOLE demanded capability (HIGH), everything
    else LOW and no memory — so the answer is grounded on the injected blocks alone."""
    return (
        requirements.needs_viewer is Signal.HIGH
        and requirements.needs_private is Signal.LOW
        and requirements.needs_web is Signal.LOW
        and requirements.needs_action is Signal.LOW
        and not requirements.needs_memory
    )
