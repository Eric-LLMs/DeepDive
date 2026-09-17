"""Direct engine tests: the one-semantic-call generator + local compiler + SLIDE_PATCH.

Deterministic throughout (scripted FakeLLM, tmp_path figure, zero network). Pins:
the five-node stats vocabulary, both loud local rescues, reroll ≤2 with condensed
feedback, honest exhaustion, single-slide patching with sibling zero-drift, and the
direct prompt's enum mirrors (same no-drift contract as the legacy prompt pins in
test_deck_workflow.py).
"""
from __future__ import annotations

import copy
import json

import pytest

from apps.api.tools.toolkit.deck import prompts as P
from apps.api.tools.toolkit.deck import repair as RP
from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.deck.generator import (
    MAX_REROLLS,
    STATS_REDUCE,
    STATS_TEXT,
    STATS_VISUAL,
    pack_sections_local,
    rescue_policy_from_grammar,
    run_direct_generation,
)
from apps.api.tools.toolkit.errors import GenerationError

DOC_ID = "doc1"
DECK_ID = "d1"

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def make_doc_rep(tmp_path) -> S.DocumentRepresentation:
    img = tmp_path / "fig_1.png"
    img.write_bytes(PNG_BYTES)
    return S.DocumentRepresentation(
        doc_id=DOC_ID, document_title="RAG Survey", page_count=1,
        text_blocks=[S.TextBlock(
            block_id="b1",
            text="Retrieval anchors generation; the measured cost is 0.565 USD.",
            locator=S.SourceLocator(doc_id=DOC_ID, page=1, start_line=1, end_line=9))],
        visual_assets=[S.VisualAsset(
            asset_id="fig_1", page=1, type=S.VisualAssetType.RASTER_IMAGE,
            path=str(img), semantic_hint="pipeline figure")])


def brief_payload(*, notes1="State the mechanism, cite the cost figure.") -> dict:
    return {
        "deck_id": DECK_ID,
        "thesis": "RAG grounds generation in retrieval.",
        "target_audience": "Engineers",
        "target_slide_count": 3,
        "presentation_style": "Technical Masterclass",
        "narrative_arc": "EVIDENCE_LADDER",
        "slides": [
            {"slide_index": 1, "title": "Why RAG", "pedagogical_purpose": "EVIDENCE",
             "central_message": "Retrieval anchors every generation step.",
             "source_section_ids": ["sec_1"],
             "visual_spec": {"visual_spec_id": "v1", "grammar": "PIPELINE_FLOW",
                             "policy": "EXPLANATORY_DIAGRAM",
                             "semantic_intent": "the two-stage flow"},
             "cards": [{"label": "Core", "takeaway": "Retrieval anchors generation",
                        "epistemic_type": "FACT", "trace_id": "t1"}],
             "speaker_notes": notes1},
            {"slide_index": 2, "title": "The Figure", "pedagogical_purpose": "EVIDENCE",
             "central_message": "The survey figure carries the whole mechanism.",
             "source_section_ids": ["sec_1"],
             "visual_spec": {"visual_spec_id": "v2", "grammar": "SOURCE_FIGURE_REUSE",
                             "policy": "SOURCE_FIDELITY",
                             "semantic_intent": "reuse the source pipeline figure",
                             "reuse_asset_id": "fig_1"},
             "cards": [],
             "speaker_notes": "Point at the arrows."},
        ],
        "traceability_graph": {
            "t1": {"trace_id": "t1", "epistemic_type": "FACT",
                   "statement": "Retrieval anchors generation.",
                   "locator": {"doc_id": DOC_ID, "page": 1, "start_line": 3},
                   "slide_ids": [1]},
        },
    }


