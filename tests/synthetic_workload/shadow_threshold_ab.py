"""Phase-F strict Threshold A/B — per-turn FULL downstream replay at each Recall
threshold; REAL Precision / Recall / F1 from actual routing (not candidate
coverage).

Method (2026-09-25, "full replay" ruling + 0.58 anchor):
  * input: the CLEAN baseline run (turns.jsonl with the RAW recall capture) +
    the LOCKED dataset v1 (never modified);
  * for EVERY (turn, threshold t) the downstream is REPLAYED THROUGH THE
    PRODUCTION ENTRY ``funnel.cascade_shadow`` (Registry -> Matcher -> Recall
    raw -> floor t -> ToolIntentModel -> Binder -> STOP). No reuse: even when
    the candidate set equals the baseline's, the real model is called again
    (this also exposes model non-determinism honestly). Per the 2026-09-26
    ruling an EMPTY model-facing candidate set short-circuits to NO_CANDIDATE
    BEFORE the hop — rows whose set is empty at threshold t cost zero calls;
  * thresholds sweep INDEPENDENTLY, one metric table per t (the 0.58 column is
    the baseline's real operating point); the production gate 0.82 and the
    dataset are never touched.

Grading (ABSTAIN / AMBIGUOUS kept out of the binary, never relabelled):
  * exact-capability (PRIMARY, honest for routing):
      TP = expected TOOL and certified the SAME capability;
      FP = certified a tool that should not have been, or a wrong capability;
      FN = expected TOOL not certified as it (missed or wrong-cap);
      TN = expected AGENT and no certification. WRONG_CAP sits in FP AND FN.
  * binary routing (secondary, cap ignored): TOOL(+) vs AGENT(-) only.
  * Wrong Interception Rate = FP / N  (any wrong tool executed, incl.
    ABSTAIN/AMBIGUOUS turns wrongly certified). Direct Tool Execution Rate =
    actual TOOL / N = Agent-LLM-calls-avoided rate (identical by construction,
    shown separately with its correct share TP/(TP+FP)).
  * latency = the funnel's total_ms (p50/p95/max), benchmark guards 8s/10s
    (runtime env only; production stays 4s/5s — see manifest).

Run (same env pins as the clean baseline):
    CHAT_TOOL_INTENT_* + CHAT_TOOL_INTENT_TIMEOUT_SECONDS=8
    + CHAT_FUNNEL_TIMEOUT_SECONDS=10, then
    .venv/Scripts/python.exe tests/synthetic_workload/shadow_threshold_ab.py \
        --baseline v1-pilot-<ts>Z [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

import shadow_runner as R  # sys.path via cwd=HERE or bootstrap below

AB_BUCKETS = (0.50, 0.55, 0.58, 0.60, 0.65, 0.70, 0.75, 0.80, 0.82)


def load_baseline(run_dir: Path) -> list[dict]:
    return R._jsonl(run_dir / "turns.jsonl")


# ── grading ───────────────────────────────────────────────────────────────────────

def grade_exact(expected: dict, actual: dict) -> str:
    et = expected["funnel"] == "TOOL"
    at = actual["funnel"] == "TOOL"
    if et and at:
        return "TP" if actual["capability_id"] == expected["capability_id"] else "FP+FN"
    if at:
        return "FP"          # expected AGENT/ABSTAIN/AMBIGUOUS -> certified
    if et:
        return "FN"
    return "TN"              # expected AGENT -> no certification


def grade_binary(expected: dict, actual: dict) -> str | None:
    """Routing-only (cap ignored); None for ABSTAIN/AMBIGUOUS (kept aside)."""
    e = expected["funnel"]
    if e not in ("TOOL", "AGENT"):
        return None
    return ("TP" if e == "TOOL" else "FP") if actual["funnel"] == "TOOL" \
        else ("FN" if e == "TOOL" else "TN")


# ── one (turn, t): FULL production replay ─────────────────────────────────────────

async def replay_one(turn: dict, t: float, deps, funnel_mod,
                     top_k: int | None = None) -> dict:
    res = await funnel_mod.cascade_shadow(
        _ctx_for(turn), deps=deps,
        **dict(R.SHADOW_RECALL, model_candidate_floor=t))
    cap = res.get("capture") or {}
    ti = cap.get("tool_intent") or {}
    actual = R.classify(res)
    recall_raw = cap.get("recall_raw") or []
    exempt = {c["capability_id"]: c["score"] for c in cap.get("candidates") or []
              if c.get("origin") != "recall"}
    rawmap: dict[str, float] = {}
    for c in recall_raw:
        rawmap[c["capability_id"]] = max(rawmap.get(c["capability_id"], 0.0), c["score"])
    for cid, s in exempt.items():
        rawmap[cid] = max(rawmap.get(cid, 0.0), s)
    set_t = R.model_set_at(cap, t, top_k)
    return {"case_id": turn["case_id"], "session_id": turn.get("session_id"),
            "turn_id": turn.get("turn_id"), "user_query": turn["user_query"],
            "expected": turn["expected"], "intent_category": turn["intent_category"],
            "scenario_category": turn["scenario_category"],
            "difficulty": turn["difficulty"], "viewer_relation": turn["viewer_relation"],
            "threshold": t, "model_set": set_t, "model_called": "tool_intent" in cap,
            "matcher": res["matcher"],
            "scores": sorted([{"capability_id": c, "score": rawmap.get(c)}
                              for c in set_t], key=lambda x: -(x["score"] or 0)),
            "raw_scores_all": {c: round(s, 6) for c, s in rawmap.items()},
            "decision": ti.get("decision"), "decision_cap": ti.get("capability_id"),
            "confidence": ti.get("confidence"), "arguments": ti.get("arguments"),
            "binder": cap.get("binder"), "fallback": res["fallback_reason"],
            "deepest_stage": res["deepest_stage"], "total_ms": res["total_ms"],
            "actual": actual}


def attribute(row: dict, margin: float) -> str:
    """Failure cause of the day, rule-based (A..F) for FP/FN/FP+FN rows."""
    if row["grade_exact"] == "TP" or row["grade_exact"] == "TN":
        return "-"
    exp, act = row["expected"], row["actual"]
    rawmap, set_t = row["raw_scores_all"], set(row["model_set"])
    hn = row["intent_category"].startswith("HARD_NEGATIVE")
    # --- FN side: expected TOOL, not certified as it ----------------------------
    if exp["funnel"] == "TOOL":
        cap_id = exp["capability_id"]
        score = rawmap.get(cap_id)
        if score is None:                       # not even scored by recall
            return "D"
        if cap_id not in set_t:                 # floor-screened away at t
            return "A" if score >= 0.55 else "D"
        others = [s for c, s in rawmap.items() if c != cap_id and c in set_t]
        if others and (max(others) - score) < margin:
            return "B"                          # near-score rival in the prompt
        return "C"                              # right card present, model still erred
    # --- FP side: a tool was wrongly certified ---------------------------------
    if act["funnel"] == "TOOL":
        if hn:
            return "E"                          # hard-negative boundary
        if exp["funnel"] == "TOOL":
            others = [s for c, s in rawmap.items()
                      if c != exp["capability_id"] and c in set_t]
            own = rawmap.get(exp["capability_id"], 0.0)
            return "B" if others and (max(others) - own) >= -margin else "C"
        return "C"                              # pure QA wrongly certified
    return "F"


# ── the pass ───────────────────────────────────────────────────────────────────────

_CTX_CACHE: dict = {}


def _ctx_for(turn: dict):
    if turn["case_id"] in _CTX_CACHE:
        return _CTX_CACHE[turn["case_id"]]
    cases, sessions, _ = R.load_dataset("v1-pilot")
    viewer, bound = None, False
    for c in cases:
        if c["case_id"] == turn["case_id"]:
            viewer = c.get("viewer_context")
            break
    else:
        for s in sessions:
            for n, tr in enumerate(s["turns"], 1):
                if tr["case_id"] == turn["case_id"]:
                    viewer = tr.get("viewer_context") or s.get("viewer_context")
                    bound = n > 1
                    break
    ctx = R.ctx_for(turn["user_query"], viewer, session_bound=bound)
    _CTX_CACHE[turn["case_id"]] = ctx
    return ctx


async def run_ab(baseline: list[dict], deps, *, funnel_mod, margin: float,
                 buckets=AB_BUCKETS) -> list[dict]:
    out: list[dict] = []
    t0 = time.monotonic()
    n_model = n_short = 0
    for turn in baseline:
        for t in buckets:
            row = await replay_one(turn, t, deps, funnel_mod)
            row["grade_exact"] = grade_exact(row["expected"], row["actual"])
            row["grade_binary"] = grade_binary(row["expected"], row["actual"])
            row["cause"] = attribute(row, margin)
            out.append(row)
            n_model += int(row["model_called"])
            n_short += int(not row["model_called"])
        if len(out) % 120 < len(buckets):
            print(f"  {len(out)}/{len(baseline) * len(buckets)} rows "
                  f"({n_model} model calls) {time.monotonic()-t0:.0f}s", flush=True)
    print(f"AB done: {len(out)} rows, {n_model} real model calls, "
          f"{n_short} rows without a model call "
          f"(NO_CANDIDATE short-circuits + gated-off turns, ruling 2026-09-26), "
          f"in {time.monotonic()-t0:.0f}s", flush=True)
    return out


# ── per-threshold metrics ────────────────────────────────────────────────────────

def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def _median4(xs):
    return round(statistics.median(xs), 4) if xs else None


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q / 100 * (len(xs) - 1))))]


def _group_metrics(rs: list[dict]) -> dict:
    tp = sum(1 for r in rs if r["grade_exact"] == "TP")
    fp = sum(1 for r in rs if r["grade_exact"] == "FP")
    fn = sum(1 for r in rs if r["grade_exact"] == "FN")
    tn = sum(1 for r in rs if r["grade_exact"] == "TN")
    wrong = sum(1 for r in rs if r["grade_exact"] == "FP+FN")
    b = Counter(r["grade_binary"] for r in rs if r["grade_binary"])
    n = len(rs)
    direct = sum(1 for r in rs if r["actual"]["funnel"] == "TOOL")
    ms = [r["total_ms"] for r in rs]
    return {
        "n": n, "TP": tp, "FP": fp, "FN": fn, "TN": tn, "WRONG_CAP": wrong,
        **_prf(tp, fp + wrong, fn + wrong),
        "binary_routing": {"n": sum(b.values()), "TP": b.get("TP", 0),
                           "FP": b.get("FP", 0), "FN": b.get("FN", 0),
                           "TN": b.get("TN", 0),
                           **_prf(b.get("TP", 0), b.get("FP", 0), b.get("FN", 0))},
        "held_abstain_ambiguous": {
            "n": sum(1 for r in rs if r["expected"]["funnel"] in ("ABSTAIN", "AMBIGUOUS")),
            "certified": sum(1 for r in rs
                             if r["expected"]["funnel"] in ("ABSTAIN", "AMBIGUOUS")
                             and r["actual"]["funnel"] == "TOOL")},
        "wrong_interception_rate": round((fp + wrong) / n, 4) if n else None,
        "direct_tool_execution_rate": round(direct / n, 4) if n else None,
        "agent_fallback_rate": round((n - direct) / n, 4) if n else None,
        "llm_calls_avoided": direct,
        "llm_avoidance_rate": round(direct / n, 4) if n else None,
        "correct_share_of_avoided": round(tp / direct, 4) if direct else None,
        "model_calls": sum(1 for r in rs if r["model_called"]),
        "empty_set_short_circuits": sum(1 for r in rs if not r["model_called"]),
        "latency_ms": {"p50": _pct(ms, 50), "p95": _pct(ms, 95),
                       "max": max(ms, default=None)},
    }


def _by_category(rs: list[dict], key: str, collapse_hn=False) -> dict:
    g: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        k = r[key]
        if collapse_hn and str(k).startswith("HARD_NEGATIVE"):
            k = "HARD_NEGATIVE"
        g[k].append(r)
    out = {}
    for k, sub in sorted(g.items()):
        tp = sum(1 for r in sub if r["grade_exact"] == "TP")
        fp = sum(1 for r in sub if r["grade_exact"] == "FP")
        fn = sum(1 for r in sub if r["grade_exact"] == "FN")
        tn = sum(1 for r in sub if r["grade_exact"] == "TN")
        wrong = sum(1 for r in sub if r["grade_exact"] == "FP+FN")
        out[k] = {"n": len(sub), "TP": tp, "FP": fp, "FN": fn, "TN": tn,
                  "WRONG_CAP": wrong, **_prf(tp, fp + wrong, fn + wrong)}
    return out


def _hard_negative(rs: list[dict]) -> dict:
    hn = [r for r in rs if r["intent_category"].startswith("HARD_NEGATIVE")]
    fps = [r for r in hn if r["actual"]["funnel"] == "TOOL"]
    return {"n": len(hn), "certified_tool": len(fps), "fp": len(fps),
            "fp_rate": round(len(fps) / len(hn), 4) if hn else None,
            "fp_cases": [{"case_id": r["case_id"], "intent": r["intent_category"],
                          "query": r["user_query"], "picked_cap": r["decision_cap"],
                          "confidence": r["confidence"], "model_set": r["model_set"],
                          "scores": {s["capability_id"]: s["score"] for s in r["scores"]},
                          "cause": r["cause"]} for r in fps]}


def _capabilities(rs: list[dict]) -> dict:
    exp: dict[str, dict] = {}
    for cap in {r["expected"]["capability_id"] for r in rs
                if r["expected"]["funnel"] == "TOOL" and r["expected"].get("capability_id")}:
        sub = [r for r in rs if r["expected"].get("capability_id") == cap]
        tp = sum(1 for r in sub if r["grade_exact"] == "TP")
        wrong = sum(1 for r in sub if r["grade_exact"] == "FP+FN")
        exp[cap] = {"n_expected": len(sub), "tp": tp, "fn": len(sub) - tp,
                    "wrong_cap": wrong, "recall": round(tp / len(sub), 4)}
    picks: dict[str, dict] = {}
    for pick in {r["decision_cap"] for r in rs if r["actual"]["funnel"] == "TOOL"}:
        sub = [r for r in rs if r["decision_cap"] == pick and r["actual"]["funnel"] == "TOOL"]
        ok = sum(1 for r in sub if r["grade_exact"] == "TP")
        picks[pick] = {"n_certified": len(sub), "correct_cap": ok,
                       "precision": round(ok / len(sub), 4)}
    return {"per_expected_capability": exp, "per_picked_capability": picks}


def _failure_causes(rs: list[dict]) -> dict:
    counts = Counter(r["cause"] for r in rs if r["cause"] not in ("-",))
    det = [{"case_id": r["case_id"], "threshold": r["threshold"],
            "grade": r["grade_exact"], "cause": r["cause"],
            "intent": r["intent_category"], "query": r["user_query"],
            "expected": r["expected"], "actual": r["actual"],
            "model_set": r["model_set"], "raw_scores": r["raw_scores_all"],
            "decision": r["decision"], "confidence": r["confidence"],
            "binder": r["binder"], "fallback": r["fallback"]}
           for r in rs if r["cause"] not in ("-",)
           and r["grade_exact"] in ("FP", "FN", "FP+FN")]
    return {"counts": dict(counts), "cases": det}


KEY_CASES = ("s6-ws-add-term-03", "sess-extract-01-t3")


def _key_case_track(rows: list[dict]) -> dict:
    out = {}
    for cid in KEY_CASES:
        out[cid] = {f"{r['threshold']:.2f}": {
            "decision": r["decision"], "cap": r["decision_cap"],
            "conf": r["confidence"], "binder": r["binder"],
            "grade": r["grade_exact"], "cause": r["cause"],
            "model_set": r["model_set"],
            "ms": round(r["total_ms"]), "model_called": r["model_called"]}
            for r in rows if r["case_id"] == cid}
    return out


def _sessions(rows: list[dict]) -> dict:
    out = {}
    for t in AB_BUCKETS:
        key = f"{t:.2f}"
        sess = []
        sids = {r["session_id"] for r in rows if r["threshold"] == t and r.get("session_id")}
        for sid in sorted(sids):
            srs = sorted((r for r in rows if r["threshold"] == t
                          and r.get("session_id") == sid), key=lambda x: x["turn_id"])
            div = next((r for r in srs
                        if r["grade_exact"] in ("FP", "FN", "FP+FN")), None)
            sess.append({"session_id": sid, "trajectory_match": div is None,
                         "first_divergence": None if div is None else {
                             "case_id": div["case_id"], "turn_id": div["turn_id"],
                             "grade": div["grade_exact"], "cause": div["cause"],
                             "expected": div["expected"], "actual": div["actual"],
                             "decision": div["decision"], "binder": div["binder"]}})
        out[key] = {"n": len(sess),
                    "trajectory_exact": sum(1 for s in sess if s["trajectory_match"]),
                    "sessions": sess}
    return out


def summarize(rows: list[dict]) -> dict:
    by_t = {}
    for t in AB_BUCKETS:
        rs = [r for r in rows if r["threshold"] == t]
        by_t[f"{t:.2f}"] = {
            **_group_metrics(rs),
            "decisions": dict(Counter(r["decision"] or "-" for r in rs)),
            "fallbacks": dict(Counter(r["fallback"] for r in rs
                                      if r["decision"] != "CONFIDENT")),
            "by_intent_category": _by_category(rs, "intent_category"),
            "by_scenario_category": _by_category(rs, "scenario_category"),
            "categories_collapse_HN": _by_category(rs, "intent_category", True),
            **_capabilities(rs),
            "hard_negative": _hard_negative(rs),
            "failure_causes": {"counts": Counter(r["cause"] for r in rs
                                                 if r["cause"] != "-")},
            "model_confidence_p50": _median4([r["confidence"] for r in rs
                                              if r.get("confidence") is not None]),
        }
    causes = _failure_causes(rows)
    causes["legend"] = {
        "A": "Recall threshold excluded the correct cap (score >=0.55 but < t)",
        "B": "near-score rival candidate contaminated the model's choice",
        "C": "ToolIntentModel judged wrongly with the right card present "
             "(REJECT/wrong pick/pure-QA certified)",
        "D": "Recall/registry coverage: correct cap never scored well enough",
        "E": "hard-negative boundary: the model certified an intent-adjacent query",
        "F": "other"}
    return {"by_threshold": by_t,
            "aggregate_failure_causes": causes,
            "key_cases": _key_case_track(rows),
            "sessions_by_threshold": _sessions(rows),
            "note": ("every row is a REAL production cascade_shadow replay at "
                     "its threshold (no reuse; per the 2026-09-26 ruling an "
                     "empty candidate set short-circuits to NO_CANDIDATE before "
                     "the hop, so only rows with at least one card pay a live "
                     "call); ABSTAIN/AMBIGUOUS are "
                     "held out of the binary and never relabelled; WRONG_CAP "
                     "counts in both FP and FN. This measures model "
                     "discrimination under benchmark 8s/10s guards; production "
                     "4s/5s timeout behaviour is NOT measured here.")}


# ── main ──────────────────────────────────────────────────────────────────────────

async def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="runs/<id> dir of the CLEAN baseline")
    ap.add_argument("--limit", type=int, default=None,
                    help="first N turns only (pipeline smoke)")
    args = ap.parse_args()
    base_dir = R.RUNS / args.baseline
    baseline = load_baseline(base_dir)
    if args.limit:
        baseline = baseline[:args.limit]

    import types

    from api.routers.chat import _embedder, llm
    from core.application.chat.intent_funnel import funnel as funnel_mod
    from core.config import settings
    from core.infrastructure.db import SessionLocal
    margin = settings.chat_funnel_margin
    deps = types.SimpleNamespace(session_factory=SessionLocal,
                                 embedder=_embedder, llm=llm)

    rows = await run_ab(baseline, deps, funnel_mod=funnel_mod, margin=margin)
    summary = summarize(rows)
    out = base_dir.parent / f"{base_dir.name}-ab"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "turn_level_ab.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    (out / "thresholds_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    manifest = {
        "ab_run": out.name, "baseline_run": base_dir.name,
        "dataset_version": "v1-pilot",
        "policy": ("FULL replay per (turn,threshold) through funnel.cascade_shadow "
                   "— no reuse; empty-set NO_CANDIDATE short-circuit (2026-09-26 "
                   "ruling): model calls == rows with a non-empty set at t; "
                   "empty_set_short_circuits counts the zero-hop rows"),
        "buckets": [round(b, 2) for b in AB_BUCKETS],
        "model_calls": sum(1 for r in rows if r["model_called"]),
        # kept as EVIDENCE, not policy: under the new contract this must be 0;
        # any non-zero value means a row abstained BEFORE the model (a fault).
        "empty_short_circuits": sum(1 for r in rows if not r["model_called"]),
        "timeout_guards": {
            "production_tool_intent_timeout_seconds": 4.0,
            "production_funnel_timeout_seconds": 5.0,
            "benchmark_tool_intent_timeout_seconds":
                settings.chat_tool_intent_timeout_seconds,
            "benchmark_funnel_timeout_seconds":
                settings.chat_funnel_timeout_seconds,
            "note": "benchmark-only runtime env overrides; production defaults untouched"},
        "production_settings_untouched": {
            "chat_funnel_min_score": settings.chat_funnel_min_score},
        "dataset_hashes": {p.name: R.sha16(p)
                           for p in sorted((R.DATA / "v1-pilot").iterdir())},
        "baseline_hashes": {p.name: R.sha16(p)
                            for p in sorted(base_dir.iterdir())},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(f"AB run -> {out.relative_to(R.HERE)}")
    for k, v in summary["by_threshold"].items():
        print(f"  t={k}: P={v['precision']:.3f} R={v['recall']:.3f} "
              f"F1={v['f1']:.3f} TP={v['TP']} FP={v['FP']} FN={v['FN']} "
              f"TN={v['TN']} WC={v['WRONG_CAP']} WIR={v['wrong_interception_rate']:.3f} "
              f"DT={v['direct_tool_execution_rate']:.3f} AF={v['agent_fallback_rate']:.3f} "
              f"HNfp={v['hard_negative']['fp']} p50={v['latency_ms']['p50']}ms "
              f"calls={v['model_calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
