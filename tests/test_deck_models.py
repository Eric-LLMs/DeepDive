"""Deck model contract tests: closed vocabularies, budgets, provenance, outline checks."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.tools.toolkit.deck.models import (
    ContentDigest,
    ContentPayload,
    DeckSpec,
    Fact,
    Item,
    ProvenanceRef,
    text_units,
)
from tests._deck_fixtures import (
    arch_slide,
    cards_slide,
    chart_slide,
    compare_slide,
    flow_slide,
    hero_slide,
    make_deck,
    make_digest,
    make_outline,
    make_slide,
    ref,
)


class TestTextUnits:
    def test_pure_latin(self):
        assert text_units("three little words") == 3

    def test_pure_cjk(self):
        assert text_units("检索增强生成") == 6

    def test_mixed(self):
        assert text_units("RAG 检索 pipeline") == 4  # 2 latin + 2 cjk

    def test_empty(self):
        assert text_units("") == 0
        assert text_units(None) == 0  # type: ignore[arg-type]


class TestProvenance:
    def test_locator_required(self):
        with pytest.raises(ValidationError, match="locator"):
            Fact(fact_id="f1", statement="x",
                 provenance=[ProvenanceRef(source_id="s", kind="document")])

    def test_lines_shape(self):
        with pytest.raises(ValidationError):
            ref(lines="abc")
        assert ref(lines="10-12").lines == "10-12"
        assert ref(lines="7").lines == "7"


class TestClosedVocabularies:
    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            make_slide(color="#ff0000")           # style is not semantic

    def test_unknown_purpose_rejected(self):
        with pytest.raises(ValidationError):
            make_slide(purpose="MOTIVATION")

    def test_coordinates_rejected(self):
        with pytest.raises(ValidationError):
            Item(label="a", x_mm=10)

    def test_unknown_relationship_rejected(self):
        with pytest.raises(ValidationError):
            make_slide(relationship="causal")


class TestPayloadShape:
    def test_single_shape_enforced(self):
        with pytest.raises(ValidationError, match="ONE shape"):
            ContentPayload(steps=[{"label": "a"}], items=[{"label": "b"}])

    def test_series_point_caps(self):
        pts = [{"x": str(i), "y": float(i), "quant_ref": "q1"} for i in range(9)]
        with pytest.raises(ValidationError):
            ContentPayload(series=[{"name": "s", "points": pts}])

    def test_comparison_ragged_cells_rejected(self):
        with pytest.raises(ValidationError):
            ContentPayload(columns=[
                {"header": "a", "cells": ["1", "2"]},
                {"header": "b", "cells": ["1"]},
            ])


class TestSlideBudgets:
    def test_long_key_message_rejected(self):
        with pytest.raises(ValidationError, match="key_message"):
            make_slide(key_message="word " * 80)

    def test_long_title_rejected(self):
        with pytest.raises(ValidationError, match="title"):
            make_slide(title="one two three four five six seven eight nine ten "
                                "eleven twelve thirteen")

    def test_cjk_message_within_budget(self):
        s = make_slide(key_message="检" * 50)
        assert text_units(s.key_message) == 50


class TestOutlineChecks:
    def test_valid_outline_passes(self):
        from apps.api.tools.toolkit.deck.models import check_outline
        o = make_outline(3)
        for s in o.all_slides():
            s.fact_refs = ["f1"]
        assert check_outline(o, 3, make_digest()) == []

    def test_missing_summary_closing_flagged(self):
        from apps.api.tools.toolkit.deck.models import check_outline
        o = make_outline(3, last_summary=False)
        errs = check_outline(o, 3, make_digest())
        assert any("SUMMARY" in e for e in errs)

    def test_slide_count_tolerance(self):
        from apps.api.tools.toolkit.deck.models import check_outline
        o = make_outline(3)
        errs = check_outline(o, 12, make_digest())   # target 12, got 3
        assert any("content slides" in e for e in errs)

    def test_dangling_fact_ref_flagged(self):
        from apps.api.tools.toolkit.deck.models import check_outline
        o = make_outline(3)
        o.all_slides()[0].fact_refs = ["f99"]
        errs = check_outline(o, 3, make_digest())
        assert any("f99" in e for e in errs)

    def test_outline_is_visual_type_pure(self):
        # the Outline vocabulary has no visual types anywhere
        o = make_outline(3)
        dumped = o.model_dump()
        blob = str(dumped)
        for vt in ("TEXT_HERO", "CARDS", "FLOWCHART", "TIMELINE",
                   "COMPARISON", "ARCHITECTURE", "CHART"):
            assert vt not in blob


class TestDigest:
    def test_duplicate_fact_ids_rejected(self):
        with pytest.raises(ValidationError, match="duplicate fact_id"):
            ContentDigest(facts=[
                Fact(fact_id="f1", statement="a", provenance=[ref()]),
                Fact(fact_id="f1", statement="b", provenance=[ref()]),
            ])

    def test_superseded_facts_drop(self):
        d = make_digest()
        d.facts[0].superseded_by = "f2"
        assert [f.fact_id for f in d.live_facts()] == ["f2", "f3"]


class TestDeckSpec:
    def test_slides_must_follow_outline(self):
        deck = make_deck([hero_slide(), cards_slide(), flow_slide()])
        bad = deck.model_dump()
        bad["slides"] = bad["slides"][:-1]           # slides must match outline exactly
        with pytest.raises(ValidationError, match="outline"):
            DeckSpec.model_validate(bad)

    def test_pages_expected_formula(self):
        deck = make_deck()
        assert deck.pages_expected() == 1 + len(deck.slides)
        dividers = deck.model_copy(update={"section_dividers": True})
        assert dividers.pages_expected() == 1 + len(deck.slides) + 1
        appendix = dividers.model_copy(update={"speaker_notes_appendix": True})
        assert appendix.pages_expected() == 1 + len(deck.slides) + 1 + 1

    def test_target_slide_count_clamped(self):
        from apps.api.tools.toolkit.deck.models import DeckOptions
        assert DeckOptions(target_slide_count=2).target_slide_count == 3
        assert DeckOptions(target_slide_count=99).target_slide_count == 20
