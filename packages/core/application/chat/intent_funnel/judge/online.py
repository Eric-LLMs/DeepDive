"""Judge backend: online small model via the platform LLM seam (8.17 fallback).

Discipline (2026-09-23 ruling): thinking is off (the platform-wide
``llm_disable_thinking`` knob already does this for the chat route), the
payload is the minimal card set from :mod:`.base`, temperature 0, and the
reply is just {capability_id, confidence}. A transport fault is
:class:`JudgeUnavailable` (fall through the ladder); a low-confidence or
off-card verdict is UNCERTAIN (escalate upward) — the online judge, like
every backend, never fabricates.

Channel (2026-09-24 deployment ruling): the judge rides a DEDICATED
small-model channel, explicit per-call forwarding like the session-summary
seam — ``chat_judge_online_model`` always forwarded when set;
``chat_judge_online_base_url``/``_api_key`` are honored only as a pair (the
endpoint without its credential is worse than riding the pinned turn
channel). All-empty config reproduces the legacy "ride the turn channel"
behavior.
"""
from __future__ import annotations

from .base import SYSTEM, JudgeUnavailable, build_prompt


def _channel_kwargs() -> dict:
    from core.config import settings

    kw: dict = {}
    model = (settings.chat_judge_online_model or "").strip()
    base_url = (settings.chat_judge_online_base_url or "").strip()
    api_key = (settings.chat_judge_online_api_key or "").strip()
    if model:
        kw["model"] = model
    if base_url and api_key:
        kw["base_url"] = base_url
        kw["api_key"] = api_key
    return kw


async def judge(query: str, candidates, entries_by_id: dict, *, llm) -> dict:
    from core.config import settings

    if llm is None:
        raise JudgeUnavailable("no llm on deps for the online judge")
    try:
        data = await llm.complete_json(
            build_prompt(query, candidates, entries_by_id), system_prompt=SYSTEM,
            timeout=settings.chat_judge_timeout_seconds,
            temperature=0.0,
            **_channel_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001 - transport/auth/parse faults all mean "unavailable"
        raise JudgeUnavailable(f"online judge failed: {exc!r}") from exc
    if not isinstance(data, dict):
        raise JudgeUnavailable("online judge reply not an object")
    return data
