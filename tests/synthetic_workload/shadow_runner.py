"""Phase-E Cascade Shadow runner — replay the synthetic workload through the
PRODUCTION node body, stop at the Binder.

Chain per turn (constraint set, 2026-09-25): Registry → Matcher → (Recall,
raw lane: min_score=0, every hit kept) → ToolIntentModel (ONE call on the
model-floor-screened union) → Binder validate → STOP. No Runtime, no
Dispatch, no event row, no settings mutation — :func:`cascade_shadow` is the
single funnel entry and it only produces routing metadata (8.8).

Replay policy:
  * sessions are replayed WHOLE, turn by turn, never flattened (constraint 4);
  * each turn sees the funnel the same way production does — query +
    TurnFacts, never conversation history;
  * the Virtual Shadow State is updated ONLY from ``expected.state_delta``
    (constraint 6): the runner feeds ground truth, it never infers state from
    the LLM and never touches real storage;
  * every result lands in ``runs/<run_id>/`` — the dataset stays read-only.

Offline thresholding (constraint 3/10): recall runs ONCE at min_score=0 and
the raw candidate list is captured; every bucket in ``BUCKETS`` is
recomputed here from the capture. Because the model saw only the floor-0.58
union, a pick outside a bucket's set is reported as ``unobserved`` — the
bucket sweep measures candidate coverage, not a re-adjudication.

Run (real services: PG registry/index + TEI embeddings required for
meaningful scores):

    .venv/Scripts/python.exe tests/synthetic_workload/shadow_runner.py [--limit N]

Library use (tests): import ``replay_records`` / ``classify`` /
``bucket_attribution`` and feed deterministic fakes — see test_shadow_runner.py.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "datasets"          # populated by generator.py; NEVER written here
RUNS = HERE / "runs"

BUCKETS = (0.55, 0.58, 0.60, 0.62, 0.65, 0.70, 0.75, 0.82)
SHADOW_RECALL = {"recall_min_score": 0.0, "model_candidate_floor": 0.58}


# ── dataset (read-only) ───────────────────────────────────────────────────────────

def _jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def load_dataset(version: str = "v1-pilot") -> tuple[list[dict], list[dict], dict]:
    d = DATA / version
    return (_jsonl(d / "query_cases.jsonl"), _jsonl(d / "session_trajectories.jsonl"),
            json.loads((d / "manifest.json").read_text(encoding="utf-8")))


# ── turn -> ctx (the contract the Matcher sees; never the transcript) ─────────────

def ctx_for(query: str, viewer: dict | None, *, session_bound: bool):
    """Build the minimal ctx ``TurnFacts.of`` consumes. ``viewer_context`` maps:
    asset_id -> viewer.asset_id, current_page -> viewer.page, selection ->
    viewer.selections, has_attachment -> body.attach (well-formed sentinel id,
    never resolved)."""
    v = None
    if viewer and viewer.get("kind") not in (None, "none"):
        v = types.SimpleNamespace(
            asset_id=viewer.get("asset_id") or "",
            page=viewer.get("current_page"),
            selections=[viewer["selection"]] if viewer.get("selection") else [],
        )
    attach = None
    if viewer and viewer.get("has_attachment"):
        attach = {"kind": "asset",
                  "asset_id": viewer.get("asset_id") or "shadow-attachment",
                  "name": "shadow.bin"}
    return types.SimpleNamespace(
        body=types.SimpleNamespace(message=query, attach=attach, viewer=v),
        owned_asset_id=None, research_turn=False, effective_handoff=None,
        session_id="shadow-session" if session_bound else "",
    )


# ── Virtual Shadow State (expected deltas ONLY) ──────────────────────────────────

def apply_delta(state: dict, delta: dict | None) -> dict:
    """Fold one ground-truth state_delta into the virtual state. Vocabulary:
    folders{name->id}, terms[{domain,term}], extracted[asset_id]. Unknown keys
    are recorded, never guessed."""
    if not delta:
        return state
    for k, v in delta.items():
        if k == "folders":
            state.setdefault("folders", {}).update(v)
        elif k == "terms":
            state.setdefault("terms", []).extend(v)
        elif k == "extracted":
            state.setdefault("extracted", []).extend(v)
        else:
            state.setdefault("_unknown", []).append({k: v})
    return state


# ── outcome classification (expected-vocabulary, offline-regradable) ──────────────

def classify(result: dict) -> dict:
    """Actual funnel outcome in the dataset's vocabulary. TOOL = a certified
    would_execute; every abstain/fallback is AGENT-level (the funnel kept its
    promise to hand the turn over byte-identical). ABSTAIN is an entry-gate
    label the cascade never sees (the runner bypasses route()), so at cascade
    level it grades like AGENT — recorded separately in the confusion matrix."""
    we = result.get("would_execute")
    if we:
        return {"funnel": "TOOL", "capability_id": we.get("capability_id"),
                "arguments": we.get("args")}
    return {"funnel": "AGENT", "capability_id": None, "arguments": None}


def agrees(expected: dict, actual: dict) -> bool:
    e = expected["funnel"]
    if e == "TOOL":
        return (actual["funnel"] == "TOOL"
                and actual["capability_id"] == expected["capability_id"])
    # AGENT / ABSTAIN / AMBIGUOUS all say: do not certify a single tool
    return actual["funnel"] != "TOOL"


# ── offline threshold buckets (from the RAW capture; no recall re-run) ────────────

def model_set_at(capture: dict, t: float, top_k: int | None = None) -> list[str]:
    """Production's model-facing set AT threshold t, recomputed from raw
    scores: matcher-origin cards are exempt from the floor; recall cards are
    floor-screened and EVERY survivor rides (the width cap was retired by the
    2026-09-26 ruling; ``top_k`` remains only as an optional offline what-if
    axis)."""
    recall_c = sorted((c for c in capture.get("recall_raw", [])
                       if c["score"] >= t), key=lambda c: c["score"],
                      reverse=True)
    if top_k is not None:
        recall_c = recall_c[:top_k]
    ids = {c["capability_id"] for c in capture.get("candidates", [])
           if c["origin"] != "recall"}
    ids.update(c["capability_id"] for c in recall_c)
    return sorted(ids)


def bucket_attribution(capture: dict, buckets=BUCKETS,
                       top_k: int | None = None) -> dict:
    """Per-bucket attribution of the ONE observed model pick: picked cap in
    the bucket's set -> valid; no pick -> no_pick; pick outside the set ->
    unobserved (this turn provides no evidence for that bucket)."""
    ti = capture.get("tool_intent") or {}
    pick = ti.get("capability_id") if ti.get("decision") == "CONFIDENT" else None
    out = {}
    for t in buckets:
        s = model_set_at(capture, t, top_k)
        if pick is None:
            out[f"{t:.2f}"] = {"set": s, "attribution": "no_pick" if ti else "no_model_call"}
        else:
            out[f"{t:.2f}"] = {"set": s,
                               "attribution": "valid" if pick in s else "unobserved"}
    return out


# ── replay ────────────────────────────────────────────────────────────────────────

async def replay_records(*, version: str = "v1-pilot", limit: int | None = None,
                         deps=None, funnel_mod=None) -> list[dict]:
    """L1 cases first (single turns), then whole sessions turn by turn.
    ``deps``/``funnel_mod`` are injectable for tests; defaults to the wired
    real bundle and the production module."""
    if funnel_mod is None:
        from core.application.chat.intent_funnel import funnel as funnel_mod
    deps = deps if deps is not None else _DEPS
    cases, sessions, _ = load_dataset(version)
    records: list[tuple[str, int, dict]] = [(c["case_id"], 0, c) for c in cases]
    for s in sessions:
        records.extend((t["case_id"], n, dict(t, _session=s))
                       for n, t in enumerate(s["turns"], 1))
    if limit:
        records = records[:limit]
    out = []
    for case_id, turn_id, rec in records:
        session = rec.get("_session")
        viewer = rec.get("viewer_context") or (session or {}).get("viewer_context")
        ctx = ctx_for(rec["user_query"], viewer,
                      session_bound=session is not None and turn_id > 1)
        result = await funnel_mod.cascade_shadow(ctx, deps=deps, **SHADOW_RECALL)
        capture = result.get("capture") or {}
        actual = classify(result)
        out.append({
            "case_id": case_id, "session_id": (session or {}).get("session_id"),
            "turn_id": turn_id or None, "record": rec.get("record"),
            "user_query": rec["user_query"],
            "scenario_category": rec["scenario_category"],
            "intent_category": rec["intent_category"],
            "viewer_relation": rec["viewer_relation"],
            "difficulty": rec["difficulty"],
            "expected": rec["expected"],
            "actual": actual, "agrees": agrees(rec["expected"], actual),
            "deepest_stage": result["deepest_stage"],
            "fallback_reason": result["fallback_reason"],
            "matcher": result["matcher"], "tool_intent": result["tool_intent"],
            "total_ms": result["total_ms"],
            "capture": capture,
            "buckets": bucket_attribution(capture),
        })
    return out


# module-level, set by main() for the real run; tests pass their own deps via
# a contextmanager-free seam: replay_records reads _DEPS so the funnel entry
# stays the single production function.
_DEPS = None


# ── aggregation ───────────────────────────────────────────────────────────────────

def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    idx = min(len(xs) - 1, max(0, round(q / 100 * (len(xs) - 1))))
    return xs[idx]


def aggregate(turns: list[dict], sessions: list[dict]) -> dict:
    from collections import Counter
    all_turns = turns
    scores = [c["score"] for t in all_turns for c in t["capture"].get("recall_raw", [])]

    def funnel_of(t: dict) -> str:
        return t["expected"]["funnel"]
    conf = Counter((funnel_of(t), t["actual"]["funnel"]) for t in all_turns)
    stage = Counter(t["deepest_stage"] for t in all_turns)
    fb = Counter(t["fallback_reason"] for t in all_turns
                 if t["actual"]["funnel"] != "TOOL")
    ti = Counter((t["capture"].get("tool_intent") or {}).get("decision", "-")
                 for t in all_turns if "tool_intent" in t["capture"])
    binder = Counter(t["capture"].get("binder", "-")
                     for t in all_turns if "binder" in t["capture"])
    sweep = {}
    for b in BUCKETS:
        key = f"{b:.2f}"
        at = Counter(t["buckets"][key]["attribution"] for t in all_turns)
        set_sizes = [len(t["buckets"][key]["set"]) for t in all_turns
                     if t["buckets"][key]["set"]]
        coverage = [t for t in all_turns if funnel_of(t) == "TOOL"]
        in_set = sum(1 for t in coverage if t["expected"]["capability_id"]
                     in t["buckets"][key]["set"])
        sweep[key] = {
            "attribution": dict(at),
            "model_set_size": {"p50": _pct(set_sizes, 50), "max": max(set_sizes, default=0)},
            "tool_expected_cap_in_set": f"{in_set}/{len(coverage)}",
        }
    def _agree_by(key):
        g: dict[str, dict[str, int]] = {}
        for t in all_turns:
            row = g.setdefault(t[key], {"n": 0, "agree": 0})
            row["n"] += 1
            row["agree"] += int(t["agrees"])
        return g

    # ── per-stage stats across the WHOLE order (Matcher → Recall → ToolIntent
    # → Adapter(entry resolution) → Candidate Gate(kind) → Binder → STOP) ──────
    matcher_state = Counter(t["matcher"].split(":", 1)[0] for t in all_turns)
    recall_runs = sum(1 for t in all_turns if "recall_raw" in t["capture"])
    confs = [c["confidence"] for t in all_turns
             if (c := t["capture"].get("tool_intent") or {}).get("confidence") is not None]
    gates = {
        # Adapter: a CONFIDENT verdict the active table no longer honors
        "adapter_version_mismatch": fb.get("REGISTRY_VERSION_MISMATCH", 0),
        # Candidate Gate: kind in the table but its rollout switch is closed
        "candidate_gate_kind_disabled": fb.get("FUNNEL_KIND_DISABLED", 0),
    }
    def _grp(pred):
        xs = [t for t in all_turns if pred(t)]
        return {"n": len(xs), "agree": sum(t["agrees"] for t in xs),
                "certified_tool": sum(1 for t in xs if t["actual"]["funnel"] == "TOOL")}
    hard_neg = [t for t in all_turns if t["intent_category"].startswith("HARD_NEGATIVE")]
    hn_fp = [{
        "case_id": t["case_id"], "intent": t["intent_category"],
        "query": t["user_query"], "matcher": t["matcher"],
        "recall_top": (t["capture"].get("recall_raw") or [{}]),
        "tool_intent": t["capture"].get("tool_intent"),
        "binder": t["capture"].get("binder"), "expected": t["expected"],
    } for t in hard_neg if t["actual"]["funnel"] == "TOOL"]
    ms = [t["total_ms"] for t in all_turns]
    return {
        "turns": len(all_turns),
        "latency_ms": {"p50": _pct(ms, 50), "p95": _pct(ms, 95), "max": max(ms, default=None)},
        "groups": {
            "expected_TOOL": _grp(lambda t: funnel_of(t) == "TOOL"),
            "expected_AGENT": _grp(lambda t: funnel_of(t) == "AGENT"),
            "expected_AMBIGUOUS": _grp(lambda t: funnel_of(t) == "AMBIGUOUS"),
            "expected_ABSTAIN": _grp(lambda t: funnel_of(t) == "ABSTAIN"),
            "hard_negative": _grp(lambda t: t in hard_neg),
        },
        "hard_negative_fp_paths": hn_fp,
        "stages": {
            "matcher_states": dict(matcher_state),
            "recall_lanes_run": recall_runs,
            "recall_lanes_skipped_hit": len(all_turns) - recall_runs,
            "tool_intent_decisions": dict(ti),
            "adapter_and_gates": gates,
            "binder_states": dict(binder),
            "certified": stage.get("certified", 0),
        },
        "model_confidence": {
            "n": len(confs), "p50": _pct(confs, 50), "p95": _pct(confs, 95),
            "below_floor_0.75": sum(1 for c in confs if c < 0.75),
        },
        "overall_agree": sum(t["agrees"] for t in all_turns),
        "confusion_expected_x_actual": {f"{e}->{a}": n for (e, a), n in sorted(conf.items())},
        "deepest_stage": dict(stage),
        "fallback_reasons": dict(fb),
        "tool_intent_decisions": dict(ti),
        "binder_states": dict(binder),
        "raw_recall_scores": {
            "n": len(scores),
            "min": min(scores, default=None), "max": max(scores, default=None),
            "mean": round(statistics.fmean(scores), 4) if scores else None,
            "p10": _pct(scores, 10), "p50": _pct(scores, 50),
            "p90": _pct(scores, 90), "p99": _pct(scores, 99),
            "histogram": {f"{lo:.1f}": sum(1 for s in scores if lo <= s < lo + 0.1)
                          for lo in [round(x * 0.1, 1) for x in range(11)]},
            "by_capability": {
                cap: {"n": len(xs), "p50": _pct(xs, 50), "max": max(xs, default=None)}
                for cap, xs in scores_by_cap(all_turns)},
        },
        "threshold_sweep": sweep,
        "agree_by": {k: _agree_by(k) for k in
                     ("scenario_category", "intent_category", "difficulty",
                      "viewer_relation")},
        "by_funnel_expected": dict(Counter(funnel_of(t) for t in all_turns)),
        "sessions": {
            "n": len(sessions),
            "trajectory_exact": sum(1 for s in sessions if s["trajectory_match"]),
            "with_first_divergence": sum(1 for s in sessions
                                         if s.get("first_divergence")),
        },
    }


def scores_by_cap(turns):
    d: dict[str, list[float]] = {}
    for t in turns:
        for c in t["capture"].get("recall_raw", []):
            d.setdefault(c["capability_id"], []).append(c["score"])
    return d.items()


# ── output ───────────────────────────────────────────────────────────────────────

def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def write_run(turns: list[dict], session_rows: list[dict], report: dict, *,
              version: str) -> Path:
    run_id = f"{version}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}Z"
    out = RUNS / run_id
    out.mkdir(parents=True, exist_ok=False)
    with (out / "turns.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for t in turns:
            f.write(json.dumps(t, ensure_ascii=False, sort_keys=True) + "\n")
    with (out / "sessions.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for s in session_rows:
            f.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    from core.config import settings

    manifest = {
        "run_id": run_id, "dataset_version": version,
        "shadow_params": dict(SHADOW_RECALL, buckets=list(BUCKETS)),
        "backend_pins": {
            "chat_tool_intent_backend": settings.chat_tool_intent_backend,
            "chat_matcher_mode": getattr(settings, "chat_matcher_mode", None),
        },
        "production_settings_untouched": {
            "chat_funnel_min_score": settings.chat_funnel_min_score,
        },
        "timeout_guards": {
            "production_tool_intent_timeout_seconds": 4.0,
            "production_funnel_timeout_seconds": 5.0,
            "benchmark_tool_intent_timeout_seconds":
                settings.chat_tool_intent_timeout_seconds,
            "benchmark_funnel_timeout_seconds":
                settings.chat_funnel_timeout_seconds,
            "note": ("benchmark-only RUNTIME env overrides (nothing in .env/"
                     "config/compose changed): this run measures model "
                     "discrimination ability and does NOT represent timeout "
                     "behavior under the production 4s/5s SLA."),
        },
        "dataset_hashes": {p.name: sha16(p)
                           for p in sorted((DATA / version).iterdir())},
        "output_hashes": {p.name: sha16(p) for p in sorted(out.iterdir())
                          if p.name != "manifest.json"},
        "records": {"turns": len(turns), "sessions": len(session_rows)},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    return out


def build_session_rows(cases_turns: list[dict], version: str) -> list[dict]:
    """Session-level view: per-turn agreement, virtual-state fold (expected
    deltas ONLY), trajectory match. The fold is recomputed deterministically
    here so the run files prove constraint 6 on their face."""
    _, sessions, _ = load_dataset(version)
    by_case = {t["case_id"]: t for t in cases_turns}
    rows = []
    for s in sessions:
        state: dict = {}
        turn_rows = []
        first_div = None
        for n, t in enumerate(s["turns"], 1):
            cid = f"{s['session_id']}-t{n}"
            if cid not in by_case:      # outside --limit: fold still honest
                turn_rows.append({"case_id": cid, "present": False})
                continue
            got = by_case[cid]
            state = apply_delta(state, t["expected"].get("state_delta"))
            if not got["agrees"] and first_div is None:
                first_div = {"case_id": cid, "turn_id": n,
                             "expected": got["expected"], "actual": got["actual"],
                             "fallback_reason": got["fallback_reason"],
                             "deepest_stage": got["deepest_stage"]}
            turn_rows.append({"case_id": cid, "present": True,
                              "agrees": got["agrees"],
                              "expected_funnel": t["expected"]["funnel"],
                              "actual_funnel": got["actual"]["funnel"],
                              "fallback_reason": got["fallback_reason"],
                              "virtual_state_after": dict(state)})
        rows.append({
            "session_id": s["session_id"], "scenario_category": s["scenario_category"],
            "goal": s["goal"], "turns": turn_rows,
            "first_divergence": first_div,
            "trajectory_match": all(r.get("agrees") for r in turn_rows
                                    if r.get("present"))
            and any(r.get("present") for r in turn_rows),
            "virtual_state_final": state,
            "deltas_applied": sum(1 for t in s["turns"]
                                  if t["expected"].get("state_delta")),
        })
    return rows


# ── CLI (real services) ──────────────────────────────────────────────────────────

async def _main() -> int:
    global _DEPS
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1-pilot")
    ap.add_argument("--limit", type=int, default=None,
                    help="first N records only (smoke); full replay by default")
    args = ap.parse_args()

    # real deps: the SAME bundle production wires (chat.py); shadow writes files
    # only — no event rows, no dispatch, no settings mutation.
    from api.routers.chat import _embedder, llm
    from core.infrastructure.db import SessionLocal

    _DEPS = types.SimpleNamespace(session_factory=SessionLocal,
                                  embedder=_embedder, llm=llm)
    t0 = time.monotonic()
    turns = await replay_records(version=args.version, limit=args.limit)
    sessions = build_session_rows(turns, args.version)
    report = aggregate(turns, sessions)
    out = write_run(turns, sessions, report, version=args.version)
    print(f"run -> {out.relative_to(HERE)}  ({time.monotonic() - t0:.1f}s, "
          f"{len(turns)} turns, agree {report['overall_agree']}/{report['turns']}, "
          f"sessions exact {report['sessions']['trajectory_exact']}/{report['sessions']['n']})")
    print(json.dumps({k: report[k] for k in ("deepest_stage", "fallback_reasons",
                                             "tool_intent_decisions", "binder_states",
                                             "raw_recall_scores")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
