"""Golden-snapshot tests for the pure Typst emitter (deterministic, no LLM)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.api.tools.toolkit.deck import layout as L
from apps.api.tools.toolkit.deck import typst_deck as T
from apps.api.tools.toolkit.deck.rules import derive_visual_plan
from tests._deck_fixtures import (
    arch_slide,
    cards_slide,
    chart_slide,
    compare_slide,
    flow_slide,
    hero_slide,
    make_deck,
    make_digest,
)

GOLDEN = Path(__file__).parent / "goldens" / "deck_all_types.typ"


def all_types_deck():
    tl = flow_slide(4, with_when=True).model_copy(update={"purpose": "TIMELINE",
                                                          "relationship": "sequential"})
    return make_deck([hero_slide(), cards_slide(4), flow_slide(5), tl,
                      compare_slide(), arch_slide(), chart_slide()])


def compiled(deck) -> str:
    digest = make_digest()
    plans = [derive_visual_plan(s, digest) for s in deck.slides]
    decks = deck.model_copy(update={"visual_plan": plans})
    lays = L.layout_deck(decks)
    return T.compile_deck_typst(decks, lays)


class TestEmitter:
    def test_deterministic(self):
        deck = all_types_deck()
        assert compiled(deck) == compiled(deck)

    def test_covers_every_slide_fn(self):
        src = compiled(all_types_deck())
        for fn in T.SLIDE_FN.values():
            assert f"#{fn}(" in src
        assert "#deckCover(" in src
        assert src.count("#pagebreak()") == len(all_types_deck().slides)

    def test_citations_emitted(self):
        src = compiled(all_types_deck())
        assert "[src1:1-3]" in src

    def test_golden_snapshot(self):
        src = compiled(all_types_deck())
        assert GOLDEN.exists(), "golden missing — regenerate via scripts or first run"
        assert src == GOLDEN.read_text(encoding="utf-8")

    def test_strings_escaped(self):
        deck = all_types_deck()
        s0 = deck.slides[0].model_copy(update={"key_message": 'he said "hi" \\ done'})
        deck = deck.model_copy(update={"slides": [s0] + list(deck.slides[1:])})
        src = compiled(deck)
        assert '"he said \\"hi\\" \\\\ done"' in src

    def test_layout_dict_keys_match_template_contract(self):
        # spot-check the keys the template actually reads
        deck = all_types_deck()
        digest = make_digest()
        for s in deck.slides:
            plan = derive_visual_plan(s, digest)
            lay = L.layout_slide(s, plan)
            assert {"title_lines", "title_pt", "kicker_lines", "kicker_pt"} <= set(lay.header)
            if lay.visual_type == "TEXT_HERO":
                assert {"message_lines", "message_pt", "emphasis"} <= set(lay.body)
            if lay.visual_type == "CHART":
                for ser in lay.body["series"]:
                    assert "name" in ser and "points" in ser


@pytest.mark.skipif(shutil.which("typst") is None,
                    reason="typst binary only present in the worker container")
class TestCompile:
    def test_fixture_deck_compiles_to_pdf(self, tmp_path):
        src = compiled(all_types_deck())
        typ = tmp_path / "deck.typ"
        typ.write_text(src, encoding="utf-8")
        pdf = tmp_path / "deck.pdf"
        res = shutil.os.system(f'typst compile "{typ}" "{pdf}"')
        assert res == 0
        assert pdf.read_bytes()[:5] == b"%PDF-"

    def test_visual_types_really_paint_geometry_in_pdf(self, tmp_path):
        # Case-C guard (real-model replay 2026-09-14): a VisualPlan saying CARDS/
        # ARCHITECTURE is worthless if the Typst template silently degrades to text.
        # Compile the all-types deck through the CANONICAL render path and count the
        # vector drawings each visual page paints (boxes/arrows/tiers/axes).
        import pymupdf

        from apps.api.tools.toolkit.deck.render import render_deck_pdf

        res = render_deck_pdf(all_types_deck(), tmp_path)
        assert res.pdf and res.report.ok
        doc = pymupdf.open(stream=res.pdf, filetype="pdf")
        try:
            # page order: cover, hero, CARDS, FLOWCHART, TIMELINE, COMPARISON, ARCH, CHART
            floors = {2: ("TEXT_HERO", 1), 3: ("CARDS", 4), 4: ("FLOWCHART", 5),
                      5: ("TIMELINE", 4), 6: ("COMPARISON", 3),
                      7: ("ARCHITECTURE", 3), 8: ("CHART", 3)}
            for pno, (vt, floor) in floors.items():
                n = len(doc[pno - 1].get_drawings())
                if vt == "TEXT_HERO":
                    assert n <= 2, f"TEXT_HERO page {pno} drew {n} paths — should be header rule only"
                else:
                    assert n >= floor, (
                        f"{vt} page {pno}: only {n} drawings (floor {floor}) — "
                        "renderer degraded to text")
        finally:
            doc.close()
