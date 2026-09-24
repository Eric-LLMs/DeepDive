"""Node 3 — Model A: one call adjudicates the capability AND extracts arguments.

Chain ruling (2026-09-24): the funnel makes AT MOST one Model A call per turn.
The former ``recheck`` second hop is deleted — it added zero information
(candidates narrowed to the one already picked, identical downstream outcome
for every verdict), and the two online TTFBs it cost were the cascade timeout.
Binder failures now exit straight to the Agent with BIND_* reasons.

Backend ladder (settings ``chat_judge_backend``):
  ``stub``   — deterministic margin rules, NO extraction power -> arguments
               stay None -> BIND_MISSING exit (honest, documented);
  ``local``  — deployed small Model A (first choice, ms-level);
  ``online`` — platform LLM route, minimal card payload (fallback);
  ``auto``   — local -> online -> stub (the deployed order of the ruling).

The contract each backend honors: CONFIDENT only with a real verdict on-card
and confidence above the floor; anything else — low confidence, off-card
invention, a backend that cannot serve — exits DOWN to the Agent (8.10),
never a fabricated route.
"""
from __future__ import annotations

import logging

from ..contract import JUDGE_CONFIDENT, JUDGE_REJECT, JUDGE_UNCERTAIN, JudgeVerdict
from .base import JudgeUnavailable

logger = logging.getLogger(__name__)

BACKENDS = ("stub", "local", "online", "auto")


def _backend() -> str:
    from core.config import settings

    backend = (settings.chat_judge_backend or "stub").strip().lower()
    if backend not in BACKENDS:
        logger.warning("unknown chat_judge_backend=%r; using stub", backend)
        return "stub"
    return backend


def _verdict_from_reply(data: dict, candidates) -> JudgeVerdict:
    cap_id = str(data.get("capability_id") or "").strip()
    valid = {c.capability_id for c in candidates}
    args = data.get("arguments")
    args = dict(args) if isinstance(args, dict) else None
    if not cap_id or cap_id.upper() == "NONE":
        return JudgeVerdict(JUDGE_REJECT, None, "model_a chose NONE")
    if cap_id not in valid:
        # off-card invention stays an uncertainty, it is never a verdict
        return JudgeVerdict(JUDGE_UNCERTAIN, None, f"off-card id {cap_id!r}")
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    from core.config import settings

    if confidence < settings.chat_judge_min_confidence:
        return JudgeVerdict(
            JUDGE_UNCERTAIN, cap_id, f"confidence {confidence:.2f} below floor",
        )
    return JudgeVerdict(
        JUDGE_CONFIDENT, cap_id, f"confidence {confidence:.2f}", arguments=args,
    )


async def _model_judge(backend, query, candidates, entries_by_id, llm, facts) -> JudgeVerdict:
    from . import local, online

    if backend == "local":
        from core.config import settings

        data = await local.judge(query, candidates, entries_by_id,
                                 url=settings.chat_judge_local_url, facts=facts)
    else:
        data = await online.judge(query, candidates, entries_by_id, llm=llm, facts=facts)
    return _verdict_from_reply(data, candidates)


async def adjudicate(query: str, candidates, *, entries_by_id: dict,
                     llm=None, facts=None) -> JudgeVerdict:
    """Run the ONE Model A pass under the configured backend ladder."""
    from core.config import settings

    if not candidates:
        return JudgeVerdict(JUDGE_REJECT, None, "no candidates")
    backend = _backend()
    chain = ({"auto": ("local", "online", "stub"),
              "local": ("local",), "online": ("online",), "stub": ("stub",)}[backend])
    for step in chain:
        if step == "stub":
            from . import stub

            return stub.judge(candidates, margin=settings.chat_funnel_margin)
        try:
            return await _model_judge(step, query, candidates, entries_by_id, llm, facts)
        except JudgeUnavailable as exc:
            logger.info("model_a %s unavailable (%r); falling through the ladder", step, exc)
    return JudgeVerdict(JUDGE_UNCERTAIN, None, "no model_a backend served")  # pragma: no cover
