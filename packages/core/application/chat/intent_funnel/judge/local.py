"""Judge backend: local small model (8.17's first choice).

Deployment reality: nothing is deployed yet, so the default
``chat_judge_local_url=""`` means NOT DEPLOYED -> :class:`JudgeUnavailable`,
which the ladder treats as fall-through, never an abstain. When an endpoint
exists, the call obeys the payload discipline from :mod:`.base` (query +
cards in, verdict + confidence out).
"""
from __future__ import annotations

import json

import httpx

from .base import SYSTEM, JudgeUnavailable, build_prompt


async def judge(query: str, candidates, entries_by_id: dict, *,
                url: str, timeout: float = 2.0) -> dict:
    """POST the minimal payload, return {capability_id, confidence}.

    Raises JudgeUnavailable on any transport/deployment absence so the caller
    falls through to the next backend (a 5xx from the judge is an UNAVAILABLE,
    not a verdict — the Judge never fabricates one from its own failure).
    """
    if not url:
        raise JudgeUnavailable("no local judge deployed")
    payload = {
        "system_prompt": SYSTEM,
        "prompt": build_prompt(query, candidates, entries_by_id),
        "temperature": 0.0,
        "max_tokens": 64,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise JudgeUnavailable(f"local judge unreachable: {exc!r}") from exc
    text = str(body.get("content") or body.get("text") or "")
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        raise JudgeUnavailable(f"local judge malformed reply: {exc!r}") from exc
    if not isinstance(data, dict):
        raise JudgeUnavailable("local judge reply not an object")
    return data
