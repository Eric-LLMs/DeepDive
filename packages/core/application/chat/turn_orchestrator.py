"""TurnOrchestrator: the pure control plane between HTTP transport and executors.

Owns the per-turn state machine: plan resolution, executor dispatch, the approval
pump, the stream commit guard, the disconnect policy (T4 invariant #3: a research
turn is never cancelled by a client disconnect) and lifecycle finalization. It
never infers policy itself — it consumes an ``ExecutionPlan``.

SSE contract: events flow to the transport VERBATIM (same frames as the legacy
router emitted), so ``apps/web`` / ``apps/desktop`` need no change.

Commit Point (design §5 step 6): the first user-visible content delta. Until then
a branch may still fall back to AGENT; after it the channel is LOCKED — an error
can only terminate the stream with a standardized event, never re-route mid-flight.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator

from core.application.chat.context import ChatTurnContext
from core.application.chat.execution_plan import (
    ExecutionPlan,
    PolicyContext,
    build_execution_plan,
)
from core.application.chat.executors.action import ActionExecutor
from core.application.chat.executors.agent import AgentExecutor
from core.application.chat.executors.base import (
    ChatDeps,
    ChatExecutor,
    EscalateToAgent,
    TurnRequest,
)
from core.application.chat.executors.composite import CompositeExecutor
from core.application.chat.executors.direct import DirectExecutor
from core.application.chat.executors.retrieval import RetrievalExecutor
from core.application.chat.executors.viewer import ViewerExecutor
from core.application.chat.lifecycle import finalize_turn, handle_research_post_turn
from core.application.chat.understanding import (
    Complexity,
    Confidence,
    Signal,
    TurnRequirements,
    resolve_requirements,
)
from core.config import settings
from core.logger import reset_log_context, set_log_context

logger = logging.getLogger(__name__)

# Honest-disclosure instruction prepended to the Agent replay after a pre-commit
# fast-path escalation (same channel as the attach/handoff notes). The private corpus
# WAS searched and came up short — the Agent must say so instead of papering over the
# gap, and must never present general/web knowledge as private-KB evidence. Under a
# private source policy the web ban is additionally enforced in the sandbox, so the
# note tells the model what the guard already hard-denies.
_HONEST_NOTE = (
    "[Private retrieval note: the user's knowledge base WAS searched for this question "
    "and returned no sufficient evidence. Be honest with the user: say their knowledge "
    "base does not contain enough information about it. NEVER present general knowledge "
    "or web content as if it came from the user's private knowledge base — if you answer "
    "from anything else, label it explicitly as external/general.]"
)
_PRIVATE_ONLY_NOTE = (
    " This turn is restricted to private sources: do not call web_search or any other "
    "external/network tool."
)


def _escalation_note(plan: ExecutionPlan) -> str | None:
    """Honest-disclosure note for escalations from branches that ACTUALLY searched the
    private corpus (LOCAL_RAG / COMPOSITE). Any other branch (DIRECT / VIEWER / ACTION)
    escalates with the user's text BYTE-FOR-BYTE untouched — a fast path that abstained
    must leave zero trace in what the Agent receives. ``None`` = prepend nothing."""
    if not plan.requires_retrieval:
        return None
    if plan.source_policy in ("private_only", "private_first"):
        return _HONEST_NOTE + _PRIVATE_ONLY_NOTE
    return _HONEST_NOTE


class TurnOrchestrator:
    def __init__(self, deps: ChatDeps) -> None:
        self.deps = deps
        # Each phase registers its branch here; the PolicyContext gates below decide
        # which kind a turn resolves to, so registration is inert until enabled.
        self._executors = {
            AgentExecutor.kind: AgentExecutor(),
            DirectExecutor.kind: DirectExecutor(),
            ViewerExecutor.kind: ViewerExecutor(),
            RetrievalExecutor.kind: RetrievalExecutor(),
            ActionExecutor.kind: ActionExecutor(),
            CompositeExecutor.kind: CompositeExecutor(),
        }

    # ── plan resolution ──────────────────────────────────────────────────────────
    async def resolve_plan(self, ctx: ChatTurnContext, deps: ChatDeps | None = None) -> ExecutionPlan:
        """The full Execution-Plan-Resolution funnel, in three ordered stages:

          (1) QIR  — Intent Routing: the existing pure L0 pass first; when L0
                     abstained from an action certification and the QIR gates are
                     live, the semantic+decision cascade gets a chance to name ONE
                     capability. QIR itself never plans, binds arguments or
                     executes — it hands back a ``RouteResult`` only.
          (2) Argument Binding — capability + raw query -> structured arguments,
                     via the EXISTING action-binding table (``bind_arguments``).
                     Parameters undeterminable => no route (C1: the Agent, which
                     owns sentence-level argument understanding, keeps the turn);
                     a binding-table inconsistency is C2 and is marked for the
                     executor's TERMINAL path, never routed to the Agent.
          (3) ``build_execution_plan`` — the SOLE policy mapper, unchanged: it
                     owns feature gates, eligibility and ``source_policy``.

        Failure classification (NOT a catch-all): every QIR-own failure shape
        (no snapshot, store down, timeout, low/ambiguous similarity, decision
        NONE, QIR exception) and the C1 binding miss ABSTAIN -> the turn maps
        to AGENT exactly as before QIR existed, user text byte-identical. A C2
        binding-integrity fault instead plans ACTION with an integrity marker;
        the executor issues the decided terminal message without touching the
        seam. Dark launch: gates closed => stages (1b)(2) never run.
        """
        policy = PolicyContext(
            fast_paths_enabled=settings.chat_fast_paths_enabled,
            direct_fast_path_enabled=settings.chat_direct_fast_path_enabled,
            viewer_fast_path_enabled=settings.chat_viewer_fast_path_enabled,
            retrieval_fast_path_enabled=settings.chat_retrieval_fast_path_enabled,
            action_enabled=settings.chat_action_fast_path_enabled,
            composite_enabled=settings.chat_composite_fast_path_enabled,
        )
        requirements = TurnRequirements()
        if policy.fast_paths_enabled:
            requirements = resolve_requirements(ctx, ctx.body.message)
            if self._qir_live(policy, requirements, deps, ctx):
                requirements = await self._qir_intent_stage(ctx, deps, requirements)
        plan = build_execution_plan(requirements, policy)
        # Sink the SOURCE POLICY for the Agent's sandbox (see Sandbox._turn_denied):
        # fencing comes from the ORIGINAL request, and only when the control plane is
        # live — with the master gate closed the neutral requirements carry no policy,
        # so dark launch stays byte-identical to the legacy agent.
        if plan.source_policy:
            ctx.agent_context = {**(ctx.agent_context or {}), "source_policy": plan.source_policy}
        return plan

    # ── (1) QIR eligibility + (2) argument binding, funnel-internal ──────────────
    @staticmethod
    def _qir_live(policy: PolicyContext, requirements: TurnRequirements,
                  deps: ChatDeps | None, ctx: ChatTurnContext) -> bool:
        """Gate: QIR only ever ADDS an action route the L0 abstained from — it
        never overrides an L0 certification, never touches web/memory-demanding
        or research/handoff turns, and stays physically dark unless every
        relevant switch is on."""
        if not (settings.chat_qir_enabled and policy.fast_paths_enabled
                and policy.action_enabled and deps is not None):
            return False
        if requirements.requested_action is not None:  # L0 already certified
            return False
        if requirements.needs_web is not Signal.LOW or requirements.needs_memory:
            return False
        if getattr(ctx, "research_turn", False) or getattr(ctx, "effective_handoff", None):
            return False
        from core.application.chat.sanitization import is_pure_user_text
        return is_pure_user_text(ctx.body.message or "")

    async def _qir_intent_stage(self, ctx: ChatTurnContext, deps: ChatDeps,
                                requirements: TurnRequirements) -> TurnRequirements:
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
            route = await qir.route(
                message, snapshot=snapshot,
                embedder=deps.embedder(), llm=deps.llm,
                top_k=settings.chat_qir_top_k, min_score=settings.chat_qir_min_score,
                margin=settings.chat_qir_margin,
                decision_enabled=settings.chat_qir_decision_enabled,
                timeout_seconds=settings.chat_qir_timeout_seconds,
            )
            if route is None:
                return requirements
            cap = snapshot.get(route.capability_id) if snapshot is not None else None
            if cap is None or not cap.enabled or cap.id != route.capability_id:
                return requirements  # metadata drift between route and bind
            from core.application.chat.actions import (
                ActionIntegrityFailure, bind_arguments,
            )
            try:
                args = bind_arguments(cap.tool_binding, message, ctx)
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
                        "registry_version": route.registry_version,
                        "qir_stage": route.stage,
                        "binding_integrity": exc.reason,
                    },
                    complexity=Complexity.LOW, confidence=Confidence.HIGH,
                    private_only=requirements.private_only,
                    external_ok=requirements.external_ok,
                )
            if args is None:
                # C1: structured arguments not determinable from this sentence +
                # context. Fall through untouched — the Agent owns understanding.
                return requirements
            # Same construction shape as the L0 action-hit branch (only the
            # action fields are set; source facts ride through).
            return TurnRequirements(
                needs_action=Signal.HIGH,
                requested_action={
                    "tool": cap.tool_binding, "args": args,
                    "capability_id": cap.id,
                    "registry_version": route.registry_version,
                    "qir_stage": route.stage,
                },
                complexity=Complexity.LOW, confidence=Confidence.HIGH,
                private_only=requirements.private_only,
                external_ok=requirements.external_ok,
            )
        except Exception as exc:  # fail-open, contract-pinned by tests
            logger.info("qir stage fail-open: %r", exc)
            return requirements

    def executor_for(self, plan: ExecutionPlan) -> ChatExecutor:
        executor = self._executors.get(plan.kind)
        if executor is None:  # an unmapped kind must never hard-fail the turn
            logger.warning("no executor for plan kind=%s; falling back to AGENT", plan.kind)
            executor = self._executors[AgentExecutor.kind]
        return executor

    # ── non-streaming turn (/chat) ───────────────────────────────────────────────
    async def run_turn(self, ctx: ChatTurnContext) -> dict:
        plan = await self.resolve_plan(ctx, self.deps)
        executor = self.executor_for(plan)
        req = TurnRequest(ctx=ctx, deps=self.deps, plan=plan)
        research_continuing = False
        logger.info(
            "chat.plan-resolved turn_kind=%s reason=%s", plan.kind.value, plan.reason
        )
        try:
            try:
                result = await executor.run(req)
            except EscalateToAgent as esc:
                # Pre-commit fast-path fallback (Phase 4 Fail-Closed): the staged
                # retrieval refused to answer without sufficient private evidence. The
                # Agent — with its tools — owns the turn now, governed by the turn's
                # SOURCE POLICY from the original request (web stays banned under a
                # private policy; the replay is told to disclose the corpus gap honestly).
                logger.info(
                    "chat.plan-escalated from=%s to=agent reason=%s",
                    plan.kind.value, esc.reason,
                )
                note = _escalation_note(plan)
                if note:
                    ctx.user_text = note + "\n\n" + ctx.user_text
                executor = self._executors[AgentExecutor.kind]
                result = await executor.run(req)
        finally:
            # /chat owns the run for the lifetime of this request. Hand the finished
            # interactive turn to the worker chain unless it hit a stop condition; the
            # single-task run slot is released only when the run is NOT handed off.
            research_continuing = await handle_research_post_turn(ctx, self.deps)
        payload = {
            "answer": result.final_answer,
            "messages": result.messages,
            "usage": result.usage,
        }
        try:
            resp = await finalize_turn(ctx, self.deps, payload, tool="chat")
        finally:
            if ctx.log_tokens is not None:
                reset_log_context(ctx.log_tokens)
        resp["messages"] = result.messages
        if research_continuing:
            resp["research_continuing"] = True
        return resp

    # ── streaming turn (/chat/stream) — mirrors the legacy gen() pump exactly ────
    async def stream_turn(
        self, ctx: ChatTurnContext, *, t_entry: float
    ) -> AsyncIterator[dict]:
        """Yield SSE frame dicts (``{"data": json}``) for one streaming turn."""
        deps = self.deps
        plan = await self.resolve_plan(ctx, deps)
        executor = self.executor_for(plan)
        req = TurnRequest(ctx=ctx, deps=deps, plan=plan)
        # Tag every log line the stream emits with the user + session. Set inside the
        # generator (not the route) because the SSE generator runs after the handler
        # returns — the sibling pump task inherits it.
        log_tokens = set_log_context(user_id=str(ctx.user_id), session_id=str(ctx.session_id))
        # The agent may block on a human-in-the-loop approval (awaiting POST
        # /approvals/{id}), so a plain `async for` over run_stream would deadlock — the
        # stream can't advance while the approval-request frame sits unyielded. Pump the
        # stream into a queue in a sibling task, and let the ApprovalStore's sink push
        # approval frames into the same queue; this generator only ever reads the queue.
        frames: asyncio.Queue = asyncio.Queue()
        from agent.security.approvals import (  # lazy: keeps import light for tests
            ApprovalStore,
            set_request_approval,
        )

        store = ApprovalStore(
            deps.new_approval_bridge().broker,
            user_id=str(ctx.user_id),
            sink=lambda evt: frames.put_nowait(("approval", evt)),
        )
        set_request_approval(store)

        async def pump(runner: ChatExecutor):
            # NOTE: only ever await INSIDE the executor stream (never on frames). A
            # cancelled pump awaiting frames.put would swallow CancelledError in its
            # finally and strand the run_stream generator's cleanup; the queue is
            # unbounded so put_nowait never blocks, and a client disconnect lands the
            # CancelledError in the loop below (turn-cancelled logging + memory close).
            try:
                async for evt in runner.stream(
                    req, progress_sink=lambda evt: frames.put_nowait(("agent", evt))
                ):
                    frames.put_nowait(("agent", evt))
            except EscalateToAgent as esc:
                # Pre-commit only: an executor raises this before emitting any event,
                # so nothing was committed and the swap below is legal (design §5).
                logger.info(
                    "chat.stream-escalated from=%s to=agent reason=%s",
                    plan.kind.value, esc.reason,
                )
                frames.put_nowait(("escalate", esc.reason))
            finally:
                # Sentinel so the consumer below always terminates after the stream ends,
                # including on cancellation (the loop already logs turn-cancelled).
                frames.put_nowait(("agent", {"type": "done", "data": None}))

        pump_task = asyncio.create_task(pump(executor))
        final = None
        research_continuing = False
        # Stream Commit Point: set on the first user-visible content delta. Until then
        # a pre-execution block may still fall back; after it the channel is locked
        # (errors terminate the stream; transparent executor switching is forbidden).
        committed = False
        # Escalation bookkeeping: at most ONE pre-commit swap (fast path -> AGENT). The
        # abandoned pump still enqueues its done-None sentinel; the loop below must skip
        # it (stale_sentinels) or the turn would "finish" with an empty payload.
        stale_sentinels = 0
        switched = False

        def collect_done_payload() -> dict | None:
            """Disconnect path: pull the done payload the drained pump left in the queue
            (the run_stream event, not the None sentinel) when the loop never saw it."""
            try:
                while True:
                    kind, data = frames.get_nowait()
                    if (
                        kind == "agent"
                        and isinstance(data, dict)
                        and data.get("type") == "done"
                        and data.get("data") is not None
                    ):
                        return data["data"]
            except asyncio.QueueEmpty:
                return None

        # TTFT anchors: first SSE frame vs first visible content delta, both from t_entry.
        first_event_ms = first_content_ms = None
        try:
            while True:
                kind, data = await frames.get()
                if kind == "approval":
                    # Approval-request frame: forward verbatim (client POSTs /approvals/{id}).
                    yield {"data": json.dumps(data, ensure_ascii=False, default=str)}
                    continue
                if kind == "escalate":
                    # Pre-commit fast-path → AGENT swap (Phase 4). Re-pump the Agent into
                    # the SAME queue; the abandoned pump's sentinel is skipped below.
                    if committed or switched:
                        # Channel locked / already switched: never re-route mid-flight —
                        # surface a terminal error instead (design §5 Commit Point).
                        error_evt = {
                            "type": "error",
                            "data": {"message": f"escalation after commit: {data}"},
                        }
                        yield {"data": json.dumps(error_evt, ensure_ascii=False)}
                        frames.put_nowait(("agent", {"type": "done", "data": None}))
                        continue
                    switched = True
                    stale_sentinels += 1  # the escalated pump still enqueues its sentinel
                    # Pre-first-event only: prepend the honest-disclosure instruction to
                    # the Agent replay ONLY when the abandoned branch had actually
                    # searched the corpus; every other escalation is zero-pollution.
                    note = _escalation_note(plan)
                    if note:
                        ctx.user_text = note + "\n\n" + ctx.user_text
                    pump_task = asyncio.create_task(pump(self._executors[AgentExecutor.kind]))
                    continue
                if data["type"] == "done":
                    if stale_sentinels:
                        stale_sentinels -= 1
                        continue
                    final = data["data"]
                    break
                if first_event_ms is None:
                    first_event_ms = (time.perf_counter() - t_entry) * 1000
                if first_content_ms is None and data.get("type") == "content":
                    first_content_ms = (time.perf_counter() - t_entry) * 1000
                    committed = True  # past the Commit Point — the channel is now locked
                    logger.info(
                        "chat.stream-first-token first_event_ms=%.0f first_content_ms=%.0f",
                        first_event_ms, first_content_ms,
                    )
                yield {"data": json.dumps(data, ensure_ascii=False)}
        finally:
            set_request_approval(None)
            if ctx.research_turn:
                # Server-owned research run (T4 invariant #3): a client disconnect must
                # never cancel an active run. Let the pump drain to completion so the
                # agent's turn and its tool executions finish server-side, then still
                # finalize the turn and release the single-task run slot.
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
                if final is None:
                    # The loop was abandoned (client disconnect) before the done frame;
                    # run the same post-turn bookkeeping even though no client is left.
                    with contextlib.suppress(Exception):
                        await finalize_turn(ctx, deps, collect_done_payload(), tool="chat_stream")
                research_continuing = await handle_research_post_turn(ctx, deps)
            else:
                # Non-research turn: the run is owned by the SSE pipe. Client disconnect
                # stops it so the turn's awaits re-raise CancelledError and unwind cleanly.
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
            # Release the request-scoped log context: this generator may be closed at any
            # yield point, so the tags must not leak into the next unit of work.
            reset_log_context(log_tokens)

        # Normal completion path: the loop broke on the run's done frame.
        t_fin = time.perf_counter()
        done = await finalize_turn(ctx, deps, final, tool="chat_stream")
        logger.info(
            "chat.stream-end total_ms=%.0f finalize_ms=%.0f committed=%s",
            (time.perf_counter() - t_entry) * 1000, (time.perf_counter() - t_fin) * 1000,
            committed,
        )
        if research_continuing:
            done["research_continuing"] = True
        yield {"data": json.dumps({"type": "done", "data": done}, ensure_ascii=False)}
