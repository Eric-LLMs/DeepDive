"""Visual Compiler M1: brief → template → geometry → Typst → PDF, zero LLM.

Pins the plan §M1.6-7 contracts: the 14-grammar → 7-template mapping, EXPLICIT
degradations (every fallback carries a reason that survives into
``RenderReport.layout_warnings`` — never silent, §9.4), chart-spec validation
shared with the materializer, locator→citation formatting, brief-native Typst
emit determinism (same brief + assets ⇒ byte-identical source, §1.2.6), the
Pillow chart materializer's byte stability, and the real-typst render gate
including source-figure embedding.
"""
from __future__ import annotations

import shutil

import pytest

from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.deck.compiler import layout_engine as LE
from apps.api.tools.toolkit.deck.compiler import materializer as MZ
from apps.api.tools.toolkit.deck.compiler import theme as TH
from apps.api.tools.toolkit.deck.compiler.theme import ACADEMIC_LIGHT, theme_for
from apps.api.tools.toolkit.deck.errors import DeckLayoutError
from apps.api.tools.toolkit.deck.render import (
    brief_to_marp,
    brief_to_pptx_slides,
    render_brief_pdf,
)
from apps.api.tools.toolkit.deck.typst_deck import compile_brief_typst
from tests.test_deck_workflow import DOC_ID, PNG_BYTES, brief_payload

G = S.VisualGrammar
E = S.VisualGenerationPolicy


# ── brief/slide builders ──────────────────────────────────────────────────────

def slide(i: int, grammar: str, *, n_cards: int = 2, policy: str = "EXPLANATORY_DIAGRAM",
          gen: dict | None = None, reuse: str | None = None) -> dict:
    return {
        "slide_index": i, "title": f"Title {i}", "pedagogical_purpose": "EVIDENCE",
        "central_message": f"Message number {i}.", "source_section_ids": ["sec_1"],
        "visual_spec": {"visual_spec_id": f"v{i}", "grammar": grammar,
                        "policy": policy, "semantic_intent": "intent",
                        "reuse_asset_id": reuse, "generation_spec": gen},
        "cards": [{"label": f"L{j}", "takeaway": f"takeaway {j}",
                   "epistemic_type": "FACT", "trace_id": "t1"}
                  for j in range(1, n_cards + 1)],
        "speaker_notes": "notes",
    }


def brief_from(slides: list[dict]) -> S.PresentationBrief:
    data = brief_payload("d1", ["sec_1"], with_fig=False)
    data["slides"] = slides
    return S.PresentationBrief.model_validate(data)


def chart_gen(**over) -> dict:
    spec = {"chart": "bar", "labels": ["Q1", "Q2", "Q3"], "values": [0.4, 0.565, 0.9]}
    spec.update(over)
    return spec


def figure_asset(tmp_path, *, ok: bool = True) -> S.VisualAsset:
    path = tmp_path / "raster_p2_1.png"
    path.write_bytes(PNG_BYTES if ok else b"not an image")
    return S.VisualAsset(asset_id="fig_1", page=2,
                         type=S.VisualAssetType.RASTER_IMAGE, path=str(path),
                         semantic_hint="Figure 1: the RAG pipeline")


# ── the mapping is total and the fallbacks are explicit ──────────────────────

def test_all_14_grammars_map_happily():
    for g, (tpl, fn) in LE.HAPPY.items():
        flags = {"asset_ok": g in (G.SOURCE_FIGURE_REUSE, G.ANNOTATED_FIGURE),
                 "chart_ok": g is G.DATA_CHART, "n_cards": 4}
        t, f, why = LE.resolve_layout_template(g, **flags)
        assert (t, f) == (tpl, fn) and why is None, g


@pytest.mark.parametrize(("grammar", "flags", "tpl", "fn"), [
    (G.SOURCE_FIGURE_REUSE, {"asset_ok": False, "chart_ok": False, "n_cards": 2},
     LE.LayoutTemplate.SPLIT_TWO_COLUMN, "cardsSlide"),
    (G.DATA_CHART, {"asset_ok": False, "chart_ok": False, "n_cards": 0},
     LE.LayoutTemplate.CENTERED_THESIS, "thesisSlide"),
    (G.QUADRANT_MATRIX, {"asset_ok": False, "chart_ok": False, "n_cards": 3},
     LE.LayoutTemplate.SPLIT_TWO_COLUMN, "cardsSlide"),
    (G.PIPELINE_FLOW, {"asset_ok": False, "chart_ok": False, "n_cards": 1},
     LE.LayoutTemplate.SPLIT_TWO_COLUMN, "cardsSlide"),
    (G.TABLE, {"asset_ok": False, "chart_ok": False, "n_cards": 0},
     LE.LayoutTemplate.CENTERED_THESIS, "thesisSlide"),
])
def test_degradations_carry_explicit_reasons(grammar, flags, tpl, fn):
    t, f, why = LE.resolve_layout_template(grammar, **flags)
    assert (t, f) == (tpl, fn)
    assert why, "a fallback without a reason is a silent degradation"


