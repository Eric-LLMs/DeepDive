"""Shadow Mode — the 8.15 measurement infrastructure, formalizing step 3's hook.

Tri-state switch: ``settings.chat_matcher_mode`` (validated in ``core.config``
docs + :func:`funnel.route`):

* ``off``    — the node never runs (dark-launch default);
* ``shadow`` — the Registry-backed Matcher runs on every turn and its verdict is
  logged as ``would_*`` telemetry next to the L0 outcome; routing is untouched
  and the Agent keeps the turn byte-identically;
* ``on``     — the P2 promotion (Matcher becomes an authoritative ACTION router
  after the measured L0 equivalence). In P1 it runs SHADOW semantics with a
  warning: a mis-set switch must never silently hand routing to a node that has
  only ever measured itself in the dark.

Two invariants this module owns:

1. observation, never behavior — every failure is fail-quiet; the only
   observable effect of a broken shadow node is absent log lines (8.15: "结果
   不得影响当前 Agent 行为");
2. cost isolation — the whole observation runs under an ``execution_mode=shadow``
   pin (8.14), so any LLM/embedding usage a future shadow stage records settles
   outside real-user billing. The pin is always reset, exception included.
"""
from __future__ import annotations

import logging

from core.infrastructure.request_context import (
    reset_request_execution_mode,
    set_request_execution_mode,
)

from .contract import MATCH_AMBIGUOUS, MATCH_HIT, MatchResult

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")

# fallback_reason vocabulary (8.10: prefixed codes, never a bare word)
_FALLBACK_REASONS = {
    MATCH_HIT: "-",
    MATCH_AMBIGUOUS: "matcher_ambiguous",
}
_DEFAULT_FALLBACK = "matcher_miss"
_WOULD_STAGE = "matcher"  # the node's own stage id; Judge/Decision arrive in P2

_warned_on = False


def matcher_mode() -> str:
    """The sanitized tri-state value; anything unknown fails safe to ``off``."""
    from core.config import settings

    mode = (settings.chat_matcher_mode or "off").strip().lower()
    if mode not in MODES:
        logger.warning("unknown chat_matcher_mode=%r; treating as off", mode)
        return "off"
    return mode


async def observe(ctx, deps, requirements, mode: str) -> None:
    """Run the Matcher in the dark and log its verdict. Never raises, never
    routes; pins execution_mode=shadow for its duration only."""
    global _warned_on
    if mode == "on" and not _warned_on:
        _warned_on = True  # once per process — a mis-set switch, not a per-turn event
        logger.warning(
            "chat_matcher_mode=on: authoritative Matcher routing is a P2 deliverable; "
            "running shadow semantics only, L0 stays authoritative"
        )
    token = set_request_execution_mode("shadow")
    try:
        await _match_and_log(ctx, deps, requirements, mode)
    except Exception as exc:  # noqa: BLE001 - shadow is observation, never behavior
        logger.info("matcher shadow fail-quiet: %r", exc)
    finally:
        reset_request_execution_mode(token)


async def _match_and_log(ctx, deps, requirements, mode: str) -> None:
    from . import matcher
    from .registry import active_view  # inside: keeps the monkeypatch seam alive

    view = await active_view(session_factory=deps.session_factory)
    if view is None:
        return  # nothing published yet — no comparison possible
    res = matcher.match(getattr(ctx.body, "message", "") or "", view)
    l0_tool = (requirements.requested_action or {}).get("tool")
    logger.info(
        "matcher_shadow mode=%s version=%d state=%s registry_version=%s "
        "would_route=%s would_capability=%s would_stage=%s confidence=%s "
        "fallback_reason=%s candidates=%s l0_tool=%s",
        mode, view.version, res.state, view.fingerprint,
        "t" if res.state == MATCH_HIT else "f",
        res.capability_id or "-", _WOULD_STAGE,
        _confidence(res),
        _FALLBACK_REASONS.get(res.state, _DEFAULT_FALLBACK),
        ",".join(res.candidates) or "-", l0_tool or "-",
    )


def _confidence(res: MatchResult) -> str:
    """The deterministic Matcher is always sure of a table hit; a real
    calibrated confidence arrives with the Judge (P2, 8.13's data-first plan)."""
    return "1.0" if res.state == MATCH_HIT else "0.0"
