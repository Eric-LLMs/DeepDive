"""Native PptxCompiler (M2): PresentationBrief + layouts → real .pptx bytes.

Every one of the 7 ``LayoutTemplate`` forms (§6.4) gets a hand-written
drawing branch keyed by the resolved Typst function — an unmapped combination
raises :class:`~..errors.DeckLayoutError` instead of silently flattening to
bullets (loud failure, §9.4). Text stays in NATIVE editable objects (title,
cards, tables); charts embed the SAME deterministic PNGs as
:mod:`.materializer` (QUANTITATIVE_CODE is one shared source for both
compilers), and figure grammars embed the sliced asset file verbatim.

Geometry reuses the layout records the Typst path consumes (slot sizes, fitted
pt, funnel fills), so both renderers agree on what fits; speaker notes and the
``[doc:line]`` citations land on each slide, mirroring the PDF.
"""
from __future__ import annotations

from collections.abc import Callable
from io import BytesIO

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

from .. import schema as S
from ..errors import DeckLayoutError
from ..layout import (
    BODY_H_MM,
    CONTENT_W_MM,
    HEADER_H_MM,
    MARGIN_X_MM,
    MARGIN_Y_MM,
    PAGE_H_MM,
    PAGE_W_MM,
    TIERS,
)
from .layout_engine import BriefSlideLayout, card_detail, chart_frame
from .materializer import render_chart_png
from .theme import ThemeTokens

_MM = 36000                                    # EMU per millimeter
_BODY_TOP = MARGIN_Y_MM + HEADER_H_MM          # where every body band starts


def _mm(v: float) -> Emu:
    return Emu(round(v * _MM))


def _rgb(t: tuple[int, int, int]) -> RGBColor:
    return RGBColor(*t)


def _webhex(s: str) -> RGBColor:
    return RGBColor(int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))


# ── primitive helpers ─────────────────────────────────────────────────────────

def _blank(prs: Presentation, theme: ThemeTokens):
    slide = prs.slides.add_slide(prs.slide_layouts[6])   # blank master layout
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0,
                                prs.slide_width, prs.slide_height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = _rgb(theme.background)
    bg.line.fill.background()
    bg.shadow.inherit = False
    return slide


