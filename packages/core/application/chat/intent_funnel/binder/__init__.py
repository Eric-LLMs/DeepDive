"""Node 4 — Binder: Model A draft + Registry schema -> BoundArguments.

Four states, never a naked None (8.7): the argument truth is a STATE, and the
non-COMPLETE states exit to the Agent (chain ruling 2026-09-24: the recheck
hop is gone — on the active path the Binder VALIDATES Model A's extraction
(:func:`validate`) and never extracts itself). The Binder executes nothing
(8.8: routing metadata is all the funnel ever produces).

:func:`bind` (extract-then-validate via the entry's declared plugin) remains
for the LEGACY lanes (L0/QIR compat, p5 monkeypatch seam); the new funnel only
calls :func:`validate`.

Since the 2026-09-24 structure rulings this node DEFINES the binding pipeline —
:func:`bind_arguments` (capability + raw query -> structured args) and
:func:`validate_action` (the executor's final schema gate) moved here from
``chat.actions``, which keeps a lazy façade for its historic import surface.

The extraction ENGINE of the legacy path stays code (ruling 8.1-b): arg_slots
declares WHICH source feeds each slot — ``user_input`` (default) or
``plugin:<name>``; the extractor bodies live in the registry roster
(:mod:`core.application.chat.intent_funnel.registry.plugins`), which the
publish gate validates and this node resolves. The Registry entry's ``arg_slots``
is the slot whitelist — anything an extractor returns outside it is dirty
data, not an answer.
"""
from __future__ import annotations

import logging

from core.application.chat.actions import (
    ActionIntegrityFailure,
    ActionSchemaError,
    is_negated_request,
)

from ..contract import (
    BIND_COMPLETE,
    BIND_INVALID,
    BIND_MISSING,
    BoundArguments,
)
from ..registry.plugins import DIRECT_TOOLS, plugin_extractor

logger = logging.getLogger(__name__)


# ── the chain-ruling entry (2026-09-24): Model A extracted, Binder validates ───────

def validate(entry, args) -> BoundArguments:
    """Normalize/validate Model A's argument DRAFT against the Registry's
    CANONICAL parameter schema (``entry.parameters``, 0007 ruling). The Binder
    extracts nothing on this path — it is a pure gate: unknown slot -> INVALID;
    missing/blank required slot -> MISSING (Agent owns the clarification);
    over-length -> INVALID. The executor's own ``validate_action`` stays the
    final runtime-side gate; the publish gate guarantees the two schemas agree.
    A capability with an empty schema needs no arguments at all: any draft (or
    none, e.g. the stub backend's) normalizes to COMPLETE {}."""
    schema = dict(entry.parameters or {})
    if not schema:
        return BoundArguments(BIND_COMPLETE, {})
    if not isinstance(args, dict):
        return BoundArguments(BIND_MISSING)
    if set(args) - set(schema):
        return BoundArguments(BIND_INVALID, args)
    out: dict[str, str] = {}
    for name, raw in schema.items():
        spec = raw if isinstance(raw, dict) else {}
        val = args.get(name)
        if val is None or (isinstance(val, str) and not val.strip()):
            if spec.get("required", True):
                return BoundArguments(BIND_MISSING)
            continue
        if not isinstance(val, str):
            val = str(val)
        val = val.strip()
        max_len = spec.get("max_len")
        if max_len is not None and len(val) > int(max_len):
            return BoundArguments(BIND_INVALID, args)
        out[name] = val
    return BoundArguments(BIND_COMPLETE, out)


# ── the binding pipeline (moved from chat.actions, behavior byte-identical) ────────

def bind_arguments(tool_binding: str, text: str, ctx, *,
                   extract=None) -> dict | None:
    """Argument Binding layer: ``Capability + raw Query -> structured arguments``.

    This is stage (2) of the frozen plan-resolution pipeline — owned by the
    Binder, NOT by the routing nodes (which only output ``capability_id``). The
    default extractor is the tool's registry-roster regex recognizer; callers
    with an entry-declared ``plugin:<name>`` source pass that callable via
    ``extract`` (8.1-b). That is an IMPLEMENTATION CHOICE, not an architectural
    principle — a future constrained argument parser may replace it behind this
    same function shape. Whatever the binding mechanism, its output still must
    pass :func:`validate_action` and the Runtime governance funnel before
    anything executes.

    ``None`` means the structured arguments cannot be determined from THIS
    sentence + context (C1: parameters incomplete) — the caller lets the original
    Agent path handle the turn, untouched. A missing binding entry is a registry
    inconsistency (C2) and raises ``ActionIntegrityFailure``.
    """
    if is_negated_request(text):
        return None  # second layer of the negation defense (routed despite L0)
    spec = DIRECT_TOOLS.get(tool_binding)
    if spec is None or spec.extract is None:
        raise ActionIntegrityFailure(f"no argument binding for tool {tool_binding!r}")
    return (extract or spec.extract)((text or "").strip(), ctx)


