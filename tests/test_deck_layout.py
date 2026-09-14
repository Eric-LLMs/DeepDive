"""Layout engine tests: deterministic slots, conservative measurement, no trimming."""
from __future__ import annotations

import pytest

from apps.api.tools.toolkit.deck import layout as L
from apps.api.tools.toolkit.deck.errors import DeckLayoutError
from apps.api.tools.toolkit.deck.rules import budget_violations, derive_visual_plan
from tests._deck_fixtures import (
    arch_slide,
    cards_slide,
    chart_slide,
    compare_slide,
    flow_slide,
    hero_slide,
    make_deck,
    make_digest,
    slide_payload,
    Step,
)
from apps.api.tools.toolkit.deck.models import ContentPayload


def laid_out(slide):
    plan = derive_visual_plan(slide, make_digest())
    return L.layout_slide(slide, plan)


class TestMeasurement:
    def test_cjk_wider_than_latin(self):
        cjk = L.text_width_mm("检索增强", 16)
        lat = L.text_width_mm("abcd", 16)
        assert cjk > lat

    def test_wrap_never_drops_text(self):
        text = "很长的中文文本需要换行 demonstrate wrapping behavior"
        lines = L.wrap_lines(text, 50.0, 16)
        joined = "".join(lines).replace(" ", "")
        original = text.replace(" ", "")
        assert set(original) <= set(joined)
        assert len(joined) == len(original)

    def test_overlong_latin_word_kept_intact(self):
        word = "Supercalifragilisticexpialidocious"
        lines = L.wrap_lines(word, 10.0, 16)
        assert lines == [word]

    def test_wrap_preserves_punctuation_verbatim(self):
        text = '配置 "A/B" 比例 72.5%; 结束。「引用」'
        lines = L.wrap_lines(text, 40.0, 16)
        joined = "".join(lines).replace(" ", "")
        assert joined == text.replace(" ", "")   # errata #4: not one char altered/dropped


class TestSlots:
    def test_hero_fits(self):
        lay = laid_out(hero_slide())
        assert lay.visual_type == "TEXT_HERO"
        assert lay.body["message_lines"]

    def test_cards_grid_geometry(self):
        lay = laid_out(cards_slide(4))
        b = lay.body
        assert b["cols"] == 4
        assert b["slot_w_mm"] * b["cols"] + b["gap_mm"] * (b["cols"] - 1) <= L.CONTENT_W_MM + 0.1
        assert len(b["cards"]) == 4

    def test_flow_lane(self):
        lay = laid_out(flow_slide(6))
        assert lay.body["n"] == 6
        assert lay.body["node_w_mm"] > 0

    def test_timeline_layout(self):
        s = flow_slide(4)
        s = s.model_copy(update={"purpose": "TIMELINE"})
        lay = laid_out(s)
        assert lay.visual_type == "TIMELINE"
        assert all(len(st["when_lines"]) >= 0 for st in lay.body["steps"])

    def test_arch_bands(self):
        lay = laid_out(arch_slide())
        assert len(lay.body["bands"]) == 3

    def test_comparison_table(self):
        lay = laid_out(compare_slide())
        assert len(lay.body["cols"]) == 2
        assert len(lay.body["cols"][0]["cells"]) == 3

    def test_chart_bars_scaled(self):
        lay = laid_out(chart_slide())
        b = lay.body
        assert b["kind"] in ("bar", "line")
        for s in b["series"]:
            for pt in s["points"]:
                assert 0 <= pt["y_mm"] <= b["plot_h_mm"] + 0.01


class TestNoTrimming:
    def test_impossible_content_raises(self):
        # thousands of CJK chars cannot fit even the smallest tier — must raise, not clip
        monster = "检" * 3000
        s = hero_slide().model_copy(update={"key_message": monster})
        with pytest.raises(DeckLayoutError):
            laid_out(s)

    def test_no_ellipsis_anywhere(self):
        lay = laid_out(cards_slide(4))
        blob = str(lay.body) + str(lay.header)
        assert "…" not in blob and "..." not in blob

    def test_key_message_never_lost(self):
        s = hero_slide()
        lay = laid_out(s)
        joined = "".join(lay.body["message_lines"])
        src = s.key_message
        assert set(src.replace(" ", "").replace(",", "")) <= set(
            joined.replace(" ", "").replace(",", ""))


class TestFitViolations:
    """Geometry dry-run: catches what unit budgets can't see (production job ef08eb70 —
    a 7-word Latin detail passed the 18-unit step budget yet overflowed a 6-node
    flowchart slot at the micro tier, killing the job at render time)."""

    def test_clean_slide_reports_no_violation(self):
        s = flow_slide(6)
        plan = derive_visual_plan(s, make_digest())
        assert L.fit_violations(s, plan) == []

    def test_long_latin_detail_flagged_despite_unit_budget(self):
        # the exact string that overflowed in production
        long_detail = "Choose b; int8 symmetric range is taken"
        s = flow_slide(6)
        s = s.model_copy(update={"payload": slide_payload(
            steps=[Step(label=f"步骤{i}", detail=long_detail) for i in range(1, 7)])})
        digest = make_digest()
        plan = derive_visual_plan(s, digest)
        # unit budgets are satisfied — only the geometry check can see this
        assert budget_violations(s, plan, digest) == []
        errs = L.fit_violations(s, plan)
        assert len(errs) == 1
        assert "does not fit even at micro" in errs[0]
        assert s.slide_id in errs[0]


class TestDeckLevel:
    def test_layout_deck_full(self):
        deck = make_deck([hero_slide(), cards_slide(), flow_slide(),
                          arch_slide(), compare_slide(), chart_slide()])
        lays = L.layout_deck(deck)
        assert len(lays) == len(deck.slides)
        assert [l.visual_type for l in lays] == \
            [p.visual_type for p in deck.visual_plan]
