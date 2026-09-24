"""Node 3 — Judge: pluggable adjudication over Recall/Matcher candidates.

Backend ladder (settings ``chat_judge_backend``):
  ``stub``   — deterministic margin rules (the transition default, §6-P2);
  ``local``  — deployed small judge model (8.17 first choice);
  ``online`` — platform LLM route, minimal payload (8.17 fallback);
  ``auto``   — local -> online -> stub (the deployed order of the ruling).

The contract each backend honors: CONFIDENT only with a real verdict; anything
else — low confidence, off-card invention, a backend that cannot serve — exits
UPWARD (UNCERTAIN / fall-through), never to the Agent floor (§4.1: escalation
only goes up). :func:`recheck` is the 8.7-ruling binding-review entry: a
non-COMPLETE binder result passes here first; the Judge can REJECT the
capability, and everything else continues up to Decision/Agent.
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
    if not cap_id or cap_id.upper() == "NONE":
        return JudgeVerdict(JUDGE_REJECT, None, "judge chose NONE")
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
    return JudgeVerdict(JUDGE_CONFIDENT, cap_id, f"confidence {confidence:.2f}")


async def _model_judge(backend, query, candidates, entries_by_id, llm) -> JudgeVerdict:
    from . import local, online

    if backend == "local":
        from core.config import settings

        data = await local.judge(query, candidates, entries_by_id,
                                 url=settings.chat_judge_local_url)
    else:
        data = await online.judge(query, candidates, entries_by_id, llm=llm)
    return _verdict_from_reply(data, candidates)


async def adjudicate(query: str, candidates, *, entries_by_id: dict,
                     llm=None) -> JudgeVerdict:
    """Run one Judge pass under the configured backend ladder."""
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
            return await _model_judge(step, query, candidates, entries_by_id, llm)
        except JudgeUnavailable as exc:
            logger.info("judge %s unavailable (%r); falling through the ladder", step, exc)
    return JudgeVerdict(JUDGE_UNCERTAIN, None, "no judge backend served")  # pragma: no cover


async def recheck(query: str, capability_id: str, *, entry, issue: str,
                  candidates, llm=None) -> JudgeVerdict:
    """The 8.7 binding-review pass: can this capability still stand given an
    extraction problem? Only a REJECT changes the outcome downstream (the
    capability is abandoned WITHOUT reaching the Agent as a false route);
    anything else escalates with the BIND_* reason attached.

    Ladder (2026-09-24 fix): every non-stub backend really gets called —
    ``auto`` tries local first and falls through to online, exactly like
    :func:`adjudicate`; the earlier "auto + no local -> UNCERTAIN" short
    circuit skipped the online step the ruling requires. Only the
    deterministic stub (no extraction power) answers "cannot review"
    directly."""
    from core.config import settings

    backend = _backend()
    if backend == "stub":
        return JudgeVerdict(JUDGE_UNCERTAIN, capability_id, f"binding {issue}")
    probe = [c for c in candidates if c.capability_id == capability_id]
    if not probe:
        return JudgeVerdict(JUDGE_UNCERTAIN, capability_id, f"binding {issue}")
    chain = {"auto": ("local", "online"),
             "local": ("local",), "online": ("online",)}[backend]
    recheck_query = f"{query}\n\n(binding problem: {issue})"
    for step in chain:
        if step == "local" and not settings.chat_judge_local_url:
            continue  # not deployed: fall through, never abstain-to-Agent
        try:
            return await _model_judge(
                step, recheck_query, probe, {capability_id: entry}, llm)
        except JudgeUnavailable as exc:
            logger.info("judge recheck %s unavailable (%r); trying next", step, exc)
    logger.info("judge recheck found no serving backend; escalating unresolved")
    return JudgeVerdict(JUDGE_UNCERTAIN, capability_id, f"binding {issue}")
