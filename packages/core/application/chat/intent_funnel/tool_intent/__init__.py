"""Node 3 — ToolIntentModel: one call adjudicates the capability AND extracts arguments.

Chain ruling (2026-09-24): the funnel makes AT MOST one ToolIntentModel call per turn.
The former ``recheck`` second hop is deleted — it added zero information
(candidates narrowed to the one already picked, identical downstream outcome
for every verdict), and the two online TTFBs it cost were the cascade timeout.
Binder failures now exit straight to the Agent with BIND_* reasons.

Backend ladder (settings ``chat_tool_intent_backend``):
  ``stub``   — deterministic margin rules, NO extraction power -> arguments
               stay None -> BIND_MISSING exit (honest, documented);
  ``local``  — deployed small ToolIntentModel (first choice, ms-level);
  ``online`` — platform LLM route, minimal card payload (fallback);
  ``auto``   — local -> online -> stub (the deployed order of the ruling).

The contract each backend honors: CONFIDENT only with a real verdict on-card
and confidence above the floor; anything else — low confidence, off-card
invention, a backend that cannot serve — exits DOWN to the Agent (8.10),
never a fabricated route.
"""
from __future__ import annotations

import logging

from ..contract import (
    TOOL_INTENT_CONFIDENT,
    TOOL_INTENT_REJECT,
    TOOL_INTENT_UNCERTAIN,
    ToolIntentVerdict,
)
from .base import ToolIntentUnavailable

logger = logging.getLogger(__name__)

BACKENDS = ("stub", "local", "online", "auto")


def _backend() -> str:
    from core.config import settings

    backend = (settings.chat_tool_intent_backend or "stub").strip().lower()
    if backend not in BACKENDS:
        logger.warning("unknown chat_tool_intent_backend=%r; using stub", backend)
        return "stub"
    return backend


def _verdict_from_reply(data: dict, candidates) -> ToolIntentVerdict:
    cap_id = str(data.get("capability_id") or "").strip()
    valid = {c.capability_id for c in candidates}
    args = data.get("arguments")
    args = dict(args) if isinstance(args, dict) else None
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not cap_id or cap_id.upper() == "NONE":
        return ToolIntentVerdict(TOOL_INTENT_REJECT, None, "tool_intent chose NONE",
                                 confidence=None)
    if cap_id not in valid:
        # off-card invention stays an uncertainty, it is never a verdict
        return ToolIntentVerdict(TOOL_INTENT_UNCERTAIN, None, f"off-card id {cap_id!r}",
                                 confidence=None)
    from core.config import settings

    if confidence < settings.chat_tool_intent_min_confidence:
        return ToolIntentVerdict(
            TOOL_INTENT_UNCERTAIN, cap_id, f"confidence {confidence:.2f} below floor",
            confidence=confidence,  # telemetry: the raw value, floor kept honest
        )
    return ToolIntentVerdict(
        TOOL_INTENT_CONFIDENT, cap_id, f"confidence {confidence:.2f}", arguments=args,
        confidence=confidence,
    )


async def _model_call(backend, query, candidates, entries_by_id, llm, facts) -> ToolIntentVerdict:
    from . import local, online

    if backend == "local":
        from core.config import settings

        data = await local.model_reply(query, candidates, entries_by_id,
                                 url=settings.chat_tool_intent_local_url, facts=facts)
    else:
        data = await online.model_reply(query, candidates, entries_by_id, llm=llm, facts=facts)
    return _verdict_from_reply(data, candidates)


async def select_and_extract(query: str, candidates, *, entries_by_id: dict,
                     llm=None, facts=None) -> ToolIntentVerdict:
    """Run the ONE ToolIntentModel pass under the configured backend ladder.

    Action Detection is unconditional (ruling 2026-09-25): an EMPTY candidate
    list is a legitimate input — the model sees the explicit "(none registered
    for this turn)" card set and can only answer NONE -> REJECT. There is no
    pre-model short-circuit any more."""
    from core.config import settings

    backend = _backend()
    chain = ({"auto": ("local", "online", "stub"),
              "local": ("local",), "online": ("online",), "stub": ("stub",)}[backend])
    for step in chain:
        if step == "stub":
            from . import stub

            return stub.evaluate(candidates, margin=settings.chat_funnel_margin)
        try:
            return await _model_call(step, query, candidates, entries_by_id, llm, facts)
        except ToolIntentUnavailable as exc:
            logger.info("tool_intent %s unavailable (%r); falling through the ladder", step, exc)
    return ToolIntentVerdict(TOOL_INTENT_UNCERTAIN, None, "no tool_intent backend served")  # pragma: no cover