class ScriptedLLM:
    """Queue transport; each reply is a dict (deep-copied) or an Exception."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete_json(self, prompt, system, *, timeout=None, usage_out=None,
                            images=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        if usage_out is not None:
            usage_out.update(prompt_tokens=111, completion_tokens=222)
        r = self.replies[0] if self.replies else json.loads(json.dumps(brief_payload()))
        if isinstance(r, Exception):
            raise r                       # stay queued: the transport fallback sees it too
        self.replies.pop(0)
        return copy.deepcopy(r)

    async def complete(self, prompt, system, *, timeout=None):
        # structured.complete_json falls back here after a transport raise; keep the
        # scripted exception faithful to the transport contract.
        r = self.replies[0] if self.replies else None
        if isinstance(r, Exception):
            raise r
        return json.dumps(brief_payload())


async def _run(llm, doc_rep, stats_out=None):
    controls = S.PresentationControls(target_audience="Engineers",
                                      target_slide_count=3)
    return await run_direct_generation(llm, doc_rep, controls, deck_id=DECK_ID,
                                       stats_out=stats_out)


# ── happy path + stats vocabulary ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_happy_single_call(tmp_path):
    doc_rep = make_doc_rep(tmp_path)
    llm = ScriptedLLM([brief_payload()])
    stats: dict = {}
    brief = await _run(llm, doc_rep, stats)
    assert len(brief.slides) == 2 and llm.prompts and len(llm.replies) == 0
    assert stats["D/synthesize"]["calls"] == 1
    assert stats[STATS_TEXT]["calls"] == 0 and "local_seconds" in stats[STATS_TEXT]
    assert stats[STATS_VISUAL]["calls"] == 0
    assert stats[STATS_REDUCE]["calls"] == 0
    st = stats["D/synthesize"]
    assert set(st) >= {"calls", "rejected", "llm_seconds", "prompt_tokens",
                       "completion_tokens", "repairs"}
    assert st["prompt_tokens"] == 111
    # raw source (not a digest) rode the one prompt, with real line numbers
    assert "0.565 USD" in llm.prompts[0] and '"start_line": 1' in llm.prompts[0]
    # echo contract: deck_id was demanded
    assert f"DECK_ID: {DECK_ID}" in llm.prompts[0]


def test_pack_sections_deterministic(tmp_path):
    rep = make_doc_rep(tmp_path)
    a = pack_sections_local(rep)
    b = pack_sections_local(rep)
    assert json.dumps(a) == json.dumps(b)
    assert [s["section_id"] for s in a] == ["sec_1"]


# ── REDUCE rescues (loud, local, zero extra calls) ───────────────────────────

def test_rescue_grammar_in_policy_derived():
    data = {"slides": [{"visual_spec": {
        "visual_spec_id": "v1", "grammar": "PIPELINE_FLOW",
        "policy": "PIPELINE_FLOW", "semantic_intent": "x"}}]}
    events: list[str] = []
    rescue_policy_from_grammar(data, events)
    assert data["slides"][0]["visual_spec"]["policy"] == "EXPLANATORY_DIAGRAM"
    assert events and "derived" in events[0]


def test_rescue_figure_reuse_policy():
    data = {"slides": [{"visual_spec": {
        "visual_spec_id": "v1", "grammar": "SOURCE_FIGURE_REUSE",
        "policy": "SOURCE_FIGURE_REUSE", "semantic_intent": "x",
        "reuse_asset_id": "fig_1"}}]}
    events: list[str] = []
    rescue_policy_from_grammar(data, events)
    assert data["slides"][0]["visual_spec"]["policy"] == "SOURCE_FIDELITY"


def test_rescue_never_touches_legal_policy():
    good = brief_payload()
    events: list[str] = []
    rescue_policy_from_grammar(good, events)
    assert events == []


# ── reroll ≤2 with condensed feedback, honest exhaustion ─────────────────────

@pytest.mark.asyncio
async def test_direct_reroll_recovers(tmp_path):
    doc_rep = make_doc_rep(tmp_path)
    bad = brief_payload()
    bad["slides"][1]["visual_spec"] = {   # jsonschema-legal, model-illegal: no data
        "visual_spec_id": "v2", "grammar": "DATA_CHART",
        "policy": "QUANTITATIVE_CODE", "semantic_intent": "cost trend"}
    llm = ScriptedLLM([bad, brief_payload()])
    stats: dict = {}
    brief = await _run(llm, doc_rep, stats)
    assert brief is not None
    assert len(llm.replies) == 0
    assert stats["D/synthesize"]["rejected"] == 1
    assert stats["D/reroll_1"]["calls"] == 1
    # corrective feedback rode the reroll prompt (condensed, actionable)
    assert "previous reply failed validation" in llm.prompts[1]


@pytest.mark.asyncio
async def test_direct_reroll_exhaustion_raises(tmp_path):
    doc_rep = make_doc_rep(tmp_path)
    bad = brief_payload()
    bad["slides"][0]["source_section_ids"] = ["sec_99"]   # closed-world violation
    llm = ScriptedLLM([bad] * (MAX_REROLLS + 1))
    with pytest.raises(GenerationError, match="after 3 attempts"):
        await _run(llm, doc_rep, {})
    assert len(llm.prompts) == MAX_REROLLS + 1   # never a fourth call


@pytest.mark.asyncio
async def test_direct_transport_error_fails_loud(tmp_path):
    doc_rep = make_doc_rep(tmp_path)
    llm = ScriptedLLM([RuntimeError("channel down")])
    with pytest.raises(GenerationError, match="channel down"):
        await _run(llm, doc_rep, {})


# ── SLIDE_PATCH: single-slide diff, siblings never in the model's hands ───────

@pytest.mark.asyncio
async def test_direct_patches_untraceable_note(tmp_path):
    doc_rep = make_doc_rep(tmp_path)
    # legal everywhere upstream, but gate #3: "42" grounds to nothing
    first = brief_payload(notes1="Expect 42 guests per table.")
    patched = brief_payload(notes1="State the mechanism, cite the cost figure.")
    patch_reply = {"slide": patched["slides"][0], "new_trace_nodes": {}}
    llm = ScriptedLLM([first, patch_reply])
    stats: dict = {}
    brief = await _run(llm, doc_rep, stats)
    assert "42" not in brief.slides[0].speaker_notes
    assert stats["D/patch_1"]["calls"] == 1
    # second model call saw ONLY the one slide — sibling and brief JSON absent
    assert llm.prompts[1].count("The Figure") == 0
    assert '"deck_id"' not in llm.prompts[1]
    # and the reroll budget was untouched (this is not a re-generation)
    assert "D/reroll_1" not in stats


@pytest.mark.asyncio
async def test_patch_merges_new_nodes_and_keeps_siblings():
    brief = S.PresentationBrief.model_validate(brief_payload())
    reply = {"slide": {**brief_payload()["slides"][0], "speaker_notes": "clean"},
             "new_trace_nodes": {"t9": {
                 "trace_id": "t9", "epistemic_type": "CLAIM",
                 "statement": "The authors argue grounding is decisive.",
                 "slide_ids": [1]}}}
    errs, merged = RP._merge(brief, reply, 1)
    assert not errs and merged is not None
    assert merged.traceability_graph["t9"].epistemic_type == S.EpistemicType.CLAIM
    assert merged.slides[1] == brief.slides[1]        # sibling untouched
    assert merged.thesis == brief.thesis


@pytest.mark.asyncio
async def test_patch_rejects_restating_existing_nodes_and_wrong_index():
    brief = S.PresentationBrief.model_validate(brief_payload())
    clash = {"slide": {**brief_payload()["slides"][0], "speaker_notes": "x"},
             "new_trace_nodes": {"t1": {
                 "trace_id": "t1", "epistemic_type": "FACT", "statement": "rewritten",
                 "locator": {"doc_id": DOC_ID, "page": 1, "start_line": 99}}}}
    errs, merged = RP._merge(brief, clash, 1)
    assert merged is None and any("not restate" in e for e in errs)
    wrong = {"slide": {**brief_payload()["slides"][0], "slide_index": 7},
             "new_trace_nodes": {}}
    errs, merged = RP._merge(brief, wrong, 1)
    assert merged is None and any("slide_index" in e for e in errs)


# ── direct prompt contract: enums + eight storytelling dimensions ─────────────

def test_direct_system_enums_no_drift():
    sys_txt = P.direct_brief_system("")
    for enum in S.VisualGrammar:
        assert enum.value in sys_txt
    for enum in S.VisualGenerationPolicy:
        assert enum.value in sys_txt
    for enum in S.EpistemicType:
        assert enum.value in sys_txt
    assert str(P.VISUAL_POLICIES) in sys_txt          # blacklist names the closed trio


def test_direct_system_carries_storytelling_and_blacklist():
    sys_txt = P.direct_brief_system("LANGUAGE: strictly in English. ")
    for dim in ("OVERVIEW", "PROCESS", "KEY CONCEPTS", "RELATIONSHIPS",
                "EVIDENCE", "EXAMPLES", "VISUAL STORYTELLING", "TAKEAWAY"):
        assert dim in sys_txt
    assert "COMMON ERRORS" in sys_txt
    assert "LANGUAGE: strictly in English. " in sys_txt
    assert "UNTRUSTED" in sys_txt                     # source firewall present


def test_legacy_synthesis_output_unchanged_by_clause_params():
    # defaults keep Pass D byte-identical: the grounding clauses only matter to direct
    base = P.synthesis_system()
    assert "From the GlobalMentalModel + visual readings produce ONE" in base
    assert "(must carry a locator copied from the model's metrics/evidence_refs)" in base
    assert "COMMON ERRORS" not in base