def validate_action(tool: str, args: dict) -> dict:
    """Final schema gate (executor, BEFORE the seam): the tool must be on the allowlist,
    every required slot present, str, stripped, within length bounds. Raises
    :class:`ActionSchemaError` — a pre-execution, side-effect-free failure the executor
    may safely escalate (the Agent owns the clarification)."""
    spec = DIRECT_TOOLS.get(tool)
    if spec is None:
        raise ActionSchemaError(f"tool not on the direct-call allowlist: {tool!r}")
    if not isinstance(args, dict):
        raise ActionSchemaError("args must be a mapping")
    out: dict[str, str] = {}
    for slot, max_len in spec.arg_schema.items():
        val = args.get(slot)
        if not isinstance(val, str) or not (val := val.strip()):
            raise ActionSchemaError(f"missing slot: {slot}")
        if len(val) > max_len:
            raise ActionSchemaError(f"slot too long: {slot} (>{max_len})")
        out[slot] = val
    return {"tool": tool, "args": out}


# ── the funnel-facing entry (four states, 8.7) ─────────────────────────────────────

def _entry_extractor(entry) -> object:
    """Resolve the entry's declared extraction source (8.1-b).

    All slots must agree: either the default ``user_input`` (returns None ->
    the tool's roster recognizer) or ONE shared ``plugin:<name>``. A plugin
    name surviving to runtime but missing from the roster is a C2 integrity
    fault (the publish gate is supposed to have rejected it)."""
    sources = set()
    for slot in (entry.arg_slots or {}).values():
        src = slot.get("source") if isinstance(slot, dict) else slot
        sources.add(str(src or "user_input"))
    if not sources - {"user_input"}:
        return None
    plugins = {s for s in sources if s.startswith("plugin:")}
    if len(plugins) != 1 or plugins != sources:
        raise ActionIntegrityFailure(
            f"{entry.capability_id}: arg_slots sources mix plugin:/default: "
            f"{sorted(sources)}")
    name = next(iter(plugins))
    fn = plugin_extractor(name)
    if fn is None:
        raise ActionIntegrityFailure(
            f"{entry.capability_id}: {name!r} not registered in the plugin roster")
    return fn


def bind(entry, query: str, ctx) -> BoundArguments:
    """Bind one certified capability. Raises ActionIntegrityFailure (C2) when
    the tool table does not honor what the Registry promised — the funnel
    treats that as the terminal-integrity path, never an Agent recovery."""
    extract = _entry_extractor(entry)  # 8.1-b: plugin:<name> or None (roster default)
    args = bind_arguments(entry.tool_binding, query, ctx, extract=extract)  # C2 may raise
    if args is None:
        return BoundArguments(BIND_MISSING)

    schema = DIRECT_TOOLS[entry.tool_binding].arg_schema
    slots = entry.arg_slots or {k: {"source": "user_input"} for k in schema}
    # declared slots are the whitelist; an extractor result outside them — or
    # missing one they declared — is a dirty-data signal, not a partial answer
    if set(args.keys()) != set(slots.keys()):
        return BoundArguments(BIND_INVALID, args)
    for name, value in args.items():
        bound = schema.get(name)
        if not isinstance(value, str) or not value.strip():
            return BoundArguments(BIND_INVALID, args)
        if bound and len(value) > int(bound):
            return BoundArguments(BIND_INVALID, args)
    # BIND_AMBIGUOUS (contract) becomes reachable when a tool gains conflicting
    # extractors (multi-source slots); the constant and the funnel's escalation
    # for it are already wired, so adding producers later touches no
    # orchestration code.
    return BoundArguments(BIND_COMPLETE, args)
