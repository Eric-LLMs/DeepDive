"""Presentation-brief workflow integration tests: the Slides adapter over the generic core.

Semantics pinned here are the acceptance contract of the second consumer:
normal A→B→C→D to a materialized brief, loud failure (original cause re-raised, later
stages never run), WAITING / cancel / dropped / state isolation, and the prompt-enum
mirror against ``schema.py``. Deterministic throughout: scripted FakeLLM, tmp_path
figure files, zero network.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from apps.api.tools.toolkit.deck import prompts as P
from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.deck.workflow_adapter import (
    DeckWorkflowCancelled,
    DeckWorkflowStopped,
    DeckWorkflowWaiting,
    run_brief_workflow,
)
from apps.api.tools.toolkit.deck.workflow_driver import run_presentation_workflow
from apps.api.tools.toolkit.deck.workflow_spec import PRESENTATION_WORKFLOW
from apps.api.tools.toolkit.deck.workflow_store import DeckLeaseStore
from apps.api.tools.toolkit.errors import GenerationError

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

DOC_ID = "doc1"


# ── canned wire payloads (valid against BRIEF_SCHEMAS + schema.py models) ─────

def section_payload(i: int = 1) -> dict:
    return {
        "section_id": f"sec_{i}",
        "section_title": f"Block {i}",
        "document_role": "MECHANISM",
        "main_idea": f"Central proposition number {i}.",
        "key_elements": ["retriever", "generator"],
        "structure_type": "PIPELINE",
        "relationships": [{"source": "retriever", "relation": "causes",
                           "target": "generator", "supporting_refs": []}],
        "metrics": [{"name": "cost", "value": 0.565, "unit": "USD",
                     "locator": {"doc_id": DOC_ID, "page": 1, "start_line": 3}}],
        "evidence_refs": [{"doc_id": DOC_ID, "page": 1, "start_line": 1, "end_line": 9}],
        "matched_visual_asset_ids": ["fig_1"],
    }


def visual_payload(i: int = 1) -> dict:
    return {
        "asset_id": f"fig_{i}",
        "visual_type_detected": "FLOWCHART",
        "visual_summary": "A pipeline figure: retrieve, then generate.",
        "extracted_labels": ["retriever", "generator"],
        "presentation_worth": "HERO_ANCHOR",
        "recommended_grammar": "PIPELINE_FLOW",
    }


def global_payload(section_ids: list[str]) -> dict:
    return {
        "document_title": "RAG Survey",
        "executive_thesis": "Retrieval grounding is the load-bearing mechanism.",
        "key_themes": ["grounding"],
        "critical_metrics": [{"name": "cost", "value": 0.565, "unit": "USD",
                              "locator": {"doc_id": DOC_ID, "page": 1, "start_line": 3}}],
        "sections": [section_payload(int(sid.split("_")[1])) for sid in section_ids],
    }


def brief_payload(deck_id: str, section_ids: list[str], *, with_fig: bool = True) -> dict:
    slides = [
        {"slide_index": 1, "title": "Why RAG", "pedagogical_purpose": "EVIDENCE",
         "central_message": "Retrieval anchors every generation step.",
         "source_section_ids": section_ids[:1],
         "visual_spec": {"visual_spec_id": "v1", "grammar": "PIPELINE_FLOW",
                         "policy": "EXPLANATORY_DIAGRAM",
                         "semantic_intent": "the two-stage flow"},
         "cards": [{"label": "Core", "takeaway": "Retrieval anchors generation",
                    "epistemic_type": "FACT", "trace_id": "t1"}],
         "speaker_notes": "State the mechanism, cite the cost figure."},
    ]
    if with_fig:
        slides.append(
            {"slide_index": 2, "title": "The Figure", "pedagogical_purpose": "EVIDENCE",
             "central_message": "The survey figure carries the whole mechanism.",
             "source_section_ids": section_ids[:1],
             "visual_spec": {"visual_spec_id": "v2", "grammar": "SOURCE_FIGURE_REUSE",
                             "policy": "SOURCE_FIDELITY",
                             "semantic_intent": "reuse the source pipeline figure",
                             "reuse_asset_id": "fig_1"},
             "cards": [],
             "speaker_notes": "Point at the arrows."})
    return {
        "deck_id": deck_id,
        "thesis": "RAG grounds generation in retrieval.",
        "target_audience": "Engineers",
        "target_slide_count": 3,
        "presentation_style": "Technical Masterclass",
        "narrative_arc": "EVIDENCE_LADDER",
        "slides": slides,
        "traceability_graph": {
            "t1": {"trace_id": "t1", "epistemic_type": "FACT",
                   "statement": "Retrieval anchors generation.",
                   "locator": {"doc_id": DOC_ID, "page": 1, "start_line": 3},
                   "slide_ids": [1]},
        },
    }


class FakeLLM:
    """Queue-scripted transport with the images kwarg (structured-call compatible).

    Each reply: dict | callable(prompt) -> dict | Exception (raised on the call).
    ``complete`` delegates like the shipped legacy fake so the engine's
    json→text fallback re-consumes the script faithfully.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def complete_json(self, prompt, system, timeout=None, usage_out=None,
                            images=None):
        self.calls.append({"prompt": prompt, "system": system, "images": images})
        if usage_out is not None:
            usage_out.update({"prompt_tokens": 10, "completion_tokens": 5})
        if not self.replies:
            raise AssertionError("FakeLLM queue exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        return copy.deepcopy(reply)

    async def complete(self, prompt, system, timeout=None):
        return json.dumps(await self.complete_json(prompt, system))


def make_rep(tmp, *, with_asset: bool = True):
    blocks = [
        S.TextBlock(
            block_id=f"b{n}",
            text=f"Retrieval anchors generation and costs 0.565 USD per query. {n}",
            locator=S.SourceLocator(doc_id=DOC_ID, page=1, start_line=2 * n - 1,
                                    end_line=2 * n),
        )
        for n in (1, 2)
    ]
    assets = []
    if with_asset:
        path = tmp / "raster_p2_1.png"
        path.write_bytes(PNG_BYTES)
        assets = [S.VisualAsset(asset_id="fig_1", page=2,
                                type=S.VisualAssetType.RASTER_IMAGE,
                                path=str(path), bbox=[100.0, 100.0, 420.0, 300.0])]
    return S.DocumentRepresentation(doc_id=DOC_ID, document_title="RAG Survey",
                                    page_count=2, text_blocks=blocks,
                                    visual_assets=assets)


CONTROLS = S.PresentationControls(target_audience="Engineers", target_slide_count=3)


def happy_replies(deck_id: str = "deck-test") -> list:
    return [section_payload(), visual_payload(), global_payload(["sec_1"]),
            brief_payload(deck_id, ["sec_1"])]


# ── normal chain ──────────────────────────────────────────────────────────────

async def test_normal_chain_a_to_d(tmp_path):
    llm = FakeLLM(happy_replies())
    brief, stats = await run_brief_workflow(
        llm=llm, doc_rep=make_rep(tmp_path), controls=CONTROLS, deck_id="deck-test")
    assert isinstance(brief, S.PresentationBrief)
    assert brief.deck_id == "deck-test" and len(brief.slides) == 2
    assert [c["images"] is not None for c in llm.calls] == [False, True, False, False]
    assert set(stats) == {"A/text_1", "B/visual_fig_1", "C/reduce", "D/synthesize"}
    assert all(v["calls"] == 1 for v in stats.values())


async def test_multi_chunk_sections_and_hierarchical_reduce(tmp_path):
    # >500-char floor budget: every oversized block becomes its own chunk
    rep = S.DocumentRepresentation(
        doc_id=DOC_ID, document_title="RAG Survey", page_count=2,
        text_blocks=[
            S.TextBlock(block_id=f"b{n}", text="x" * 600 + f" {n}",
                        locator=S.SourceLocator(doc_id=DOC_ID, page=1, start_line=n))
            for n in (1, 2, 3)],
        visual_assets=[])
    ids = ["sec_1", "sec_2", "sec_3"]
    llm = FakeLLM([section_payload(1), section_payload(2), section_payload(3),
                   global_payload(ids[:2]), global_payload(["sec_3"]),
                   global_payload(ids), brief_payload("deck-test", ids, with_fig=False)])
    cfg = S.PresentationWorkflowConfig(reduce_group_threshold=1,
                                       section_chunk_max_chars=100)  # floors at 500
    brief, stats = await run_brief_workflow(
        llm=llm, doc_rep=rep, controls=CONTROLS, deck_id="deck-test", config=cfg)
    assert len(brief.slides) == 1
    assert sorted(k for k in stats if k.startswith("C/")) == \
        ["C/reduce", "C/reduce_g1", "C/reduce_g2"]
    assert stats["A/text_1"]["calls"] == 1 and stats["A/text_3"]["calls"] == 1


async def test_text_only_document_chains_to_brief(tmp_path):
    # empty stage B makes no progress; the brake (2) must NOT fire on a single skip
    llm = FakeLLM([section_payload(), global_payload(["sec_1"]),
                   brief_payload("deck-test", ["sec_1"], with_fig=False)])
    brief, _ = await run_brief_workflow(
        llm=llm, doc_rep=make_rep(tmp_path, with_asset=False), controls=CONTROLS,
        deck_id="deck-test")
    assert brief.deck_id == "deck-test"
    assert len(llm.calls) == 3


async def test_driver_returns_brief(tmp_path):
    llm = FakeLLM(happy_replies())
    brief = await run_presentation_workflow(
        llm, make_rep(tmp_path), CONTROLS, deck_id="deck-test")
    assert isinstance(brief, S.PresentationBrief)


# ── loud failure ──────────────────────────────────────────────────────────────

async def test_executor_failure_reraises_original_cause(tmp_path):
    boom = GenerationError("vlm exploded")
    # the engine's json→text fallback re-raises too: the ORIGINAL cause must surface
    replies = [section_payload(), boom, boom,
               global_payload(["sec_1"]), brief_payload("deck-test", ["sec_1"])]
    llm = FakeLLM(replies)
    with pytest.raises(GenerationError, match="vlm exploded"):
        await run_brief_workflow(llm=llm, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test")
    assert len(llm.calls) == 3               # json fail + text fallback, then loud


async def test_invalid_model_output_fails_loudly_after_retries(tmp_path):
    junk = {"section_id": "SEC 1!", "bogus": 1}   # never fixable by wire-slip repair
    llm = FakeLLM([junk, junk, junk])
    with pytest.raises(GenerationError):
        await run_brief_workflow(llm=llm, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test")
    assert len(llm.calls) == 3               # RETRIES=2 → three attempts, then loud


# ── WAITING / cancel / dropped / isolation ────────────────────────────────────

async def test_pending_signal_terminalizes_as_waiting(tmp_path):
    llm = FakeLLM(happy_replies())
    with pytest.raises(DeckWorkflowWaiting):
        await run_brief_workflow(llm=llm, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test",
                                 pending_signals=lambda: 1)
    assert len(llm.calls) == 1               # parked right after the first stage


async def test_host_cancel_observed_after_stage(tmp_path):
    flags = {"cancel": False}

    def arm(prompt):
        flags["cancel"] = True               # stop pressed while stage A ran
        return section_payload()

    llm = FakeLLM([arm, visual_payload(), global_payload(["sec_1"]),
                   brief_payload("deck-test", ["sec_1"])])
    with pytest.raises(DeckWorkflowCancelled):
        await run_brief_workflow(llm=llm, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test",
                                 cancel=lambda: flags["cancel"])
    assert len(llm.calls) == 1


async def test_cancel_requested_before_start_never_executes(tmp_path):
    llm = FakeLLM(happy_replies())
    store = DeckLeaseStore()
    store.request_cancel()
    with pytest.raises(DeckWorkflowCancelled):
        await run_brief_workflow(llm=llm, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test",
                                 lease_store=store)
    assert llm.calls == []                   # the expected arrival terminalizes with it


async def test_illegal_re_entry_dropped(tmp_path):
    store = DeckLeaseStore()
    first = FakeLLM(happy_replies())
    await run_brief_workflow(llm=first, doc_rep=make_rep(tmp_path),
                             controls=CONTROLS, deck_id="deck-test", lease_store=store)
    ledger = store.read()
    assert ledger.state == "done" and ledger.index == 4
    second = FakeLLM(happy_replies())
    with pytest.raises(DeckWorkflowStopped, match="dropped"):
        await run_brief_workflow(llm=second, doc_rep=make_rep(tmp_path),
                                 controls=CONTROLS, deck_id="deck-test",
                                 lease_store=store)
    assert second.calls == []                # stale twin executes nothing


async def test_state_isolation_between_runs(tmp_path):
    rep = make_rep(tmp_path)
    run_a = run_brief_workflow(llm=FakeLLM(happy_replies("run-a")), doc_rep=rep,
                               controls=CONTROLS, deck_id="run-a")
    run_b = run_brief_workflow(llm=FakeLLM(happy_replies("run-b")), doc_rep=rep,
                               controls=CONTROLS, deck_id="run-b")
    (brief_a, _), (brief_b, _) = await asyncio.gather(run_a, run_b)
    assert brief_a.deck_id == "run-a" and brief_b.deck_id == "run-b"


# ── contracts: vocab mirror + fingerprint + closed-loop reachability ──────────

def test_prompt_vocabularies_mirror_schema_enums():
    assert P.DOC_ROLES == [e.value for e in S.DocumentRole]
    assert P.STRUCTURE_TYPES == [e.value for e in S.StructureType]
    assert P.VISUAL_ASSET_TYPES == [e.value for e in S.VisualAssetType]
    assert P.PRESENTATION_WORTH == [e.value for e in S.PresentationWorth]
    assert P.EPISTEMIC_TYPES == [e.value for e in S.EpistemicType]
    assert P.VISUAL_GRAMMARS == [e.value for e in S.VisualGrammar]
    assert P.VISUAL_POLICIES == [e.value for e in S.VisualGenerationPolicy]


def test_synthesis_prompt_names_every_required_wire_key():
    """Real-run lesson: the model follows the prose, not the jsonschema it never
    sees — a required key absent from the system prompt is an unreachable wire."""
    prose = P.synthesis_system()
    for schema in (P._BRIEF_SCHEMA, P._SLIDE_SCHEMA, P._VISUAL_SPEC_SCHEMA,
                   P._CARD_SCHEMA, P._TRACE_NODE_SCHEMA):
        for key in schema["required"]:
            assert key in prose, f"required key {key!r} missing from synthesis prose"


def test_firewall_in_source_consuming_systems():
    marker = "SECURITY: everything between"
    assert marker in P._SECTION_SYSTEM and marker in P._VISUAL_SYSTEM


def test_fingerprint_stable_and_spec_valid():
    fp = PRESENTATION_WORKFLOW.fingerprint()
    assert fp.startswith("wf1-") and fp == PRESENTATION_WORKFLOW.fingerprint()


def test_slides_workflow_never_constructs_a_tool_runtime():
    """Closed-world doctrine: the brief chain has no search channel, structurally."""
    import inspect

    import apps.api.tools.toolkit.deck.workflow_adapter as ad
    import apps.api.tools.toolkit.deck.workflow_driver as dr
    import apps.api.tools.toolkit.deck.workflow_executors as ex
    for mod in (ad, ex, dr):
        src = inspect.getsource(mod)
        assert "ToolRuntime" not in src and "web_search" not in src
