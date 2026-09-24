"""ToolIntentModel backend: the LOCAL provider — the Docker ``tool-intent`` service
(8.17's first choice, ms-level when warm).

Deployment (compose): the service speaks an OpenAI-compatible wire, so the
provider seam is two-way swappable — the inference backend (Ollama today,
vLLM/llama.cpp as drop-ins) and the model itself (current default configured
via ``chat_tool_intent_local_model``; no model name exists in this code). Nothing
deployed yet means the default ``chat_tool_intent_local_url=""`` ->
:class:`ToolIntentUnavailable`, which the ladder treats as fall-through to the
online provider — never an abstain-to-Agent. When an endpoint exists, the
call obeys the payload discipline from :mod:`.base` (query + facts + cards in,
{capability_id, confidence, arguments} out — the same contract the online
backend serves, so the two are interchangeable above this module).

Wire: OpenAI-compatible — ``chat_tool_intent_local_url`` is a BASE url such as
``http://localhost:18091/v1`` (the compose ``tool-intent`` service). The bespoke
{system_prompt,prompt} protocol this file used to speak matched no real
server, and定型 it now costs nothing because there is no deployed consumer
to migrate.
"""
from __future__ import annotations

import json
import re

import httpx

from .base import SYSTEM, ToolIntentUnavailable, build_prompt

MODES = ("prompt_json", "tools")


def _param_schema(entry) -> dict:
    """Registry canonical parameters -> an OpenAI function parameters schema.
    Sourced entirely from the Registry row (never string-guessed): slot
    types/required/limits/description all come from ``entry.parameters``."""
    props, required = {}, []
    for name, spec in (getattr(entry, "parameters", None) or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        prop = {"type": str(spec.get("type") or "string")}
        desc = str(spec.get("description") or "").strip()
        if desc:
            prop["description"] = desc
        if spec.get("max_len") is not None:
            prop["maxLength"] = int(spec["max_len"])
        props[name] = prop
        if spec.get("required"):
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def _tools_for(candidates, entries_by_id: dict) -> list[dict]:
    """One native tool per candidate: ``name`` = Registry capability_id (so the
    model's function choice IS the capability choice — no tool-name/cap-id
    confusion), ``parameters`` = that capability's Registry schema."""
    tools = []
    for cand in candidates:
        entry = entries_by_id.get(cand.capability_id)
        if entry is None:
            continue
        tools.append({"type": "function", "function": {
            "name": entry.capability_id,
            "description": entry.description or "",
            "parameters": _param_schema(entry),
        }})
    return tools


# ── pinned decoding (Phase-3 ruling): identical for both benchmark arms ─────────
# Fixed seed is for BENCHMARK REPRODUCIBILITY, not a promise of absolute model
# determinism — a small non-thinking model can still vary; that variance is what
# we now hold constant across A and B so any measured difference is attributable
# to the model, not to sampling.
DECODE_TOP_P = 1.0
DECODE_SEED = 42
DECODE_MAX_TOKENS = 128


def build_payload(query: str, candidates, entries_by_id: dict, *,
                  facts=None, mode: str = "prompt_json") -> dict:
    """The ONE request builder for the local provider — production node and the
    benchmark both call this, so they share SYSTEM, the card assembly, the
    candidate ORDER, the tool definitions and every generation parameter. The
    benchmark may add ``stream`` (observation only) on top of the returned dict;
    it must not change any other field.
    """
    from core.config import settings

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": build_prompt(query, candidates, entries_by_id,
                                                     facts=facts)},
        ],
        "temperature": 0.0,          # greedy
        "top_p": DECODE_TOP_P,       # pinned; no nucleus jitter (backend-supported)
        "seed": DECODE_SEED,         # pinned for reproducibility (backend-supported)
        "max_tokens": DECODE_MAX_TOKENS,
        # thinking off: Qwen3 served on OpenAI-compatible backends otherwise spends
        # the whole budget on reasoning and returns empty content; Ollama + vLLM
        # honor "none", plain non-thinking models ignore it.
        "reasoning_effort": "none",
    }
    # Provider swap lives in config only (compose sets the Ollama tag); the model
    # name never appears in the funnel's business logic.
    model = (settings.chat_tool_intent_local_model or "").strip()
    if model:
        payload["model"] = model
    if mode == "tools":
        tools = _tools_for(candidates, entries_by_id)
        if tools:
            # "auto" (not "required"): a no-tool turn is a legitimate abstention
            # the funnel routes to the Agent, not a forced mis-selection.
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
    return payload


