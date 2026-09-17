"""Direct generation engine — the default ``slides`` path (promoted 2026-09-17).

One semantic LLM call designs the whole deck from the RAW extracted text; every
other step is deterministic local code. The Brief chain
(:mod:`.workflow_driver`, A/C + full-brief rewrite matrix) stays untouched and
selectable via ``settings.slides_generation_mode == "legacy"``.

Node vocabulary (same stage names as the legacy chain, new semantics; stats
labels keep the shipped letter families so one log vocabulary covers both
engines):

* ``A/text_local``    TEXT_UNDERSTAND — local conceptual-block packing only,
  zero LLM (full raw text goes to the one call; never a digest — big inputs
  beyond one-shot capacity fail the existing capacity check loudly).
* ``B/visual_skipped`` VISUAL_UNDERSTAND — not mounted this round (retained).
* ``D/synthesize``     SYNTHESIZE — the single semantic generation; failures
  reroll at most :data:`MAX_REROLLS` times with condensed error feedback.
* ``C/reduce_local``   REDUCE — pure local canonicalization: mechanical
  wire-slip repair + two loud enum rescues + schema/echo validation.
* ``D/patch_{i}``      SLIDE_PATCH — single-slide diff repair (new node,
  :mod:`.repair`); siblings never enter the model context.

QA gates and rendering are shared with the legacy engine (:mod:`.qa`,
:mod:`.render`); the canonical :class:`PresentationBrief` is the same contract.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import prompts as P
from . import qa as QA
from . import repair as REPAIR
from . import schema as S
from . import structured as ST
from ..errors import GenerationError
from ..outputs import validate

logger = logging.getLogger(__name__)

MAX_REROLLS = 2                 # extra generation calls after the first (总令: ≤2 重掷)
SECTION_PACK_BUDGET = 8000      # chars per conceptual block (same packing as Pass A)

STAGE_TEXT = "TEXT_UNDERSTAND"
STAGE_VISUAL = "VISUAL_UNDERSTAND"
STAGE_REDUCE = "REDUCE"
STAGE_SYNTH = "SYNTHESIZE"
STAGE_PATCH = "SLIDE_PATCH"

STATS_TEXT = "A/text_local"
STATS_VISUAL = "B/visual_skipped"
STATS_REDUCE = "C/reduce_local"
STATS_SYNTH = "D/synthesize"


def _local_stat(stats: dict | None, label: str, seconds: float) -> dict | None:
    """A local (zero-LLM) node entry: shipped stat shape with calls=0 plus the
    measured wall seconds."""
    if stats is None:
        return None
    st = ST.stat(stats, label)
    st["local_seconds"] = round(seconds, 2)
    return st


# ── TEXT_UNDERSTAND: deterministic local packing (replaces Pass A's universe) ─

def pack_sections_local(doc_rep: S.DocumentRepresentation,
                        *, budget: int = SECTION_PACK_BUDGET) -> list[dict[str, Any]]:
    """Pack TextBlocks into ``sec_i`` conceptual chunks under a char budget.

    Zero-LLM, byte-deterministic. The single call must cite these ids in
    ``source_section_ids`` and copy locators from the block headers — this is
    the address system the canonical brief's traceability graph references.
    """
    budget = max(500, budget)
    sections: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    size = 0

    def flush() -> None:
        if not current:
            return
        i = len(sections) + 1
        first, last = current[0]["locator"], current[-1]["locator"]
        sections.append({
            "section_id": f"sec_{i}",
            "page": first.get("page"),
            "start_line": first.get("start_line"),
            "end_line": last.get("end_line"),
            "blocks": current.copy(),
        })
        current.clear()

    for block in doc_rep.text_blocks:
        loc = block.locator.model_dump(mode="json")
        item = {"text": block.text, "locator": loc}
        if current and size + len(block.text) > budget:
            flush()
        current.append(item)
        size += len(block.text)
    flush()
    return sections


# ── REDUCE: local canonicalization (loud mechanical rescues, zero LLM) ───────

_POLICY_FOR_GRAMMAR = {
    "SOURCE_FIGURE_REUSE": "SOURCE_FIDELITY",
    "ANNOTATED_FIGURE": "SOURCE_FIDELITY",
}


def rescue_policy_from_grammar(data: dict, events: list[str]) -> None:
    """Cross-field wire slips the legacy chain fixed via full rewrites — derived
    locally here. (1) A VISUAL_GRAMMARS value written into ``policy`` is a field
    slip: derive the policy from the grammar via the canonical rule. Only ever
    touches a policy that is already schema-invalid. (2) QUANTITATIVE_CODE
    without a generation_spec cannot be drawn and fabricating values is
    forbidden: degrade the drawing MEDIUM to the template engine (grammar/intent
    untouched). Recorded loudly, never silent."""
    legal = {e.value for e in S.VisualGenerationPolicy}
    grammars = {e.value for e in S.VisualGrammar}
    for i, slide in enumerate(data.get("slides") or [], 1):
        spec = slide.get("visual_spec") if isinstance(slide, dict) else None
        if not isinstance(spec, dict):
            continue
        policy = spec.get("policy")
        if not isinstance(policy, str) or policy in legal:
            continue
        if policy not in grammars:
            continue
        grammar = spec.get("grammar")
        derived = _POLICY_FOR_GRAMMAR.get(
            grammar,
            "QUANTITATIVE_CODE" if (grammar == "DATA_CHART"
                                    and spec.get("generation_spec"))
            else "EXPLANATORY_DIAGRAM")
        events.append(f"policy {policy!r} is a grammar value -> derived "
                      f"{derived!r} from grammar {grammar!r} (slide {i})")
        spec["policy"] = derived
        if (derived == "QUANTITATIVE_CODE"
                and not isinstance(spec.get("generation_spec"), dict)):
            spec["policy"] = "EXPLANATORY_DIAGRAM"
            spec["generation_spec"] = None
            events.append(f"slide {i}: QUANTITATIVE_CODE without generation_spec "
                          "-> EXPLANATORY_DIAGRAM (no fabricated chart data)")


# ── the engine ────────────────────────────────────────────────────────────────

async def run_direct_generation(
    llm: Any,
    doc_rep: S.DocumentRepresentation,
    controls: S.PresentationControls,
    *,
    deck_id: str,
    stats_out: dict | None = None,
) -> S.PresentationBrief:
    """Produce the canonical brief with ONE semantic call (+ rerolls ≤2) plus the
    local compiler; fills ``stats_out`` in place (shipped deck_stats shape)."""
    stats = stats_out if stats_out is not None else {}
    t_run = time.perf_counter()

    # ── TEXT_UNDERSTAND (local packing only; the FULL raw text is what gets sent) ──
    t0 = time.perf_counter()
    sections = pack_sections_local(doc_rep)
    if not sections:
        raise GenerationError("direct slides: no text blocks after ingest — nothing to "
                              "present (check the source extracted as text)")
    st = _local_stat(stats, STATS_TEXT, time.perf_counter() - t0)
    if st is not None:
        st["note"] = "local conceptual-block packing, 0 LLM"
    if stats is not None:
        ST.stat(stats, STATS_VISUAL)["note"] = \
            "visual understanding not mounted this round"

    valid_sections = {s["section_id"] for s in sections}
    known_assets = {a.asset_id for a in doc_rep.visual_assets}
    directives = (P.language_rule(controls.language)
                  + P.format_rule(controls.format_mode))
    system = P.direct_brief_system(directives)
    base_prompt = P.direct_brief_prompt(
        deck_id,
        json.dumps({"document_title": doc_rep.document_title,
                    "doc_id": doc_rep.doc_id,
                    "sections": sections}, ensure_ascii=False),
        json.dumps([{"asset_id": a.asset_id, "page": a.page, "type": a.type.value,
                     "caption": a.semantic_hint or a.nearby_text or ""}
                    for a in doc_rep.visual_assets], ensure_ascii=False),
        controls)
    check = QA.make_brief_check(deck_id, valid_sections, known_assets)
    reduce_st = ST.stat(stats, STATS_REDUCE)

    # ── SYNTHESIZE (the one semantic call) + REDUCE (local gate) + reroll ──
    brief: S.PresentationBrief | None = None
    report: QA.QAReport | None = None
    current_prompt = base_prompt
    for attempt in range(MAX_REROLLS + 1):
        label = STATS_SYNTH if attempt == 0 else f"D/reroll_{attempt}"
        usage: dict = {}
        t0 = time.perf_counter()
        try:
            data = await ST.complete_json(llm, current_prompt, system,
                                          timeout=ST.default_call_timeout(),
                                          usage_out=usage)
        except GenerationError:
            ST.record_attempt(stats, label, t0, usage, rejected=True)
            raise
        except Exception as exc:  # transport/timeout: one honest attempt record, then out
            ST.record_attempt(stats, label, t0, usage, rejected=True)
            raise GenerationError(f"direct slides: LLM call failed: {exc}") from exc
        ST.record_attempt(stats, label, t0, usage, rejected=False)

        # REDUCE (local): mechanical slips + rescues, then schema → model+echo → gates
        t1 = time.perf_counter()
        events: list[str] = []
        ST.repair_wire_slips(data, events)
        rescue_policy_from_grammar(data, events)
        errs = validate(P.BRIEF_SCHEMAS["brief"], data)
        model: S.PresentationBrief | None = None
        if not errs:
            errs, model = check(data)
        if not errs and model is not None:
            brief = model
            report = QA.run_qa_suite(brief, assets=doc_rep.asset_map(),
                                     valid_sections=valid_sections)
            hard = [i for i in report.errors
                    if i.kind in ("graph", "global") or i.slide_index is None]
            if not hard:
                reduce_st["local_seconds"] = round(
                    reduce_st.get("local_seconds", 0.0)
                    + (time.perf_counter() - t1), 2)
                if events:
                    reduce_st["repairs"].extend(events)
                break                          # remaining errors are slide-level
            errs = [str(i) for i in hard]
        reduce_st["local_seconds"] = round(
            reduce_st.get("local_seconds", 0.0) + (time.perf_counter() - t1), 2)
        if events:
            reduce_st["repairs"].extend(events)
        errs = ST.condense_errors(errs)
        ST.bump_rejected(stats, label)
        logger.info("direct %s attempt %d rejected: %s", deck_id, attempt + 1, errs[:3])
        if attempt == MAX_REROLLS:
            raise GenerationError(
                f"direct slides ({deck_id}): SYNTHESIZE failed after "
                f"{MAX_REROLLS + 1} attempts: " + "; ".join(errs[:6]))
        current_prompt = P.corrective_retry_prompt(errs, base_prompt)

    assert brief is not None and report is not None  # loop only breaks with both set

    # ── SLIDE_PATCH: single-slide diff repairs for slide-level gate errors ──
    by_slide: dict[int, list[str]] = {}
    for e in report.errors:
        if e.kind in ("plan", "notes") and e.slide_index is not None:
            by_slide.setdefault(e.slide_index, []).append(str(e))
    for idx in sorted(by_slide):
        logger.info("direct %s slide %d patched for %d gate issue(s)",
                    deck_id, idx, len(by_slide[idx]))
        brief = await REPAIR.patch_slide(
            llm, brief, idx, by_slide[idx], sections, controls, stats=stats)
        report = QA.run_qa_suite(brief, assets=doc_rep.asset_map(),
                                 valid_sections=valid_sections)
    if report.errors:
        raise GenerationError(
            f"direct slides ({deck_id}): QA gates still failing: "
            + "; ".join(str(i) for i in report.errors[:6]))
    if report.warnings:
        logger.info("direct %s QA warnings: %s", deck_id,
                    "; ".join(str(i) for i in report.warnings[:10]))

    ST.log_stats_summary(deck_id, stats, time.perf_counter() - t_run)
    return brief
