"""Generic structured-LLM engine for the Presentation Brief workflow.

Additive sibling of :mod:`.passes`: same hard-won contract (stream accumulate →
deterministic wire-slip repair → jsonschema → Pydantic + semantic extra_check →
condensed corrective retry → loud fail after attempts), re-pointed at the new
IR (schema.py) and extended with multimodal input (``images=``) for the visual
understanding pass. The legacy DeckSpec engine keeps its own copy untouched
until every consumer migrates (refactor-boundary doctrine).

Every wire-slip repaired here is *mechanical* (quoting/typo/null/enum-head);
semantics are never altered and numbers are never invented.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Optional

from pydantic import ValidationError

from core.config import settings

from ..errors import GenerationError
from ..outputs import extract_json, validate
from . import prompts as P

logger = logging.getLogger(__name__)

RETRIES = 2               # corrective retries per call (3 attempts total)

# fields that a model may emit as {"start":N,"end":M} for a line range
_RANGE_KEYS = ("lines",)


# ── LLM call (mirrors passes._complete_json, plus images) ─────────────────────

async def complete_json(llm, prompt: str, system: str,
                        timeout: float | None = None,
                        usage_out: dict | None = None,
                        images: list[str] | None = None) -> dict:
    """JSON mode when available, tolerant parse otherwise.

    ``images`` (data-URLs) is probed kwarg-style so older/fake transports keep
    working; a transport that rejects image parts mid-call raises and the caller
    decides the degradation policy (visual pass → skip understanding, keep asset).
    """
    fn = getattr(llm, "complete_json", None)
    if fn is not None:
        try:
            return await fn(prompt, system, timeout=timeout, usage_out=usage_out,
                            images=images)
        except TypeError:
            try:
                return await fn(prompt, system, timeout=timeout, usage_out=usage_out)
            except TypeError:
                return await fn(prompt, system, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - best-effort, fall back
            logger.info("complete_json unavailable (%s); falling back to parse", exc)
    raw = await llm.complete(prompt, system, timeout=timeout)
    data = extract_json(raw)
    if data is None:
        raise GenerationError("model response was not valid JSON")
    return data


def pyd_errors(exc: ValidationError) -> list[str]:
    return [f"{'->'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}"
            for e in exc.errors()]


def check_model(d: dict, model_cls) -> tuple[list[str], Any]:
    """schema-validated dict → Pydantic load; returns (errors, model | None)."""
    try:
        return [], model_cls.model_validate(d)
    except ValidationError as exc:
        return pyd_errors(exc), None


# ── stats (same shape as the shipped deck_stats) ──────────────────────────────

def stat(stats: dict | None, label: str) -> dict | None:
    if stats is None:
        return None
    return stats.setdefault(label, {"calls": 0, "rejected": 0, "llm_seconds": 0.0,
                                    "prompt_tokens": 0, "completion_tokens": 0,
                                    "repairs": []})


def record_attempt(stats, label, t0, usage, *, rejected: bool) -> None:
    st = stat(stats, label)
    if st is None:
        return
    st["calls"] += 1
    st["rejected"] += 1 if rejected else 0
    st["llm_seconds"] = round(st["llm_seconds"] + (time.perf_counter() - t0), 1)
    st["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    st["completion_tokens"] += usage.get("completion_tokens", 0) or 0


def bump_rejected(stats, label) -> None:
    st = stat(stats, label)
    if st is not None:
        st["rejected"] += 1


def record_repairs(stats, label, repairs: list[str]) -> None:
    st = stat(stats, label)
    if st is not None:
        st["repairs"].extend(repairs)


def log_stats_summary(deck_id: str, stats: dict, wall_s: float) -> None:
    if stats is None:
        return
    tot_c = sum(v["calls"] for v in stats.values())
    tot_in = sum(v["prompt_tokens"] for v in stats.values())
    tot_out = sum(v["completion_tokens"] for v in stats.values())
    logger.info("BRIEF STATS %s total: llm_calls=%d prompt_tokens=%d "
                "completion_tokens=%d generate_wall=%.1fs",
                deck_id, tot_c, tot_in, tot_out, wall_s)
    logger.info("BRIEF STATS %s detail: %s", deck_id,
                json.dumps(stats, ensure_ascii=False))


# ── wire-slip repair (mechanical only, never semantic) ────────────────────────

_NUMERIC_STR_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_TOO_LONG_RE = re.compile(r"^(?P<path>[^:]+): \[.*\] is too long$", re.DOTALL)

# known enum field → closed value list (single source: schema.py, no drift vs prompts)
def _enum_table() -> dict[str, list[str]]:
    from . import schema as S
    return {
        "document_role": [e.value for e in S.DocumentRole],
        "structure_type": [e.value for e in S.StructureType],
        "epistemic_type": [e.value for e in S.EpistemicType],
        "grammar": [e.value for e in S.VisualGrammar],
        "policy": [e.value for e in S.VisualGenerationPolicy],
        "presentation_worth": [e.value for e in S.PresentationWorth],
        "type": [e.value for e in S.VisualAssetType],
        "visual_type_detected": [
            "ARCHITECTURE_DIAGRAM", "FLOWCHART", "BENCHMARK_PLOT",
            "SYSTEM_TAXONOMY", "METAPHOR_ILLUSTRATION"],
    }


_ENUM_TABLE: dict[str, list[str]] = {}


def _norm_enum(v, allowed: list[str]) -> Optional[str]:
    """Unique enum member v denotes; None when ambiguous/unknown (leave to validation)."""
    if not isinstance(v, str):
        return None
    head = re.split(r"[:：]", v, maxsplit=1)[0].strip().lower()
    head = re.sub(r"[\s\-]+", "_", head)
    cands = [a for a in allowed if head == a.lower() or head.startswith(a.lower() + "_")]
    if len(cands) == 1:
        return cands[0]
    upper_cands = [a for a in allowed if head.upper() == a or head.upper().startswith(a + "_")]
    return upper_cands[0] if len(upper_cands) == 1 else None


# optional fields whose null is a slip (absent is valid)
_NULLABLE_DROP = (
    "subtitle", "bbox", "nearby_text", "semantic_hint", "source_excerpt",
    "start_line", "end_line", "page", "message_id", "messageId",
    "locator", "problem_motivation", "solution_approach", "visual_potential",
    "internal_topology", "reuse_asset_id", "generation_spec", "unit", "metric_highlight",
    "recommended_grammar", "user_guidance",
)

# numeric-looking string slips: model quotes numbers (Metric.value, axis values)
_NUMBER_FIELDS = ("value", "y", "x0", "y0", "x1", "y1")


def _walk(node, fix):
    if isinstance(node, dict):
        fix(node)
        for v in node.values():
            _walk(v, fix)
    elif isinstance(node, list):
        for v in node:
            _walk(v, fix)


def repair_wire_slips(data: Any, events: list[str] | None = None) -> Any:
    """In-place deterministic repair for Brief-pass output shapes.

    Repairs (all mechanical): typo ``provisionance``→``provenance``; enum head
    normalization against the closed lists in schema.py; ``{"start":N,"end":M}``
    locator objects → ``start_line``/``end_line`` ints; quoted numbers → numbers;
    nulls on optional fields dropped; bare int ``value`` lists in generation_spec
    left alone (validated downstream).
    """
    if not isinstance(data, dict):
        return data
    global _ENUM_TABLE
    if not _ENUM_TABLE:
        _ENUM_TABLE = _enum_table()

    def fix(node: dict) -> None:
        if "provisionance" in node and "provenance" not in node:
            node["provenance"] = node.pop("provisionance")
            if events is not None:
                events.append("typo provisionance->provenance")
        for field, allowed in _ENUM_TABLE.items():
            if field in node:
                fixed = _norm_enum(node[field], allowed)
                if fixed is not None and fixed != node[field]:
                    if events is not None:
                        events.append(f"enum {field}: {node[field][:40]!r} -> {fixed}")
                    node[field] = fixed
        # locator range object → explicit start/end fields
        for key in _RANGE_KEYS:
            v = node.get(key)
            if isinstance(v, dict) and ("start" in v or "end" in v):
                s, e = v.get("start"), v.get("end")
                if isinstance(s, (int, str)) and str(s).isdigit():
                    node["start_line"] = int(s)
                    if isinstance(e, (int, str)) and str(e).isdigit() and int(e) != int(s):
                        node["end_line"] = int(e)
                    node.pop(key)
                    if events is not None:
                        events.append(f"locator {key}{{start,end}} -> start_line/end_line")
        for f in _NULLABLE_DROP:
            if f in node and node[f] is None:
                node.pop(f)
        for f in _NUMBER_FIELDS:
            v = node.get(f)
            if f == "value" and (
                    "name" not in node and "metric" not in node):
                continue                      # only Metric.value / point y coerce
            if isinstance(v, str) and _NUMERIC_SAFE.match(v.strip().replace(",", "")):
                s = v.strip().replace(",", "")
                node[f] = float(s) if "." in s else int(s)
                if events is not None:
                    events.append(f"quoted number {f}={v!r}")
        bbox = node.get("bbox")
        if isinstance(bbox, list):
            node["bbox"] = [float(x) if isinstance(x, (int, float)) else x for x in bbox]

    _walk(data, fix)
    return data


_NUMERIC_SAFE = re.compile(r"^-?\d+(?:\.\d+)?$")


def condense_errors(errors: list[str]) -> list[str]:
    """Rewrite jsonschema's giant payload dumps into short actionable advice."""
    out = []
    for e in errors:
        m = _TOO_LONG_RE.match(e)
        if m:
            path = m.group("path")
            if path.endswith("cards"):
                out.append(f"{path}: too many cards — keep at most 4 "
                           "(HARD maximum 4); merge related ones.")
            else:
                out.append(f"{path}: array is too long — keep ONLY the most "
                           "decision-relevant items; drop the least important ones until "
                           "the array fits within the allowed maximum.")
        elif "is not of type 'number'" in e:
            out.append(e + " — emit a bare JSON number with no quotes (13.7, not \"13.7\"); "
                       "if the source gives a range or approximation, record one metric "
                       "per endpoint or omit it — never force a number.")
        elif len(e) > 240:
            out.append(e[:240] + "… (truncated)")
        else:
            out.append(e)
    return out


