"""Judge backend: local small model (8.17's first choice).

Deployment reality (2026-09-24 ruling): nothing is deployed yet, so the
default ``chat_judge_local_url=""`` means NOT DEPLOYED -> :class:`JudgeUnavailable`,
which the ladder treats as fall-through, never an abstain-to-Agent. When an
endpoint exists, the call obeys the payload discipline from :mod:`.base`
(query + cards in, verdict + confidence out).

Wire: OpenAI-compatible — ``chat_judge_local_url`` is a BASE url such as
``http://localhost:18090/v1`` (llama.cpp server, vLLM, or any host uvicorn
exposing ``/chat/completions``). The bespoke {system_prompt,prompt} protocol
this file used to speak matched no real server, and定型 it now costs nothing
because there is no deployed consumer to migrate.
"""
from __future__ import annotations

import json

import httpx

from .base import SYSTEM, JudgeUnavailable, build_prompt


async def judge(query: str, candidates, entries_by_id: dict, *,
                url: str, timeout: float = 2.0) -> dict:
    """POST the minimal payload to ``{url}/chat/completions``, return
    {capability_id, confidence}.

    Raises JudgeUnavailable on any transport/deployment absence so the caller
    falls through to the next backend (a 5xx from the judge is an UNAVAILABLE,
    not a verdict — the Judge never fabricates one from its own failure).
    """
    if not url:
        raise JudgeUnavailable("no local judge deployed")
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_prompt(query, candidates, entries_by_id)},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url.rstrip("/") + "/chat/completions", json=payload)
            resp.raise_for_status()
            body = resp.json()
        text = str(
            body["choices"][0]["message"]["content"]
        )
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise JudgeUnavailable(f"local judge unreachable/bad reply: {exc!r}") from exc
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        raise JudgeUnavailable(f"local judge malformed reply: {exc!r}") from exc
    if not isinstance(data, dict):
        raise JudgeUnavailable("local judge reply not an object")
    return data