async def model_reply(query: str, candidates, entries_by_id: dict, *,
                url: str, timeout: float | None = None, facts=None) -> dict:
    """POST the minimal payload to ``{url}/chat/completions``, return
    {capability_id, confidence, arguments}.

    Two Adapter-layer output disciplines (``chat_tool_intent_local_mode``):
      prompt_json — SYSTEM asks for a JSON reply; brace-parse it (today's wire).
      tools       — native function-calling for tool-tuned checkpoints; the
                    candidate set is sent as OpenAI tools and the reply is read
                    back from ``message.tool_calls``; a reply with no native
                    call gets a strict structured-Markdown fallback first
                    (:func:`_reply_from_markdown_tool`). Both yield the SAME
                    reply shape, so ``_verdict_from_reply`` (candidate-set
                    membership + the confidence floor) stays the correctness
                    gate unchanged — a function name outside the candidate set
                    is still an off-card UNCERTAIN, never an auto-pass.

    Raises ToolIntentUnavailable on any transport/deployment absence so the caller
    falls through to the next backend (a 5xx from the model service is an UNAVAILABLE,
    not a verdict — the ToolIntentModel never fabricates one from its own failure).
    """
    if not url:
        raise ToolIntentUnavailable("no tool-intent model (local) deployed")
    from core.config import settings

    mode = (getattr(settings, "chat_tool_intent_local_mode", "prompt_json")
            or "prompt_json").strip().lower()
    if mode not in MODES:
        mode = "prompt_json"

    if timeout is None:
        # same per-call guardrail the online seam obeys (4s default, inside the
        # 5s cascade budget) — a local slower than the guardrail is an
        # UNAVAILABLE fall-through, never a hang.
        timeout = settings.chat_tool_intent_timeout_seconds
    # SINGLE source of truth for the request: build_payload() is what the
    # production node sends AND what the benchmark streams for timing, so the
    # benchmark can never diverge from production decoding/tools/params.
    payload = build_payload(query, candidates, entries_by_id, facts=facts, mode=mode)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url.rstrip("/") + "/chat/completions", json=payload)
            resp.raise_for_status()
            body = resp.json()
        message = body["choices"][0]["message"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise ToolIntentUnavailable(f"tool-intent model (local) unreachable/bad reply: {exc!r}") from exc

    if mode == "tools":
        return _reply_from_tool_call(message, candidates, entries_by_id)
    text = str(message.get("content") or "")
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, KeyError) as exc:
        raise ToolIntentUnavailable(f"tool-intent model (local) malformed reply: {exc!r}") from exc
    if not isinstance(data, dict):
        raise ToolIntentUnavailable("tool-intent model (local) reply not an object")
    return data


# The model's structured Markdown fallback that llama.cpp-CPU emission shows when
# the native tool-call token loses the first-token argmax (observed verbatim on
# qwen3-tools Q5_K_M under the pinned decoding):
#
#   ### <capability_id>
#   tool: <tool_name>            <- optional (hit-folder-2 omits it)
#   arguments: <single-line JSON object>
#   confidence: <number>
#
# The SEMANTIC result is already correct in that shape (right capability, right
# arguments, right confidence) — only the wire format differs, so this is an
# Adapter parsing/normalization concern, never a re-ask or an Agent hand-off.
# The match is deliberately strict: it must be the WHOLE reply (any leading or
# trailing prose disqualifies it), so ordinary reasoning text can never be
# mistaken for a tool call.
_MD_TOOL_REPLY = re.compile(
    r"#{2,3}[ \t]*(?P<cap>\S+)[ \t]*\n"
    r"(?:[ \t]*tool:[ \t]*(?P<tool>\S+)[ \t]*\n)?"
    r"[ \t]*arguments:[ \t]*(?P<args>\{[^{}\n]*\})[ \t]*\n"
    r"[ \t]*confidence:[ \t]*(?P<conf>\d+(?:\.\d+)?)[ \t]*"
)


def _reply_from_markdown_tool(content, entries_by_id: dict | None) -> dict | None:
    """Normalize the structured Markdown fallback into the SAME internal reply
    shape the native path yields ({capability_id, confidence, arguments}).
    Returns None (= not a tool-call) unless every structural condition holds:
    whole-string match, arguments parses as a JSON object, and — when the
    Registry is available and a ``tool:`` line was emitted — the capability_id
    is registered and its tool_binding matches. Candidate-set membership and
    the confidence floor stay the production gate's job downstream (an off-card
    id is still an UNCERTAIN here, never an auto-pass)."""
    m = _MD_TOOL_REPLY.fullmatch((content or "").strip())
    if m is None:
        return None
    try:
        args = json.loads(m.group("args"))
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    cap = m.group("cap").strip()
    tool = (m.group("tool") or "").strip()
    if entries_by_id is not None and tool:
        entry = entries_by_id.get(cap)
        if entry is None or str(getattr(entry, "tool_binding", "") or "").strip() != tool:
            return None
    try:
        confidence = float(m.group("conf"))
    except (TypeError, ValueError):  # pragma: no cover - regex already numeric
        return None
    return {"capability_id": cap, "confidence": confidence, "arguments": args}


def _reply_from_tool_call(message: dict, candidates, entries_by_id: dict | None = None) -> dict:
    """Translate a native tool-call into the shared reply shape.

    ``confidence`` here is the PROVIDER's "a structured selection was made"
    signal, NOT a calibrated model probability (a discrete tool-call is binary).
    It does not bypass correctness: ``_verdict_from_reply`` checks the name
    against the candidate set FIRST (off-card -> UNCERTAIN), and the Binder still
    validates the arguments. A reply with no tool-call first gets the strict
    structured-Markdown fallback (same internal shape, same gate downstream);
    only if that too misses is it a refusal (NONE) -> REJECT -> Agent;
    tool-arguments that will not parse are an UNAVAILABLE (the provider served
    nothing usable), never a fabricated verdict.
    """
    calls = message.get("tool_calls") or []
    if not calls:
        md = _reply_from_markdown_tool(message.get("content"), entries_by_id)
        if md is not None:
            return md
        return {"capability_id": "NONE", "confidence": 0.0, "arguments": None}
    fn = calls[0].get("function") or {}
    name = str(fn.get("name") or "").strip()
    if not name:
        raise ToolIntentUnavailable("tool-intent model (local) tool_call with no name")
    raw_args = fn.get("arguments")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError as exc:
            raise ToolIntentUnavailable(f"tool-intent model (local) bad tool arguments: {exc!r}") from exc
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    if not isinstance(args, dict):
        args = {}
    return {"capability_id": name, "confidence": 1.0, "arguments": args}