def test_quadrant_matrix_with_four_cards_is_not_a_fallback():
    t, f, why = LE.resolve_layout_template(G.QUADRANT_MATRIX,
                                           asset_ok=False, chart_ok=False, n_cards=4)
    assert t is LE.LayoutTemplate.QUADRANT_2X2 and f == "cardsSlide" and why is None


# ── chart spec validation (shared with the materializer) ─────────────────────

def test_chart_frame_accepts_and_infers():
    assert LE.chart_frame(chart_gen()) == ("bar", ["Q1", "Q2", "Q3"],
                                           [0.4, 0.565, 0.9], "")
    kind, *_ = LE.chart_frame({"labels": ["2021", "2022"], "values": [1, 2]})
    assert kind == "line"                          # numeric labels ⇒ ordered axis


def test_chart_frame_rejects_unusable_specs():
    for junk in (None, {}, {"labels": ["a"], "values": [1]},
                 {"labels": ["a", "b"], "values": [1]},
                 {"labels": ["a", "b"], "values": ["n/a", 1]},
                 {"labels": [str(i) for i in range(9)], "values": list(range(9))}):
        assert LE.chart_frame(junk) is None


# ── layout building: geometry, citations, figure reuse ───────────────────────

def test_cards_geometry_mirrors_v1_and_citations_survive():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    lays, warns = LE.build_deck_layouts(brief, {})
    assert warns == []
    lay = lays[0]
    assert lay.fn == "cardsSlide"
    assert lay.body["slot_w_mm"] == round((306.67 - 8.0) / 2, 2)   # v1 formula
    assert lay.body["cols"] == 2 and lay.body["rows"] == 1
    assert lay.citations == [f"[{DOC_ID}:3]"]                      # t1: line 3
    assert lay.header["kicker_lines"], "cards keep the central message kicker"


def test_thesis_header_has_no_duplicate_kicker():
    brief = brief_from([slide(1, "TEXTUAL_THESIS", n_cards=0)])
    lay = LE.build_deck_layouts(brief, {})[0][0]
    assert lay.fn == "thesisSlide"
    assert lay.header["kicker_lines"] == []
    assert lay.body["message_lines"] and lay.body["chips"] == []


def test_figure_slide_embeds_asset_and_records_source(tmp_path):
    asset = figure_asset(tmp_path)
    brief = brief_from([slide(1, "SOURCE_FIGURE_REUSE", n_cards=2,
                              policy="SOURCE_FIDELITY", reuse="fig_1")])
    lays, warns = LE.build_deck_layouts(brief, {asset.asset_id: asset})
    lay = lays[0]
    assert warns == [] and lay.fn == "figureSlide"
    assert lay.body["name"] == "figure_s1.png"
    assert lay.body["w_mm"] > 0 and lay.body["h_mm"] > 0
    assert lay.body["caption_lines"] == [asset.semantic_hint]
    assert lay.body["notes"]["cards"] and lay.figure_src == str(asset.path)


def test_missing_asset_degrades_to_cards_loudly(tmp_path):
    brief = brief_from([slide(1, "ANNOTATED_FIGURE", n_cards=2,
                              policy="SOURCE_FIDELITY", reuse="ghost")])
    lays, warns = LE.build_deck_layouts(brief, {})
    assert lays[0].fn == "cardsSlide"
    assert warns and "not on disk" in warns[0]


def test_undecodable_asset_degrades_to_cards(tmp_path):
    asset = figure_asset(tmp_path, ok=False)
    brief = brief_from([slide(1, "SOURCE_FIGURE_REUSE", n_cards=2,
                              policy="SOURCE_FIDELITY", reuse="fig_1")])
    lays, warns = LE.build_deck_layouts(brief, {asset.asset_id: asset})
    assert lays[0].fn == "cardsSlide" and warns


def test_template_diversity_set():
    brief = brief_from([
        slide(1, "TEXTUAL_THESIS", n_cards=0),
        slide(2, "STRUCTURED_CARDS", n_cards=3),
        slide(3, "DATA_CHART", policy="QUANTITATIVE_CODE", gen=chart_gen()),
        slide(4, "PIPELINE_FLOW", n_cards=4),
    ])
    lays, warns = LE.build_deck_layouts(brief, {})
    assert warns == []
    kinds = LE.layout_template_kinds(lays)
    assert kinds == {LE.LayoutTemplate.CENTERED_THESIS,
                     LE.LayoutTemplate.SPLIT_TWO_COLUMN,
                     LE.LayoutTemplate.DATA_DASHBOARD,
                     LE.LayoutTemplate.HORIZONTAL_FLOW}


