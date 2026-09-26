"""Guard tests for the Phase-E Cascade Shadow seam (funnel + runner).

All fakes, no services: a scripted registry view, a 2-D index whose cosines
ARE the chosen scores, and an online-backend LLM double. The tests pin the
invariants that make a shadow run trustworthy:

  * production is byte-identical when the seam is unused (defaults None);
  * the raw lane captures every scored candidate (min_score=0), while the
    model still sees a floor-screened set;
  * buckets are recomputed OFFLINE from the capture, recall never re-runs;
  * cascade_shadow never persists an event row, never dispatches, and pins
    execution_mode=shadow for the duration (restored after);
  * the Virtual Shadow State folds ONLY expected.state_delta.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shadow_runner as R
from core.application.chat.intent_funnel import funnel
from core.application.chat.intent_funnel import recall as recall_mod
from core.application.chat.intent_funnel import registry as reg_mod
from core.application.chat.intent_funnel.registry import entry as entry_mod
from core.infrastructure.request_context import (
    get_request_execution_mode,
)

# ── the live-capability mirror (3 caps, dataset-consistent slots) ─────────────────

def _entries():
    return (
        entry_mod.CapabilityEntry(
            capability_id="cap-create-folder", tool_binding="create_folder",
            description="create a folder", patterns=("新建文件夹",),
            parameters={"name": {"type": "string", "required": True}},
        ),
        entry_mod.CapabilityEntry(
            capability_id="cap-add-term", tool_binding="add_term",
            description="add a vocabulary term",
            parameters={"term": {"type": "string", "required": True},
                        "domain": {"type": "string", "required": True},
                        "definition": {"type": "string", "required": False}},
        ),
        entry_mod.CapabilityEntry(
            capability_id="cap-pdf-extract-text", tool_binding="pdf_extract_text",
            description="extract pdf text",
            parameters={"asset_id": {"type": "string", "required": True}},
        ),
    )


def _view(fp: str) -> entry_mod.RegistryLiveView:
    return entry_mod.RegistryLiveView(fingerprint=fp, entries=_entries())


# Index whose cosines are EXACT scores: query vector (1,0); corpus-row vector
# for score s is (s, sqrt(1-s^2)). cap-create-folder scores 0.60, cap-add-term 0.90.
_SCORES = {"cap-create-folder": 0.60, "cap-add-term": 0.90,
           "cap-pdf-extract-text": 0.30}


def _index(version="ix-1"):
    corpus = []
    for e in _entries():
        s = _SCORES[e.capability_id]
        corpus.append(SimpleNamespace(
            kind="standard", query_id=f"{e.capability_id}-q1",
            capability_id=e.capability_id, query="ex one", language="en",
            standard_query_id=None,
            vector=(s, math.sqrt(max(0.0, 1 - s * s)))))
    return SimpleNamespace(version=version, corpus=tuple(corpus))


class _Embedder:
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _LLM:
    """Online ToolIntentModel double: scripted reply per query substring, and
    it REMEMBERS the execution mode observed at call time (pin proof)."""

    def __init__(self, replies: dict[str, dict] | None = None):
        self.replies = replies or {}
        self.calls = []

    async def complete_json(self, prompt, **kw):
        self.calls.append({"prompt": prompt, "kw": kw,
                           "mode": get_request_execution_mode()})
        for frag, reply in self.replies.items():
            if frag in prompt:
                return dict(reply)
        return {"capability_id": "NONE"}


def _deps(llm):
    return SimpleNamespace(session_factory=None, embedder=lambda: _Embedder(),
                           llm=llm)


@pytest.fixture
def wired(monkeypatch, request):
    """Patch the two storage seams; per-test fingerprint keeps the Matcher's
    compile cache from aliasing views across tests."""
    fp = f"fp-{request.node.name}"
    async def fake_active_view(*, session_factory=None):
        return _view(fp)

    async def fake_load_index(session_factory):
        return _index()

    monkeypatch.setattr(reg_mod, "active_view", fake_active_view)
    monkeypatch.setattr(recall_mod, "load_index", fake_load_index)
    monkeypatch.setattr("core.config.settings.chat_tool_intent_backend", "online")
    # fail LOUD if the shadow path ever reaches the production persist hook
    async def no_persist(*a, **k):
        raise AssertionError("shadow must never persist an event row")

    monkeypatch.setattr(funnel, "_persist_event", no_persist)
    return fp


# ── funnel seam: production identity + raw capture ────────────────────────────────

@pytest.mark.asyncio
async def test_default_call_is_production_recall(wired):
    """No seam args -> settings' quality gate stays INSIDE recall: the 0.60
    card is filtered before the model (min_score 0.82 default), only 0.90 rides."""
    llm = _LLM({"cap": {"capability_id": "cap-add-term", "confidence": 0.9,
                        "arguments": {"term": "keystone", "domain": "工程"}}})
    ctx = R.ctx_for("帮我把 keystone 收录到工程词汇库", None, session_bound=False)
    trace = funnel._new_trace()
    out = await funnel._run_cascade(ctx, _deps(llm), _req(), trace)
    assert out is not None and out.requested_action["capability_id"] == "cap-add-term"
    assert trace["recall_count"] == 1          # 0.60 and 0.30 were filtered in recall
    assert llm.calls and "cap-create-folder" not in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_shadow_lane_keeps_raw_scores_and_floors_the_model_set(wired):
    llm = _LLM({"keystone": {"capability_id": "cap-add-term", "confidence": 0.9,
                             "arguments": {"term": "keystone", "domain": "工程"}}})
    ctx = R.ctx_for("把 keystone 加入工程词汇库", None, session_bound=False)
    result = await funnel.cascade_shadow(ctx, deps=_deps(llm), **R.SHADOW_RECALL)
    cap = result["capture"]
    # raw lane: EVERY candidate recall scored survived (constraint 3)
    raw = {c["capability_id"]: c["score"] for c in cap["recall_raw"]}
    assert raw == {"cap-add-term": 0.9, "cap-create-folder": 0.6,
                   "cap-pdf-extract-text": 0.3}
    # model-facing set at floor 0.58: the 0.30 card is gone, 0.60 rides
    assert {c["capability_id"] for c in cap["candidates"]} >= {"cap-add-term",
                                                               "cap-create-folder"}
    assert cap["tool_intent"]["decision"] == "CONFIDENT"
    assert cap["tool_intent"]["confidence"] == pytest.approx(0.9)
    assert cap["binder"] == "COMPLETE"
    assert result["would_execute"]["capability_id"] == "cap-add-term"
    # the pin: the model call happened inside execution_mode=shadow; the token
    # reset restored the ambient default afterwards
    assert llm.calls[0]["mode"] == "shadow"
    assert get_request_execution_mode() == "production"
    assert result["execution_mode"] == "shadow"


@pytest.mark.asyncio
async def test_aggregation_collapses_hits_but_recall_raw_stays_raw(wired, monkeypatch):
    """E1 (final semantics 2026-09-26): the Capability Candidate Aggregation sits
    AFTER the raw capture — capture["recall_raw"] keeps EVERY >= gate hit
    (cap-add-term arrives twice), while the model-facing set carries ONE
    capability-level card per capability: the winning hit's score AND
    provenance ride, the losing duplicate is gone, the offline sweep is
    unaffected."""
    dup_rows = [
        SimpleNamespace(kind="standard", query_id="add-hi",
                        capability_id="cap-add-term", query="ex-hi", language="en",
                        standard_query_id=None,
                        vector=(0.95, math.sqrt(1 - 0.95 ** 2))),
        SimpleNamespace(kind="standard", query_id="add-lo",
                        capability_id="cap-add-term", query="ex-lo", language="en",
                        standard_query_id=None,
                        vector=(0.90, math.sqrt(1 - 0.90 ** 2))),
        SimpleNamespace(kind="standard", query_id="fold-1",
                        capability_id="cap-create-folder", query="ex one",
                        language="en", standard_query_id=None,
                        vector=(0.60, math.sqrt(1 - 0.60 ** 2))),
    ]
    dup_index = SimpleNamespace(version="ix-dup", corpus=tuple(dup_rows))

    async def fake_load(session_factory):
        return dup_index

    monkeypatch.setattr(recall_mod, "load_index", fake_load)
    llm = _LLM({"cap": {"capability_id": "cap-add-term", "confidence": 0.9,
                        "arguments": {"term": "keystone", "domain": "工程"}}})
    ctx = R.ctx_for("把 keystone 加入工程词汇库", None, session_bound=False)
    trace = funnel._new_trace()
    capture: dict = {}
    out = await funnel._run_cascade(ctx, _deps(llm), _req(), trace,
                                    recall_min_score=0.5, capture=capture)
    assert out is not None and out.requested_action["capability_id"] == "cap-add-term"
    # raw capture: untouched by aggregation — BOTH cap-add-term hits ride
    raw = [(c["capability_id"], c["score"], c["matched_example"])
           for c in capture["recall_raw"]]
    assert raw.count(("cap-add-term", 0.95, "ex-hi")) == 1
    assert raw.count(("cap-add-term", 0.9, "ex-lo")) == 1
    assert trace["recall_count"] == 3            # the raw count, pre aggregation
    # model-facing set: capability-level — one cap-add-term card at the WIN
    add = [c for c in capture["candidates"] if c["capability_id"] == "cap-add-term"]
    assert len(add) == 1 and add[0]["score"] == 0.95 and add[0]["matched_example"] == "ex-hi"
    prompt = llm.calls[0]["prompt"]
    assert prompt.count("### cap-add-term") == 1     # 0.90 duplicate collapsed
    assert "ex-lo" not in prompt                    # loser provenance is gone


@pytest.mark.asyncio
async def test_shadow_abstains_never_dispatches_never_persists(wired):
    """A REJECT verdict is an Agent exit with metadata only — no would_execute."""
    llm = _LLM({"今天天气": {"capability_id": "NONE"}})
    ctx = R.ctx_for("今天天气怎么样", None, session_bound=False)
    result = await funnel.cascade_shadow(ctx, deps=_deps(llm), **R.SHADOW_RECALL)
    assert result["would_execute"] is None
    assert result["fallback_reason"] == "TOOL_INTENT_REJECT"
    assert result["capture"]["tool_intent"]["decision"] == "REJECT"


# ── offline buckets (recomputed from capture, no recall re-run) ───────────────────

def _capture_for(pick: str | None):
    return {"recall_raw": [{"capability_id": c, "score": s, "origin": "recall",
                            "matched_example": ""} for c, s in _SCORES.items()],
            "candidates": [{"capability_id": c, "score": s, "origin": "recall",
                            "matched_example": ""} for c, s in _SCORES.items()
                           if s >= 0.58],
            "tool_intent": ({"decision": "CONFIDENT", "capability_id": pick}
                            if pick else {"decision": "REJECT", "capability_id": None})}


def test_bucket_sets_and_attribution():
    # top_k=3 keeps every floor-surviving card here (tiny fake corpus)
    assert set(R.model_set_at(_capture_for("cap-add-term"), 0.55, 3)) == {
        "cap-add-term", "cap-create-folder"}          # 0.30 is below every bucket
    assert set(R.model_set_at(_capture_for("cap-add-term"), 0.62, 3)) == {"cap-add-term"}
    at = R.bucket_attribution(_capture_for("cap-add-term"), top_k=3)
    assert at["0.65"]["attribution"] == "valid"       # pick 0.90 is in the 0.65 set
    assert R.model_set_at(_capture_for("cap-add-term"), 0.65, 3) == ["cap-add-term"]
    at2 = R.bucket_attribution(_capture_for("cap-create-folder"), top_k=3)
    # a pick the model made at floor 0.58 is UNOBSERVED for the 0.65 bucket
    assert at2["0.65"]["attribution"] == "unobserved"
    assert at2["0.55"]["attribution"] == "valid"
    at3 = R.bucket_attribution(_capture_for(None), top_k=3)
    assert at3["0.60"]["attribution"] == "no_pick"
    assert at3["0.60"]["set"] == ["cap-add-term", "cap-create-folder"]


def test_model_set_respects_top_k_cap():
    cap = {"recall_raw": [{"capability_id": f"c{i}", "score": 0.9 - i * 0.05,
                           "origin": "recall", "matched_example": ""}
                          for i in range(6)],
           "candidates": [], "tool_intent": {"decision": "REJECT", "capability_id": None}}
    assert len(R.model_set_at(cap, 0.58, 3)) == 3      # floor-survivors capped at k


# ── classification / agreement / virtual state ────────────────────────────────────

def test_classify_and_agree_rules():
    assert R.classify({"would_execute": {"capability_id": "x", "args": {}}}) == {
        "funnel": "TOOL", "capability_id": "x", "arguments": {}}
    assert R.classify({"would_execute": None})["funnel"] == "AGENT"
    assert R.agrees({"funnel": "TOOL", "capability_id": "x"},
                    {"funnel": "TOOL", "capability_id": "x"})
    assert not R.agrees({"funnel": "TOOL", "capability_id": "x"},
                        {"funnel": "TOOL", "capability_id": "y"})
    # every no-certify expected label is satisfied by an AGENT actual
    for e in ("AGENT", "ABSTAIN", "AMBIGUOUS"):
        assert R.agrees({"funnel": e}, {"funnel": "AGENT"})
        assert not R.agrees({"funnel": e}, {"funnel": "TOOL", "capability_id": "x"})


def test_virtual_state_folds_expected_deltas_only():
    s = {}
    s = R.apply_delta(s, {"folders": {"papers": "vfolder-papers"}})
    s = R.apply_delta(s, {"terms": [{"domain": "工程", "term": "keystone"}]})
    s = R.apply_delta(s, None)
    s = R.apply_delta(s, {"extracted": ["ast-nlp-survey"]})
    assert s == {"folders": {"papers": "vfolder-papers"},
                 "terms": [{"domain": "工程", "term": "keystone"}],
                 "extracted": ["ast-nlp-survey"]}
    s = R.apply_delta({}, {"weird_key": 1})
    assert s["_unknown"] == [{"weird_key": 1}]


def test_ctx_for_turn_facts_mapping():
    ctx = R.ctx_for("总结这一页", {"kind": "pdf", "asset_id": "a-1",
                                  "current_page": 7, "selection": "para"},
                    session_bound=True)
    from core.application.chat.intent_funnel.contract import TurnFacts

    facts = TurnFacts.of(ctx)
    assert facts.has_viewer and facts.viewer_asset_id == "a-1"
    assert facts.viewer_current_page == 7 and facts.has_viewer_selection
    assert facts.has_turn_context and not facts.has_attachment
    plain = TurnFacts.of(R.ctx_for("你好", None, session_bound=False))
    assert not plain.has_viewer and not plain.has_turn_context


# ── session replay against the LOCKED dataset (fake funnel, whole sessions) ───────

@pytest.mark.asyncio
async def test_replay_whole_sessions_and_fold_matches_dataset(wired):
    """Full-workload replay with a canned funnel (all fakes, in-process): the
    214-record natural workload runs through the production node body, L1
    singletons first and sessions whole (never flattened), and the session
    virtual state equals exactly the cumulative expected deltas."""
    llm = _LLM({"": {"capability_id": "cap-create-folder", "confidence": 0.9,
                     "arguments": {"name": "anything"}}})
    turns = await R.replay_records(version="v1-pilot", deps=_deps(llm))
    cases, sessions, _ = R.load_dataset("v1-pilot")
    assert len(turns) == len(cases) + sum(len(s["turns"]) for s in sessions)
    assert all(t["record"] == "query_case" for t in turns[:len(cases)])
    rows = R.build_session_rows(turns, "v1-pilot")
    assert len(rows) == len(sessions)
    by_sid = {s["session_id"]: s for s in sessions}
    folded_any = 0
    for row in rows:
        s = by_sid[row["session_id"]]
        assert all(r["present"] for r in row["turns"])         # full run: nothing missing
        state = {}
        for t in s["turns"]:
            state = R.apply_delta(state, t["expected"].get("state_delta"))
        assert row["virtual_state_final"] == state             # ground-truth fold only
        # canned funnel agrees where the label is TOOL-ish; the predicate is
        # recomputed, not assumed (an all-CONFIDENT canned pick must NOT agree
        # with the deliberate AGENT labels).
        assert row["trajectory_match"] == all(
            t["agrees"] for t in turns
            if t["session_id"] == row["session_id"])
        folded_any += 1
    assert folded_any == len(sessions)
    assert any(not row["trajectory_match"] for row in rows)    # honest disagreement exists


@pytest.mark.asyncio
async def test_aggregate_shape_over_fake_run():
    t = {"case_id": "x", "capture": _capture_for("cap-add-term"),
         "expected": {"funnel": "TOOL", "capability_id": "cap-add-term"},
         "actual": {"funnel": "TOOL", "capability_id": "cap-add-term"},
         "agrees": True, "deepest_stage": "certified",
         "fallback_reason": "-", "matcher": "MISS", "total_ms": 5,
         "user_query": "q", "scenario_category": "S1_READING",
         "intent_category": "TOOL_ACTION", "difficulty": "EASY",
         "viewer_relation": "NO_VIEWER",
         "buckets": R.bucket_attribution(_capture_for("cap-add-term"), top_k=3)}
    rep = R.aggregate([t], [])
    assert rep["turns"] == 1 and rep["overall_agree"] == 1
    assert set(rep["threshold_sweep"]) == {f"{b:.2f}" for b in R.BUCKETS}
    assert rep["raw_recall_scores"]["n"] == 3
    assert rep["threshold_sweep"]["0.55"]["tool_expected_cap_in_set"] == "1/1"


def _req():
    from core.application.chat.understanding import Complexity, Confidence, Signal, TurnRequirements

    return TurnRequirements(complexity=Complexity.LOW, confidence=Confidence.LOW,
                            needs_web=Signal.LOW, needs_memory=False)
