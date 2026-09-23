"""Judge backend: online small model via the platform LLM seam (8.17 fallback).

Discipline (2026-09-23 ruling): thinking is off (the platform-wide
``llm_disable_thinking`` knob already does this for the chat route), the
payload is the minimal card set from :mod:`.base`, and the reply is just
{capability_id, confidence}. A transport fault is :class:`JudgeUnavailable`
(fall through the ladder); a low-confidence or off-card verdict is UNCERTAIN
(escalate upward) — the online judge, like every backend, never fabricates.
"""
from __future__ import annotations

from .base import SYSTEM, JudgeUnavailable, build_prompt


async def judge(query: str, candidates, entries_by_id: dict, *, llm) -> dict:
    if llm is None:
        raise JudgeUnavailable("no llm on deps for the online judge")
    try:
        data = await llm.complete_json(
            build_prompt(query, candidates, entries_by_id), system_prompt=SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001 - transport/auth/parse faults all mean "unavailable"
        raise JudgeUnavailable(f"online judge failed: {exc!r}") from exc
    if not isinstance(data, dict):
        raise JudgeUnavailable("online judge reply not an object")
    return data
