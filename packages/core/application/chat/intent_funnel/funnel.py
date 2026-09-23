"""Intent Funnel orchestration.

P0 moved the legacy QIR cascade (:func:`run_intent_stage`) out of the
orchestrator unchanged; P1 added the Registry Matcher's shadow hook; P2 (this
file, ``_cascade``) adds the TARGET four-node chain — Matcher → Recall →
Judge → Decision → Binder — with escalate-only-upward and 8.10-classified
fail-open. The new chain is gated by its OWN switch (``chat_funnel_enabled``,
default OFF): with it closed, route() is byte-identical to the accepted P1
behavior and the legacy path stays as it was — it is dark historical
implementation, not the correctness baseline of the new architecture.
"""
from __future__ import annotations

import asyncio
import logging

from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)
from core.config import settings

from . import shadow
from .contract import (
    JUDGE_CONFIDENT,
    MATCH_AMBIGUOUS,
    MATCH_HIT,
    MATCH_MISS,
    REASON_BIND_AMBIGUOUS,
    REASON_BIND_INVALID,
    REASON_BIND_MISSING,
    REASON_CASCADE_ERROR,
    REASON_CASCADE_TIMEOUT,
    REASON_DECISION_NONE,
    REASON_DECISION_TIMEOUT,
    REASON_JUDGE_TIMEOUT,
    REASON_KIND_DISABLED,
    REASON_NO_CANDIDATE,
    REASON_RECALL_TIMEOUT,
    REASON_RECALL_UNAVAILABLE,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_VERSION_MISMATCH,
    BoundArguments,
    Candidate,
    IntentVerdict,
    TurnFacts,
)

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
    With the P2 gate open, hands off to the four-node cascade (:func:`_cascade`);
    with it closed, falls back to the legacy QIR path (dark by its own gates).
    """
    # P1 step-5 tri-state (8.15): unless the switch is off, run the Registry
    # Matcher in the dark and log its would_* verdict. Observation only.
    if deps is not None:
        mode = shadow.matcher_mode()
        if mode != "off":
            await shadow.observe(ctx, deps, requirements, mode)
    # Migration compat boundary (P1 ruling): a turn L0 already certified is
    # never touched by the new lane while L0 stays in charge. This is a scoping
    # fact of the coexistence period, NOT a statement that L0 is the baseline.
    if requirements.requested_action is not None:
        return requirements
    if funnel_live(requirements, deps, ctx):
        return await _cascade(ctx, deps, requirements)
    if not qir_live(requirements, deps, ctx):
        return requirements
    return await run_intent_stage(ctx, deps, requirements)


def funnel_live(requirements: TurnRequirements, deps, ctx) -> bool:
    """Gate for the P2 target cascade: master funnel switch + the plan-level
    action sub-gate + the common-layer guardrails (:mod:`guardrails`). From P3
    on, non-ACTION kinds additionally pass :func:`kind_enabled` per candidate;
    this gate stays the funnel-wide door."""
    if not (settings.chat_funnel_enabled and settings.chat_fast_paths_enabled
            and settings.chat_action_fast_path_enabled and deps is not None):
        return False
    from . import guardrails

    return guardrails.turn_veto(ctx.body.message or "", requirements, ctx) is None


def kind_enabled(kind: str) -> bool:
    """P3 per-kind rollout gate (逐开关灰度): ACTION rides the master funnel gate
    (the caller already passed it); each widened kind needs its own switch, and
    an unknown kind routes nothing. Being IN the table was never the same as
    being ON."""
    if kind in ("", "action"):
        return True
    if kind == "private":
        return settings.chat_funnel_private_enabled
    if kind == "web":
        return settings.chat_funnel_web_enabled
    return False


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


# ── P2 target cascade: Matcher → Recall → Judge → Decision → Binder → (Agent) ─────

def _stage_reason(stage: str, *, timed_out: bool) -> str:
    """8.10 reason code for the exit point the cascade died at."""
    if timed_out:
        return {
            "recall": REASON_RECALL_TIMEOUT,
            "judge": REASON_JUDGE_TIMEOUT,
            "decision": REASON_DECISION_TIMEOUT,
        }.get(stage, REASON_CASCADE_TIMEOUT)
    return {
        "registry": REASON_REGISTRY_UNAVAILABLE,
        "recall": REASON_RECALL_UNAVAILABLE,
    }.get(stage, REASON_CASCADE_ERROR)


async def _cascade(ctx, deps, requirements: TurnRequirements) -> TurnRequirements:
    """Run the four-node cascade under one wall-clock budget, fail-open to the
    ORIGINAL requirements on every abstain/fault (8.10: the Agent's input stays
    byte-identical). Returns the same object the pre-P2 contract guarantees;
    only a certified turn produces a NEW requirements (never a mutation)."""
    import time

    trace = {"stage": "registry", "matcher": "-", "recall_count": 0,
             "recall_top": "-", "judge": "-", "decision": "-",
             "final_route": "agent", "fallback": "-", "registry": "-", "index": "-"}
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(
            _run_nodes(ctx, deps, requirements, trace),
            settings.chat_funnel_timeout_seconds,
        )
    except asyncio.TimeoutError:
        out, trace["fallback"] = None, _stage_reason(trace["stage"], timed_out=True)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract, never sinks the turn
        out = None
        trace["fallback"] = _stage_reason(trace["stage"], timed_out=False)
        logger.info("funnel fail-open at %s: %r", trace["stage"], exc)
    if out is not None:
        trace["final_route"] = "action"
    elif trace["fallback"] == "-":
        trace["fallback"] = REASON_CASCADE_ERROR  # belt: abstain without a reason is a fault
    logger.info(
        "funnel_trace deepest_stage=%s matcher=%s recall_count=%d recall_top=%s "
        "judge=%s decision=%s final_route=%s fallback_reason=%s "
        "registry_version=%s index_version=%s total_ms=%d",
        trace["stage"], trace["matcher"], trace["recall_count"], trace["recall_top"],
        trace["judge"], trace["decision"], trace["final_route"], trace["fallback"],
        trace["registry"], trace["index"], int((time.monotonic() - t0) * 1000),
    )
    return out if out is not None else requirements


async def _run_nodes(ctx, deps, requirements, trace):
    """The cascade body. Returns a certified TurnRequirements, or None after
    setting trace['fallback'] — the caller converts None into the original
    object. Raises only for faults, which the caller maps by trace['stage']."""
    from core.application.chat.actions import ActionIntegrityFailure

    from . import binder, decision, guardrails, matcher, recall
    from .judge import adjudicate as judge_adjudicate
    from .judge import recheck as judge_recheck
    from .registry import active_view as registry_active_view
    from .registry.entry import STATUS_ACTIVE

    message = ctx.body.message or ""

    # ── Registry + Recall index (the Build-Then-Swap pair, §8.3) ──────────────
    view = await registry_active_view(session_factory=deps.session_factory)
    if view is None or not view.entries:
        trace["fallback"] = REASON_REGISTRY_UNAVAILABLE  # nothing published: no table to consult
        return None
    trace["registry"] = view.fingerprint
    entries_by_id = {
        e.capability_id: e for e in view.entries
        if e.enabled and e.status == STATUS_ACTIVE
    }
    trace["stage"] = "recall"
    index = await recall.load_index(deps.session_factory)
    if index is None:
        trace["fallback"] = REASON_RECALL_UNAVAILABLE  # registry without its paired index
        return None
    trace["index"] = index.version

    # ── Node 1: Matcher (table-only; the negation guard applies BEFORE it ───────
    # can certify anything, ruling 8.1-a) ────────────────────────────────────────
    mres = matcher.match(message, TurnFacts.of(ctx), view)
    if mres.state != MATCH_MISS and guardrails.negated(message):
        mres = matcher.MatchResult(state=MATCH_MISS, registry_version=mres.registry_version)
    trace["matcher"] = f"{mres.state}:{mres.capability_id or mres.candidates or '-'}"

    candidates: dict[str, Candidate] = {}
    cap_id: str | None = None
    stage = "matcher"
    if mres.state == MATCH_AMBIGUOUS:
        for cid in mres.candidates:
            candidates[cid] = Candidate(cid, 0.0, origin="matcher_ambiguous")
    elif mres.state == MATCH_HIT:
        if shadow.matcher_mode() == "on":
            cap_id = mres.capability_id  # deterministic certification, no spend below
        else:
            candidates[mres.capability_id] = Candidate(
                mres.capability_id, 0.0, origin="matcher_hit",
            )

    # ── Node 2: Recall — quality gate only, then Node 3 / Node 4 upward ────────
    if cap_id is None:
        rres = await recall.recall(
            index, message, embedder=deps.embedder(),
            top_k=settings.chat_funnel_top_k, min_score=settings.chat_funnel_min_score,
        )
        for cand in rres.candidates:  # recall scores win provenance: calibrated
            candidates[cand.capability_id] = cand
        trace["recall_count"] = len(candidates)
        if candidates:
            top = max(candidates.values(), key=lambda c: c.score)
            trace["recall_top"] = f"{top.capability_id}@{top.score:.3f}"
        cands = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
        if not cands:
            trace["fallback"] = REASON_NO_CANDIDATE
            return None
        trace["stage"] = "judge"
        jv = await judge_adjudicate(message, cands, entries_by_id=entries_by_id, llm=deps.llm)
        trace["judge"] = f"{jv.decision}:{jv.capability_id or '-'}"
        if jv.decision == JUDGE_CONFIDENT:
            cap_id, stage = jv.capability_id, "judge"
        else:  # UNCERTAIN / REJECT escalate upward — the ONLY exit is Decision
            trace["stage"] = "decision"
            dr = await decision.adjudicate(
                message, cands, entries_by_id=entries_by_id, llm=deps.llm,
            )
            trace["decision"] = dr.capability_id or "NONE"
            if dr.capability_id is None:
                trace["fallback"] = REASON_DECISION_NONE
                return None
            cap_id, stage = dr.capability_id, "decision"

    # ── Capability → Binder (four states, §8.7) → certified ACTION metadata ────
    entry = entries_by_id.get(cap_id)
    if entry is None:  # a verdict the active table no longer honors: refuse
        trace["fallback"] = REASON_VERSION_MISMATCH
        return None
    if not kind_enabled(entry.intent_kind):  # P3: in the table, but not ON
        trace["fallback"] = REASON_KIND_DISABLED
        return None
    trace["stage"] = "binder"
    try:
        bound = binder.bind(entry, message, ctx)
    except ActionIntegrityFailure as exc:
        # C2 at the routing layer (unchanged doctrine): the Registry promises a
        # binding the tool table does not honor — terminal marker, never Agent.
        logger.error("funnel.binding integrity: %s", exc.reason)
        trace["stage"] = "certified"
        return _certified(requirements, entry, None, index.version, view.fingerprint,
                          stage="binder", integrity=exc.reason)
    if not bound.is_complete:
        issue = bound.state.lower()
        rv = await judge_recheck(
            message, entry.capability_id, entry=entry, issue=issue,
            candidates=[candidates[entry.capability_id]]
            if entry.capability_id in candidates else [], llm=deps.llm,
        )
        trace["judge"] = f"recheck:{rv.decision}"
        # Whatever the recheck says, the Agent gets the turn + the reason: a
        # REJECT confirms abandonment; an unresolved case is clarified by the
        # Agent (8.7 — the question is the Agent's, last step by design).
        trace["fallback"] = {
            "MISSING": REASON_BIND_MISSING,
            "AMBIGUOUS": REASON_BIND_AMBIGUOUS,
            "INVALID": REASON_BIND_INVALID,
        }.get(bound.state, REASON_BIND_MISSING)
        return None
    trace["stage"] = "certified"
    return _certified(requirements, entry, bound.args, index.version, view.fingerprint,
                      stage=stage)


def _certified(requirements, entry, args, index_version, registry_fp, *,
               stage: str, integrity: str | None = None) -> TurnRequirements:
    """Certified turn: the same construction shape as the legacy branches
    (only action fields set; source facts ride through). ``registry_version``
    carries the INDEX version because the executor's TOCTOU re-validates against
    it (known dual-namespace residue, unified in P4)."""
    action = {
        "tool": entry.tool_binding, "args": args,
        "capability_id": entry.capability_id,
        "registry_version": index_version,
        "funnel_registry_version": registry_fp,
        "funnel_stage": stage,
        "funnel_kind": entry.intent_kind,
    }
    if integrity is not None:
        action["binding_integrity"] = integrity
    return TurnRequirements(
        needs_action=Signal.HIGH, requested_action=action,
        complexity=Complexity.LOW, confidence=Confidence.HIGH,
        private_only=requirements.private_only,
        external_ok=requirements.external_ok,
    )
