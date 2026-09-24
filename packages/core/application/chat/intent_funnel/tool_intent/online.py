"""ToolIntentModel backend: online small model via the platform LLM seam (8.17 fallback).

Discipline (2026-09-23 ruling): thinking is off (the platform-wide
``llm_disable_thinking`` knob already does this for the chat route), the
payload is the minimal card set from :mod:`.base`, temperature 0, and the
reply is just {capability_id, confidence, arguments}. A transport fault is
:class:`ToolIntentUnavailable` (fall through the ladder); a low-confidence or
off-card verdict is UNCERTAIN — the online ToolIntentModel, like every backend, never
fabricates.

Channel (2026-09-24 deployment ruling): the model service rides a DEDICATED
small-model channel, explicit per-call forwarding like the session-summary
seam — ``chat_tool_intent_online_model`` always forwarded when set;
``chat_tool_intent_online_base_url``/``_api_key`` are honored only as a pair (the
endpoint without its credential is worse than riding the pinned turn
channel). All-empty config reproduces the legacy "ride the turn channel"
behavior.
"""
from __future__ import annotations

from .base import SYSTEM, ToolIntentUnavailable, build_prompt


def _channel_kwargs() -> dict:
    from core.config import settings

    kw: dict = {}
    model = (settings.chat_tool_intent_online_model or "").strip()
    base_url = (settings.chat_tool_intent_online_base_url or "").strip()
    api_key = (settings.chat_tool_intent_online_api_key or "").strip()
    if model:
        kw["model"] = model
    if base_url and api_key:
        kw["base_url"] = base_url
        kw["api_key"] = api_key
    return kw


# ToolIntentModel reply: {capability_id, confidence, arguments}. The argument draft
# adds a short object per required slot — 256 is generous for the seeded
# single/two-slot tools while keeping the output bound hard.
TOOL_INTENT_MAX_TOKENS = 256


async def model_reply(query: str, candidates, entries_by_id: dict, *, llm,
                facts=None) -> dict:
    from core.config import settings

    if llm is None:
        raise ToolIntentUnavailable("no llm on deps for the online ToolIntentModel")
    try:
        data = await llm.complete_json(
            build_prompt(query, candidates, entries_by_id, facts=facts),
            system_prompt=SYSTEM,
            timeout=settings.chat_tool_intent_timeout_seconds,
            temperature=0.0,
            max_tokens=TOOL_INTENT_MAX_TOKENS,
            disable_thinking=True,
            **_channel_kwargs(),
        )
    except Exception as exc:
        raise ToolIntentUnavailable(f"tool-intent model (online) failed: {exc!r}") from exc
    if not isinstance(data, dict):
        raise ToolIntentUnavailable("tool-intent model (online) reply not an object")
    return data
