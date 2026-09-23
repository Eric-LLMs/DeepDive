"""Node 5 — Binder: Capability + Query + Facts + arg_slots -> BoundArguments.

Four states, never a naked None (8.7): the argument truth is a STATE, and the
non-COMPLETE states escalate UPWARD (Judge.recheck first, Agent clarification
last) — the Binder itself executes nothing (8.8: routing metadata is all the
funnel ever produces).

The extraction ENGINE stays code (ruling 8.1-b): arg_slots declares WHICH
source feeds each slot; the extractor bodies (the "look at context, abstain if
wrong" judgement) live in the runtime action table, registered and versioned
alongside the tool. The Registry entry's ``arg_slots`` is the slot whitelist —
anything an extractor returns outside it is dirty data, not an answer.
"""
from __future__ import annotations

import logging

from core.application.chat.actions import DIRECT_TOOLS, bind_arguments

from .contract import (
    BIND_COMPLETE,
    BIND_INVALID,
    BIND_MISSING,
    BoundArguments,
)

logger = logging.getLogger(__name__)


def bind(entry, query: str, ctx) -> BoundArguments:
    """Bind one certified capability. Raises ActionIntegrityFailure (C2) when
    the tool table does not honor what the Registry promised — the funnel
    treats that as the terminal-integrity path, never an Agent recovery."""
    args = bind_arguments(entry.tool_binding, query, ctx)  # C2 may raise here
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
