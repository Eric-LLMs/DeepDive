"""Model A backend: the LOCAL provider — the Docker ``model-a`` service
(8.17's first choice, ms-level when warm).

Deployment (compose): the service speaks an OpenAI-compatible wire, so the
provider seam is two-way swappable — the inference backend (Ollama today,
vLLM/llama.cpp as drop-ins) and the model itself (current default configured
via ``chat_judge_local_model``; no model name exists in this code). Nothing
deployed yet means the default ``chat_judge_local_url=""`` ->
:class:`JudgeUnavailable`, which the ladder treats as fall-through to the
online provider — never an abstain-to-Agent. When an endpoint exists, the
call obeys the payload discipline from :mod:`.base` (query + facts + cards in,
{capability_id, confidence, arguments} out — the same contract the online
backend serves, so the two are interchangeable above this module).

Wire: OpenAI-compatible — ``chat_judge_local_url`` is a BASE url such as
``http://localhost:18090/v1`` (the compose ``model-a`` service). The bespoke
{system_prompt,prompt} protocol this file used to speak matched no real
server, and定型 it now costs nothing because there is no deployed consumer
to migrate.
"""
from __future__ import annotations

import json

import httpx

from .base import SYSTEM, JudgeUnavailable, build_prompt


async def judge(query: str, candidates, entries_by_id: dict, *,
                url: str, timeout: float = 2.0, facts=None) -> dict:
    """POST the minimal payload to ``{url}/chat/completions``, return
    {capability_id, confidence, arguments}.

    Raises JudgeUnavailable on any transport/deployment absence so the caller
    falls through to the next backend (a 5xx from the judge is an UNAVAILABLE,
    not a verdict — the Judge never fabricates one from its own failure).
    """
    if not url:
        raise JudgeUnavailable("no local judge deployed")
    from core.config import settings

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_prompt(query, candidates, entries_by_id,
                                                     facts=facts)},
        ],
        "temperature": 0.0,
        "max_tokens": 256,
    }
    # Provider swap lives in config: the model name (current default an
    # Ollama-served small tool-calling model) is forwarded when set and never
    # appears in the funnel's business logic.
    model = (settings.chat_judge_local_model or "").strip()
    if model:
        payload["model"] = model
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
