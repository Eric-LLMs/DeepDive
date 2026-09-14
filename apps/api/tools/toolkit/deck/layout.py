"""Layout planning — deterministic slots from a budget-satisfied DeckSpec (docs §5.3).

Input contract: slides have ALREADY passed the Pass C budget validators, so fitting is
purely formatting. The engine measures text conservatively (CJK ≈ 1.05em, Latin ≈ 0.50em
per char), wraps to lines, and picks the largest fixed size tier that keeps every block
inside its slot.

**Semantic preservation (errata #4):** NOTHING is ever trimmed, ellipsized, or dropped
here — full text is laid out or the engine raises :class:`LayoutOverflow` (loud failure).
If that happens the construction guarantee upstream is broken and the job must fail.

Output: per-slide layout dicts (already-wrapped lines + mm geometry) consumed verbatim by
:mod:`typst_deck` — the emitter adds no intelligence of its own.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .errors import DeckLayoutError
from .models import Slide, VisualPlan
# ── page & type constants (MUST mirror deck/templates/slides.typ) ─────────────
PAGE_W_MM, PAGE_H_MM = 338.67, 190.5          # 16:9
MARGIN_X_MM, MARGIN_Y_MM = 16.0, 12.0
CONTENT_W_MM = PAGE_W_MM - 2 * MARGIN_X_MM    # 306.67
CONTENT_H_MM = PAGE_H_MM - 2 * MARGIN_Y_MM    # 166.5
HEADER_H_MM = 26.0                            # title + kicker band on content slides
BODY_H_MM = CONTENT_H_MM - HEADER_H_MM        # the slot every visual fills

_PT_TO_MM = 0.352777
# fixed tiers (pt): display, title, body, caption, micro — mirrors slides.typ
TIERS: dict[str, float] = {"display": 34, "title": 26, "body": 16, "caption": 12, "micro": 10}
FIT_ORDER = ("display", "title", "body", "caption", "micro")   # largest first
LEADING = 1.30                                                   # line height factor

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿　-〿＀-￯]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-./%]*")


def text_width_mm(text: str, size_pt: float) -> float:
    """Conservative mixed-script width estimate for a single line."""
    em = size_pt * _PT_TO_MM
    w = 0.0
    for ch in text:
        if _CJK.match(ch):
            w += 1.05 * em
        elif ch == " ":
            w += 0.30 * em
        else:
            w += 0.52 * em
    return w


def wrap_lines(text: str, width_mm: float, size_pt: float) -> list[str]:
    """Greedy wrap that preserves EVERY source character (errata #4: no silent edits).

    Atoms are Latin words, single CJK chars, and single other chars (punctuation stays
    attached to its neighbours); a space is emitted only where the source had one.
    CJK breaks anywhere; Latin words stay atomic (an over-long word gets its own line
    rather than being split — measurement accounts for it).
    """
    if not text:
        return []
    atoms: list[tuple[str, bool]] = []          # (token, preceded-by-space in source)
    pending_space = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            pending_space = True
            i += 1
            continue
        m = _LATIN_WORD.match(text, i)
        if m:
            atoms.append((m.group(), pending_space))
            i = m.end()
        else:                                    # CJK char or any other single char
            atoms.append((ch, pending_space))
            i += 1
        pending_space = False
    lines: list[str] = []
    cur = ""
    for tok, sp in atoms:
        cand = cur + (" " if sp and cur else "") + tok
        if cur and text_width_mm(cand, size_pt) > width_mm:
            lines.append(cur)
            cur = tok
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def block_height(lines: int, size_pt: float) -> float:
    return lines * size_pt * _PT_TO_MM * LEADING


def _fit(text: str, width_mm: float, height_mm: float,
         tiers: tuple[str, ...] = FIT_ORDER, max_lines: int | None = None) -> tuple[float, list[str]]:
    """Largest tier whose wrapped block fits the box (or the line cap). Raises when even
    ``micro`` fails — never a silent trim."""
    for name in tiers:
        pt = TIERS[name]
        lines = wrap_lines(text, width_mm, pt)
        if not lines:
            return pt, lines
        if max_lines is not None and len(lines) > max_lines:
            continue
        if block_height(len(lines), pt) <= height_mm + 0.01:
            return pt, lines
    raise DeckLayoutError(
        f"content does not fit even at {FIT_ORDER[-1]} tier "
        f"({text[:40]!r}…): layout must not trim, fix the semantic budget instead"
    )


# ── per-slide layout structures (plain dicts for the emitter) ─────────────────

@dataclass
class SlideLayout:
    visual_type: str
    header: dict = field(default_factory=dict)        # {title:[lines,pt], kicker:[..]}
    body: dict = field(default_factory=dict)          # type-specific geometry + lines


def _header(slide: Slide, *, kicker: bool = True) -> dict:
    title_pt, title_lines = _fit(slide.title, CONTENT_W_MM, HEADER_H_MM * 0.55,
                                 ("title", "body", "caption", "micro"))
    if not kicker:   # TEXT_HERO renders the message big — no duplicate kicker
        return {"title_lines": title_lines, "title_pt": title_pt,
                "kicker_lines": [], "kicker_pt": TIERS["body"]}
    kicker_pt, kicker_lines = _fit(slide.key_message, CONTENT_W_MM,
                                   HEADER_H_MM * 0.35, ("body", "caption", "micro"))
    return {"title_lines": title_lines, "title_pt": title_pt,
            "kicker_lines": kicker_lines, "kicker_pt": kicker_pt}


def layout_slide(slide: Slide, plan: VisualPlan) -> SlideLayout:
    lay = SlideLayout(
        visual_type=plan.visual_type,
        header=_header(slide, kicker=plan.visual_type != "TEXT_HERO"))
    p = slide.payload
    vt = plan.visual_type

    if vt == "TEXT_HERO":
        pt, lines = _fit(slide.key_message, CONTENT_W_MM * 0.92, BODY_H_MM * 0.85)
        lay.body = {"message_lines": lines, "message_pt": pt,
                    "emphasis": plan.intent.emphasis}

    elif vt == "CARDS":
        n = len(p.items)
        cols = min(n, 4) if n >= 3 else n or 1
        rows = math.ceil(n / cols)
        gap = 8.0
        slot_w = (CONTENT_W_MM - gap * (cols - 1)) / cols
        slot_h = (BODY_H_MM - gap * (rows - 1)) / rows
        cards = []
        for it in p.items:
            lpt, llines = _fit(it.label, slot_w - 12, slot_h * 0.32,
                               ("body", "caption", "micro"))
            dpt, dlines = _fit(it.detail, slot_w - 12, slot_h * 0.55,
                               ("caption", "micro"))
            cards.append({"label_lines": llines, "label_pt": lpt,
                          "detail_lines": dlines, "detail_pt": dpt})
        lay.body = {"cols": cols, "rows": rows, "gap_mm": gap,
                    "slot_w_mm": round(slot_w, 2), "slot_h_mm": round(slot_h, 2),
                    "cards": cards, "direction": plan.intent.direction}

    elif vt in ("FLOWCHART", "TIMELINE"):
        n = len(p.steps)
        gap = 10.0 if vt == "FLOWCHART" else 6.0
        # max(1, n): budget checks flag empty/degenerate payloads — this dry-run must
        # report geometry, not crash on a zero-width division.
        node_w = (CONTENT_W_MM - gap * max(0, n - 1)) / max(1, n)
        node_h = 30.0 if vt == "FLOWCHART" else 0.0
        steps = []
        for st in p.steps:
            lpt, llines = _fit(st.label, node_w - (8 if vt == "FLOWCHART" else 4),
                               (node_h * 0.5) if vt == "FLOWCHART" else 12.0,
                               ("body", "caption", "micro"))
            dpt, dlines = _fit(st.detail, node_w - 4,
                               (node_h * 0.4) if vt == "FLOWCHART" else 10.0,
                               ("caption", "micro"))
            rpt, rlines = _fit(st.when, node_w - 4, 8.0, ("caption", "micro"))
            steps.append({"label_lines": llines, "label_pt": lpt,
                          "detail_lines": dlines, "detail_pt": dpt,
                          "when_lines": rlines, "when_pt": rpt})
        lay.body = {"n": n, "gap_mm": gap, "node_w_mm": round(node_w, 2),
                    "node_h_mm": node_h, "steps": steps}

    elif vt == "COMPARISON":
        cols = p.columns
        n = len(cols)
        gap = 6.0
        col_w = (CONTENT_W_MM - gap * max(0, n - 1)) / max(1, n)   # see FLOWCHART guard
        out = []
        for c in cols:
            hpt, hlines = _fit(c.header, col_w - 6, 10.0, ("body", "caption", "micro"))
            cells = []
            for cell in c.cells:
                pt, lines = _fit(cell, col_w - 6, BODY_H_MM / max(1, len(c.cells)) - 2,
                                 ("caption", "micro"))
                cells.append({"lines": lines, "pt": pt})
            out.append({"header_lines": hlines, "header_pt": hpt, "cells": cells})
        lay.body = {"cols": out, "gap_mm": gap, "col_w_mm": round(col_w, 2)}

    elif vt == "ARCHITECTURE":
        groups: list[str] = []
        for it in p.items:
            if it.group not in groups:
                groups.append(it.group)
        bands = []
        gap = 8.0
        band_h = (BODY_H_MM - gap * (len(groups) - 1)) / max(1, len(groups))
        for g in groups:
            nodes = [it for it in p.items if it.group == g]
            node_w = (CONTENT_W_MM - gap * (len(nodes) + 1)) / (len(nodes) + 1)
            laid = []
            for nd in nodes:
                pt, lines = _fit(nd.label, node_w - 8, band_h * 0.6,
                                 ("body", "caption", "micro"))
                laid.append({"lines": lines, "pt": pt, "detail": nd.detail})
            gpt, glines = _fit(g, CONTENT_W_MM, band_h * 0.28, ("caption", "micro"))
            bands.append({"group_lines": glines, "group_pt": gpt, "nodes": laid,
                          "node_w_mm": round(node_w, 2)})
        lay.body = {"bands": bands, "band_h_mm": round(band_h, 2), "gap_mm": gap,
                    "direction": plan.intent.direction}

    elif vt == "CHART":
        # plot area under the header; every value/point was quant-gated upstream
        plot_w = CONTENT_W_MM - 20.0
        plot_h = BODY_H_MM - 24.0
        all_vals = [q.y for s in p.series for q in s.points]
        vmax, vmin = max(all_vals), min(min(all_vals), 0.0)
        span = (vmax - vmin) or 1.0
        series_out = []
        for s in p.series:
            pts = []
            for xi, q in enumerate(s.points):
                fx = (xi + 0.5) / len(s.points)            # slot-centred x
                fy = 1.0 - (q.y - vmin) / span             # top-down y (mm from plot top)
                _, label_lines = _fit(q.x, plot_w / len(s.points) + 6.0, 8.0, ("micro",))
                pts.append({"x_mm": round(fx * plot_w, 2), "y_mm": round(fy * plot_h, 2),
                            "value": q.y, "label_lines": label_lines})
            # legend label is a plain string rendered at a fixed size by the template
            series_out.append({"name": s.name, "points": pts})
        # numeric-looking x labels ⇒ an ordered axis ⇒ line chart; else grouped bars
        kind = "line" if _is_ordered(p) else "bar"
        lay.body = {"kind": kind, "plot_w_mm": round(plot_w, 2),
                    "plot_h_mm": round(plot_h, 2),
                    "v_min": vmin, "v_max": vmax, "series": series_out,
                    "grid": 4}
    else:  # pragma: no cover - VisualType is closed
        raise DeckLayoutError(f"unknown visual type {vt}")
    return lay


def _is_ordered(payload) -> bool:
    """Line chart when x labels look like an ordered axis (dates/years/numbers)."""
    xs = [pt.x for s in payload.series for pt in s.points[:1]]
    if not xs:
        return False
    return all(re.search(r"\d", x) for x in xs)


def layout_deck(deck) -> list[SlideLayout]:
    plans = {pl.slide_id: pl for pl in deck.visual_plan}
    return [layout_slide(s, plans[s.slide_id]) for s in deck.slides]


def fit_violations(slide: Slide, plan: VisualPlan) -> list[str]:
    """Dry-run the geometry for one slide: the SAME conservative measurement the
    renderer uses, reported as corrective-retry error strings instead of raised.

    Unit budgets (rules.py) can pass while a long-worded Latin string still wraps past
    a narrow slot — this closes the "budget OK, layout dead" gap deterministically.
    """
    try:
        layout_slide(slide, plan)
        return []
    except DeckLayoutError as exc:
        msg = str(exc)
        return [f"slide {slide.slide_id}: {msg} — shorten the offending label/detail "
                "so it fits the slot at the micro tier (keep each line short: fewer, "
                "smaller words; do not drop the meaning)"]
