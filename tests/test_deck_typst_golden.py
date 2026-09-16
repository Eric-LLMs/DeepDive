"""Golden-snapshot tests for the brief-native Typst emitter (zero LLM, §1.2.6).

Same brief + same assets ⇒ byte-identical Typst source: the golden file is the
drift tripwire for emitter or template changes. The all-templates deck exercises
every Typst function the Visual Compiler can emit, in the happy (non-degraded)
configuration — degradations are pinned with their own reasons in
``tests/test_deck_compiler.py``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from apps.api.tools.toolkit.deck import typst_deck as T
from apps.api.tools.toolkit.deck.compiler import layout_engine as LE
from tests.test_deck_compiler import brief_from, chart_gen, figure_asset, slide

GOLDEN = Path(__file__).parent / "goldens" / "deck_brief_all_types.typ"


def all_templates_brief(tmp_path):
    slides = [
        slide(1, "TEXTUAL_THESIS", n_cards=0),
        slide(2, "STRUCTURED_CARDS", n_cards=4),
        slide(3, "PIPELINE_FLOW", n_cards=3),
        slide(4, "TIMELINE", n_cards=3),
        slide(5, "CIRCULAR_LOOP", n_cards=3),
        slide(6, "INVERTED_PYRAMID", n_cards=3),
        slide(7, "COMPARISON", n_cards=3),
        slide(8, "SYSTEM_BLUEPRINT", n_cards=3),
        slide(9, "QUADRANT_MATRIX", n_cards=4),
        slide(10, "TABLE", n_cards=3),
        slide(11, "DATA_CHART", n_cards=0, policy="QUANTITATIVE_CODE",
              gen=chart_gen()),
        slide(12, "SOURCE_FIGURE_REUSE", n_cards=2, policy="SOURCE_FIDELITY",
              reuse="fig_1"),
    ]
    brief = brief_from(slides)
    asset = figure_asset(tmp_path)
    layouts, warns = LE.build_deck_layouts(brief, {asset.asset_id: asset})
    assert warns == []                      # happy path: no silent degradation
    return brief, layouts, asset


def compiled(tmp_path) -> str:
    brief, layouts, _ = all_templates_brief(tmp_path)
    return T.compile_brief_typst(brief, layouts, document_title="RAG Survey",
                                 source_names=["doc1.pdf"])


class TestEmitter:
    def test_template_carries_version_2(self):
        assert "#let SLIDES_TEMPLATE_VERSION = 2" in T.load_template()

    def test_deterministic(self, tmp_path):
        assert compiled(tmp_path) == compiled(tmp_path)

    def test_covers_every_template_fn(self, tmp_path):
        src = compiled(tmp_path)
        for _, fn in LE.HAPPY.values():
            assert f"#{fn}(" in src, fn
        assert src.count("#deckCover(") == 1
        assert src.count("#pagebreak()") == 12      # cover + one per slide

    def test_citations_emitted(self, tmp_path):
        src = compiled(tmp_path)
        assert "[doc1:3]" in src                     # t1's page+start_line locator

    def test_flattened_vs_nested_bodies(self, tmp_path):
        # thesisSlide reads its body FLATTENED; everything else nests under body
        src = compiled(tmp_path)
        assert 'message_lines: ("Message number 1.",)' in src   # 1-tuple trailing comma
        assert 'body: (cols:' in src

    def test_strings_escaped(self, tmp_path):
        brief, _layouts, _asset = all_templates_brief(tmp_path)
        s0 = brief.slides[0].model_copy(
            update={"central_message": 'he said "hi" \\ done'})
        brief = brief.model_copy(update={"slides": [s0] + list(brief.slides[1:])})
        src = T.compile_brief_typst(brief, LE.build_deck_layouts(
            brief, {})[0], document_title="t")
        assert '"he said \\"hi\\" \\\\ done"' in src

    def test_golden_snapshot(self, tmp_path):
        src = compiled(tmp_path)
        if os.environ.get("DECK_GOLDEN_UPDATE") == "1":
            GOLDEN.write_text(src, encoding="utf-8")
            pytest.skip("golden regenerated")
        assert GOLDEN.exists(), "golden missing — run with DECK_GOLDEN_UPDATE=1"
        assert src == GOLDEN.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("typst") is None,
                    reason="typst binary only present in the worker container")
class TestCompile:
    def test_golden_source_compiles_to_pdf(self, tmp_path):
        brief, layouts, _asset = all_templates_brief(tmp_path)
        src = T.compile_brief_typst(brief, layouts, document_title="RAG Survey",
                                    source_names=["doc1.pdf"])
        for lay in layouts:                           # what render.render_brief_pdf does
            if lay.figure_src:
                shutil.copyfile(lay.figure_src, tmp_path / lay.body["name"])
        typ = tmp_path / "deck.typ"
        typ.write_text(src, encoding="utf-8")
        pdf = tmp_path / "deck.pdf"
        assert shutil.os.system(f'typst compile "{typ}" "{pdf}"') == 0
        assert pdf.read_bytes()[:5] == b"%PDF-"
