"""ActionExecutor: the Phase 5A fast path — direct dispatch of an allowlisted tool.

Serves turns the policy resolved to ``PlanKind.ACTION``: L0 certified that the turn
demands EXACTLY one registered tool with fully-determined args, so the ReAct loop is
unnecessary flow — the seam calls the SAME ``ToolRuntime.execute`` the Agent would
(approval waterfall, guards, sandbox, ACL pipeline all inherited).

Stage order encodes the side-effect boundary isolation (Phase 5 hard constraint 1):

  1. schema gate (:func:`validate_action`) — pre-execution, side-effect-free; a
     malformed request escalates and the Agent owns the clarification;
  2. seam presence — not wired ⇒ escalate (LLM fallback intact, capability unchanged);
  3. the seam call is the SIDE-EFFECT BOUNDARY. Before it: anything the seam can PROVE
     did not execute raises :class:`ActionPreflightFailure` ⇒ escalate. After the tool
     body was entered, any other exception means the state is UNKNOWN — this executor
     emits one honest terminal message and finishes normally; it must NEVER re-run the
     action through the Agent (that is how duplicate folders get made);
  4. ``{"ok": False, "reason"}`` = a DECIDED denial (approval refused, policy guard) —
     no side effect happened and re-asking the Agent would only re-trigger it; the
     denial is surfaced honestly as a terminal answer.

Everything before the first yielded event is pre-commit; the events after (content →
internal done, user+assistant rows persisted) mirror DIRECT exactly. No LLM call: the
confirmation text is the tool's own deterministic output.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from core.application.chat.actions import (
    ActionPreflightFailure,
    ActionSchemaError,
    validate_action,
)
from core.application.chat.execution_plan import PlanKind
from core.application.chat.executors.base import (
    ChatEvent,
    EscalateToAgent,
    ProgressSink,
    TurnRequest,
)
from core.application.chat.executors.direct import DirectExecutor, DirectResult

logger = logging.getLogger(__name__)

# Honest terminal messages (no LLM on this branch — the words ARE the answer).
_STATE_UNKNOWN = (
    "That operation could not be confirmed — it may or may not have been applied, and "
    "I did not retry it automatically to avoid doing it twice. Please check the result "
    "and try again only if it is missing."
)
_DENIED_PREFIX = "Could not complete that request: "


class ActionExecutor(DirectExecutor):
    kind = PlanKind.ACTION

    async def _dispatch(self, req: TurnRequest) -> str:
        """Run the staged dispatch; returns the terminal message. Raises
        :class:`EscalateToAgent` ONLY on proven pre-execution failure."""
        action = req.plan.action or {}
        tool, args = action.get("tool"), action.get("args")

        # 1. final schema gate, BEFORE the seam — malformed ⇒ nothing executed.
        try:
            validated = validate_action(str(tool or ""), args if isinstance(args, dict) else {})
        except ActionSchemaError as exc:
            raise EscalateToAgent(f"action schema: {exc}") from exc

        # 2. seam presence.
        if req.deps.run_tool is None:
            raise EscalateToAgent("action seam not wired")

        # 3. the side-effect boundary.
        try:
            result = await req.deps.run_tool(validated["tool"], validated["args"], req.ctx)
        except ActionPreflightFailure as exc:
            # Proved: the body never ran. The Agent may clarify / re-plan.
            raise EscalateToAgent(f"preflight: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001 - STATE UNKNOWN by contract
            logger.warning("chat.action state-unknown tool=%s: %r", validated["tool"], exc)
            return _STATE_UNKNOWN

        # 4. decided denial (terminal, no side effect) or success.
        if result.get("ok") is False:
            return _DENIED_PREFIX + str(result.get("reason") or "the operation was denied.")
        return str(result.get("output") or "Done.")

    # ── stream / run: identical terminal shape, no LLM call ──────────────────────
    async def stream(
        self, req: TurnRequest, *, progress_sink: ProgressSink
    ) -> AsyncIterator[ChatEvent]:
        # Everything up to the first yield is pre-commit: an escalation here is legal.
        message = await self._dispatch(req)
        ctx = req.ctx
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in (ctx.history or [])
            if isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
        ]
        await ctx.session_memory.append_message("user", ctx.user_text)
        await ctx.session_memory.append_message("assistant", message)
        yield {"type": "content", "data": message}
        yield {
            "type": "done",
            "data": {
                "answer": message,
                "messages": [*history, {"role": "user", "content": ctx.user_text},
                             {"role": "assistant", "content": message}],
                "usage": {},
                "error": None,
                "cost_usd": None,
            },
        }

    async def run(self, req: TurnRequest) -> DirectResult:
        message = await self._dispatch(req)
        ctx = req.ctx
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in (ctx.history or [])
            if isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
        ]
        await ctx.session_memory.append_message("user", ctx.user_text)
        await ctx.session_memory.append_message("assistant", message)
        messages = [
            *history,
            {"role": "user", "content": ctx.user_text},
            {"role": "assistant", "content": message},
        ]
        return DirectResult(messages=messages, final_answer=message, usage={})