def _text(slide, x: float, y: float, w: float, h: float, text: str, pt: float,
          color: RGBColor, *, bold: bool = False,
          align: PP_ALIGN = PP_ALIGN.LEFT,
          anchor: MSO_ANCHOR = MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(_mm(x), _mm(y), _mm(w), _mm(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(pt)
    r.font.bold = bold
    r.font.color.rgb = color
    return box


def _style_run(r, pt: float, color: RGBColor, *, bold: bool = False) -> None:
    r.font.size = Pt(pt)
    r.font.bold = bold
    r.font.color.rgb = color


def _card_box(slide, x: float, y: float, w: float, h: float, theme: ThemeTokens,
              label: str, label_pt: float, detail: str, detail_pt: float):
    """A rounded card: bold label + detail paragraphs, both editable.

    An empty label renders as a single detail-only paragraph (no blank first
    line) — the architecture bands use it that way.
    """
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                _mm(x), _mm(y), _mm(w), _mm(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = _rgb(theme.card)
    sh.line.color.rgb = _rgb(theme.border)
    sh.line.width = Pt(0.75)
    sh.shadow.inherit = False
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = _mm(3)
    if label:
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _style_run(p.add_run(), label_pt, _rgb(theme.primary), bold=True)
        p.runs[0].text = label
        p2 = tf.add_paragraph()
    else:
        p2 = tf.paragraphs[0]
    p2.alignment = PP_ALIGN.CENTER
    _style_run(p2.add_run(), detail_pt, _rgb(theme.ink))
    p2.runs[0].text = detail
    return sh


def _header(slide, plan: S.SlidePlan, lay: BriefSlideLayout, theme: ThemeTokens) -> None:
    h = lay.header
    _text(slide, MARGIN_X_MM, MARGIN_Y_MM, CONTENT_W_MM, 10.0,
          plan.title, h["title_pt"], _rgb(theme.primary), bold=True)
    if h.get("kicker_lines"):
        _text(slide, MARGIN_X_MM, MARGIN_Y_MM + 10.5, CONTENT_W_MM, 7.0,
              plan.subtitle or plan.central_message, h["kicker_pt"],
              _rgb(theme.muted))


def _footer(slide, lay: BriefSlideLayout, theme: ThemeTokens) -> None:
    if lay.citations:
        _text(slide, PAGE_W_MM - MARGIN_X_MM - 90.0, PAGE_H_MM - 8.5, 90.0, 5.0,
              "  ".join(lay.citations), TIERS["micro"], _rgb(theme.faint),
              align=PP_ALIGN.RIGHT)


def _notes(slide, plan: S.SlidePlan, lay: BriefSlideLayout) -> None:
    parts = [plan.speaker_notes.strip()] if plan.speaker_notes.strip() else []
    if lay.citations:
        parts.append("Sources: " + "  ".join(lay.citations))
    if parts:
        slide.notes_slide.notes_text_frame.text = "\n".join(parts)


# ── the seven template branches (dispatched by Typst fn, mirroring slides.typ) ─

def _thesis(slide, plan, lay, assets, theme):
    _header(slide, plan, lay, theme)
    b = lay.body
    _text(slide, MARGIN_X_MM + 6, _BODY_TOP + 6, CONTENT_W_MM - 12,
          BODY_H_MM - 20, plan.central_message, b["message_pt"],
          _rgb(theme.ink), bold=True, align=PP_ALIGN.CENTER,
          anchor=MSO_ANCHOR.MIDDLE)
    if b["chips"]:
        _text(slide, MARGIN_X_MM, PAGE_H_MM - MARGIN_Y_MM - 8.0, CONTENT_W_MM, 6.0,
              "  ·  ".join(b["chips"]), TIERS["caption"], _rgb(theme.muted),
              align=PP_ALIGN.CENTER)


def _arch(slide, plan, lay, assets, theme):
    _header(slide, plan, lay, theme)
    b = lay.body
    y = _BODY_TOP
    for band, c in zip(b["bands"], plan.cards):
        band_h = b["band_h_mm"]
        _text(slide, MARGIN_X_MM, y, CONTENT_W_MM, band_h * 0.28,
              c.label, band["group_pt"], _rgb(theme.muted), bold=True)
        _card_box(slide, MARGIN_X_MM, y + band_h * 0.30, CONTENT_W_MM,
                  band_h * 0.66, theme, "", 1, card_detail(c),
                  band["nodes"][0]["pt"])
        y += band_h + b["gap_mm"]


def _steps(slide, plan, lay, assets, theme):
    """flowSlide / timelineSlide / loopSlide share the horizontal axis math."""
    _header(slide, plan, lay, theme)
    b = lay.body
    n = b["n"]
    if lay.fn == "timelineSlide":
        axis_y = _BODY_TOP + 26.0
        ln = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _mm(MARGIN_X_MM),
                                    _mm(axis_y), _mm(CONTENT_W_MM), _mm(0.7))
        ln.fill.solid()
        ln.fill.fore_color.rgb = _rgb(theme.border)
        ln.line.fill.background()
        ln.shadow.inherit = False
        for i, (step, c) in enumerate(zip(b["steps"], plan.cards)):
            cx = MARGIN_X_MM + i * (b["node_w_mm"] + b["gap_mm"])
            r = 3.0
            ov = slide.shapes.add_shape(
                MSO_SHAPE.OVAL, _mm(cx + b["node_w_mm"] / 2 - r),
                _mm(axis_y + 0.35 - r), _mm(2 * r), _mm(2 * r))
            ov.fill.solid()
            ov.fill.fore_color.rgb = _rgb(theme.accent)
            ov.line.color.rgb = _rgb(theme.surface)
            ov.shadow.inherit = False
            _text(slide, cx, axis_y - 11.0, b["node_w_mm"], 8.0, c.label,
                  step["when_pt"], _rgb(theme.primary), bold=True,
                  align=PP_ALIGN.CENTER)
            _text(slide, cx, axis_y + 7.0, b["node_w_mm"], 30.0, card_detail(c),
                  step["detail_pt"], _rgb(theme.ink), align=PP_ALIGN.CENTER)
        return
    box_h = b["node_h_mm"]
    y = _BODY_TOP + (BODY_H_MM - box_h) / 2 - 8.0
    for i, (step, c) in enumerate(zip(b["steps"], plan.cards)):
        x = MARGIN_X_MM + i * (b["node_w_mm"] + b["gap_mm"])
        _card_box(slide, x, y, b["node_w_mm"], box_h, theme,
                  c.label, step["label_pt"], card_detail(c), step["detail_pt"])
        if i < n - 1:
            _text(slide, x + b["node_w_mm"] - 1, y + box_h / 2 - 4,
                  b["gap_mm"] + 2, 8.0, "→", TIERS["body"], _rgb(theme.accent),
                  bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    if lay.fn == "loopSlide":
        _text(slide, MARGIN_X_MM, y + box_h + 4.0, CONTENT_W_MM, 6.0,
              "↺  " + b["loop_label"], TIERS["caption"], _rgb(theme.muted),
              align=PP_ALIGN.CENTER)


def _funnel(slide, plan, lay, assets, theme):
    _header(slide, plan, lay, theme)
    b = lay.body
    y = _BODY_TOP
    for lvl, (label, detail) in zip(b["levels"],
                                    [(c.label, card_detail(c)) for c in plan.cards]):
        x = MARGIN_X_MM + (CONTENT_W_MM - lvl["w_mm"]) / 2
        sh = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, _mm(x), _mm(y),
                                    _mm(lvl["w_mm"]),
                                    _mm(b["level_h_mm"] - b["gap_mm"]))
        sh.fill.solid()
        sh.fill.fore_color.rgb = _webhex(lvl["fill"])
        sh.line.fill.background()
        sh.shadow.inherit = False
        tf = sh.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _style_run(p.add_run(), lvl["label_pt"], _webhex(lvl["text"]), bold=True)
        p.runs[0].text = label
        p2 = tf.add_paragraph()
        p2.alignment = PP_ALIGN.CENTER
        _style_run(p2.add_run(), lvl["detail_pt"], _webhex(lvl["text"]))
        p2.runs[0].text = detail
        y += b["level_h_mm"] + b["gap_mm"]


def _chart(slide, plan, lay, assets, theme):
    _header(slide, plan, lay, theme)
    frame = chart_frame(plan.visual_spec.generation_spec)
    if frame is None:                       # pragma: no cover - upstream would have degraded
        raise DeckLayoutError(f"pptx: slide {plan.slide_index} has no plottable spec")
    _kind, labels, values, _name = frame
    b = lay.body
    png = render_chart_png(labels, values, kind=b["kind"],
                           name=b["series"][0]["name"], theme=theme)
    box_w, box_h = b["plot_w_mm"], BODY_H_MM - 8.0     # the PNG canvas is 16:9
    w, h = box_w, box_w * 9 / 16
    if h > box_h:
        h, w = box_h, box_h * 16 / 9
    x = MARGIN_X_MM + (CONTENT_W_MM - w) / 2
    y = _BODY_TOP + (BODY_H_MM - h) / 2
    slide.shapes.add_picture(BytesIO(png), _mm(x), _mm(y), _mm(w), _mm(h))


def _figure(slide, plan, lay, assets, theme):
    asset = assets.get(plan.visual_spec.reuse_asset_id or "")
    if asset is None or not lay.figure_src:  # pragma: no cover - upstream would have degraded
        raise DeckLayoutError(f"pptx: slide {plan.slide_index} figure asset is gone")
    _header(slide, plan, lay, theme)
    b = lay.body
    has_notes = b["notes"]["cards"]
    col_w = CONTENT_W_MM * 0.62 if has_notes else CONTENT_W_MM
    w, h = b["w_mm"], b["h_mm"]
    x = MARGIN_X_MM + (col_w - w) / 2
    y = _BODY_TOP + (BODY_H_MM - b["caption_pt"] * 0.5 - h) / 2 - 3.0
    with open(lay.figure_src, "rb") as fh:
        slide.shapes.add_picture(BytesIO(fh.read()), _mm(x), _mm(y), _mm(w), _mm(h))
    cap = " ".join(b["caption_lines"])
    if cap:
        _text(slide, MARGIN_X_MM, y + h + 2.5, col_w, 7.0, cap, b["caption_pt"],
              _rgb(theme.faint), align=PP_ALIGN.CENTER)
    if has_notes:
        nx = MARGIN_X_MM + CONTENT_W_MM * 0.66
        note_w = CONTENT_W_MM * 0.34
        slot_h, gap = b["notes"]["slot_h_mm"], b["notes"]["gap_mm"]
        for i, (cell, c) in enumerate(zip(b["notes"]["cards"], plan.cards)):
            _card_box(slide, nx, _BODY_TOP + i * (slot_h + gap), note_w, slot_h,
                      theme, c.label, cell["label_pt"], card_detail(c),
                      cell["detail_pt"])


def _set_cell(cell, text: str, pt: float, color: RGBColor, *, bold: bool) -> None:
    cell.text = text
    cell.margin_left = cell.margin_right = _mm(2)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = cell.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    if p.runs:
        _style_run(p.runs[0], pt, color, bold=bold)


def _compare(slide, plan, lay, assets, theme):
    """COMPARISON → native table: card label = column header, takeaway/±metric rows."""
    _header(slide, plan, lay, theme)
    b = lay.body
    ncols = len(b["cols"])
    nrows = 1 + max(len(c["cells"]) for c in b["cols"])
    gf = slide.shapes.add_table(nrows, ncols, _mm(MARGIN_X_MM), _mm(_BODY_TOP + 6),
                                _mm(CONTENT_W_MM), _mm(9.0 * nrows))
    table = gf.table
    table.first_row = True
    ink, prim = _rgb(theme.ink), _rgb(theme.primary)
    for j, c in enumerate(plan.cards):
        _set_cell(table.cell(0, j), c.label, TIERS["body"], prim, bold=True)
        cells = [c.takeaway] + ([c.metric_highlight] if c.metric_highlight else [])
        for i in range(nrows - 1):
            _set_cell(table.cell(1 + i, j),
                      cells[i] if i < len(cells) else "",
                      TIERS["caption"], ink, bold=False)


def _table(slide, plan, lay, assets, theme):
    """TABLE → native two-column table: label | detail, one row per card."""
    _header(slide, plan, lay, theme)
    rows = [(c.label, card_detail(c)) for c in plan.cards]
    gf = slide.shapes.add_table(len(rows), 2, _mm(MARGIN_X_MM), _mm(_BODY_TOP + 6),
                                _mm(CONTENT_W_MM), _mm(9.5 * len(rows)))
    table = gf.table
    table.first_row = True
    ink, prim = _rgb(theme.ink), _rgb(theme.primary)
    for i, (label, detail) in enumerate(rows):
        _set_cell(table.cell(i, 0), label, TIERS["body"], prim, bold=True)
        _set_cell(table.cell(i, 1), detail, TIERS["caption"], ink, bold=False)


def _cards(slide, plan, lay, assets, theme):
    """cardsSlide: STRUCTURED_CARDS grid or QUADRANT_MATRIX 2×2 (body carries cols)."""
    _header(slide, plan, lay, theme)
    b = lay.body
    cols = b["cols"]
    for i, (cell, c) in enumerate(zip(b["cards"], plan.cards)):
        r, col = divmod(i, cols)
        x = MARGIN_X_MM + col * (b["slot_w_mm"] + b["gap_mm"])
        y = _BODY_TOP + r * (b["slot_h_mm"] + b["gap_mm"])
        _card_box(slide, x, y, b["slot_w_mm"], b["slot_h_mm"], theme,
                  c.label, cell["label_pt"], card_detail(c), cell["detail_pt"])


_DISPATCH: dict[str, Callable] = {
    "thesisSlide": _thesis,
    "archSlide": _arch,
    "flowSlide": _steps,
    "timelineSlide": _steps,
    "loopSlide": _steps,
    "funnelSlide": _funnel,
    "chartSlide": _chart,
    "figureSlide": _figure,
    "compareSlide": _compare,
    "tableSlide": _table,
    "cardsSlide": _cards,
}


# ── cover + deck entry point ──────────────────────────────────────────────────

def _cover(prs: Presentation, brief: S.PresentationBrief, theme: ThemeTokens,
           document_title: str, source_names: list[str]) -> None:
    slide = _blank(prs, theme)
    _text(slide, MARGIN_X_MM + 10, PAGE_H_MM * 0.32, CONTENT_W_MM - 20, 22.0,
          document_title or brief.deck_id, TIERS["display"], _rgb(theme.primary),
          bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    ln = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                _mm(PAGE_W_MM / 2 - 30), _mm(PAGE_H_MM * 0.32 + 24),
                                _mm(60), _mm(0.9))
    ln.fill.solid()
    ln.fill.fore_color.rgb = _rgb(theme.accent)
    ln.line.fill.background()
    ln.shadow.inherit = False
    _text(slide, MARGIN_X_MM + 20, PAGE_H_MM * 0.32 + 32, CONTENT_W_MM - 40, 12.0,
          brief.thesis, TIERS["body"], _rgb(theme.ink), align=PP_ALIGN.CENTER)
    _text(slide, MARGIN_X_MM, PAGE_H_MM - MARGIN_Y_MM - 14, CONTENT_W_MM, 6.0,
          f"{len(brief.slides)} slides · {brief.target_audience}",
          TIERS["caption"], _rgb(theme.muted), align=PP_ALIGN.CENTER)
    if source_names:
        _text(slide, MARGIN_X_MM, PAGE_H_MM - MARGIN_Y_MM - 8, CONTENT_W_MM, 5.0,
              "Sources: " + ", ".join(source_names), TIERS["micro"],
              _rgb(theme.faint), align=PP_ALIGN.CENTER)


def build_brief_pptx(brief: S.PresentationBrief, layouts: list[BriefSlideLayout],
                     assets: dict[str, S.VisualAsset], *, theme: ThemeTokens,
                     document_title: str = "",
                     source_names: list[str] | None = None) -> bytes:
    """Cover + one native slide per layout. Unmapped fns fail loudly."""
    if len(layouts) != len(brief.slides):
        raise DeckLayoutError(
            f"pptx: {len(brief.slides)} slides but {len(layouts)} layouts")
    prs = Presentation()
    prs.slide_width, prs.slide_height = _mm(PAGE_W_MM), _mm(PAGE_H_MM)
    _cover(prs, brief, theme, document_title, list(source_names or []))
    for plan, lay in zip(brief.slides, layouts):
        fn = _DISPATCH.get(lay.fn)
        if fn is None:
            raise DeckLayoutError(
                f"pptx: slide {plan.slide_index} template "
                f"{lay.template.value}/{lay.fn} has no PPTX branch")
        slide = _blank(prs, theme)
        fn(slide, plan, lay, assets, theme)
        _footer(slide, lay, theme)
        _notes(slide, plan, lay)
    buf = BytesIO()
    prs.save(buf)
    return buf.getvalue()


__all__ = ["build_brief_pptx"]
