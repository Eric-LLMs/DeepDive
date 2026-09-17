"""SLIDE_PATCH — single-slide diff repair for the direct engine (new node).

The legacy chain repairs one bad slide by echoing the COMPLETE brief through
the model (the field-lock existed only because the whole deck rode the wire —
and every sibling could still drift). Here the model only ever sees the ONE
problem slide plus the trace nodes it cites and the source excerpt behind it,
and replies with only that slide; the server merges the diff. Siblings cannot
drift because they are not in the request.

Budget (总令 §8.5 spirit): at most :data:`MAX_SLIDE_PATCHES` model calls per
slide; exhaustion raises :class:`GenerationError` — a deck never ships quietly
broken. The reply rides the same structured engine as everything else
(mechanical wire-slip repair + jsonschema + condensed corrective retry).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..errors import GenerationError
from ..outputs import validate
from . import prompts as P
from . import schema as S
from . import structured as ST

logger = logging.getLogger(__name__)

MAX_SLIDE_PATCHES = 2          # model calls per slide (incl. the corrective retry)


def _slide_json(slide: S.SlidePlan) -> str:
    return json.dumps(slide.model_dump(mode="json"), ensure_ascii=False)


def _cited_trace_nodes(brief: S.PresentationBrief, slide: S.SlidePlan) -> dict:
    ids = {c.trace_id for c in slide.cards} | {
        t for t, n in brief.traceability_graph.items() if slide.slide_index in n.slide_ids}
    return {t: brief.traceability_graph[t].model_dump(mode="json")
            for t in ids if t in brief.traceability_graph}


def _source_excerpt(sections: list[dict], slide: S.SlidePlan) -> str:
    cited = [s for s in sections if s["section_id"] in slide.source_section_ids]
    if not cited:                              # cite-all keeps the patch honest
        cited = sections
    return json.dumps(cited, ensure_ascii=False)


def _merge(brief: S.PresentationBrief, patch: dict,
           target: int) -> tuple[list[str], S.PresentationBrief | None]:
    """Apply the diff locally: replace slide ``target``, ADD new trace nodes only
    (editing/removing existing nodes is a contract violation → rejected)."""
    errs, new_slide = ST.check_model(patch["slide"], S.SlidePlan)
    if errs:
        return errs, None
    if new_slide.slide_index != target:
        return [f"slide_index must stay {target} (got {new_slide.slide_index})"], None
    nodes, nerr = {}, []
    for tid, raw in (patch.get("new_trace_nodes") or {}).items():
        e, m = ST.check_model(raw, S.TraceabilityNode)
        if e:
            nerr.extend(e)
            continue
        if m.trace_id != tid:
            nerr.append(f"trace node key {tid!r} != trace_id {m.trace_id!r}")
            continue
        nodes[tid] = m
    if nerr:
        return nerr, None
    old_ids = set(brief.traceability_graph)
    clash = sorted(old_ids & set(nodes))
    if clash:
        return [f"new_trace_nodes may not restate existing ids: {clash}"], None

    dump = brief.model_dump(mode="json")
    dump["slides"] = [s if s["slide_index"] != target else patch["slide"]
                      for s in dump["slides"]]
    dump["traceability_graph"].update(
        {t: n.model_dump(mode="json") for t, n in nodes.items()})
    return ST.check_model(dump, S.PresentationBrief)


async def patch_slide(llm: Any, brief: S.PresentationBrief, target: int,
                      issues: list[str], sections: list[dict],
                      controls: S.PresentationControls, *,
                      stats: dict | None = None) -> S.PresentationBrief:
    """Repair one slide with ≤ MAX_SLIDE_PATCHES model calls; returns the merged
    brief or raises. The reply contract is the slide diff — the rest of the deck
    is never sent and never rewritten."""
    slide = brief.slide_map().get(target)
    if slide is None:
        raise GenerationError(f"SLIDE_PATCH: slide {target} not in the brief")
    prompt = P.slide_patch_prompt(
        _slide_json(slide),
        json.dumps(_cited_trace_nodes(brief, slide), ensure_ascii=False),
        issues, _source_excerpt(sections, slide), controls)
    system = P.slide_patch_system()
    label = f"D/patch_{target}"

    current = prompt
    last_errs: list[str] = []
    for attempt in range(MAX_SLIDE_PATCHES):
        usage: dict = {}
        t0 = time.perf_counter()
        try:
            data = await ST.complete_json(llm, current, system,
                                          timeout=ST.default_call_timeout(),
                                          usage_out=usage)
        except Exception as exc:  # noqa: BLE001 - one honest record, then out
            ST.record_attempt(stats, label, t0, usage, rejected=True)
            raise GenerationError(f"SLIDE_PATCH slide {target}: LLM call failed: {exc}") \
                from exc
        events: list[str] = []
        ST.repair_wire_slips(data, events)
        ST.record_attempt(stats, label, t0, usage, rejected=False)
        if events and stats is not None:
            ST.record_repairs(stats, label, events)
        errs = validate(P.BRIEF_SCHEMAS["slide_patch"], data)
        merged: S.PresentationBrief | None = None
        if not errs:
            errs, merged = _merge(brief, data, target)
        if not errs and merged is not None:
            logger.info("SLIDE_PATCH slide %d repaired (attempt %d)", target, attempt + 1)
            return merged
        last_errs = ST.condense_errors(errs)
        ST.bump_rejected(stats, label)
        logger.info("SLIDE_PATCH slide %d attempt %d rejected: %s",
                    target, attempt + 1, last_errs[:3])
        current = P.corrective_retry_prompt(last_errs, prompt)
    raise GenerationError(
        f"SLIDE_PATCH slide {target} failed after {MAX_SLIDE_PATCHES} attempts: "
        + "; ".join(last_errs[:6]))
