"""Layer-1 QA gates + the bounded repair matrix (plan §M1.8 / 总令 §8.5, §9.4-5).

Pins: the pure-code gates (epistemic ledger, number traceability, budgets,
geometry dry-run, template diversity, anchor ratio), the hard/soft split (graph
and ≥5-slide diversity errors block; fallbacks and the 80% bar only warn), the
field-locked repairs (≤2 per slide, notes-only lock, sibling byte-equality,
graph-additive-not-mutable), the ≤1 re-synthesize leg, loud exhaustion — and the
closed-world invariant: qa.py stays search-free and gate-clean briefs cost zero
extra LLM calls.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from apps.api.tools.toolkit.deck import qa
from apps.api.tools.toolkit.deck import schema as S
from apps.api.tools.toolkit.errors import GenerationError
from tests.test_deck_compiler import brief_from, chart_gen, slide
from tests.test_deck_workflow import (
    CONTROLS,
    FakeLLM,
    brief_payload,
    global_payload,
    happy_replies,
    make_rep,
    run_brief_workflow,
)

E = S.EpistemicType


# ── gate helpers ──────────────────────────────────────────────────────────────

def facts(report: qa.QAReport) -> list[str]:
    return [e.message for e in report.errors]


def kinds(report: qa.QAReport) -> set[str]:
    return {e.kind for e in report.errors}


def bad_fact_node(brief: S.PresentationBrief, tid: str = "t_orphan") -> None:
    # model_construct skips the FACT-needs-locator validator: exactly the
    # from-disk / legacy-graph shape the ledger gate must not trust.
    brief.traceability_graph[tid] = S.TraceabilityNode.model_construct(
        trace_id=tid, epistemic_type=E.FACT, statement="Ungrounded claim.",
        locator=None, supporting_facts=[], slide_ids=[], visual_asset_ids=[])


# ── the suite is pure code: clean briefs pass silently ────────────────────────

def test_clean_brief_passes_with_zero_llm():
    brief = brief_from([slide(1, "TEXTUAL_THESIS", n_cards=0),
                        slide(2, "STRUCTURED_CARDS", n_cards=3)])
    report = qa.run_qa_suite(brief)
    assert report.ok, facts(report)
    assert report.errors == []


def test_anchors_and_diversity_are_warnings_not_errors():
    # 3 uniform slides: both soft bars fire, neither blocks (§8: no fabricated
    # figures to meet quotas; small decks cannot honestly carry 3 grammars).
    brief = brief_from([{**slide(1, "PIPELINE_FLOW", n_cards=1), "slide_index": i}
                        for i in (1, 2, 3)])
    for s in brief.slides[:2]:
        s.visual_spec.has_dominant_anchor = False
    report = qa.run_qa_suite(brief)
    assert report.ok, facts(report)
    msgs = " | ".join(w.message for w in report.warnings)
    assert "anchor ratio" in msgs and "one layout template" in msgs


def test_diversity_is_hard_for_large_uniform_decks():
    brief = brief_from([slide(i, "TEXTUAL_THESIS", n_cards=0) for i in range(1, 7)])
    report = qa.run_qa_suite(brief)
    assert not report.ok
    assert "global" in kinds(report) and "template diversity" in facts(report)[0]


# ── content gates: numbers, geometry, ledger ──────────────────────────────────

def test_fabricated_number_in_speaker_notes_is_a_notes_error():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    brief.slides[0].speaker_notes = "Cost dropped to 0.999 USD per query."
    report = qa.run_qa_suite(brief)
    err = next(e for e in report.errors if e.kind == "notes")
    assert err.slide_index == 1 and "0.999" in err.message
    # small integers stay tolerated (enumerations are not claims)
    brief.slides[0].speaker_notes = "Walk the three steps, then pause."
    assert qa.run_qa_suite(brief).ok


def test_chart_values_must_bottom_out_in_grounded_text():
    brief = brief_from([slide(1, "DATA_CHART", n_cards=1,
                              policy="QUANTITATIVE_CODE", gen=chart_gen())])
    report = qa.run_qa_suite(brief)
    assert any("chart value" in e.message for e in report.errors)
    # the same chart is legal once the number is a trace statement
    brief.traceability_graph["t1"].statement = "Cost fell to 0.4, 0.565, 0.9."
    assert [e for e in qa.run_qa_suite(brief).errors if "chart" in e.message] == []


def test_geometry_dry_run_surfaces_as_plan_error():
    brief = brief_from([slide(1, "TEXTUAL_THESIS", n_cards=0)])
    # 4000 CJK chars: no tier keeps the wrapped block inside the body slot —
    # the compiler's loud _fit raise must be caught and attributed here.
    brief.slides[0].central_message = "字" * 4000
    report = qa.run_qa_suite(brief)
    assert any(e.kind == "plan" and "geometry" in e.message for e in report.errors)


def test_orphan_fact_node_is_a_graph_error():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    bad_fact_node(brief)
    report = qa.run_qa_suite(brief)
    assert "graph" in kinds(report) and "SourceLocator" in facts(report)[0]


def test_grounded_synthesis_must_bottom_out_in_fact_or_claim():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    brief.traceability_graph["t_s"] = S.TraceabilityNode(
        trace_id="t_s", epistemic_type=E.GROUNDED_SYNTHESIS,
        statement="Synthesis.", supporting_facts=["t_ghost"])
    report = qa.run_qa_suite(brief)
    assert any("t_ghost" in e.message for e in report.errors if e.kind == "graph")


def test_untraceable_card_and_budget_breaks_are_plan_errors():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    brief.slides[0].cards[0].trace_id = "t_missing"
    brief.slides[0].title = "One " * 30 + "too long"
    report = qa.run_qa_suite(brief)
    assert "plan" in kinds(report)
    msgs = " ".join(facts(report))
    assert "t_missing" in msgs and "title is" in msgs


def test_reuse_of_unknown_asset_is_a_plan_error(tmp_path):
    from tests.test_deck_compiler import figure_asset
    asset = figure_asset(tmp_path)
    brief = brief_from([slide(1, "SOURCE_FIGURE_REUSE", n_cards=2,
                              policy="SOURCE_FIDELITY", reuse="ghost")])
    report = qa.run_qa_suite(brief, assets={asset.asset_id: asset})
    assert any("not a figure" in e.message for e in report.errors
               if e.kind == "plan" and e.slide_index == 1)
    # a KNOWN asset whose file vanished only warns: the renderer degrades loudly.
    brief2 = brief_from([slide(1, "SOURCE_FIGURE_REUSE", n_cards=2,
                               policy="SOURCE_FIDELITY", reuse=asset.asset_id)])
    Path(asset.path).unlink()
    report2 = qa.run_qa_suite(brief2, assets={asset.asset_id: asset})
    assert report2.ok and report2.warnings


# ── the repair matrix ─────────────────────────────────────────────────────────

def fixed_notes(brief: S.PresentationBrief, idx: int, notes: str) -> dict:
    slides = [s.model_copy(update={"speaker_notes": notes})
              if s.slide_index == idx else s for s in brief.slides]
    return brief.model_copy(update={"slides": slides}).model_dump(mode="json")


async def run_guarded(brief, replies, **kw):
    llm = FakeLLM(replies)
    stats: dict = {}
    out = await qa.ensure_brief_clean(llm=llm, deck_id=brief.deck_id, brief=brief,
                                       controls=CONTROLS, stats=stats, **kw)
    return out, llm, stats


async def test_notes_repair_is_field_locked_and_bounded():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    brief.slides[0].speaker_notes = "Cost dropped to 0.999 USD per query."
    good = fixed_notes(brief, 1, "Cost dropped sharply — cite the table.")
    drifted = copy.deepcopy(good)
    drifted["slides"][0]["title"] = "Hijacked Title"       # violates the notes lock
    out, llm, stats = await run_guarded(brief, [drifted, good])
    assert out.slides[0].speaker_notes == "Cost dropped sharply — cite the table."
    assert out.slides[0].title == brief.slides[0].title     # sibling fields intact
    assert len(llm.calls) == 2                               # lock fed back, then fixed
    assert stats["D-repair/notes_1"]["calls"] == 2
    assert stats["D-repair/notes_1"]["rejected"] == 1


async def test_notes_repair_exhaustion_raises_loudly():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    brief.slides[0].speaker_notes = "Cost dropped to 0.999 USD per query."
    same = brief.model_dump(mode="json")                    # valid, lock-clean, still bad
    with pytest.raises(GenerationError, match="still failing after 2 repairs"):
        await run_guarded(brief, [copy.deepcopy(same), copy.deepcopy(same)])


async def test_plan_repair_may_change_only_the_target_slide():
    brief = brief_from([slide(1, "TEXTUAL_THESIS", n_cards=0),
                        slide(2, "STRUCTURED_CARDS", n_cards=3)])
    brief.slides[0].central_message = "字" * 4000          # geometry error
    good = brief.model_copy(update={"slides": [
        brief.slides[0].model_copy(update={"central_message": "短句可排版。"}),
        brief.slides[1]]})
    bad = brief.model_copy(update={"slides": [
        brief.slides[0].model_copy(update={"central_message": "短句可排版。"}),
        brief.slides[1].model_copy(update={"title": "Touched Sibling"})]})
    out, llm, stats = await run_guarded(
        brief, [bad.model_dump(mode="json"), good.model_dump(mode="json")])
    assert out.slides[1].title == brief.slides[1].title
    assert stats["D-repair/1"]["rejected"] == 1
    retry = llm.calls[1]["prompt"]           # §9.5: the drift is fed back, not eaten
    assert "byte-identical" in retry


async def test_graph_defects_trigger_exactly_one_resynthesize():
    brief = brief_from([slide(1, "STRUCTURED_CARDS", n_cards=2)])
    bad_fact_node(brief)
    clean = brief_payload("d1", ["sec_1"], with_fig=False)  # no orphan node
    model = S.GlobalMentalModel.model_validate(global_payload(["sec_1"]))
    stats: dict = {}
    llm = FakeLLM([clean])
    out = await qa.ensure_brief_clean(
        llm=llm, deck_id="d1", brief=brief, controls=CONTROLS, model=model,
        stats=stats, synth_prompt="SYNTH-PROMPT", synth_system="SYNTH-SYSTEM")
    assert not qa.run_qa_suite(out).errors
    assert stats["D-repair/resynth"]["calls"] == 1
    assert "SYNTH-PROMPT" in llm.calls[0]["prompt"]        # wrapped, not replaced
    assert "graph" in llm.calls[0]["prompt"]               # QA failures told to it
    # without a model there is no honest re-grounding path — loud, not silent
    with pytest.raises(GenerationError, match="mental model"):
        await run_guarded(brief, [])


async def test_clean_brief_costs_zero_repair_calls(tmp_path):
    # full-chain: gate-clean scripted brief → the stats keep exactly the 4 stages
    llm = FakeLLM(happy_replies())
    brief, stats = await run_brief_workflow(
        llm=llm, doc_rep=make_rep(tmp_path), controls=CONTROLS, deck_id="deck-test")
    assert set(stats) == {"A/text_1", "B/visual_fig_1", "C/reduce", "D/synthesize"}
    assert len(llm.calls) == 4
    assert not qa.run_qa_suite(brief,
                               assets=make_rep(tmp_path).asset_map()).errors


async def test_full_chain_repairs_a_fabricated_note(tmp_path):
    replies = happy_replies()
    replies[-1] = copy.deepcopy(replies[-1])
    replies[-1]["slides"][0]["speaker_notes"] = "Cite the 0.999 cost."
    fix = fixed_notes(S.PresentationBrief.model_validate(replies[-1]), 1,
                      "Cite the cost figure.")
    llm = FakeLLM(replies + [fix])
    brief, stats = await run_brief_workflow(
        llm=llm, doc_rep=make_rep(tmp_path), controls=CONTROLS, deck_id="deck-test")
    assert brief.slides[0].speaker_notes == "Cite the cost figure."
    assert stats["D-repair/notes_1"]["calls"] == 1
    assert len(llm.calls) == 5


# ── doctrine regression: the QA leg is search-free by construction ────────────

def test_qa_module_never_touches_llm_transport_or_search():
    import inspect
    src = inspect.getsource(qa)
    assert "ToolRuntime" not in src and "web_search" not in src
    assert "complete_json" not in src          # only via structured_call
