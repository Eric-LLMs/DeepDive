"""ActionExecutor: the Phase 5A fast path — direct dispatch of an allowlisted tool.

Serves turns the policy resolved to ``PlanKind.ACTION``: L0 certified that the turn
demands EXACTLY one registered tool with fully-determined args, so the ReAct loop is
unnecessary flow — the seam calls the SAME ``ToolRuntime.execute`` the Agent would
(approval waterfall, guards, sandbox, ACL pipeline all inherited).

Stage order encodes the side-effect boundary isolation (Phase 5 hard constraint 1):

  1. existence + schema gate (live ``ToolRuntime.schemas()`` roster, then
     :func:`validate_action`) — pre-execution, side-effect-free; a tool the roster
     does not register is a C2 TERMINAL (the Agent never re-plans around a system
     fault); a malformed REQUEST (argument shape) escalates and the Agent owns the
     clarification; a routing-stamped ``binding_integrity`` marker (stage-2 C2)
     terminates first;
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


def _tool_roster(deps) -> dict[str, dict[str, dict]] | None:
    """tool -> {slot: {"max_len": int, "required": bool}} projected from the
    live ``ToolRuntime.schemas()`` — the one tool existence/schema truth since
    the 2026-09-26 ruling (max_len 0 = the schema states no length bound). A
    schema with no ``required`` list is treated as fully required (fail
    closed). None when no runtime is reachable — callers fail closed honestly."""
    runtime = getattr(getattr(deps, "agent", None), "runtime", None)
    if runtime is None:
        return None
    out: dict[str, dict[str, dict]] = {}
    for s in runtime.schemas():
        params = s.get("parameters") or {}
        props = params.get("properties") or {}
        req = params.get("required")
        required_all = req is None
        out[str(s["name"])] = {
            str(k): {"max_len": int((v or {}).get("maxLength") or 0),
                     "required": bool(required_all or (k in (req or [])))}
            for k, v in props.items()
        }
    return out


class ActionExecutor(DirectExecutor):
    kind = PlanKind.ACTION

    async def _dispatch(self, req: TurnRequest) -> str:
        """Run the staged dispatch; returns the terminal message. Raises
        :class:`EscalateToAgent` ONLY on proven pre-execution failure."""
        action = req.plan.action or {}
        tool, args = action.get("tool"), action.get("args")

        # Tool existence/schema truth (ruling 2026-09-26): the LIVE
        # ``ToolRuntime.schemas()`` roster — the legacy DIRECT_TOOLS table is
        # no longer consulted anywhere on the dispatch path.
        roster = _tool_roster(req.deps)

        # 0. Route/execute TOCTOU re-validation (8.9). The funnel-certified turn
        #    stamps ``funnel_registry_version`` — the Registry content fingerprint
        #    of the live view — and re-validates against the active view: same
        #    fingerprint, capability still active and enabled, same tool binding,
        #    tool present on the live ToolRuntime roster, and the kind gate still
        #    open (a mid-turn flip to
        #    OFF must not execute a widened kind — 入表≠开闸 holds at dispatch
        #    too). Any drift is C3 TERMINAL: a routed turn never executes on
        #    blind trust. (QIR retirement, migration 0014: the legacy
        #    ``registry_version``/qir_store branch is gone — the live tables are
        #    the only routing namespace.)
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
                or entry.tool_binding != tool or tool not in (roster or {})
                or not funnel_mod.kind_enabled(entry.intent_kind)
            ):
                logger.warning(
                    "chat.action route-stale(capability=%s stamped=%s active=%s",
                    cap_id, funnel_fp, view.fingerprint if view else None,
                )
                return _TERMINAL_STALE_ROUTE

        # 0.5 Routing-stage binding integrity (stage-2 C2, stamped in
        #     resolve_plan): the route promised a capability whose binding the
        #     action table does not honor. Decided TERMINAL — the Agent must not
        #     re-plan around a system inconsistency. Checked BEFORE the schema gate
        #     because such an action intentionally carries no args.
        if action.get("binding_integrity"):
            logger.error(
                "chat.action binding-integrity tool=%s: %s",
                tool, action.get("binding_integrity"),
            )
            return _TERMINAL_INTEGRITY

        # 0.6 Tool existence on the LIVE roster is a C2 system-integrity fact, not
        #     a user-input problem: a certified turn naming a tool the runtime does
        #     not register means the route promised a capability the system lacks —
        #     honest terminal, seam never entered, the Agent must NOT re-plan around
        #     a system fault (same doctrine as the binding-integrity marker above).
        #     The legacy DIRECT_TOOLS table is not consulted anywhere on this path.
        if roster is not None and str(tool or "") not in roster:
            logger.error("chat.action integrity: tool=%s not on the live ToolRuntime "
                         "roster", tool)
            return _TERMINAL_INTEGRITY

        # 1. final schema gate, BEFORE the seam — malformed ⇒ nothing executed.
        #    Argument-shape problems are the user-input class: escalate and let the
        #    Agent clarify. The roster is the schema truth (ruling 2026-09-26); a
        #    missing roster fails closed through the schema error.
        try:
            validated = validate_action(str(tool or ""),
                                        args if isinstance(args, dict) else {},
                                        tool_schemas=roster or {})
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