def test_compare_cells_padded_for_the_table_template():
    brief = brief_from([slide(1, "COMPARISON", n_cards=2),
                        {"slide_index": 2, "title": "Asym",
                         "pedagogical_purpose": "TRADE_OFF",
                         "central_message": "Asymmetric columns.",
                         "source_section_ids": ["sec_1"],
                         "visual_spec": {"visual_spec_id": "v2", "grammar": "COMPARISON",
                                         "policy": "EXPLANATORY_DIAGRAM",
                                         "semantic_intent": "x"},
                         "cards": [
                             {"label": "A", "takeaway": "t1", "metric_highlight": "m",
                              "epistemic_type": "CLAIM", "trace_id": "t1"},
                             {"label": "B", "takeaway": "t2",
                              "epistemic_type": "CLAIM", "trace_id": "t1"}],
                         "speaker_notes": ""}])
    lay = LE.build_deck_layouts(brief, {})[0][1]
    lengths = [len(c["cells"]) for c in lay.body["cols"]]
    assert len(set(lengths)) == 1, "the template indexes every column at every row"


# ── the emitter stays pure and deterministic ─────────────────────────────────

def test_compile_brief_typst_is_deterministic_and_complete():
    brief = brief_from([
        slide(1, "SOURCE_FIGURE_REUSE", n_cards=1, policy="SOURCE_FIDELITY",
              reuse="fig_1"),
        slide(2, "TEXTUAL_THESIS", n_cards=2),
    ])
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        a = figure_asset(Path(td))
        lays, _ = LE.build_deck_layouts(brief, {a.asset_id: a})
    src1 = compile_brief_typst(brief, lays, document_title="RAG Survey",
                               source_names=["paper.pdf"])
    src2 = compile_brief_typst(brief, lays, document_title="RAG Survey",
                               source_names=["paper.pdf"])
    assert src1 == src2
    assert "#deckCover(" in src1 and "#figureSlide(" in src1 and "#thesisSlide(" in src1
    assert src1.count("#pagebreak()") == 2
    assert '"figure_s1.png"' in src1
    # thesis body is FLATTENED (hero contract); figures nest under body
    assert "message_lines: (" in src1 and "body: (" in src1


def test_brief_citation_formats():
    lines = LE.cite_locator(S.SourceLocator(doc_id=DOC_ID, start_line=3, end_line=7))
    page = LE.cite_locator(S.SourceLocator(doc_id=DOC_ID, page=2))
    msg = LE.cite_locator(S.SourceLocator(doc_id="t.md", message_id="a2b3c4d5e6"))
    assert (lines, page, msg) == (f"[{DOC_ID}:3-7]", f"[{DOC_ID}:p2]", "[t.md:#a2b3c4d5]")


# ── theme mirrors the template palette (they cannot import each other) ───────

def test_academic_light_mirrors_slides_typ():
    from apps.api.tools.toolkit.deck.typst_deck import load_template
    tpl = load_template()
    for role, hex_ in [("primary", "#1F3A5F"), ("accent", "#C4531B"),
                       ("ink", "#22303C"), ("muted", "#5B6B7A"),
                       ("faint", "#8A97A3"), ("border", "#B9C4CF"),
                       ("card", "#F4F6F8"), ("band", "#EEF2F6")]:
        rgb = getattr(ACADEMIC_LIGHT, role)
        assert f'rgb("{hex_}")' in tpl, hex_
        assert MZ._hex(rgb).upper() == hex_.upper(), role
    assert theme_for("Technical Masterclass") is ACADEMIC_LIGHT
    assert theme_for("Dark Blueprint") is TH.DARK_BLUEPRINT


# ── materializer: deterministic Pillow drawings (PPTX/M2 feed) ───────────────

def test_materializer_is_byte_stable_and_valid():
    a = MZ.render_chart_png(["Q1", "Q2", "Q3"], [0.4, 0.565, 0.9], kind="bar",
                            name="Cost", theme=ACADEMIC_LIGHT)
    b = MZ.render_chart_png(["Q1", "Q2", "Q3"], [0.4, 0.565, 0.9], kind="bar",
                            name="Cost", theme=ACADEMIC_LIGHT)
    assert a == b and a[:8] == b"\x89PNG\r\n\x1a\n"
    c = MZ.render_chart_png(["2021", "2022"], [1.0, 2.5], kind="line",
                            name="Growth", theme=ACADEMIC_LIGHT)
    assert a != c
    import io

    from PIL import Image
    im = Image.open(io.BytesIO(a))
    assert im.size == (MZ._W, MZ._H)


