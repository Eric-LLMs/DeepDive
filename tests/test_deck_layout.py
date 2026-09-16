"""Pure text-measurement geometry: conservative metrics, lossless wrap, fit tiers.

Portable contract migrated from the DeckSpec-era layout tests: these are the
measurement atoms the Visual Compiler shares (:mod:`compiler.layout_engine`
imports ``_fit``/``wrap_lines``/tiers). Nothing here knows any IR. Pins:
CJK wider than Latin, wrapping never drops or alters a character (errata #4),
overlong Latin words stay atomic, ``_fit`` picks the largest tier and RAISES
loudly instead of trimming.
"""
from __future__ import annotations

import pytest

from apps.api.tools.toolkit.deck import layout as L
from apps.api.tools.toolkit.deck.errors import DeckLayoutError


class TestMeasurement:
    def test_cjk_wider_than_latin(self):
        assert L.text_width_mm("检索增强", 16) > L.text_width_mm("abcd", 16)

    def test_wrap_never_drops_text(self):
        text = "很长的中文文本需要换行 demonstrate wrapping behavior"
        joined = "".join(L.wrap_lines(text, 50.0, 16)).replace(" ", "")
        original = text.replace(" ", "")
        assert set(original) <= set(joined)
        assert len(joined) == len(original)

    def test_overlong_latin_word_kept_intact(self):
        word = "Supercalifragilisticexpialidocious"
        assert L.wrap_lines(word, 10.0, 16) == [word]

    def test_wrap_preserves_punctuation_verbatim(self):
        text = '配置 "A/B" 比例 72.5%; 结束。「引用」'
        joined = "".join(L.wrap_lines(text, 40.0, 16)).replace(" ", "")
        assert joined == text.replace(" ", "")   # errata #4: not one char altered/dropped

    def test_block_height_scales_with_lines(self):
        h1 = L.block_height(1, 16)
        assert L.block_height(3, 16) == pytest.approx(3 * h1)


class TestFitTiers:
    def test_picks_the_largest_fitting_tier(self):
        pt, lines = L._fit("Short line", L.CONTENT_W_MM, 20.0)
        assert pt == L.TIERS["display"] and lines == ["Short line"]

    def test_empty_text_needs_no_room(self):
        pt, lines = L._fit("", 10.0, 1.0)
        assert pt == L.TIERS["display"] and lines == []

    def test_tier_order_and_leading_are_the_shared_contract(self):
        assert list(L.TIERS) == list(L.FIT_ORDER)         # display → micro
        assert L.TIERS["display"] > L.TIERS["micro"]

    def test_impossible_content_raises_not_trims(self):
        monster = "检" * 3000        # no tier keeps this inside the body slot
        with pytest.raises(DeckLayoutError, match="does not fit even at micro"):
            L._fit(monster, L.CONTENT_W_MM, L.BODY_H_MM)

    def test_max_lines_caps_tier_selection(self):
        text = "one " * 200
        with pytest.raises(DeckLayoutError):
            L._fit(text, L.CONTENT_W_MM * 0.2, L.BODY_H_MM, max_lines=2)

    def test_fit_bounds_height_not_single_line_width(self):
        # known atom behavior (pinned because the QA geometry gate relies on it):
        # a single overlong Latin word wraps to ONE atomic line — height fits,
        # so _fit passes even though the line overflows the box horizontally.
        pt, lines = L._fit("x" * 500, 20.0, 20.0)
        assert lines == ["x" * 500] and pt > 0


class TestNoTrimming:
    def test_no_ellipsis_anywhere_in_wrapped_output(self):
        text = "很长的中文文本需要换行 demonstrate wrapping behavior " * 50
        blob = "|".join(L.wrap_lines(text, 60.0, 12))
        assert "…" not in blob and "..." not in blob

    def test_message_never_lost(self):
        src = "Cost fell to 0.565 USD — 检索增强 anchors every generation step."
        joined = "".join(L.wrap_lines(src, 80.0, 16))
        assert set(src.replace(" ", "").replace(",", "")) <= set(
            joined.replace(" ", "").replace(",", ""))
