"""Render stage for the deck engine: DeckSpec/PresentationBrief → deck.pdf (+ exports).

Two entry points share one compile/inspect pipeline:

* :func:`render_deck_pdf` — legacy DeckSpec path (retires with DeckSpec);
* :func:`render_brief_pdf` — Visual Compiler path (M1): brief → compiler layouts →
  brief-native Typst emit. The ``RenderReport`` gate stays loud — a failing report
  makes the pipeline raise; nothing downstream silently degrades, and the compiler's
  explicit template fallbacks surface in ``layout_warnings`` (§9.4).

Compat exports (errata #7): Marp ``.md`` and the pptx ``(heading, bullets)`` list are
derived from the SAME model so all formats agree; the canonical artifact stays deck.pdf.
Speaker notes are NOT rendered into the PDF (they live in deck.json only).
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

# reuse the battle-tested CLI wrapper from the artifact compiler
from artifact_compiler.typst_compiler import run_typst_compile

from . import schema as S
from .layout import PAGE_H_MM, PAGE_W_MM, layout_deck
from .models import DeckSpec, RenderReport
from .typst_deck import compile_brief_typst, compile_deck_typst

_ASPECT = PAGE_W_MM / PAGE_H_MM          # 16:9
_WARN_LINE = re.compile(r"^(warn|error): (.*)$", re.MULTILINE)


@dataclass
class DeckRender:
    typst_source: str
    pdf: bytes | None
    report: RenderReport


def _inspect_pdf(pdf_path: Path, report: RenderReport) -> None:
    import pymupdf  # lazy: keep module import cheap for pure-export consumers

    doc = pymupdf.open(pdf_path)
    try:
        report.pages_actual = doc.page_count
        if doc.page_count > 0:
            p = doc[0]
            ratio = p.rect.width / p.rect.height if p.rect.height else 0.0
            report.aspect_ok = abs(ratio - _ASPECT) < 0.02
    finally:
        doc.close()


def render_deck_pdf(deck: DeckSpec, workdir: Path, *,
                    template: str | None = None,
                    typst_bin: str = "typst") -> DeckRender:
    """Compile the deck to ``workdir/deck.pdf`` and validate against RenderReport."""
    layouts = layout_deck(deck)
    source = compile_deck_typst(deck, layouts, template)
    report = RenderReport(pages_expected=deck.pages_expected())
    pdf_bytes = _compile_and_inspect(source, workdir, report, typst_bin=typst_bin)
    return DeckRender(typst_source=source, pdf=pdf_bytes, report=report)


def _compile_and_inspect(source: str, workdir: Path, report, *,
                         typst_bin: str) -> bytes | None:
    """Shared compile → PDF inspection for both render paths (report duck-typed)."""
    out_pdf = Path(workdir) / "deck.pdf"
    ok, stderr = run_typst_compile(source, workdir, out_pdf, typst_bin=typst_bin)
    report.compiled = ok
    report.typst_warnings = [m.group(2) for m in _WARN_LINE.finditer(stderr or "")]
    report.missing_fonts = [w for w in report.typst_warnings if "font" in w.lower()]
    if ok:
        try:
            _inspect_pdf(out_pdf, report)
        except Exception as exc:  # noqa: BLE001 - a broken PDF is a failed render
            report.compiled = False
            report.typst_warnings.append(f"pdf inspection failed: {exc}")
        else:
            report.overflow_suspect = [
                f"page {i + 1} text exceeds page box"
                for i in range(report.pages_actual)
                if _page_overflows(out_pdf, i)
            ]
    return out_pdf.read_bytes() if ok and out_pdf.exists() else None


def _page_overflows(pdf_path: Path, page_index: int) -> bool:
    """Cheap overflow heuristic: text bbox crossing the mediabox edge by > 1pt."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_index]
        box = page.rect
        td = page.get_text("dict")
        for block in td.get("blocks", []):
            bb = block.get("bbox")
            if not bb:
                continue
            if bb[0] < box.x0 - 1 or bb[1] < box.y0 - 1 or \
               bb[2] > box.x1 + 1 or bb[3] > box.y1 + 1:
                return True
        return False
    finally:
        doc.close()


# ── compat exports from the SAME DeckSpec (errata #7) ─────────────────────────

