"""The transient brief→DeckSpec render bridge: mapping + downstream compatibility.

Pins what the pipeline's render stage relies on until the M1 Visual Compiler
retires this module: slide/count/order consistency (``layout_deck`` indexes the
visual plan by slide id), the semantic mapping (cards→CARDS, QUANTITATIVE_CODE→
one CHART series, message-only→TEXT_HERO), trace locators surviving as provenance
refs, and the compat exports rendering from the bridged model.
"""
from __future__ import annotations

from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.deck.bridge import brief_to_deckspec
from apps.api.tools.toolkit.deck.layout import layout_deck
from apps.api.tools.toolkit.deck.models import SourceKind
from apps.api.tools.toolkit.deck.render import deck_to_marp, deck_to_pptx_slides
from tests.test_deck_workflow import DOC_ID, brief_payload

BRIEF = S.PresentationBrief.model_validate(brief_payload("d1", ["sec_1"]))


def _bridge(brief: S.PresentationBrief):
    return brief_to_deckspec(brief, document_title="RAG Survey",
                             source_names=["paper.pdf"])


def test_slide_shape_mapping_and_order():
    deck = _bridge(BRIEF)
    assert [s.slide_id for s in deck.slides] == ["s1", "s2"]
    assert [p.slide_id for p in deck.visual_plan] == ["s1", "s2"]          # layout needs both
    types = {p.slide_id: p.visual_type for p in deck.visual_plan}
    assert types == {"s1": "CARDS", "s2": "TEXT_HERO"}                     # figure reuse degrades to hero
    assert deck.slides[0].key_message == "Retrieval anchors every generation step."
    assert deck.slides[0].payload.items[0].detail == "Retrieval anchors generation"
    assert deck.slides[-1].purpose == "SUMMARY"
    assert deck.pages_expected() == 3                                      # 1 cover + 2 content


def test_trace_locators_survive_as_provenance():
    deck = _bridge(BRIEF)
    refs = deck.slides[0].provenance_refs
    assert len(refs) == 1
    assert refs[0].source_id == DOC_ID
    assert refs[0].page == 1 and refs[0].lines == "3"
    assert refs[0].kind is SourceKind.document


def test_digest_carries_traceability_statements():
    deck = _bridge(BRIEF)
    assert [f.statement for f in deck.digest.facts] == ["Retrieval anchors generation."]


def test_chart_spec_bridges_to_one_series():
    data = brief_payload("d2", ["sec_1"], with_fig=False)
    data["slides"][0]["visual_spec"] = {
        "visual_spec_id": "v1", "grammar": "DATA_CHART",
        "policy": "QUANTITATIVE_CODE", "semantic_intent": "cost trend",
        "generation_spec": {"chart": "bar", "labels": ["Q1", "Q2", "Q3"],
                            "values": [0.4, 0.565, 0.9]}}
    deck = _bridge(S.PresentationBrief.model_validate(data))
    plan = deck.visual_plan[0]
    assert plan.visual_type == "CHART"
    pts = deck.slides[0].payload.series[0].points
    assert [(p.x, p.y) for p in pts] == [("Q1", 0.4), ("Q2", 0.565), ("Q3", 0.9)]


def test_unusable_chart_spec_falls_back_to_cards():
    data = brief_payload("d2", ["sec_1"], with_fig=False)
    data["slides"][0]["visual_spec"] = {
        "visual_spec_id": "v1", "grammar": "DATA_CHART",
        "policy": "QUANTITATIVE_CODE", "semantic_intent": "cost trend",
        "generation_spec": {"chart": "bar", "labels": ["Q1"], "values": ["n/a"]}}
    deck = _bridge(S.PresentationBrief.model_validate(data))
    assert deck.visual_plan[0].visual_type == "CARDS"


def test_bridged_deck_flows_through_layout_and_exports():
    deck = _bridge(BRIEF)
    layouts = layout_deck(deck)                      # exercises the geometry pipeline
    assert len(layouts) == 2
    md = deck_to_marp(deck)
    assert "## Why RAG" in md and "[doc1:3]" in md   # citations survive into the compat md
    pptx = deck_to_pptx_slides(deck)
    assert isinstance(pptx, list) and pptx[0][0] == "Why RAG"
