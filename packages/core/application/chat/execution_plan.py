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
    action_enabled: bool = False           # Phase 5A
    composite_enabled: bool = False        # Phase 5B
    web_enabled: bool = False              # later


@dataclass(frozen=True)
class ExecutionPlan:
    """What the orchestrator must do for this turn — the sole scheduling input.

    ``requires_memory`` only marks HYDRATION ELIGIBILITY; the authoritative recall
    decision stays with ``MemoryService.should_recall()``. ``action`` carries the
    normalized Action Request (ACTION kind only); final validation/authz happen in
    Pre-flight, not here. ``reason`` is the decision trace for telemetry.

    ``source_policy`` records the ORIGINAL request's source restriction (``None`` /
    ``"private_only"`` / ``"private_first"``); it is derived from user intent, NEVER
    from a fast-path failure, and is what fences the Agent's tools after escalation.
    """

    kind: PlanKind
    requires_memory: bool = False
    requires_viewer: bool = False
    requires_retrieval: bool = False
    action: dict | None = None
    subrequests: tuple[str, ...] = ()
    reason: str = ""
    source_policy: str | None = None


def _source_policy(requirements, kind: PlanKind) -> str | None:
    """Map the source FACTS to the policy that fences the escalated Agent.

    Three states, per the Fail-Closed clarification:
      * ``private_only``   — explicit KB-only: no web, even with approval offered;
      * ``private_first``  — the turn maps to LOCAL_RAG without an explicit external
        permission: the corpus is the ONLY sanctioned source, so an escalation must
        disclose the gap honestly instead of silently widening to the web;
      * ``None``           — everything else (mixed requests, explicit "you may
        search the web", non-private turns): the normal permission/approval funnel
        governs. RAG failing does NOT create this fence — the user's intent does.
    """
    if requirements.private_only and requirements.needs_web is not Signal.HIGH:
        return "private_only"
    # COMPOSITE carries a private-recall sub-request, so it fences exactly like
    # LOCAL_RAG: an escalation inherits the same source policy (the Sandbox keeps
    # HARD-DENYing NETWORK for private_only/private_first after the swap).
    if kind in (PlanKind.LOCAL_RAG, PlanKind.COMPOSITE) and not requirements.external_ok:
        return "private_first"
    return None


def _plan(requirements, kind: PlanKind, **kw) -> ExecutionPlan:
    return ExecutionPlan(kind=kind, source_policy=_source_policy(requirements, kind), **kw)


def _agent(requirements, reason: str) -> ExecutionPlan:
    return _plan(requirements, PlanKind.AGENT, requires_memory=True, reason=reason)