def deck_to_marp(deck: DeckSpec) -> str:
    """Marp Markdown: the legacy slides.md output, now derived from the deck model."""
    out: list[str] = ["---", "marp: true", "theme: default", "paginate: true", "---", ""]
    out += [f"# {deck.title}", ""]
    for s in deck.slides:
        out += ["---", "", f"## {s.title}", "", f"**Core idea:** {s.key_message}", ""]
        for line in _support_points(s):
            out.append(f"- {line}")
        cites = " ".join(_cite_strings(s))
        if cites:
            out += ["", f"*Sources: {cites}*"]
        if s.speaker_notes:
            out += ["", f"<!-- Speaker notes: {s.speaker_notes} -->"]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def deck_to_pptx_slides(deck: DeckSpec) -> list[tuple[str, str]]:
    """``(heading, bullets)`` tuples for media.build_text_pptx."""
    return [(s.title, "\n".join(_support_points(s))) for s in deck.slides]


def _support_points(slide) -> list[str]:
    p = slide.payload
    if p.items:
        return [f"{i.label}: {i.detail}".strip(": ").strip() for i in p.items]
    if p.steps:
        return [f"{st.when + ' — ' if st.when else ''}{st.label}: {st.detail}".strip(": ")
                for st in p.steps]
    if p.columns:
        rows = max((len(c.cells) for c in p.columns), default=0)
        return [" | ".join([c.header for c in p.columns])] + [
            " | ".join(c.cells[r] if r < len(c.cells) else "" for c in p.columns)
            for r in range(rows)]
    if p.series:
        return [f"{s.name}: " + ", ".join(f"{pt.x}={pt.y}" for pt in s.points)
                for s in p.series]
    return []


def _cite_strings(slide) -> list[str]:
    from .typst_deck import _cite
    return [_cite(r) for r in slide.provenance_refs]


# ── Visual Compiler path: PresentationBrief → PDF (+ brief-derived exports) ───

@dataclass
class BriefDeckRender:
    typst_source: str
    pdf: bytes | None
    report: S.RenderReport


def render_brief_pdf(brief: S.PresentationBrief, assets: list[S.VisualAsset],
                     workdir: Path, *, document_title: str = "",
                     source_names: list[str] | None = None,
                     template: str | None = None,
                     typst_bin: str = "typst") -> BriefDeckRender:
    """Zero-LLM render of the canonical brief: compiler layouts → typst → PDF.

    Referenced source slices are copied into ``workdir`` under the names the
    figure templates read, so the same brief + assets re-emit byte-identically.
    Every explicit template degradation lands in ``report.layout_warnings``.
    """
    from .compiler.layout_engine import build_deck_layouts

    wd = Path(workdir)
    layouts, warns = build_deck_layouts(brief, {a.asset_id: a for a in assets})
    for lay in layouts:
        if lay.figure_src:
            shutil.copyfile(lay.figure_src, wd / lay.body["name"])
    source = compile_brief_typst(brief, layouts, document_title=document_title,
                                 source_names=list(source_names or []),
                                 template=template)
    report = S.RenderReport(pages_expected=1 + len(brief.slides),
                            layout_warnings=warns)
    pdf_bytes = _compile_and_inspect(source, wd, report, typst_bin=typst_bin)
    return BriefDeckRender(typst_source=source, pdf=pdf_bytes, report=report)


def brief_to_marp(brief: S.PresentationBrief, *, document_title: str = "") -> str:
    """Marp Markdown derived straight from the brief (compat export)."""
    from .compiler.layout_engine import card_detail, cite_locator

    out: list[str] = ["---", "marp: true", "theme: default", "paginate: true",
                      "---", "", f"# {document_title or brief.deck_id}", ""]
    for s in brief.slides:
        out += ["---", "", f"## {s.title}", "", f"**Core idea:** {s.central_message}", ""]
        for c in s.cards:
            out.append(f"- {c.label}: {card_detail(c)}".rstrip(": "))
        cites: list[str] = []
        for c in s.cards:
            node = brief.traceability_graph.get(c.trace_id)
            if node is not None and node.locator is not None:
                cite = cite_locator(node.locator)
                if cite not in cites:
                    cites.append(cite)
        if cites:
            out += ["", f"*Sources: {' '.join(cites)}*"]
        if s.speaker_notes:
            out += ["", f"<!-- Speaker notes: {s.speaker_notes} -->"]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def brief_to_pptx_slides(brief: S.PresentationBrief) -> list[tuple[str, str]]:
    """``(heading, bullets)`` tuples for media.build_text_pptx, from the brief."""
    from .compiler.layout_engine import card_detail

    out: list[tuple[str, str]] = []
    for s in brief.slides:
        bullets = "\n".join(f"{c.label}: {card_detail(c)}".rstrip(": ")
                            for c in s.cards)
        out.append((s.title, bullets or s.central_message))
    return out
