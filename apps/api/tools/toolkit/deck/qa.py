"""Layer-1 content/epistemic QA + the bounded repair matrix (总令 §8, plan §M1.8).

The gate suite is PURE CODE — zero LLM, zero search (the closed-world invariant):
epistemic ledger integrity, number traceability (speaker notes and chart values),
budget re-checks, a geometry dry-run against the Visual Compiler, template
diversity and the anchor ratio. Fallback degradations and soft quality bars
surface as warnings (logged, never blocking — ≥80% anchors is a quality goal,
not a quota; fabricating a figure to meet it is the worse failure, §9.4).

When gates find ERRORS the repair matrix escalates MINIMALLY:

* graph/global defects      → one full re-synthesize (≤1, §8.5);
* slide content/geometry    → repair_slide_plan per slide (≤2, siblings locked);
* speaker-note numbers      → repair_speaker_notes per slide (≤2, field-locked to
  the notes themselves — everything else must come back byte-identical, §9.5);
* overflow / rematerialize  → 0 LLM: the compiler's tier fit raises into the
  geometry gate above, and Typst re-emission is already deterministic (§1.2.6).

Exhausted budgets raise :class:`GenerationError` — a deck never ships quietly
broken. Repairs ride the same structured engine as synthesis (wire-slip repair,
jsonschema, condensed corrective retry), so a field-lock violation feeds the
model its own mistake instead of silently accepting drift.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..errors import GenerationError
from . import prompts as P
from . import schema as S
from . import structured as ST
from .compiler import layout_engine as LE
from .errors import DeckLayoutError

logger = logging.getLogger(__name__)

# kinds: "plan" (slide content/geometry) | "notes" (speaker notes) |
#        "graph" (traceability ledger) | "global" (deck-level structure)
KINDS = ("plan", "notes", "graph", "global")


@dataclasses.dataclass(frozen=True)
class Issue:
    kind: str
    message: str
    slide_index: int | None = None

    def __str__(self) -> str:
        where = f"slide {self.slide_index}: " if self.slide_index is not None else ""
        return f"[{self.kind}] {where}{self.message}"


@dataclasses.dataclass
class QAReport:
    errors: list[Issue] = dataclasses.field(default_factory=list)
    warnings: list[Issue] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fmt(issues: list[Issue], limit: int = 6) -> str:
    return "; ".join(str(i) for i in issues[:limit])


# ── number traceability helpers ───────────────────────────────────────────────

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
SMALL_INT_MAX = 12          # enumerations / section refs, not claimed quantities


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s).lstrip("0")
    return d or "0"


def _forms(tok: str) -> set[str]:
    """Match forms of a numeric token: 56.5 / 0.565 / 565% anchor each other."""
    base = tok.replace(",", "").strip()
    pct = base.endswith("%")
    core = base.rstrip("%")
    out = {_digits(core)}
    try:
        f = float(core)
    except ValueError:
        return out
    if f:
        alt = f / 100.0 if pct else (f * 100.0 if 0 < abs(f) < 1 else None)
        if alt is not None:
            out.add(_digits(f"{alt:.10g}"))
    return out


def _grounded_forms(brief: S.PresentationBrief) -> set[str]:
    """Every number the deck itself can vouch for (never the text under test)."""
    parts: list[str] = [brief.thesis]
    for s in brief.slides:
        parts += [s.title, s.subtitle or "", s.central_message,
                  s.visual_spec.semantic_intent]
        for c in s.cards:
            parts += [c.label, c.takeaway, c.metric_highlight or ""]
        gen = s.visual_spec.generation_spec or {}
        parts += [str(x) for x in (gen.get("labels") or []) if isinstance(x, str)]
    for node in brief.traceability_graph.values():
        parts.append(node.statement)
    text = "\n".join(parts)
    out: set[str] = set()
    for tok in _NUM_RE.findall(text):
        out |= _forms(tok)
    return out


# ── the Layer-1 gate suite (pure code) ────────────────────────────────────────

def run_qa_suite(brief: S.PresentationBrief, *,
                 assets: dict[str, S.VisualAsset] | None = None,
                 valid_sections: set[str] | None = None) -> QAReport:
    """All Layer-1 gates in one pass. ``assets``/``valid_sections`` are the
    grounding context (DocumentRepresentation map / model section ids); gates
    that need them degrade to no-ops when the caller withholds them."""
    report = QAReport()
    err: Callable[[str, str, int | None], None] = \
        lambda kind, msg, idx=None: report.errors.append(Issue(kind, msg, idx))
    warn: Callable[[str, str, int | None], None] = \
        lambda kind, msg, idx=None: report.warnings.append(Issue(kind, msg, idx))
    graph = brief.traceability_graph

    # 1. epistemic ledger (defense-in-depth: the model validators cover this for
    #    freshly-validated briefs; QA also guards briefs loaded from disk).
    for tid, node in graph.items():
        et = node.epistemic_type
        if et == S.EpistemicType.FACT and node.locator is None:
            err("graph", f"FACT node {tid!r} carries no SourceLocator", None)
        if et == S.EpistemicType.GROUNDED_SYNTHESIS:
            if not node.supporting_facts:
                err("graph", f"GROUNDED_SYNTHESIS node {tid!r} has no supporting_facts")
            for sid in node.supporting_facts:
                dep = graph.get(sid)
                if dep is None:
                    err("graph", f"node {tid!r} rests on unknown trace id {sid!r}")
                elif dep.epistemic_type in (S.EpistemicType.GROUNDED_SYNTHESIS,
                                            S.EpistemicType.INTERPRETATION):
                    err("graph", f"node {tid!r} rests on {dep.epistemic_type.value} "
                                 f"{sid!r} — synthesis must bottom out in FACT/CLAIM")
    # 2. card grounding + budgets (budget caps re-checked, not trusted)
    for s in brief.slides:
        for c in s.cards:
            if c.trace_id not in graph:
                err("plan", f"card {c.label!r} stands on unknown trace id {c.trace_id!r}",
                    s.slide_index)
        for field, value, cap in (
                ("title", s.title, S.TITLE_MAX),
                ("central_message", s.central_message, S.CENTRAL_MESSAGE_MAX),
                *((f"cards[{i}].takeaway", c.takeaway, S.CARD_TAKEAWAY_MAX)
                  for i, c in enumerate(s.cards))):
            units = S.text_units(value)
            if not 1 <= units <= cap:
                err("plan", f"{field} is {units} units, budget is 1..{cap}", s.slide_index)
        if not s.source_section_ids:
            err("plan", "slide cites no source section (source_section_ids empty)",
                s.slide_index)
        if valid_sections is not None:
            bad = [sid for sid in s.source_section_ids if sid not in valid_sections]
            if bad:
                err("plan", f"source_section_ids not in the mental model: {bad}",
                    s.slide_index)
        rid = s.visual_spec.reuse_asset_id
        if rid and assets is not None:
            if rid not in assets:
                err("plan", f"reuse_asset_id {rid!r} is not a figure of this document",
                    s.slide_index)
            elif not Path(assets[rid].path).is_file():
                warn("plan", f"asset {rid!r} file is missing; the renderer will fall "
                             "back to cards loudly", s.slide_index)
        if rid and s.visual_spec.grammar not in (
                S.VisualGrammar.SOURCE_FIGURE_REUSE, S.VisualGrammar.ANNOTATED_FIGURE):
            # the schema bans this combination; the gate keeps the ban visible as a
            # slide-level error (patchable), never a silent drop at render time
            err("plan", f"reuse_asset_id {rid!r} paired with structural grammar "
                        f"{s.visual_spec.grammar.value}: figure reuse is legal only "
                        "on SOURCE_FIGURE_REUSE/ANNOTATED_FIGURE — promote the slide "
                        "to a figure slide or drop the asset", s.slide_index)
    # 3. number traceability
    grounded = _grounded_forms(brief)
    for s in brief.slides:
        for tok in _NUM_RE.findall(s.speaker_notes):
            if tok.isdigit() and int(tok) <= SMALL_INT_MAX:
                continue                      # enumerations are not claims
            if not (_forms(tok) & grounded):
                err("notes", f"speaker_notes number {tok!r} is not traceable to any "
                             "statement, metric or figure in this deck", s.slide_index)
        gen = s.visual_spec.generation_spec
        if s.visual_spec.policy == S.VisualGenerationPolicy.QUANTITATIVE_CODE and gen:
            for v in (gen.get("values") or []):
                if not (_forms(str(v)) & grounded):
                    err("plan", f"chart value {v!r} appears in no grounded statement; "
                                "anchor it or drop the chart", s.slide_index)
    # 4. geometry dry-run + explicit fallbacks (0 LLM, §1.2.6 compiler)
    layouts = []
    for p in brief.slides:
        try:
            lay = LE.build_slide_layout(p, graph, assets or {})
        except DeckLayoutError as exc:
            err("plan", f"geometry: {exc}", p.slide_index)
            continue
        layouts.append(lay)
        if lay.fallback_reason:
            warn("plan", f"fell back to {lay.template.value}: {lay.fallback_reason}",
                 p.slide_index)
    # 5. template diversity — hard only when the deck is big enough to honestly
    #    carry 3 grammars; small uniform decks get a warning instead.
    kinds = LE.layout_template_kinds(layouts)
    if len(brief.slides) >= 5 and len(kinds) < 3:
        err("global", f"template diversity {sorted(k.value for k in kinds)}: decks of "
                      "≥5 slides must use at least 3 layout templates")
    elif len(brief.slides) >= 3 and len(kinds) < 2:
        warn("global", "every slide shares one layout template")
    # 6. anchor ratio: quality goal (§8), never a hard quota.
    anchored = len(brief.slides_with_anchor()) / max(1, len(brief.slides))
    if anchored < 0.8:
        warn("global", f"anchor ratio {anchored:.0%} below the 80% quality goal")
    return report


# ── field-locked repair calls ─────────────────────────────────────────────────

MAX_SLIDE_REPAIRS = 2         # per slide, both flavors (总令 §8.5: 单页 ≤2 次)
MAX_RESYNTHESIZE = 1          # whole-deck rerun budget

_REPAIR_SYSTEM = (
    "You are repairing a REJECTED presentation brief. Change ONLY what the repair "
    "instruction allows; every other byte must be echoed back unchanged. Budgets are "
    "hard (title/central_message/takeaway unit caps, at most 4 cards). Numbers are "
    "the strict currency: a figure may only appear where a traceability statement "
    "vouches for it — otherwise add a FACT node with a locator copied from the model, "
    "or drop the number. " + P._GROUNDING_RULES
    + "Reply with the single complete brief JSON only."
)


def _brief_json(brief: S.PresentationBrief) -> str:
    return json.dumps(brief.model_dump(mode="json"), ensure_ascii=False)


def _lock_problems(orig: S.PresentationBrief, new: S.PresentationBrief, *,
                   target: int, notes_only: bool) -> list[str]:
    """Diff a repair reply against the field lock (§9.5: nothing else drifts)."""
    o, n = orig.model_dump(mode="json"), new.model_dump(mode="json")
    errs: list[str] = []
    for k in ("deck_id", "thesis", "target_audience", "target_slide_count",
              "presentation_style", "narrative_arc"):
        if o[k] != n[k]:
            errs.append(f"{k} must not change")
    osl = {s["slide_index"]: s for s in o["slides"]}
    nsl = {s["slide_index"]: s for s in n["slides"]}
    if set(osl) != set(nsl):
        errs.append("the slide set (count and slide_index values) must not change")
    for i, s in osl.items():
        t = nsl.get(i)
        if t is None:
            continue
        a, b = dict(s), dict(t)
        if i == target and notes_only:
            a.pop("speaker_notes", None)
            b.pop("speaker_notes", None)
        elif i != target:
            pass                                  # siblings: fully byte-identical
        if a != b and (i != target or notes_only):
            errs.append(f"slide {i} "
                        + ("may only change its speaker_notes"
                           if i == target else "must stay byte-identical"))
    if notes_only and o["traceability_graph"] != n["traceability_graph"]:
        errs.append("traceability_graph must not change in a notes-only repair")
    elif not notes_only:
        for tid, node in o["traceability_graph"].items():
            if n["traceability_graph"].get(tid) != node:
                errs.append(f"traceability node {tid!r} must not change "
                            "(add new ids only, never edit existing)")
    return errs


def make_brief_check(deck_id: str, valid_sections: set[str], known_assets: set[str]
                     ) -> ST.ExtraCheck:
    """Synthesis-gate extra_check: echo contract + closed-world ids. Shared by
    the SYNTHESIZE executor and the QA re-synthesize leg (single source)."""
    def check(data: dict) -> tuple[list[str], Any]:
        errs, model = ST.check_model(data, S.PresentationBrief)
        if model is not None:
            if model.deck_id != deck_id:
                errs.append(f"deck_id must echo {deck_id!r}")
            unknown = sorted({sid for s in model.slides
                              for sid in s.source_section_ids
                              if sid not in valid_sections})
            if unknown:
                errs.append(f"source_section_ids not in the mental model: {unknown}")
            misplaced = sorted({s.visual_spec.reuse_asset_id for s in model.slides
                                if s.visual_spec.reuse_asset_id
                                and s.visual_spec.reuse_asset_id not in known_assets})
            if misplaced:
                errs.append(f"reuse_asset_id not a figure of this document: {misplaced}")
        return errs, model
    return check


async def _repair(*, llm: Any, brief: S.PresentationBrief, target: int,
                  issues: list[Issue], controls: S.PresentationControls,
                  notes_only: bool, stats: dict | None) -> S.PresentationBrief:
    msgs = [str(i) for i in issues]
    prompt = (P.repair_notes_prompt if notes_only else P.repair_slide_prompt)(
        _brief_json(brief), target, msgs, controls)
    label = f"D-repair/notes_{target}" if notes_only else f"D-repair/{target}"

    def lock(data: dict) -> tuple[list[str], Any]:
        errs, model = ST.check_model(data, S.PresentationBrief)
        if model is not None:
            errs.extend(_lock_problems(brief, model, target=target,
                                       notes_only=notes_only))
        return errs, model

    loaded, _ = await ST.structured_call(
        llm, prompt=prompt, system=_REPAIR_SYSTEM,
        schema=P.BRIEF_SCHEMAS["brief"], extra_check=lock, label=label,
        call_timeout=ST.default_call_timeout(), stats=stats)
    return loaded


async def _resynthesize(*, llm: Any, deck_id: str,
                        model: S.GlobalMentalModel, report: QAReport,
                        doc_rep: S.DocumentRepresentation | None,
                        synth_prompt: str, synth_system: str,
                        stats: dict | None) -> S.PresentationBrief:
    known = {a.asset_id for a in doc_rep.visual_assets} if doc_rep else set()
    hint = P.corrective_retry_prompt(
        [str(i) for i in report.errors if i.kind in ("graph", "global")],
        synth_prompt)
    loaded, _ = await ST.structured_call(
        llm, prompt=hint, system=synth_system,
        schema=P.BRIEF_SCHEMAS["brief"],
        extra_check=make_brief_check(deck_id, set(model.section_map()), known),
        label="D-repair/resynth",
        call_timeout=ST.default_call_timeout(), stats=stats)
    return loaded


# ── the matrix ────────────────────────────────────────────────────────────────

async def ensure_brief_clean(*, llm: Any, deck_id: str,
                             brief: S.PresentationBrief,
                             controls: S.PresentationControls,
                             model: S.GlobalMentalModel | None = None,
                             doc_rep: S.DocumentRepresentation | None = None,
                             stats: dict | None = None,
                             synth_prompt: str = "",
                             synth_system: str = "") -> S.PresentationBrief:
    """Gate the brief; on errors run the minimal repair that can fix them; return
    a gate-clean brief or raise :class:`GenerationError` loudly.

    ``synth_prompt``/``synth_system`` are the original synthesis inputs (the
    executor carries them); they are required only if a graph/global defect
    triggers the one allowed full rerun.
    """
    assets = doc_rep.asset_map() if doc_rep else None
    valid = set(model.section_map()) if model else None
    report = run_qa_suite(brief, assets=assets, valid_sections=valid)
    if report.warnings:
        logger.info("QA %s: %s", deck_id, _fmt(report.warnings, 10))
    if report.ok:
        return brief

    if any(e.kind in ("graph", "global") for e in report.errors):
        if model is None or not synth_prompt:
            raise GenerationError(
                f"deck QA ({deck_id}): graph/global defects need the mental model for "
                "a re-synthesize pass, which was not provided: " + _fmt(report.errors))
        remaining = MAX_RESYNTHESIZE
        while remaining:
            remaining -= 1
            brief = await _resynthesize(
                llm=llm, deck_id=deck_id, model=model, report=report,
                doc_rep=doc_rep, synth_prompt=synth_prompt, synth_system=synth_system,
                stats=stats)
            report = run_qa_suite(brief, assets=assets, valid_sections=valid)
            if report.ok:
                return brief
            if not any(e.kind in ("graph", "global") for e in report.errors):
                break                            # rest is slide-level; fall through
        else:
            raise GenerationError(
                f"deck QA ({deck_id}): re-synthesize did not resolve graph/global "
                "defects: " + _fmt(report.errors))

    by_slide: dict[int, list[Issue]] = {}
    for e in report.errors:
        if e.kind in ("plan", "notes") and e.slide_index is not None:
            by_slide.setdefault(e.slide_index, []).append(e)
    if not by_slide:
        raise GenerationError(
            f"deck QA ({deck_id}): unrepairable defects: " + _fmt(report.errors))

    for idx, errs in sorted(by_slide.items()):
        for _attempt in range(MAX_SLIDE_REPAIRS):
            only_notes = all(e.kind == "notes" for e in errs)
            brief = await _repair(
                llm=llm, brief=brief, target=idx, issues=errs, controls=controls,
                notes_only=only_notes, stats=stats)
            report = run_qa_suite(brief, assets=assets, valid_sections=valid)
            errs = [e for e in report.errors if e.slide_index == idx]
            if not errs:
                break
        else:
            raise GenerationError(
                f"deck QA ({deck_id}): slide {idx} still failing after "
                f"{MAX_SLIDE_REPAIRS} repairs: " + _fmt(errs))
    if not report.ok:
        raise GenerationError(
            f"deck QA ({deck_id}): repairs did not converge: " + _fmt(report.errors))
    return brief