def test_materializer_rejects_unknown_kind():
    with pytest.raises(DeckLayoutError):
        MZ.render_chart_png(["a", "b"], [1, 2], kind="pie", name="x",
                            theme=ACADEMIC_LIGHT)


def test_material_asset_name_shape():
    assert MZ.material_asset_name("d1", 3, "v7") == "d1_slide_3_v7.png"


# ── zero LLM by construction + closed-world ──────────────────────────────────

def test_compiler_modules_never_touch_llm_or_search():
    import inspect
    for mod in (LE, MZ, TH):
        src = inspect.getsource(mod)
        assert "ToolRuntime" not in src and "web_search" not in src
        assert "complete" not in src.replace("completed", ""), mod.__name__


# ── the real render gate (typst binary) ──────────────────────────────────────

@pytest.mark.skipif(shutil.which("typst") is None,
                    reason="typst binary only present in the worker container")
def test_render_brief_pdf_compiles_every_template_family(tmp_path):
    asset = figure_asset(tmp_path)
    # unique per-slide trace locators (doc_id is shared — allowed)
    brief = brief_from([
        slide(1, "TEXTUAL_THESIS", n_cards=2),
        slide(2, "STRUCTURED_CARDS", n_cards=4),
        slide(3, "PIPELINE_FLOW", n_cards=3),
        slide(4, "TIMELINE", n_cards=3),
        slide(5, "INVERTED_PYRAMID", n_cards=3),
        slide(6, "CIRCULAR_LOOP", n_cards=3),
        slide(7, "COMPARISON", n_cards=3),
        slide(8, "TABLE", n_cards=3),
        slide(9, "SYSTEM_BLUEPRINT", n_cards=3),
        slide(10, "DATA_CHART", policy="QUANTITATIVE_CODE", gen=chart_gen()),
        slide(11, "SOURCE_FIGURE_REUSE", n_cards=2, policy="SOURCE_FIDELITY",
              reuse="fig_1"),
        slide(12, "QUADRANT_MATRIX", n_cards=4),
    ])
    res = render_brief_pdf(brief, [asset], tmp_path,
                           document_title="RAG Survey", source_names=["paper.pdf"])
    assert res.report.layout_warnings == []
    assert res.report.ok, res.report.typst_warnings
    assert res.report.pages_expected == res.report.pages_actual == 13
    assert res.pdf[:5] == b"%PDF-"
    assert (tmp_path / "figure_s11.png").is_file()       # slice copied beside the .typ

    import pymupdf
    doc = pymupdf.open(stream=res.pdf, filetype="pdf")
    try:
        assert doc.page_count == 13
        # geometry really paints (Case-C doctrine): the CARDS page draws its
        # boxes, the chart page its axes/bars, and the figure page embeds pixels.
        assert len(doc[2].get_drawings()) >= 4, "CARDS degraded to text"
        assert len(doc[10].get_drawings()) >= 3, "CHART degraded to text"
        assert len(doc[11].get_images()) >= 1, "figure slide lost its embedded image"
        assert doc[1].rect.width / doc[1].rect.height == pytest.approx(
            338.67 / 190.5, abs=0.02)
    finally:
        doc.close()


@pytest.mark.skipif(shutil.which("typst") is None,
                    reason="typst binary only present in the worker container")
def test_render_reports_fallbacks_not_failures(tmp_path):
    brief = brief_from([slide(1, "SOURCE_FIGURE_REUSE", n_cards=2,
                              policy="SOURCE_FIDELITY", reuse="ghost")])
    res = render_brief_pdf(brief, [], tmp_path, document_title="X")
    assert res.report.ok
    assert len(res.report.layout_warnings) == 1 and "not on disk" in res.report.layout_warnings[0]


# ── brief-derived compat exports ──────────────────────────────────────────────

def test_brief_exports_derive_from_the_brief_alone():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    md = brief_to_marp(brief, document_title="RAG Survey")
    assert md.startswith("---\nmarp: true")
    assert "# RAG Survey" in md and "## Title 1" in md
    assert "- L1: takeaway 1" in md and f"*Sources: [{DOC_ID}:3]*" in md
    assert "<!-- Speaker notes: notes -->" in md
    pptx = brief_to_pptx_slides(brief)
    assert pptx == [("Title 1", "L1: takeaway 1\nL2: takeaway 2")]
