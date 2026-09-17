"""Visual Compiler layout engine: PresentationBrief → template-resolved geometry.

The 14 ``VisualGrammar`` values map onto the 7 ``LayoutTemplate`` forms (§6.4),
each backed by one Typst function. Every degradation is EXPLICIT — the reason
rides along into :attr:`BriefSlideLayout.fallback_reason` and lands in
``RenderReport.layout_warnings``; an unsupported combination never vanishes
silently (§9.4).

Slot math MIRRORS :mod:`..layout` v1 exactly (shared ``_fit``/``wrap_lines``/
tiers, same CONTENT/HEADER/BODY constants), so the conservative measurement
that gated the old chain still gates the new one — and a block that cannot fit
even at micro raises :class:`~..errors.DeckLayoutError` (loud failure).

Input contract: the brief already passed schema + semantic gates, so this is
purely formatting. Content is never trimmed (except deterministic ellipsis on
chart-axis labels and thesis chips, which are display ornaments, not claims).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .. import schema as S
from ..errors import DeckLayoutError
from ..layout import BODY_H_MM, CONTENT_W_MM, HEADER_H_MM, TIERS, _fit
from .theme import ACADEMIC_LIGHT, ThemeTokens, _mix

# ── template vocabulary (§6.4) ────────────────────────────────────────────────

class LayoutTemplate(str, Enum):
    CENTERED_THESIS = "CENTERED_THESIS"
    FULL_WIDTH_ARCHITECTURE = "FULL_WIDTH_ARCHITECTURE"
    HORIZONTAL_FLOW = "HORIZONTAL_FLOW"
    QUADRANT_2X2 = "QUADRANT_2X2"
    DATA_DASHBOARD = "DATA_DASHBOARD"
    NATIVE_TABLE = "NATIVE_TABLE"
    SPLIT_TWO_COLUMN = "SPLIT_TWO_COLUMN"


G = S.VisualGrammar

# grammar → (template, typst fn) for the happy path
HAPPY: dict[G, tuple[LayoutTemplate, str]] = {
    G.HERO_METAPHOR: (LayoutTemplate.CENTERED_THESIS, "thesisSlide"),
    G.TEXTUAL_THESIS: (LayoutTemplate.CENTERED_THESIS, "thesisSlide"),
    G.SYSTEM_BLUEPRINT: (LayoutTemplate.FULL_WIDTH_ARCHITECTURE, "archSlide"),
    G.PIPELINE_FLOW: (LayoutTemplate.HORIZONTAL_FLOW, "flowSlide"),
    G.TIMELINE: (LayoutTemplate.HORIZONTAL_FLOW, "timelineSlide"),
    G.CIRCULAR_LOOP: (LayoutTemplate.HORIZONTAL_FLOW, "loopSlide"),
    G.INVERTED_PYRAMID: (LayoutTemplate.HORIZONTAL_FLOW, "funnelSlide"),
    G.DATA_CHART: (LayoutTemplate.DATA_DASHBOARD, "chartSlide"),
    G.SOURCE_FIGURE_REUSE: (LayoutTemplate.DATA_DASHBOARD, "figureSlide"),
    G.ANNOTATED_FIGURE: (LayoutTemplate.DATA_DASHBOARD, "figureSlide"),
    G.COMPARISON: (LayoutTemplate.NATIVE_TABLE, "compareSlide"),
    G.TABLE: (LayoutTemplate.NATIVE_TABLE, "tableSlide"),
    G.QUADRANT_MATRIX: (LayoutTemplate.QUADRANT_2X2, "cardsSlide"),
    G.STRUCTURED_CARDS: (LayoutTemplate.SPLIT_TWO_COLUMN, "cardsSlide"),
}


def resolve_layout_template(g: G, *, asset_ok: bool, chart_ok: bool,
                            n_cards: int) -> tuple[LayoutTemplate, str, str | None]:
    """Grammar + usability facts → (template, fn, fallback_reason).

    ``reason`` is non-None exactly when the grammar's happy template could not
    be honored — the caller must surface it, never swallow it.
    """
    def degrade(why: str) -> tuple[LayoutTemplate, str, str]:
        if n_cards > 0:
            return LayoutTemplate.SPLIT_TWO_COLUMN, "cardsSlide", why
        return LayoutTemplate.CENTERED_THESIS, "thesisSlide", why

    if g in (G.HERO_METAPHOR, G.TEXTUAL_THESIS):
        return HAPPY[g][0], HAPPY[g][1], None
    if g == G.DATA_CHART:
        if not chart_ok:
            return degrade("DATA_CHART generation_spec is not plottable "
                           "(labels/values unusable)")
        return HAPPY[g][0], HAPPY[g][1], None
    if g in (G.SOURCE_FIGURE_REUSE, G.ANNOTATED_FIGURE):
        if not asset_ok:
            return degrade(f"{g.value} names an asset that is not on disk")
        return HAPPY[g][0], HAPPY[g][1], None
    if g == G.SYSTEM_BLUEPRINT:
        if n_cards == 0:
            return degrade("SYSTEM_BLUEPRINT has no cards to band")
        return HAPPY[g][0], HAPPY[g][1], None
    if g in (G.PIPELINE_FLOW, G.TIMELINE, G.CIRCULAR_LOOP, G.INVERTED_PYRAMID):
        if n_cards < 2:
            return degrade(f"{g.value} needs at least 2 steps, got {n_cards}")
        return HAPPY[g][0], HAPPY[g][1], None
    if g == G.QUADRANT_MATRIX:
        if n_cards != 4:
            return degrade(f"QUADRANT_MATRIX needs exactly 4 cards, got {n_cards}")
        return HAPPY[g][0], HAPPY[g][1], None
    if g in (G.COMPARISON, G.TABLE):
        if n_cards == 0:
            return degrade(f"{g.value} has no rows to tabulate")
        return HAPPY[g][0], HAPPY[g][1], None
    if g == G.STRUCTURED_CARDS:
        if n_cards == 0:
            return degrade("STRUCTURED_CARDS has no cards")
        return HAPPY[g][0], HAPPY[g][1], None
    raise DeckLayoutError(f"no layout template for grammar {g.value}")   # pragma: no cover


# ── chart spec validation (shared with the materializer) ──────────────────────

def chart_frame(spec: dict | None) -> tuple[str, list[str], list[float], str] | None:
    """Validate a QUANTITATIVE_CODE spec → ``(kind, labels, values, name)`` or None.

    2..8 pairs, float-coercible values; an explicit ``chart`` knob wins, else
    numeric-looking first labels imply an ordered axis (line), otherwise bars.
    """
    if not isinstance(spec, dict):
        return None
    labels, values = spec.get("labels"), spec.get("values")
    if (not isinstance(labels, list) or not isinstance(values, list)
            or not 2 <= len(labels) <= 8 or len(labels) != len(values)):
        return None
    vals: list[float] = []
    for v in values:
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            return None
    kind = spec.get("chart")
    if kind not in ("bar", "line"):
        kind = "line" if re.search(r"\d", str(labels[0])) else "bar"
    name = str(spec.get("name") or "")
    return kind, [str(x) for x in labels], vals, name


# ── provenance citations from trace locators ─────────────────────────────────

def cite_locator(loc: S.SourceLocator) -> str:
    """The shipped ``[name:locator]`` convention, brief-locator flavour."""
    if loc.start_line is not None:
        seg = (str(loc.start_line) if loc.end_line in (None, loc.start_line)
               else f"{loc.start_line}-{loc.end_line}")
        return f"[{loc.doc_id}:{seg}]"
    if loc.page is not None:
        return f"[{loc.doc_id}:p{loc.page}]"
    if loc.message_id:
        return f"[{loc.doc_id}:#{loc.message_id[:8]}]"
    return f"[{loc.doc_id}]"


def _slide_citations(plan: S.SlidePlan, traceability: dict[str, S.TraceabilityNode]
                     ) -> list[str]:
    out: list[str] = []
    for c in plan.cards:
        node = traceability.get(c.trace_id)
        if node is None or node.locator is None:
            continue
        cite = cite_locator(node.locator)
        if cite not in out:
            out.append(cite)
    if not out and plan.source_section_ids:
        for c in plan.cards:                       # claims without locators still cite sections
            node = traceability.get(c.trace_id)
            if node is not None:
                out.append(f"[{node.trace_id}]")
    return out


# ── per-slide layout record ───────────────────────────────────────────────────

@dataclass
class BriefSlideLayout:
    slide_index: int
    template: LayoutTemplate
    fn: str                                   # Typst function name (slides.typ)
    header: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    fallback_reason: str | None = None
    figure_src: str | None = None             # asset file to copy into the workdir


def card_detail(c: S.SlideContentCard) -> str:
    return c.takeaway + (f" — {c.metric_highlight}" if c.metric_highlight else "")


def _header(plan: S.SlidePlan, *, kicker: bool) -> dict:
    title_pt, title_lines = _fit(plan.title, CONTENT_W_MM, HEADER_H_MM * 0.55,
                                 ("title", "body", "caption", "micro"))
    if not kicker:      # thesis renders the message big — no duplicate kicker
        return {"title_lines": title_lines, "title_pt": title_pt,
                "kicker_lines": [], "kicker_pt": TIERS["body"]}
    msg = plan.subtitle or plan.central_message
    kicker_pt, kicker_lines = _fit(msg, CONTENT_W_MM, HEADER_H_MM * 0.35,
                                   ("body", "caption", "micro"))
    return {"title_lines": title_lines, "title_pt": title_pt,
            "kicker_lines": kicker_lines, "kicker_pt": kicker_pt}


# ── body builders (geometry mirrored from layout.py v1) ───────────────────────

def _card_pair(c: S.SlideContentCard) -> tuple[str, str]:
    return c.label, card_detail(c)


def _cards_body(cards: list[tuple[str, str]], *, width_mm: float,
                cols: int | None = None, rows_budget: float | None = None) -> dict:
    n = len(cards)
    cols = cols or (min(n, 4) if n >= 3 else n or 1)
    rows = math.ceil(n / cols)
    gap = 8.0
    slot_w = (width_mm - gap * (cols - 1)) / cols
    body_h = rows_budget if rows_budget is not None else BODY_H_MM
    slot_h = (body_h - gap * (rows - 1)) / rows
    out = []
    for label, detail in cards:
        lpt, llines = _fit(label, slot_w - 12, slot_h * 0.32,
                           ("body", "caption", "micro"))
        dpt, dlines = _fit(detail, slot_w - 12, slot_h * 0.55,
                           ("caption", "micro"))
        out.append({"label_lines": llines, "label_pt": lpt,
                    "detail_lines": dlines, "detail_pt": dpt})
    return {"cols": cols, "rows": rows, "gap_mm": gap,
            "slot_w_mm": round(slot_w, 2), "slot_h_mm": round(slot_h, 2),
            "cards": out}


def _steps_body(cards: list[S.SlideContentCard], *, timeline: bool) -> dict:
    n = len(cards)
    gap = 6.0 if timeline else 10.0
    node_w = (CONTENT_W_MM - gap * max(0, n - 1)) / max(1, n)
    node_h = 0.0 if timeline else 30.0
    steps = []
    for c in cards:
        # TIMELINE: the card label rides the axis as the "when" marker, the
        # takeaway becomes the body — no duplicated text.
        label = "" if timeline else c.label
        detail = card_detail(c)
        when = c.label if timeline else ""
        lpt, llines = _fit(label, node_w - (8 if not timeline else 4),
                           (node_h * 0.5) if not timeline else 12.0,
                           ("body", "caption", "micro"))
        dpt, dlines = _fit(detail, node_w - 4,
                           (node_h * 0.4) if not timeline else 10.0,
                           ("caption", "micro"))
        rpt, rlines = _fit(when, node_w - 4, 8.0, ("caption", "micro"))
        steps.append({"label_lines": llines, "label_pt": lpt,
                      "detail_lines": dlines, "detail_pt": dpt,
                      "when_lines": rlines, "when_pt": rpt})
    return {"n": n, "gap_mm": gap, "node_w_mm": round(node_w, 2),
            "node_h_mm": node_h, "steps": steps,
            "loop_label": "feedback loop"}


def _funnel_body(cards: list[tuple[str, str]], theme: ThemeTokens) -> dict:
    n = len(cards)
    gap = 6.0
    level_h = (BODY_H_MM - gap * (n - 1)) / n
    max_w, min_w = CONTENT_W_MM * 0.82, CONTENT_W_MM * 0.38
    levels = []
    for i, (label, detail) in enumerate(cards):
        frac = i / (n - 1) if n > 1 else 0.0
        w = max_w - (max_w - min_w) * frac
        fill = _mix(theme.surface, theme.primary, 0.72 - 0.5 * frac)
        lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
        text = (255, 255, 255) if lum < 140 else theme.ink
        lpt, llines = _fit(label, w - 12, level_h * 0.45, ("body", "caption", "micro"))
        dpt, dlines = _fit(detail, w - 12, level_h * 0.45, ("caption", "micro"))
        levels.append({"w_mm": round(w, 2), "label_lines": llines, "label_pt": lpt,
                       "detail_lines": dlines, "detail_pt": dpt,
                       "fill": "#{:02X}{:02X}{:02X}".format(*fill),
                       "text": "#{:02X}{:02X}{:02X}".format(*text)})
    return {"levels": levels, "level_h_mm": round(level_h, 2), "gap_mm": gap}


def _arch_body(cards: list[S.SlideContentCard]) -> dict:
    gap = 8.0
    band_h = (BODY_H_MM - gap * max(0, len(cards) - 1)) / max(1, len(cards))
    bands = []
    for c in cards:
        node_w = (CONTENT_W_MM - gap * 2) / 2
        pt, lines = _fit(card_detail(c), node_w - 8, band_h * 0.6,
                         ("body", "caption", "micro"))
        gpt, glines = _fit(c.label, CONTENT_W_MM, band_h * 0.28, ("caption", "micro"))
        bands.append({"group_lines": glines, "group_pt": gpt,
                      "nodes": [{"lines": lines, "pt": pt, "detail": ""}],
                      "node_w_mm": round(node_w, 2)})
    return {"bands": bands, "band_h_mm": round(band_h, 2), "gap_mm": gap,
            "direction": "layers-top-down"}


def _compare_body(cards: list[S.SlideContentCard]) -> dict:
    n = len(cards)
    gap = 6.0
    col_w = (CONTENT_W_MM - gap * max(0, n - 1)) / max(1, n)
    cols = []
    for c in cards:
        hpt, hlines = _fit(c.label, col_w - 6, 10.0, ("body", "caption", "micro"))
        cell_texts = [c.takeaway] + ([c.metric_highlight] if c.metric_highlight else [])
        cells = []
        for cell in cell_texts:
            pt, lines = _fit(cell, col_w - 6, BODY_H_MM / max(1, len(cell_texts)) - 2,
                             ("caption", "micro"))
            cells.append({"lines": lines, "pt": pt})
        cols.append({"header_lines": hlines, "header_pt": hpt, "cells": cells})
    # the template indexes every column at every row — pad short columns with
    # empty (never-trim) cells so the table can never run off the end.
    nrows = max((len(c["cells"]) for c in cols), default=0)
    for c in cols:
        while len(c["cells"]) < nrows:
            c["cells"].append({"lines": [], "pt": TIERS["caption"]})
    return {"cols": cols, "gap_mm": gap, "col_w_mm": round(col_w, 2)}


def _table_body(cards: list[tuple[str, str]]) -> dict:
    left_w = CONTENT_W_MM * 0.32
    right_w = CONTENT_W_MM - left_w - 8.0
    row_h = BODY_H_MM / max(1, len(cards))
    rows = []
    for label, detail in cards:
        lpt, llines = _fit(label, left_w - 8, row_h - 4, ("body", "caption", "micro"))
        dpt, dlines = _fit(detail, right_w - 8, row_h - 4, ("caption", "micro"))
        rows.append({"label_lines": llines, "label_pt": lpt,
                     "detail_lines": dlines, "detail_pt": dpt})
    return {"rows": rows, "left_fr": 0.32}


def _thesis_body(plan: S.SlidePlan, max_chip: int = 28) -> dict:
    pt, lines = _fit(plan.central_message, CONTENT_W_MM * 0.92, BODY_H_MM * 0.85)
    chips = [c.label if len(c.label) <= max_chip else c.label[:max_chip - 1] + "…"
             for c in plan.cards]
    return {"message_lines": lines, "message_pt": pt, "chips": chips}


def _chart_body(frame: tuple[str, list[str], list[float], str], name: str) -> dict:
    kind, labels, values, spec_name = frame
    plot_w, plot_h = CONTENT_W_MM - 20.0, BODY_H_MM - 24.0
    vmax, vmin = max(values), min(min(values), 0.0)
    span = (vmax - vmin) or 1.0
    pts = []
    for xi, (x, v) in enumerate(zip(labels, values)):
        fx = (xi + 0.5) / len(labels)
        fy = 1.0 - (v - vmin) / span
        _, label_lines = _fit(x, plot_w / len(labels) + 6.0, 8.0, ("micro",))
        pts.append({"x_mm": round(fx * plot_w, 2), "y_mm": round(fy * plot_h, 2),
                    "value": v, "label_lines": label_lines})
    return {"kind": kind, "plot_w_mm": round(plot_w, 2), "plot_h_mm": round(plot_h, 2),
            "v_min": vmin, "v_max": vmax,
            "series": [{"name": spec_name or name, "points": pts}], "grid": 4}


def _px_size(path: str) -> tuple[int, int] | None:
    from PIL import Image

    try:
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - undecodable asset ⇒ figureSlide is unusable
        return None


def _figure_body(plan: S.SlidePlan, asset: S.VisualAsset, cards: list[tuple[str, str]]
                 ) -> dict | None:
    """Contain-fit the slice into the slot; optional side-note column (§6.2)."""
    px = _px_size(asset.path)
    if not px or px[0] <= 0 or px[1] <= 0:
        return None
    hint = asset.semantic_hint or asset.nearby_text or ""
    cap_pt, cap_lines = (TIERS["caption"], []) if not hint else \
        _fit(hint, CONTENT_W_MM * 0.7, 7.0, ("caption", "micro"))
    caption_h = 9.0 if cap_lines else 0.0
    if cards:
        img_w, notes_w = CONTENT_W_MM * 0.62, CONTENT_W_MM * 0.34
        gap = CONTENT_W_MM * 0.04
        img_col_fr = round(img_w / notes_w, 3)
    else:
        img_w, notes_w, gap, img_col_fr = CONTENT_W_MM, 0.0, 0.0, 0.0
    box_h = BODY_H_MM - caption_h - 2.0
    ratio = px[0] / px[1]
    w_mm, h_mm = img_w, img_w / ratio
    if h_mm > box_h:
        h_mm, w_mm = box_h, box_h * ratio
    body = {
        "name": f"figure_s{plan.slide_index}{Path(asset.path).suffix.lower()}",
        "w_mm": round(w_mm, 2), "h_mm": round(h_mm, 2),
        "caption_lines": cap_lines, "caption_pt": cap_pt,
        "notes": (_cards_body(cards, width_mm=notes_w, cols=1,
                              rows_budget=BODY_H_MM) if cards else
                  {"cols": 1, "rows": 0, "gap_mm": 0.0, "slot_w_mm": 0.0,
                   "slot_h_mm": 0.0, "cards": []}),
        "img_col_fr": img_col_fr, "gap_mm": round(gap, 2),
    }
    return body


# ── the per-slide and per-deck entry points ──────────────────────────────────

def build_slide_layout(plan: S.SlidePlan, traceability: dict[str, S.TraceabilityNode],
                       assets: dict[str, S.VisualAsset],
                       theme: ThemeTokens = ACADEMIC_LIGHT) -> BriefSlideLayout:
    spec = plan.visual_spec
    g = spec.grammar
    # schema pairing rule: reuse_asset_id exists only on the two figure
    # grammars — every asset the brief names therefore reaches a template with a
    # real figure slot (or the QA/patch legs made it fail loudly upstream);
    # there is no structural path that can drop an asset on the floor.
    asset = assets.get(spec.reuse_asset_id or "")
    asset_ok = bool(asset) and Path(asset.path).is_file()
    frame = chart_frame(spec.generation_spec) \
        if spec.policy is S.VisualGenerationPolicy.QUANTITATIVE_CODE else None
    chart_ok = frame is not None
    n_cards = len(plan.cards)

    template, fn, reason = resolve_layout_template(
        g, asset_ok=asset_ok, chart_ok=chart_ok, n_cards=n_cards)

    lay = BriefSlideLayout(slide_index=plan.slide_index, template=template, fn=fn,
                           citations=_slide_citations(plan, traceability),
                           fallback_reason=reason)
    cards = [_card_pair(c) for c in plan.cards]

    if fn == "thesisSlide":
        lay.header = _header(plan, kicker=False)
        lay.body = _thesis_body(plan)
    elif fn == "cardsSlide":
        lay.header = _header(plan, kicker=True)
        cols = 2 if template is LayoutTemplate.QUADRANT_2X2 else None
        lay.body = _cards_body(cards, width_mm=CONTENT_W_MM, cols=cols)
    elif fn in ("flowSlide", "timelineSlide", "loopSlide"):
        lay.header = _header(plan, kicker=True)
        lay.body = _steps_body(plan.cards, timeline=(fn == "timelineSlide"))
    elif fn == "funnelSlide":
        lay.header = _header(plan, kicker=True)
        lay.body = _funnel_body(cards, theme)
    elif fn == "archSlide":
        lay.header = _header(plan, kicker=True)
        lay.body = _arch_body(plan.cards)
    elif fn == "compareSlide":
        lay.header = _header(plan, kicker=True)
        lay.body = _compare_body(plan.cards)
    elif fn == "tableSlide":
        lay.header = _header(plan, kicker=True)
        lay.body = _table_body(cards)
    elif fn == "chartSlide" and frame is not None:
        lay.header = _header(plan, kicker=True)
        lay.body = _chart_body(frame, plan.title)
    elif fn == "figureSlide" and asset is not None:
        body = _figure_body(plan, asset, cards)
        if body is None:                          # unreadable pixels → degrade again
            sub = plan.model_copy(update={"visual_spec": spec.model_copy(
                update={"grammar": G.STRUCTURED_CARDS,
                        "policy": S.VisualGenerationPolicy.EXPLANATORY_DIAGRAM,
                        "reuse_asset_id": None})})
            lay = build_slide_layout(sub, traceability, assets, theme)
            # the second-leg degradation must stay visible too (§9.4)
            lay.fallback_reason = (reason or
                                   f"{g.value} names an undecodable asset file")
            return lay
        lay.header = _header(plan, kicker=True)
        lay.body = body
        lay.figure_src = asset.path
    else:                                         # pragma: no cover - map is closed
        raise DeckLayoutError(f"no body builder for {template.value}/{fn}")
    return lay


def build_deck_layouts(brief: S.PresentationBrief,
                       assets: dict[str, S.VisualAsset],
                       theme: ThemeTokens = ACADEMIC_LIGHT
                       ) -> tuple[list[BriefSlideLayout], list[str]]:
    """All slides + the collected explicit-degradation warnings (§9.4)."""
    layouts = [build_slide_layout(p, brief.traceability_graph, assets, theme)
               for p in brief.slides]
    warns = [f"slide {lay.slide_index}: fell back to {lay.template.value} — "
             f"{lay.fallback_reason}"
             for lay in layouts if lay.fallback_reason]
    return layouts, warns


def layout_template_kinds(layouts: list[BriefSlideLayout]) -> set[LayoutTemplate]:
    """Diversity set for the QA hard assertion (≥3 template kinds per deck)."""
    return {lay.template for lay in layouts}


__all__ = [
    "HAPPY",
    "BriefSlideLayout",
    "LayoutTemplate",
    "build_deck_layouts",
    "build_slide_layout",
    "card_detail",
    "chart_frame",
    "cite_locator",
    "layout_template_kinds",
    "resolve_layout_template",
]
