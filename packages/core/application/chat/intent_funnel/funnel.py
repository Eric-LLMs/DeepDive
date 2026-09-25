"""Intent Funnel orchestration.

P0 moved the legacy QIR cascade (:func:`run_intent_stage`) out of the
orchestrator unchanged; P1 added the Registry Matcher's shadow hook. The
2026-09-24 chain correction pins the ACTIVE target chain to one hop:

    Matcher HIT  ─┐
                  ├→ ToolIntentModel (ONE call: select + extract) → Binder (validate) → Execute
    MISS/AMB → Recall ─┘

every non-COMPLETE outcome exits to the Agent (8.10). The recheck second hop
and the Decision LLM are deleted — the two online TTFBs they cost were the
cascade timeout, and neither added information. The chain is gated by its own
switch (``chat_funnel_enabled``, default OFF): with it closed, route() is
byte-identical to the accepted P1 behavior and the legacy QIR path stays as it
was — dark historical implementation, not the correctness baseline.
"""
from __future__ import annotations

import asyncio
import logging
import types

from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
)
from core.config import settings

from . import shadow
from .contract import (
    MATCH_AMBIGUOUS,
    MATCH_HIT,
    MATCH_MISS,
    REASON_BIND_AMBIGUOUS,
    REASON_BIND_INVALID,
    REASON_BIND_MISSING,
    REASON_CASCADE_ERROR,
    REASON_CASCADE_TIMEOUT,
    REASON_KIND_DISABLED,
    REASON_RECALL_TIMEOUT,
    REASON_RECALL_UNAVAILABLE,
    REASON_REGISTRY_UNAVAILABLE,
    REASON_TOOL_INTENT_REJECT,
    REASON_TOOL_INTENT_TIMEOUT,
    REASON_TOOL_INTENT_UNCERTAIN,
    REASON_VERSION_MISMATCH,
    TOOL_INTENT_CONFIDENT,
    TOOL_INTENT_REJECT,
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
    With the funnel gate open, hands off to the single-hop cascade
    (:func:`_cascade`); with it closed, falls back to the legacy QIR path
    (dark by its own gates).
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
    except Exception as exc:
        logger.info("qir stage fail-open: %r", exc)
        return requirements


# ── Active chain: Matcher → (Recall) → ToolIntentModel → Binder(validate) → (Agent) ──────

def _stage_reason(stage: str, *, timed_out: bool) -> str:
    """8.10 reason code for the exit point the cascade died at."""
    if timed_out:
        return {
            "recall": REASON_RECALL_TIMEOUT,
            "tool_intent": REASON_TOOL_INTENT_TIMEOUT,  # the ONE model hop owns the budget
        }.get(stage, REASON_CASCADE_TIMEOUT)
    return {
        "registry": REASON_REGISTRY_UNAVAILABLE,
        "recall": REASON_RECALL_UNAVAILABLE,
    }.get(stage, REASON_CASCADE_ERROR)


def _new_trace() -> dict:
    """The shared per-run trace record: production routing and the 8.5 query
    preview fill the SAME fields — one observability shape (8.12), one event
    row shape, so a preview can be diffed against real traffic line for line.
    The retired Decision node's ``decision`` field was dropped outright
    (migration 0008) — trace lines and event rows carry no historical name."""
    return {"stage": "registry", "matcher": "-", "recall_count": 0,
            "recall_top": "-", "tool_intent": "-",
            "final_route": "agent", "fallback": "-", "registry": "-",
            "index": "-", "capability": None, "total_ms": 0}


async def _run_cascade(ctx, deps, requirements: TurnRequirements,
                       trace: dict, *,
                       recall_min_score: float | None = None,
                       recall_top_k: int | None = None,
                       model_candidate_floor: float | None = None,
                       capture: dict | None = None) -> TurnRequirements | None:
    """One wall-clock-budgeted cascade run with 8.10-classified fail-open.
    Returns a certified TurnRequirements or None (fallback reason in
    ``trace``); the caller decides what None means (production: the original
    object; preview: an agent-route verdict). Never raises.

    Phase-E shadow seam (all default None = byte-identical production): the
    evaluation entry overrides the Recall quality-gate parameters and passes a
    ``capture`` dict to record per-node telemetry the production trace does
    not carry (raw scores, model confidence, binder state). It never gains
    execution authority — the cascade only produces routing metadata (8.8)."""
    import time

    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(
            _run_nodes(ctx, deps, requirements, trace,
                       recall_min_score=recall_min_score, recall_top_k=recall_top_k,
                       model_candidate_floor=model_candidate_floor, capture=capture),
            settings.chat_funnel_timeout_seconds,
        )
    except asyncio.TimeoutError:
        out, trace["fallback"] = None, _stage_reason(trace["stage"], timed_out=True)
    except Exception as exc:
        out = None
        trace["fallback"] = _stage_reason(trace["stage"], timed_out=False)
        logger.info("funnel fail-open at %s: %r", trace["stage"], exc)
    if out is not None:
        trace["final_route"] = "action"
        trace["capability"] = (out.requested_action or {}).get("capability_id")
    elif trace["fallback"] == "-":
        trace["fallback"] = REASON_CASCADE_ERROR  # belt: abstain without a reason is a fault
    trace["total_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def _log_trace(trace: dict) -> None:
    logger.info(
        "funnel_trace deepest_stage=%s matcher=%s recall_count=%d recall_top=%s "
        "tool_intent=%s final_route=%s fallback_reason=%s "
        "registry_version=%s index_version=%s total_ms=%d",
        trace["stage"], trace["matcher"], trace["recall_count"], trace["recall_top"],
        trace["tool_intent"], trace["final_route"], trace["fallback"],
        trace["registry"], trace["index"], trace["total_ms"],
    )


async def _persist_event(deps, ctx, trace: dict) -> None:
    """8.12: one row per route decision, best-effort. Telemetry must never sink
    a turn or delay a preview, and an unwired session factory (unit tests,
    dark lanes) is a silent no-op. The raw query is deliberately NOT stored;
    ``execution_mode`` (8.14) separates production from shadow/preview/test."""
    factory = getattr(deps, "session_factory", None)
    if factory is None:
        return
    try:
        from core.infrastructure.db import ChatFunnelEventModel
        from core.infrastructure.request_context import (
            get_request_execution_mode,
            get_request_user_id,
        )

        async with factory() as session:
            session.add(ChatFunnelEventModel(
                execution_mode=get_request_execution_mode(),
                user_id=get_request_user_id(),
                session_id=str(getattr(ctx, "session_id", "") or "") or None,
                deepest_stage=trace["stage"], matcher=trace["matcher"],
                recall_count=trace["recall_count"], recall_top=trace["recall_top"],
                tool_intent=trace["tool_intent"],
                final_route=trace["final_route"], fallback_reason=trace["fallback"],
                registry_version=trace["registry"], index_version=trace["index"],
                capability_id=trace["capability"], total_ms=trace["total_ms"],
            ))
            await session.commit()
    except Exception as exc:
        logger.info("funnel event persist skipped: %r", exc)


async def _cascade(ctx, deps, requirements: TurnRequirements) -> TurnRequirements:
    """Run the single-hop cascade under one wall-clock budget, fail-open to the
    ORIGINAL requirements on every abstain/fault (8.10: the Agent's input stays
    byte-identical). Returns the same object the pre-P2 contract guarantees;
    only a certified turn produces a NEW requirements (never a mutation)."""
    trace = _new_trace()
    out = await _run_cascade(ctx, deps, requirements, trace)
    _log_trace(trace)
    await _persist_event(deps, ctx, trace)
    return out if out is not None else requirements


# ── §8.5 full-chain query preview (dry-run, side-effect-free) ─────────────────────

def _preview_ctx(message: str):
    """The minimal ctx a console can express as a one-off test query: pure
    text, no viewer/attachment/research/handoff. (Drafts with context facts
    ride the same contract later; the Matcher only consumes TurnFacts fields.)"""
    return types.SimpleNamespace(
        body=types.SimpleNamespace(message=message, attach=None, viewer=None),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id="",
    )


async def preview(message: str, *, deps) -> dict:
    """§8.5: run the ACTIVE (Registry, Index) pair end to end for one query —
    Registry → Matcher → (Recall) → ToolIntentModel → Binder → Final Route —
    and return the trace as a verdict, executing nothing. Side-effect-free by
    construction: the chain only produces routing metadata (8.8), run_tool is
    not even on this object graph, and conversation state is never touched.
    All embedding/LLM usage is pinned ``execution_mode=preview`` (8.14) and
    the routing event lands with the same mode (8.12). Never raises: faults
    surface as the 8.10 fallback_reason, exactly as they would in production."""
    from core.infrastructure.request_context import (
        reset_request_execution_mode,
        set_request_execution_mode,
    )

    requirements = TurnRequirements(
        complexity=Complexity.LOW, confidence=Confidence.LOW,
        needs_web=Signal.LOW, needs_memory=False,
    )
    ctx = _preview_ctx(message)
    trace = _new_trace()
    token = set_request_execution_mode("preview")
    capture: dict = {}
    try:
        out = await _run_cascade(ctx, deps, requirements, trace, capture=capture)
        await _persist_event(deps, ctx, trace)   # inside the pin: the event says "preview"
    finally:
        reset_request_execution_mode(token)
    _log_trace(trace)
    result = {
        "deepest_stage": trace["stage"], "matcher": trace["matcher"],
        "recall_count": trace["recall_count"], "recall_top": trace["recall_top"],
        "tool_intent": trace["tool_intent"],
        "final_route": trace["final_route"], "fallback_reason": trace["fallback"],
        "registry_version": trace["registry"], "index_version": trace["index"],
        "total_ms": trace["total_ms"], "execution_mode": "preview",
        # Console dry-run detail (Phase 5): what the model actually saw — each
        # candidate's origin, matched corpus sentence and kind — plus the raw
        # verdict and Binder state. Read-only projection of `capture`.
        "candidates": capture.get("candidates", []),
        "recall_raw": capture.get("recall_raw", []),
        "model_verdict": capture.get("tool_intent"),
        "binder_state": capture.get("binder"),
    }
    if out is not None:
        act = out.requested_action or {}
        result["route"] = {k: act.get(k) for k in (
            "capability_id", "tool", "args", "funnel_stage", "funnel_kind",
            "binding_integrity",
        )}
    return result


async def cascade_shadow(ctx, *, deps, requirements=None,
                         recall_min_score: float = 0.0,
                         recall_top_k: int = 10,
                         model_candidate_floor: float = 0.58) -> dict:
    """Phase-E Cascade Shadow: one dry-run turn through the SAME node body as
    production (:func:`_run_nodes` — zero orchestration duplication, so the
    shadow can never drift from shipped semantics), with the raw-score seam
    opened: Recall keeps every candidate it scored (min_score=0, top_k=10) and
    the model-facing set re-applies a floor, so ALL threshold buckets are
    recomputable OFFLINE from the captured raw scores — recall is never
    re-run per threshold. The chain stops at Binder: routing metadata only
    (8.8), no dispatch, no Runtime, no event row; usage is pinned
    ``execution_mode=shadow`` (8.14) and observability is log-only.
    ``would_execute`` is the certified turn's metadata, NOT a permission —
    by construction the cascade cannot execute anything from here.
    Never raises: faults surface as 8.10 fallback reasons, exactly as in
    production."""
    from core.infrastructure.request_context import (
        reset_request_execution_mode,
        set_request_execution_mode,
    )

    if requirements is None:
        requirements = TurnRequirements(
            complexity=Complexity.LOW, confidence=Confidence.LOW,
            needs_web=Signal.LOW, needs_memory=False,
        )
    trace = _new_trace()
    capture: dict = {}
    token = set_request_execution_mode("shadow")
    try:
        out = await _run_cascade(ctx, deps, requirements, trace,
                                 recall_min_score=recall_min_score,
                                 recall_top_k=recall_top_k,
                                 model_candidate_floor=model_candidate_floor,
                                 capture=capture)
    finally:
        reset_request_execution_mode(token)
    _log_trace(trace)
    result = {
        "deepest_stage": trace["stage"], "matcher": trace["matcher"],
        "recall_count": trace["recall_count"], "recall_top": trace["recall_top"],
        "tool_intent": trace["tool_intent"],
        "final_route": trace["final_route"], "fallback_reason": trace["fallback"],
        "registry_version": trace["registry"], "index_version": trace["index"],
        "total_ms": trace["total_ms"], "execution_mode": "shadow",
        "capture": capture,
    }
    act = (out.requested_action or {}) if out is not None else {}
    result["would_execute"] = ({k: act.get(k) for k in (
        "capability_id", "args", "funnel_stage", "funnel_kind",
    )} if out is not None else None)
    return result


async def _run_nodes(ctx, deps, requirements, trace, *,
                     recall_min_score: float | None = None,
                     recall_top_k: int | None = None,
                     model_candidate_floor: float | None = None,
                     capture: dict | None = None):
    """Run the single-hop cascade body (see _run_cascade for the shadow seam).
    Returns a certified TurnRequirements, or None after
    setting trace['fallback'] — the caller converts None into the original
    object. Raises only for faults, which the caller maps by trace['stage']."""
    from . import binder, guardrails, matcher, recall
    from .registry import active_view as registry_active_view
    from .registry.entry import STATUS_ACTIVE
    from .tool_intent import select_and_extract as tool_intent

    message = ctx.body.message or ""
    facts = TurnFacts.of(ctx)

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
    # The index is loaded even for the HIT lane: the certified turn stamps its
    # version for the executor's TOCTOU re-validation, and a Registry without
    # its Build-Then-Swap pair is unusable regardless of lane (§8.3).
    trace["stage"] = "recall"
    index = await recall.load_index(deps.session_factory)
    if index is None:
        trace["fallback"] = REASON_RECALL_UNAVAILABLE  # registry without its paired index
        return None
    trace["index"] = index.version

    # ── Node 1: Matcher (table-only; the negation guard applies BEFORE it ───────
    # can certify anything, ruling 8.1-a) ────────────────────────────────────────
    mres = matcher.match(message, facts, view)
    if mres.state != MATCH_MISS and guardrails.negated(message):
        mres = matcher.MatchResult(state=MATCH_MISS, registry_version=mres.registry_version)
    trace["matcher"] = f"{mres.state}:{mres.capability_id or mres.candidates or '-'}"
    if capture is not None:
        capture["matcher"] = {
            "state": mres.state, "capability_id": mres.capability_id,
            "candidates": list(mres.candidates or []),
            "matched_literal": mres.matched_literal,
        }

    # ── One candidate set, ONE convergence point: a HIT enters ToolIntentModel with the ─
    # same semantics as a Recall lane — the direct-certification special path is
    # deleted (chain ruling 2026-09-24). Recall runs only when the table missed.
    candidates: dict[str, Candidate] = {}
    if mres.state == MATCH_AMBIGUOUS:
        for cid in mres.candidates:
            candidates[cid] = Candidate(cid, 0.0, origin="matcher_ambiguous")
    elif mres.state == MATCH_HIT:
        candidates[mres.capability_id] = Candidate(
            mres.capability_id, 1.0, matched_example=mres.matched_literal,
            origin="matcher_hit",
        )
    if mres.state != MATCH_HIT:
        rres = await recall.recall(
            index, message, embedder=deps.embedder(),
            top_k=settings.chat_funnel_top_k if recall_top_k is None else recall_top_k,
            min_score=(settings.chat_funnel_min_score if recall_min_score is None
                       else recall_min_score),
        )
        if capture is not None:
            # the RAW lane: every candidate recall scored, pre any floor — the
            # offline threshold sweep recomputes buckets from THIS list (Phase E).
            capture["recall_raw"] = [
                {"capability_id": c.capability_id, "score": c.score,
                 "origin": c.origin, "matched_example": c.matched_example}
                for c in rres.candidates
            ]
        for cand in rres.candidates:  # recall scores win provenance: calibrated
            candidates[cand.capability_id] = cand
    trace["recall_count"] = len(candidates)
    if candidates:
        top = max(candidates.values(), key=lambda c: c.score)
        trace["recall_top"] = f"{top.capability_id}@{top.score:.3f}"
    cands = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
    if capture is not None:
        # corpus kind for the console dry-run (Phase 5): position 0 of a cap's
        # intent_corpus is the canonical sentence, the rest are synonyms.
        ex_kind = {
            (c.id, e): ("canonical" if i == 0 else "synonym")
            for c in index.capabilities for i, e in enumerate(c.examples)
        }
        capture["candidates"] = [
            {"capability_id": c.capability_id, "score": c.score, "origin": c.origin,
             "matched_example": c.matched_example,
             "kind": ex_kind.get((c.capability_id, c.matched_example), "-")}
            for c in cands
        ]
    if model_candidate_floor is not None:
        # Phase-E shadow seam: production's quality gate lives INSIDE recall's
        # (min_score, top_k); the raw lane moved them here so the model still
        # sees exactly what production would see at a given threshold —
        # floor-screened recall cards capped at top_k, plus every
        # matcher-origin card (HIT 1.0 / AMBIGUOUS 0.0: table evidence, not
        # calibrated cosine scores, exempt from both floor and cap).
        recall_c = [c for c in cands if c.origin == "recall"
                    and c.score >= model_candidate_floor][:settings.chat_funnel_top_k]
        cands = sorted([c for c in cands if c.origin != "recall"] + recall_c,
                       key=lambda c: c.score, reverse=True)
    # Action Detection is UNCONDITIONAL (ruling 2026-09-25): an empty candidate
    # set no longer short-circuits to the Agent. The model faces the empty
    # table, can only answer NONE -> REJECT. REASON_NO_CANDIDATE is therefore
    # never produced on this lane any more.

    # ── Node 2: ToolIntentModel — the ONE model call of the turn (select + extract) ────
    trace["stage"] = "tool_intent"
    jv = await tool_intent(message, cands, entries_by_id=entries_by_id,
                       llm=deps.llm, facts=facts)
    trace["tool_intent"] = f"{jv.decision}:{jv.capability_id or '-'}"
    if capture is not None:
        capture["tool_intent"] = {
            "decision": jv.decision, "capability_id": jv.capability_id,
            "confidence": jv.confidence, "arguments": jv.arguments,
            "rationale": jv.rationale,
        }
    if jv.decision != TOOL_INTENT_CONFIDENT:
        trace["fallback"] = (
            REASON_TOOL_INTENT_REJECT if jv.decision == TOOL_INTENT_REJECT
            else REASON_TOOL_INTENT_UNCERTAIN
        )
        return None

    # ── Capability → Binder validate → certified ACTION metadata ───────────────
    entry = entries_by_id.get(jv.capability_id)
    if entry is None:  # a verdict the active table no longer honors: refuse
        trace["fallback"] = REASON_VERSION_MISMATCH
        return None
    if not kind_enabled(entry.intent_kind):  # P3: in the table, but not ON
        trace["fallback"] = REASON_KIND_DISABLED
        return None
    if capture is not None:
        capture["entry"] = {"intent_kind": entry.intent_kind,
                            "tool_binding": entry.tool_binding}
    trace["stage"] = "binder"
    bound = binder.validate(entry, jv.arguments)
    if capture is not None:
        capture["binder"] = bound.state
    if not bound.is_complete:
        trace["fallback"] = {
            "MISSING": REASON_BIND_MISSING,
            "AMBIGUOUS": REASON_BIND_AMBIGUOUS,
            "INVALID": REASON_BIND_INVALID,
        }.get(bound.state, REASON_BIND_MISSING)
        return None
    trace["stage"] = "certified"
    return _certified(requirements, entry, bound.args, index.version, view.fingerprint,
                      stage="tool_intent")


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
