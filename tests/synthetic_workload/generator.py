"""Deterministic generator for the Intent Funnel synthetic workload (v1-pilot).

Design contract (see DESIGN.md): the scenario YAML files under scenarios/ are
the authoring surface; this script only EXPANDS, VALIDATES and EMITS. No RNG,
no timestamps — the same inputs always produce byte-identical JSONL, so the
dataset is a stable regression asset with forever-stable case_ids.

Outputs (datasets/<VERSION>/):
  query_cases.jsonl          L1 superset (single-turn cases)
  tool_positive.jsonl        L1 expected TOOL
  hard_negative.jsonl        L1 intent HARD_NEGATIVE_*
  natural_workload.jsonl     the balanced designed mix (all L1 + all sessions)
  stress_workload.jsonl      HN / AMBIGUOUS / HARD+ADVERSARIAL / S9 / sessions
  session_trajectories.jsonl L2/L3 whole sessions (never flattened)
  manifest.json              version, source hashes, counts, output hashes

Run:  .venv/Scripts/python.exe tests/synthetic_workload/generator.py [--check|--stats]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE / "scenarios"
DATASET_ROOT = HERE / "datasets"
VERSION = "v1-pilot"

# The published-capability universe (Phase-A verified): labels outside this set
# would measure a capability that does not exist.
CAPS = {
    "cap-create-folder": {"name"},
    "cap-add-term": {"term", "domain", "definition"},
    "cap-pdf-extract-text": {"asset_id"},
}
FUNNELS = {"TOOL", "AGENT", "AMBIGUOUS", "ABSTAIN"}
DIFFS = {"EASY", "MEDIUM", "HARD", "ADVERSARIAL"}
VRELS = {"NO_VIEWER", "GROUNDED_IN_VIEWER", "RELATED_BUT_NOT_IN_VIEWER",
         "BEYOND_VIEWER", "UNRELATED_DRIFT"}
SCENARIOS = {"S1_READING", "S2_WATCHING", "S3_LEARNING", "S4_RESEARCH", "S5_WRITING",
             "S6_WORKSPACE", "S7_TOOL_ACTION", "S8_CHIT_CHAT", "S9_CONTEXT_DRIFT",
             "S10_LIFE_ASSISTANT", "HARD_NEGATIVE"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

L1_FILES = ["tool.yaml", "workspace.yaml", "reading.yaml", "watching.yaml",
            "learning.yaml", "research.yaml", "writing.yaml", "chit_chat.yaml",
            "context_drift.yaml", "life_assistant.yaml", "hard_negative.yaml"]
SESSION_FILES = ["sessions.yaml"]


def _norm_viewer(v: dict | None) -> dict:
    v = dict(v or {"kind": "none"})
    out = {"kind": v.get("kind", "none")}
    for k in ("asset_id", "title", "current_page", "selection"):
        if v.get(k) is not None:
            out[k] = v[k]
    if v.get("has_attachment"):
        out["has_attachment"] = True
    return out


def _expected(exp: dict | None) -> dict:
    exp = exp or {}
    funnel = exp.get("funnel", "AGENT")
    if funnel not in FUNNELS:
        raise ValueError(f"bad funnel {funnel!r}")
    out = {"funnel": funnel, "capability_id": None, "arguments": None,
           "state_delta": exp.get("delta")}
    if funnel == "TOOL":
        cap = exp.get("cap")
        if cap not in CAPS:
            raise ValueError(f"TOOL label outside published caps: {cap!r}")
        args = exp.get("args")
        if not args or not set(args) <= CAPS[cap]:
            raise ValueError(f"{cap}: bad expected args {args!r}")
        out["capability_id"] = cap
        out["arguments"] = args
    return out


def _intent_ok(intent: str) -> bool:
    return intent in {
        "TOOL_ACTION", "KNOWLEDGE_QA", "CONTEXTUAL_QA", "DOCUMENT_GROUNDED_QA",
        "VIDEO_GROUNDED_QA", "BEYOND_VIEWER_QA", "RESEARCH", "WRITING", "FOLLOW_UP",
        "CHIT_CHAT", "CONTEXT_DRIFT", "LIFE_ASSISTANT",
    } or intent.startswith("HARD_NEGATIVE")


def expand_l1() -> list[dict]:
    cases: list[dict] = []
    for fname in L1_FILES:
        doc = yaml.safe_load((SCENARIO_DIR / fname).read_text(encoding="utf-8"))
        scenario = doc["scenario"]
        assert scenario in SCENARIOS, fname
        slug = {"HARD_NEGATIVE": "hn"}.get(scenario, scenario.split("_")[0].lower())
        defaults = doc.get("defaults", {})
        for group in doc["cases"]:
            gid = group["id"]
            g_intent = group.get("intent", defaults.get("intent", "KNOWLEDGE_QA"))
            g_vrel = group.get("vrel", defaults.get("vrel", "NO_VIEWER"))
            g_diff = group.get("difficulty", defaults.get("difficulty", "EASY"))
            g_viewer = group.get("viewer", defaults.get("viewer"))
            g_exp = group.get("expected", {})
            for i, var in enumerate(group["variants"], 1):
                if isinstance(var, str):
                    var = {"q": var}
                q = var["q"]
                intent = var.get("intent", g_intent)
                vrel = var.get("vrel", g_vrel)
                diff = var.get("difficulty", g_diff)
                viewer = _norm_viewer(var.get("viewer", g_viewer))
                exp = _expected({**g_exp, **({"cap": var["cap"]} if "cap" in var else {}),
                                 **({"args": var["args"]} if "args" in var else {}),
                                 **({"delta": var["delta"]} if "delta" in var else {}),
                                 **({"funnel": var["funnel"]} if "funnel" in var else {})})
                if exp["funnel"] == "TOOL" and exp["capability_id"] == "cap-pdf-extract-text":
                    if "asset_id" not in viewer:
                        raise ValueError(f"{gid}-{i}: extract TOOL needs viewer asset_id")
                    if exp["arguments"]["asset_id"] != viewer["asset_id"]:
                        raise ValueError(f"{gid}-{i}: asset_id mismatch")
                rec = {
                    "record": "query_case",
                    "case_id": f"{slug}-{gid}-{i:02d}",
                    "dataset_version": VERSION,
                    "provenance": "workload_design",
                    "turn_id": 1,
                    "conversation_history": [],
                    "user_query": q,
                    "viewer_context": viewer,
                    "scenario_category": scenario,
                    "intent_category": intent,
                    "viewer_relation": vrel,
                    "difficulty": diff,
                    "expected": exp,
                }
                if var.get("notes"):
                    rec["notes"] = var["notes"]
                cases.append(rec)
    return cases


def expand_sessions() -> list[dict]:
    sessions: list[dict] = []
    for fname in SESSION_FILES:
        doc = yaml.safe_load((SCENARIO_DIR / fname).read_text(encoding="utf-8"))
        for s in doc["sessions"]:
            sid = s["id"]
            assert ID_RE.match(sid) and sid.startswith("sess-"), sid
            turns = []
            for n, t in enumerate(s["turns"], 1):
                exp = _expected(t["exp"])
                turn = {
                    "case_id": f"{sid}-t{n}",
                    "turn_id": n,
                    "user_query": t["q"],
                    "scenario_category": s["scenario"],
                    "intent_category": t["intent"],
                    "viewer_relation": t["vrel"],
                    "difficulty": t["diff"],
                    "expected": exp,
                }
                if t.get("asst"):
                    turn["assistant_response"] = t["asst"]
                if t.get("notes"):
                    turn["notes"] = t["notes"]
                if exp["funnel"] == "TOOL" and exp["capability_id"] == "cap-pdf-extract-text":
                    turn["viewer_context"] = _norm_viewer(s.get("viewer"))
                turns.append(turn)
            sessions.append({
                "record": "session_trajectory",
                "session_id": sid,
                "dataset_version": VERSION,
                "provenance": "workload_design",
                "scenario_category": s["scenario"],
                "goal": s["goal"],
                "initial_context": s.get("initial_context", ""),
                "viewer_context": _norm_viewer(s.get("viewer")),
                "turns": turns,
            })
    return sessions


def is_stress(rec: dict) -> bool:
    def turn_stress(t: dict) -> bool:
        return (t["intent_category"].startswith("HARD_NEGATIVE")
                or t["expected"]["funnel"] in {"AMBIGUOUS", "ABSTAIN"}
                or t["difficulty"] in {"HARD", "ADVERSARIAL"}
                or t["scenario_category"] == "S9_CONTEXT_DRIFT")
    if rec["record"] == "query_case":
        return turn_stress(rec)
    return any(turn_stress(t) for t in rec["turns"])


def validate(cases: list[dict], sessions: list[dict]) -> None:
    ids = set()
    for r in cases:
        assert ID_RE.match(r["case_id"]), r["case_id"]
        assert r["case_id"] not in ids, f"duplicate {r['case_id']}"
        ids.add(r["case_id"])
        assert r["scenario_category"] in SCENARIOS
        assert _intent_ok(r["intent_category"]), r
        assert r["viewer_relation"] in VRELS
        assert r["difficulty"] in DIFFS
        assert r["user_query"].strip()
    for s in sessions:
        assert s["session_id"] not in ids
        ids.add(s["session_id"])
        assert s["scenario_category"] in SCENARIOS
        for n, t in enumerate(s["turns"], 1):
            assert t["turn_id"] == n
            assert t["case_id"] == f"{s['session_id']}-t{n}"
            assert t["case_id"] not in ids, f"duplicate {t['case_id']}"
            ids.add(t["case_id"])
            assert _intent_ok(t["intent_category"]), t
            assert t["difficulty"] in DIFFS and t["viewer_relation"] in VRELS
            if t["intent_category"] == "TOOL_ACTION":
                assert t["expected"]["funnel"] in {"TOOL", "AGENT"}, "TOOL_ACTION must say TOOL or deliberate AGENT(no-cap)"


def bucket(fn: str, key) -> dict:
    return dict(sorted(Counter(key(r) for r in fn).items()))


def build() -> dict[str, list[dict]]:
    cases = sorted(expand_l1(), key=lambda r: r["case_id"])
    sessions = sorted(expand_sessions(), key=lambda r: r["session_id"])
    validate(cases, sessions)
    files = {
        "query_cases.jsonl": cases,
        "tool_positive.jsonl": [r for r in cases if r["expected"]["funnel"] == "TOOL"],
        "hard_negative.jsonl": [r for r in cases if r["intent_category"].startswith("HARD_NEGATIVE")],
        "natural_workload.jsonl": cases + sessions,
        "stress_workload.jsonl": [r for r in cases + sessions if is_stress(r)],
        "session_trajectories.jsonl": sessions,
    }
    all_turns = cases + [t for s in sessions for t in s["turns"]]
    files["manifest.json"] = [{
        "dataset_version": VERSION,
        "provenance": "workload_design — designed synthetic mixture, NOT a real-user distribution",
        "records": {k: len(v) for k, v in files.items()},
        "turn_level_counts": {
            "by_scenario": bucket(all_turns, lambda r: r["scenario_category"]),
            "by_expected_funnel": bucket(all_turns, lambda r: r["expected"]["funnel"]),
            "by_intent": bucket(all_turns, lambda r: r["intent_category"]),
            "by_viewer_relation": bucket(all_turns, lambda r: r["viewer_relation"]),
            "by_difficulty": bucket(all_turns, lambda r: r["difficulty"]),
            "by_capability": bucket([r for r in all_turns if r["expected"]["capability_id"]],
                                    lambda r: r["expected"]["capability_id"]),
            "session_turn_lengths": bucket(sessions, lambda s: f"{len(s['turns'])}-turn"),
        },
        "changelog": {VERSION: "pilot: 10-scenario seeds + HN classes + fixed trajectories"},
    }]
    return files


def emit(files: dict[str, list[dict]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, records in files.items():
        if name.endswith(".jsonl"):
            body = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                             for r in records) + "\n"
        else:
            body = json.dumps(records[0], ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        (out_dir / name).write_text(body, encoding="utf-8", newline="\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if on-disk files differ from regenerated output")
    ap.add_argument("--stats", action="store_true", help="print distribution summary")
    args = ap.parse_args()
    files = build()
    out_dir = DATASET_ROOT / VERSION
    if args.check:
        def _jsonl_blob(rs: list[dict]) -> bytes:
            return ("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                              for r in rs) + "\n").encode()

        stale = []
        for n, rs in files.items():
            if n == "manifest.json":
                disk = out_dir / n
                if not disk.exists():
                    stale.append(n)
                    continue
                m = json.loads(disk.read_text(encoding="utf-8"))
                core = {k: v for k, v in m.items() if not k.endswith("_hashes")}
                regenerated = {k: v for k, v in rs[0].items() if not k.endswith("_hashes")}
                if core != regenerated:
                    stale.append(n)
                    continue
                for f, h in m.get("output_hashes", {}).items():
                    if not (out_dir / f).exists() or sha256(out_dir / f) != h:
                        stale.append(f"{n}:{f}")
                continue
            if not (out_dir / n).exists() or (out_dir / n).read_bytes() != _jsonl_blob(rs):
                stale.append(n)
        print("STALE:" if stale else "in sync:", *stale, sep="\n" if stale else " ")
        return 1 if stale else 0
    emit(files, out_dir)
    # manifest hashes cover every emitted file (self-hash excepted)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_hashes"] = {p.name: sha256(p) for p in sorted(SCENARIO_DIR.glob("*.yaml"))}
    manifest["output_hashes"] = {p.name: sha256(p) for p in sorted(out_dir.iterdir())
                                 if p.name != "manifest.json"}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    if args.stats:
        print(json.dumps(manifest["turn_level_counts"], ensure_ascii=False, indent=2))
        print("files:", {p.name: sha256(p) for p in sorted(out_dir.iterdir())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