# ── the engine ────────────────────────────────────────────────────────────────

ExtraCheck = Callable[[dict], tuple[list[str], Any]]


async def structured_call(llm, *, prompt: str, system: str, schema: dict,
                          extra_check: Optional[ExtraCheck] = None,
                          label: str,
                          timeout: float | None = None,
                          call_timeout: float | None = None,
                          images: list[str] | None = None,
                          stats: dict | None = None) -> tuple[Any, dict]:
    """One LLM call with corrective retries; returns (loaded_or_dict, raw_data).

    ``extra_check(data) -> (errors, loaded)`` runs after jsonschema; ``timeout``
    bounds a single attempt (a timed-out call retries in isolation); stats uses
    the shipped deck_stats shape.
    """
    current = prompt
    last_errs: list[str] = []
    for attempt in range(RETRIES + 1):
        t0 = time.perf_counter()
        usage: dict = {}
        try:
            coro = complete_json(llm, current, system, timeout=call_timeout,
                                 usage_out=usage, images=images)
            data = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except TimeoutError:
            errors = condense_errors(
                [f"model response timed out after {timeout:.0f}s — produce the complete "
                 "JSON now, staying inside every stated budget"])
            last_errs = errors
            logger.info("%s attempt %d timed out", label, attempt + 1)
            record_attempt(stats, label, t0, usage, rejected=True)
            current = P.corrective_retry_prompt(errors, prompt)
            continue
        repairs: list[str] = []
        data = repair_wire_slips(data, repairs)
        record_attempt(stats, label, t0, usage, rejected=False)
        errors = validate(schema, data)
        loaded = None
        if not errors and extra_check is not None:
            errors, loaded = extra_check(data)
        if not errors:
            if repairs and stats is not None:
                record_repairs(stats, label, repairs)
            return (loaded if loaded is not None else data), data
        errors = condense_errors(errors)
        last_errs = errors
        logger.info("%s attempt %d rejected: %s", label, attempt + 1, errors[:3])
        bump_rejected(stats, label)
        if repairs and stats is not None:
            record_repairs(stats, label, repairs)
        current = P.corrective_retry_prompt(errors, prompt)
    raise GenerationError(f"{label} failed after {RETRIES + 1} attempts: "
                          + "; ".join(last_errs[:6]))


def default_call_timeout() -> float:
    return settings.toolkit_llm_timeout_s
