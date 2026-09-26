"""Guard tests for the Intent Funnel synthetic workload dataset.

These assert the INVARIANTS that make the files usable as a permanent
regression asset: determinism, stable unique case_ids, label/capability
consistency with the published capability universe, and coverage floors for
the designed slices. The dataset is data, not behavior — none of these tests
touch the funnel code or any runtime.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generator as gen

DATA = HERE / "datasets" / gen.VERSION


def _jsonl(name: str) -> list[dict]:
    return [json.loads(ln) for ln in (DATA / name).read_text(encoding="utf-8").splitlines() if ln]


QUERY = _jsonl("query_cases.jsonl")
SESSIONS = _jsonl("session_trajectories.jsonl")
ALL_TURNS = QUERY + [t for s in SESSIONS for t in s["turns"]]


def test_emitted_files_are_current_and_deterministic():
    # --check re-derives everything in memory and diffs against disk: this
    # catches both generator drift and any hand-edit of the emitted DATA.
    r = subprocess.run([sys.executable, str(HERE / "generator.py"), "--check"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    first = (DATA / "query_cases.jsonl").read_bytes()
    subprocess.run([sys.executable, str(HERE / "generator.py")], capture_output=True, check=False)
    assert (DATA / "query_cases.jsonl").read_bytes() == first, "generation is not byte-stable"


def test_case_ids_unique_and_stable_shape():
    ids = [r["case_id"] for r in QUERY] + [t["case_id"] for s in SESSIONS for t in s["turns"]]
    assert len(ids) == len(set(ids)), "duplicate case_id"
    for sid in (s["session_id"] for s in SESSIONS):
        assert gen.ID_RE.match(sid)
    for i in ids:
        assert gen.ID_RE.match(i)


def test_every_record_declares_synthetic_provenance():
    for r in QUERY + SESSIONS:
        assert r["provenance"] == "workload_design"
        assert r["dataset_version"] == gen.VERSION


def test_tool_labels_only_use_published_capabilities():
    for t in ALL_TURNS:
        e = t["expected"]
        if e["funnel"] == "TOOL":
            assert e["capability_id"] in gen.CAPS
            assert set(e["arguments"]) <= gen.CAPS[e["capability_id"]]
            assert e["arguments"], f"TOOL row without args: {t['case_id']}"
        else:
            assert e["capability_id"] is None
        if e["capability_id"] == "cap-pdf-extract-text":
            viewer = t.get("viewer_context") or next(
                s["viewer_context"] for s in SESSIONS
                if any(x["case_id"] == t["case_id"] for x in s["turns"]))
            assert viewer.get("asset_id") == e["arguments"]["asset_id"]


def test_hard_negative_classes_full_coverage():
    intents = {t["intent_category"] for t in ALL_TURNS}
    # all 20 required hard-negative families (spec §6) must be represented
    required = {i for i in [
        "HARD_NEGATIVE_CAPABILITY_MENTION", "HARD_NEGATIVE_TOOL_NAME_MENTION",
        "HARD_NEGATIVE_ASK_WHAT_TOOL_DOES", "HARD_NEGATIVE_HOW_TO_ACTION",
        "HARD_NEGATIVE_DISCUSSING_ACTION", "HARD_NEGATIVE_HYPOTHETICAL",
        "HARD_NEGATIVE_NEGATED", "HARD_NEGATIVE_CONDITIONAL",
        "HARD_NEGATIVE_AMBIGUOUS", "HARD_NEGATIVE_WRONG_TARGET",
        "HARD_NEGATIVE_DOC_REPORTS_ACTION", "HARD_NEGATIVE_QUOTED_ACTION",
        "HARD_NEGATIVE_DONT_EXECUTE", "HARD_NEGATIVE_DEFER_EXECUTE",
        "HARD_NEGATIVE_IF_I_WANT", "HARD_NEGATIVE_CAN_YOU",
        "HARD_NEGATIVE_FEATURE_QUESTION", "HARD_NEGATIVE_TOOL_AS_NOUN",
        "HARD_NEGATIVE_VIEWER_TOOL_MENTION", "HARD_NEGATIVE_DRIFT_TOOLISH",
    ] if i.startswith("HARD_NEGATIVE")}
    missing = required - intents
    assert not missing, f"hard-negative families missing: {missing}"
    # HN rows must never be labeled TOOL (they are, by definition, no-tool)
    for t in ALL_TURNS:
        if t["intent_category"].startswith("HARD_NEGATIVE") and t["intent_category"] != "HARD_NEGATIVE_AMBIGUOUS":
            assert t["expected"]["funnel"] == "AGENT"


def test_coverage_floors_for_pilot():
    scen = {t["scenario_category"] for t in ALL_TURNS}
    assert {"S1_READING", "S2_WATCHING", "S3_LEARNING", "S4_RESEARCH", "S5_WRITING",
            "S6_WORKSPACE", "S7_TOOL_ACTION", "S8_CHIT_CHAT", "S9_CONTEXT_DRIFT",
            "S10_LIFE_ASSISTANT"} <= scen
    assert {t["viewer_relation"] for t in ALL_TURNS} == gen.VRELS
    assert {t["difficulty"] for t in ALL_TURNS} == gen.DIFFS
    assert {t["expected"]["funnel"] for t in ALL_TURNS} == gen.FUNNELS
    counts = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    assert 100 <= len(QUERY) <= 260, counts["records"]
    assert len(SESSIONS) >= 10
    turns = {len(s["turns"]) for s in SESSIONS}
    assert {2, 3, 5} <= turns, "pilot must include 2/3/5-turn trajectories"


def test_sessions_are_whole_not_flattened():
    for s in SESSIONS:
        assert s["goal"] and s["scenario_category"] in gen.SCENARIOS
        for n, t in enumerate(s["turns"], 1):
            assert t["turn_id"] == n
            assert t["case_id"] == f"{s['session_id']}-t{n}"


def test_query_cases_are_the_single_turn_superset():
    tool = _jsonl("tool_positive.jsonl")
    hn = _jsonl("hard_negative.jsonl")
    assert all(r["expected"]["funnel"] == "TOOL" for r in tool)
    assert all(r["intent_category"].startswith("HARD_NEGATIVE") for r in hn)
    nat = _jsonl("natural_workload.jsonl")
    stress = _jsonl("stress_workload.jsonl")
    assert len(nat) == len(QUERY) + len(SESSIONS)
    stress_keys = {x["case_id"] if x["record"] == "query_case" else x["session_id"] for x in stress}
    universe = {x["case_id"] for x in QUERY} | {x["session_id"] for x in SESSIONS}
    assert stress_keys <= universe


def test_manifest_hashes_cover_outputs():
    m = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in m["output_hashes"].items():
        assert gen.sha256(DATA / name) == digest, f"drift in {name}"
    assert m["source_hashes"], "scenario sources must be fingerprinted"
    assert m["provenance"].startswith("workload_design")


@pytest.mark.parametrize("rec", [json.dumps(r, ensure_ascii=False) for r in (QUERY[:2] + SESSIONS[:2])])
def test_records_are_valid_json_roundtrip(rec):
    assert json.loads(rec)
