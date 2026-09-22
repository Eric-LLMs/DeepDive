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
from core.application.chat.executors.agent import AgentExecutor
from core.application.chat.executors.base import ChatDeps, ChatExecutor, TurnRequest
from core.application.chat.executors.direct import DirectExecutor
from core.application.chat.lifecycle import finalize_turn, handle_research_post_turn
from core.application.chat.understanding import TurnRequirements, resolve_requirements
from core.config import settings
from core.logger import reset_log_context, set_log_context

logger = logging.getLogger(__name__)


class TurnOrchestrator:
    def __init__(self, deps: ChatDeps) -> None:
        self.deps = deps
        # Each phase registers its branch here; the PolicyContext gates below decide
        # which kind a turn resolves to, so registration is inert until enabled.
        self._executors = {
            AgentExecutor.kind: AgentExecutor(),
            DirectExecutor.kind: DirectExecutor(),
        }

    # ── plan resolution ──────────────────────────────────────────────────────────
    def resolve_plan(self, ctx: ChatTurnContext) -> ExecutionPlan:
        # Settings-driven gates (all OFF by default = the Phase 1 dark launch). When
        # the master gate is closed the requirement set is irrelevant to routing, so
        # we skip the L0 pass entirely and keep the neutral ABSTAIN contract.
        policy = PolicyContext(
            fast_paths_enabled=settings.chat_fast_paths_enabled,
            direct_fast_path_enabled=settings.chat_direct_fast_path_enabled,
        )
        if policy.fast_paths_enabled:
            requirements = resolve_requirements(ctx, ctx.body.message)
        else:
            requirements = TurnRequirements()
        return build_execution_plan(requirements, policy)

    def executor_for(self, plan: ExecutionPlan) -> ChatExecutor:
        executor = self._executors.get(plan.kind)
        if executor is None:  # an unmapped kind must never hard-fail the turn
            logger.warning("no executor for plan kind=%s; falling back to AGENT", plan.kind)
            executor = self._executors[AgentExecutor.kind]
        return executor

    # ── non-streaming turn (/chat) ───────────────────────────────────────────────
    async def run_turn(self, ctx: ChatTurnContext) -> dict:
        plan = self.resolve_plan(ctx)
        executor = self.executor_for(plan)
        req = TurnRequest(ctx=ctx, deps=self.deps, plan=plan)
        research_continuing = False
        logger.info(
            "chat.plan-resolved turn_kind=%s reason=%s", plan.kind.value, plan.reason
        )
        try:
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
        plan = self.resolve_plan(ctx)
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

        async def pump():
            # NOTE: only ever await INSIDE the executor stream (never on frames). A
            # cancelled pump awaiting frames.put would swallow CancelledError in its
            # finally and strand the run_stream generator's cleanup; the queue is
            # unbounded so put_nowait never blocks, and a client disconnect lands the
            # CancelledError in the loop below (turn-cancelled logging + memory close).
            try:
                async for evt in executor.stream(
                    req, progress_sink=lambda evt: frames.put_nowait(("agent", evt))
                ):
                    frames.put_nowait(("agent", evt))
            finally:
                # Sentinel so the consumer below always terminates after the stream ends,
                # including on cancellation (the loop already logs turn-cancelled).
                frames.put_nowait(("agent", {"type": "done", "data": None}))

        pump_task = asyncio.create_task(pump())
        final = None
        research_continuing = False
        # Stream Commit Point: set on the first user-visible content delta. Until then
        # a pre-execution block may still fall back; after it the channel is locked
        # (errors terminate the stream; transparent executor switching is forbidden).
        committed = False

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
                if data["type"] == "done":
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
