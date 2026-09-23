"""Funnel common layer — the system-wide safety guards (ruling 8.1-a).

These guards are deliberately CODE, not Registry data: the configuration table
is "meant to be read and edited by humans" and does not fit negation/context
veto logic. Nodes never duplicate them — :func:`funnel.route` applies
:func:`turn_veto` once before the cascade and the Matcher applies
:func:`negated` before certifying a HIT, exactly the two-call-site discipline
the legacy action layer already proved.
"""
from __future__ import annotations

from core.application.chat.actions import is_negated_request
from core.application.chat.sanitization import is_pure_user_text
from core.application.chat.understanding import Signal


def turn_veto(message: str, requirements, ctx) -> str | None:
    """Prefixed reason when the funnel must not own this turn; None = proceed.

    Mirrors the frozen gate semantics (design §4.3 zero-pollution + ruling a):
    web/memory demand belongs to the Agent's planning loop, research/handoff
    turns are inherently multi-step chains, and non-pure text (attachment
    markers, control payloads) must never be pattern-matched as user intent."""
    if not is_pure_user_text(message or ""):
        return "input_not_pure_text"
    if requirements.needs_web is not Signal.LOW or requirements.needs_memory:
        return "turn_demands_web_or_memory"
    if getattr(ctx, "research_turn", False) or getattr(ctx, "effective_handoff", None):
        return "context_research_or_handoff"
    return None


def negated(message: str) -> bool:
    """The single global negation guard (8.1-a): "不要新建文件夹" is not a request."""
    return is_negated_request(message or "")
