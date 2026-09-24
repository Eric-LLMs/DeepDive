"""ActionExecutor: the Phase 5A fast path — direct dispatch of an allowlisted tool.

Serves turns the policy resolved to ``PlanKind.ACTION``: L0 certified that the turn
demands EXACTLY one registered tool with fully-determined args, so the ReAct loop is
unnecessary flow — the seam calls the SAME ``ToolRuntime.execute`` the Agent would
(approval waterfall, guards, sandbox, ACL pipeline all inherited).

Stage order encodes the side-effect boundary isolation (Phase 5 hard constraint 1):

  1. schema gate (:func:`validate_action`) — pre-execution, side-effect-free; a
     malformed request escalates and the Agent owns the clarification; a
     routing-stamped ``binding_integrity`` marker (stage-2 C2) terminates first;
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
    DIRECT_TOOLS,
    ActionIntegrityFailure,
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
# C2 (internal binding/runtime integrity) and C3 (registry drift / governance) are
# decided TERMINAL outcomes: an honest message, no seam, and NEVER an escalation —
# the Agent must not be used as a recovery channel for system faults.
_TERMINAL_INTEGRITY = (
    "That request references an action that is not available in the current system "
    "configuration, so nothing was done. Please contact the administrator if this "
    "persists."
)
_TERMINAL_STALE_ROUTE = (
    "The capability resolved for this request is no longer active, so nothing was "
    "done. Please phrase the request again."
)


class ActionExecutor(DirectExecutor):
    kind = PlanKind.ACTION

    async def _dispatch(self, req: TurnRequest) -> str:
        """Run the staged dispatch; returns the terminal message. Raises
        :class:`EscalateToAgent` ONLY on proven pre-execution failure."""
        action = req.plan.action or {}
        tool, args = action.get("tool"), action.get("args")

        # 0. Route/execute TOCTOU re-validation (8.9), in the namespace the
        #    ROUTER actually certified (P4 unification):
        #    * a funnel-certified turn stamps ``funnel_registry_version`` — the
        #      Registry content fingerprint — and re-validates against the
        #      active Registry view: same fingerprint, capability still active
        #      and enabled, same tool binding, allowlisted tool, and the kind
        #      gate still open (a mid-turn flip to OFF must not execute a
        #      widened kind — 入表≠开闸 holds at dispatch too);
        #    * a legacy QIR/L0 turn stamps ``registry_version`` (the qir index
        #      version) — the historical check, byte-unchanged.
        #    Any drift is C3 TERMINAL: a historical RouteResult never executes
        #    on blind trust.
        registry_version = action.get("registry_version")
        funnel_fp = action.get("funnel_registry_version")
        cap_id = str(action.get("capability_id") or "")
        if funnel_fp is not None:
            from core.application.chat.intent_funnel import funnel as funnel_mod
            from core.application.chat.intent_funnel.registry import active_view
            from core.application.chat.intent_funnel.registry.entry import STATUS_ACTIVE

            view = await active_view(session_factory=req.deps.session_factory)
            entry = next(
                (e for e in (view.entries if view is not None else ())
                 if e.capability_id == cap_id), None,
            )
            if (
                view is None or view.fingerprint != str(funnel_fp)
                or entry is None or not entry.enabled or entry.status != STATUS_ACTIVE
                or entry.tool_binding != tool or tool not in DIRECT_TOOLS
                or not funnel_mod.kind_enabled(entry.intent_kind)
            ):
                logger.warning(
                    "chat.action route-stale(capability=%s stamped=%s active=%s",
                    cap_id, funnel_fp, view.fingerprint if view else None,
                )
                return _TERMINAL_STALE_ROUTE
        elif registry_version is not None:
            from core.application.chat.qir import store as qir_store

            snapshot = await qir_store.active(req.deps.session_factory)
            cap = (
                snapshot.get(cap_id)
                if snapshot is not None and snapshot.version == str(registry_version)
                else None
            )
            if (
                cap is None or not cap.enabled
                or cap.tool_binding != tool or tool not in DIRECT_TOOLS
            ):
                logger.warning(
                    "chat.action route-stale capability=%s stamped=%s active=%s",
                    cap_id, registry_version,
                    snapshot.version if snapshot else None,
                )
                return _TERMINAL_STALE_ROUTE

        # 0.5 Routing-stage binding integrity (stage-2 C2, stamped in
        #     resolve_plan): the QIR route promised a capability whose binding the
        #     action table does not honor. Decided TERMINAL — the Agent must not
        #     re-plan around a system inconsistency. Checked BEFORE the schema gate
        #     because such an action intentionally carries no args.
        if action.get("binding_integrity"):
            logger.error(
                "chat.action binding-integrity tool=%s: %s",
                tool, action.get("binding_integrity"),
            )
            return _TERMINAL_INTEGRITY

        # 1. final schema gate, BEFORE the seam — malformed ⇒ nothing executed.
        try:
            validated = validate_action(str(tool or ""), args if isinstance(args, dict) else {})
        except ActionSchemaError as exc:
            raise EscalateToAgent(f"action schema: {exc}") from exc

        # 2. seam presence. A missing seam is an internal wiring fault (C2), NOT a
        #    user-input problem — terminate honestly; never re-plan through the Agent.
        if req.deps.run_tool is None:
            logger.error("chat.action integrity: run_tool seam not wired (tool=%s)", tool)
            return _TERMINAL_INTEGRITY

        # 3. the side-effect boundary.
        try:
            result = await req.deps.run_tool(validated["tool"], validated["args"], req.ctx)
        except ActionIntegrityFailure as exc:
            # C2: registry/runtime inconsistency (unknown tool, arg-schema drift).
            # Provably pre-body, but system faults must not launder through the
            # Agent as a retry mechanism.
            logger.error("chat.action integrity failure tool=%s: %s", validated["tool"], exc.reason)
            return _TERMINAL_INTEGRITY
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