def build_execution_plan(
    requirements: TurnRequirements, policy: PolicyContext
) -> ExecutionPlan:
    """Pure mapping: TurnRequirements + policy switches -> ExecutionPlan.

    Static rules (enforced from Phase 2 on):
      * anything not HIGH-confidence, any AMBIGUOUS/ABSTAIN or capability conflict
        -> AGENT (fallback);
      * private retrieval failure escalates to AGENT under the turn's SOURCE POLICY
        (from the original request) — the failure itself never adds nor removes a
        web ban, and never silently substitutes external for private evidence;
      * dynamic multi-step chains never become COMPOSITE — COMPOSITE only aggregates
        independent parallel inputs;
      * side-effect actions with unregistered templates -> AGENT or explicit error.
    """
    if requirements.confidence is not Confidence.HIGH:
        return _agent(requirements, f"confidence={requirements.confidence.value} -> agent")
    if not policy.fast_paths_enabled:
        # Control plane ships dark: the legacy full-agent path stays authoritative.
        return _agent(requirements, "fast paths disabled -> agent")

    # ── Phase 2: DIRECT ───────────────────────────────────────────────────────────
    # A HIGH-confidence, zero-demand, short+pure turn is the direct case. Any
    # capability still HIGH (defensive — L0 gates them, this is the policy guard) or a
    # memory flag disqualifies DIRECT: memory recall authority lives with
    # MemoryService, not this path, so a recall-eligible turn stays on the Agent.
    if policy.direct_fast_path_enabled and _is_direct_eligible(requirements):
        return _plan(
            requirements, PlanKind.DIRECT, requires_memory=False,
            reason="phase2: short pure turn, no capability demand -> direct",
        )

    # ── Phase 3: VIEWER (grounded over already-injected text blocks) ──────────────
    # Only a turn the L0 engine certified as needing the viewer content, with no other
    # capability demand, maps here. ``requires_viewer`` is a marker for the executor;
    # the STUB (read_document) and image (vision) paths were never certified HIGH, so
    # opening a PDF with nothing injected still falls through to the Agent below.
    if policy.viewer_fast_path_enabled and _is_viewer_eligible(requirements):
        return _plan(
            requirements, PlanKind.VIEWER, requires_viewer=True, requires_memory=False,
            reason="phase3: viewer content already injected as text -> viewer-grounded",
        )

    # ── Phase 4: LOCAL_RAG (staged retrieval over the SHARED pipeline) ───────────
    # A HIGH-confidence turn whose SOLE demand is the private corpus. The executor
    # recalls through the existing config-driven RAGPipeline seam, gates sufficiency
    # with the existing CRAG judge, and answers grounded; failure / empty / ambiguous
    # escalate back to AGENT (Fail-Closed — this kind never re-routes to WEB).
    if policy.retrieval_fast_path_enabled and _is_retrieval_eligible(requirements):
        return _plan(
            requirements, PlanKind.LOCAL_RAG, requires_retrieval=True, requires_memory=False,
            reason="phase4: private-corpus demand is the sole capability -> staged RAG",
        )

    # ── Phase 5A: ACTION (typed direct dispatch over the registered tool registry) ──
    # L0's extractor already certified ONE allowlisted registered tool with fully-
    # determined args. An unregistered/ambiguous/under-parameterized turn never gets
    # ``requested_action`` set, so it maps to AGENT exactly as before this phase —
    # the policy mapper validates NOTHING (schema gate + authz live in the executor
    # and the host seam).
    if policy.action_enabled and _is_action_eligible(requirements):
        return _plan(
            requirements, PlanKind.ACTION, action=dict(requirements.requested_action),
            requires_memory=False,
            reason="phase5a: certified single registered tool, args fully determined -> action",
        )

    # ── Phase 5B: COMPOSITE (static independent fan-out: viewer text + private recall) ─
    # Only the {viewer + private} pair is v1-eligible — both inputs are independent and
    # known BEFORE execution, so they gather once and aggregate in one generation.
    # Anything sequenced/dynamic was never certified HIGH by L0.
    if policy.composite_enabled and _is_composite_eligible(requirements):
        return _plan(
            requirements, PlanKind.COMPOSITE,
            requires_viewer=True, requires_retrieval=True, requires_memory=False,
            subrequests=("viewer_text", "private_recall"),
            reason="phase5b: independent viewer+private inputs -> composite",
        )

    return _agent(requirements, "no enabled fast path matches this requirement set -> agent")


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


def _is_retrieval_eligible(requirements: TurnRequirements) -> bool:
    """The LOCAL_RAG guard: the private corpus is the SOLE demanded capability. A
    co-occurring web demand deliberately disqualifies rather than mixing sources —
    private retrieval failure must never downgrade to public search (fail-closed),
    and a mixed demand is the Agent's to arbitrate."""
    return (
        requirements.needs_private is Signal.HIGH
        and requirements.needs_viewer is Signal.LOW
        and requirements.needs_web is Signal.LOW
        and requirements.needs_action is Signal.LOW
        and not requirements.needs_memory
    )


def _is_action_eligible(requirements: TurnRequirements) -> bool:
    """The ACTION guard: L0 certified a registered-tool direct request. ``requested_action``
    is ONLY ever set by the DIRECT_TOOLS extractor, so presence is the full contract;
    no second registry check here (the executor's schema gate owns that)."""
    return (
        requirements.needs_action is Signal.HIGH
        and requirements.requested_action is not None
        and requirements.needs_web is Signal.LOW
        and not requirements.needs_memory
    )


def _is_composite_eligible(requirements: TurnRequirements) -> bool:
    """The COMPOSITE v1 guard: exactly the {viewer + private} pair, nothing else."""
    return (
        requirements.needs_viewer is Signal.HIGH
        and requirements.needs_private is Signal.HIGH
        and requirements.needs_web is Signal.LOW
        and requirements.needs_action is Signal.LOW
        and not requirements.needs_memory
    )
