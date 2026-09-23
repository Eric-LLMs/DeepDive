"""Intent Funnel orchestration — P0: the existing QIR cascade + argument binding,
moved out of ``TurnOrchestrator`` UNCHANGED (P0 discipline: move, don't change).

P0 honesty (frozen): this is NOT yet the final four-node Intent Funnel — it is
the current QIR orchestration extracted from the control plane so the
orchestrator only holds the turn lifecycle. The Matcher/Recall/Judge nodes get
real implementations in P1/P2 behind the contracts in ``contract.py``; today
only the gate, the QIR cascade and ``bind_arguments`` run, exactly as before
the move.
"""
from __future__ import annotations

import logging

from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)
from core.config import settings

from .contract import BoundArguments, IntentVerdict

logger = logging.getLogger(__name__)


def qir_live(requirements: TurnRequirements, deps, ctx) -> bool:
    """Gate: QIR only ever ADDS an action route the L0 abstained from — it
    never overrides an L0 certification, never touches web/memory-demanding
    or research/handoff turns, and stays physically dark unless every
    relevant switch is on."""
    if not (settings.chat_qir_enabled and settings.chat_fast_paths_enabled
            and settings.chat_action_fast_path_enabled and deps is not None):
        return False
    if requirements.requested_action is not None:  # L0 already certified
        return False
    if requirements.needs_web is not Signal.LOW or requirements.needs_memory:
        return False
    if getattr(ctx, "research_turn", False) or getattr(ctx, "effective_handoff", None):
        return False
    from core.application.chat.sanitization import is_pure_user_text
    return is_pure_user_text(ctx.body.message or "")


async def route(ctx, *, deps, requirements: TurnRequirements) -> TurnRequirements:
    """The one call the orchestrator makes (design: docs/temp.md P0 shape).

    Returns the SAME requirements object untouched whenever the funnel is dark
    or abstains — the Agent keeps the turn, byte-identical, zero pollution.
    When live, hands off to ``run_intent_stage`` (the former
    ``TurnOrchestrator._qir_intent_stage``, moved verbatim).
    """
    # P1 step-3 coexistence migration: once the QIR switch is on, ALSO run the
    # Registry-backed Matcher node in the dark — log its verdict next to the
    # legacy L0 outcome, never let it influence this turn. L0 stays authoritative
    # until the measured equivalence clears it for deletion (a P2 decision).
    if settings.chat_qir_enabled and deps is not None:
        await _shadow_matcher(ctx, deps, requirements)
    if not qir_live(requirements, deps, ctx):
        return requirements
    return await run_intent_stage(ctx, deps, requirements)


async def _shadow_matcher(ctx, deps, requirements: TurnRequirements) -> None:
    """Shadow logging only (8.15 discipline applied early to Node 1): the result
    NEVER feeds routing, and every failure is fail-quiet — a broken shadow node
    must not change the turn in any observable way except absent log lines."""
    try:
        from . import matcher
        from .registry import active_view

        view = await active_view(session_factory=deps.session_factory)
        if view is None:
            return  # nothing published yet — no comparison possible
        res = matcher.match(getattr(ctx.body, "message", "") or "", view)
        l0_tool = (requirements.requested_action or {}).get("tool")
        logger.info(
            "matcher_shadow version=%d state=%s cap=%s candidates=%s l0_tool=%s",
            view.version, res.state, res.capability_id or "-",
            ",".join(res.candidates) or "-", l0_tool or "-",
        )
    except Exception as exc:  # noqa: BLE001 - shadow is observation, never behavior
        logger.info("matcher shadow fail-quiet: %r", exc)


async def run_intent_stage(ctx, deps, requirements: TurnRequirements) -> TurnRequirements:
    """QIR cascade + Argument Binding. Outcomes are classified, not swallowed:

    * QIR abstain / C1 binding miss -> the ORIGINAL requirements (Agent keeps
      the turn, zero pollution);
    * C2 binding-integrity fault -> ACTION requirements carrying the
      ``binding_integrity`` marker (executor TERMINAL, never Agent recovery);
    * any other unexpected stage exception fail-opens to the original
      requirements — a routing-layer crash must not sink the turn."""
    from core.application.chat import qir
    from core.application.chat.qir import store as qir_store

    message = ctx.body.message or ""
    try:
        snapshot = await qir_store.active(deps.session_factory)
        route_result = await qir.route(
            message, snapshot=snapshot,
            embedder=deps.embedder(), llm=deps.llm,
            top_k=settings.chat_qir_top_k, min_score=settings.chat_qir_min_score,
            margin=settings.chat_qir_margin,
            decision_enabled=settings.chat_qir_decision_enabled,
            timeout_seconds=settings.chat_qir_timeout_seconds,
        )
        if route_result is None:
            return requirements
        # P0 adapter wiring: the legacy RouteResult speaks the node contract now;
        # downstream field-for-field identical (stage stamps stay "qir_*" keys).
        verdict = IntentVerdict.from_qir(route_result)
        cap = snapshot.get(verdict.capability_id) if snapshot is not None else None
        if cap is None or not cap.enabled or cap.id != verdict.capability_id:
            return requirements  # metadata drift between route and bind
        from core.application.chat.actions import (
            ActionIntegrityFailure,
            bind_arguments,
        )
        try:
            bound = BoundArguments.of(bind_arguments(cap.tool_binding, message, ctx))
        except ActionIntegrityFailure as exc:
            # C2 at the routing layer: the capability promises a binding the
            # existing action table does not honor — a system inconsistency,
            # NOT user-input incompleteness. The turn is planned as ACTION with
            # an integrity marker so the executor issues the decided TERMINAL
            # message; the Agent is never entered as a recovery channel.
            logger.error("qir.binding integrity: %s", exc.reason)
            return TurnRequirements(
                needs_action=Signal.HIGH,
                requested_action={
                    "tool": cap.tool_binding, "args": None,
                    "capability_id": cap.id,
                    "registry_version": verdict.registry_version,
                    "qir_stage": verdict.stage,
                    "binding_integrity": exc.reason,
                },
                complexity=Complexity.LOW, confidence=Confidence.HIGH,
                private_only=requirements.private_only,
                external_ok=requirements.external_ok,
            )
        if bound.is_missing:
            # C1: structured arguments not determinable from this sentence +
            # context. Fall through untouched — the Agent owns understanding.
            return requirements
        # Same construction shape as the L0 action-hit branch (only the
        # action fields are set; source facts ride through).
        return TurnRequirements(
            needs_action=Signal.HIGH,
            requested_action={
                "tool": cap.tool_binding, "args": bound.args,
                "capability_id": cap.id,
                "registry_version": verdict.registry_version,
                "qir_stage": verdict.stage,
            },
            complexity=Complexity.LOW, confidence=Confidence.HIGH,
            private_only=requirements.private_only,
            external_ok=requirements.external_ok,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open, contract-pinned by tests
        logger.info("qir stage fail-open: %r", exc)
        return requirements
