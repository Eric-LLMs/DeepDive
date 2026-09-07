"""P3-7B evidence: harvest context_profile distributions from the audit stream.

Reads ``turn-end`` rows (llm_calls/tokens/cost/context_profile) and any
``session-start`` context_profile lines, reports per-field distributions so the
truncation/read-window thresholds are DERIVED from telemetry, not gut numbers.
"""
import json
import statistics as st
from collections import defaultdict

ROWS = []
TS_BY_TURN = {}
for line in open("data/audit.jsonl", encoding="utf-8"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    t = r.get("ts") or ""
    tid = r.get("turn_id")
    if t and tid and t[:4] == "2026":
        TS_BY_TURN.setdefault(tid, t)
    if "context_profile" not in line:
        continue
    cp = r.get("context_profile")
    if isinstance(cp, dict) and cp.get("total_chars") is not None:
        ROWS.append((TS_BY_TURN.get(tid, ""), r.get("type") or "?", cp, r))

print(f"rows with context_profile: {len(ROWS)}")
kinds = defaultdict(int)
for ts, kind, cp, r in ROWS:
    kinds[kind] += 1
print("kinds:", dict(kinds))

FIELDS = ("estimated", "system_chars", "skill_chars", "tool_result_chars",
          "history_chars", "total_chars", "estimated_tokens")

def pct(vals, q):
    vals = sorted(vals)
    i = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
    return vals[i]

# group turn-end profiles by session (one research run = one session)
by_run = defaultdict(list)
for ts, kind, cp, r in ROWS:
    if kind == "turn-end":
        by_run[(r.get("session_id") or "?")[:12]].append((ts, cp, r))

all_te = [(ts, cp, r) for ts, kind, cp, r in ROWS if kind == "turn-end"]
print(f"\n=== ALL turn-end rows: {len(all_te)} ===")
def _dist(rows):
    for f in FIELDS:
        vals = [cp.get(f) for _, cp, _ in rows if isinstance(cp.get(f), (int, float))]
        if not vals:
            print(f"{f}: (absent)")
            continue
        print(f"{f:18s} n={len(vals):3d} min={min(vals):8.0f} p25={pct(vals,.25):8.0f} "
              f"med={st.median(vals):8.0f} p75={pct(vals,.75):8.0f} p90={pct(vals,.9):8.0f} "
              f"max={max(vals):9.0f}")
_dist(all_te)

for date in sorted(by_run):
    rows = sorted(by_run[date], key=lambda x: x[0])
    if len(rows) < 6:
        continue
    print(f"\n=== turn-end rows session={date}: {len(rows)} ===")
    _dist(rows)
    # per-turn derived ratios: how much of context is tool results vs history
    tr = [cp["tool_result_chars"] / cp["total_chars"] for _, cp, _ in rows
          if cp.get("total_chars") and cp.get("tool_result_chars") is not None]
    hc = [cp["history_chars"] / cp["total_chars"] for _, cp, _ in rows
          if cp.get("total_chars") and cp.get("history_chars") is not None]
    if tr:
        print(f"tool_result share of total: med={st.median(tr):.2%} max={max(tr):.2%}")
    if hc:
        print(f"history share of total:     med={st.median(hc):.2%} max={max(hc):.2%}")
    # token growth across the run (turn order)
    toks = [cp.get("estimated_tokens") for _, cp, _ in rows if cp.get("estimated_tokens")]
    if toks:
        print(f"estimated_tokens first->last: {toks[0]:.0f} -> {toks[-1]:.0f} (x{toks[-1]/max(1,toks[0]):.1f})")

# cost/token context from turn-end payload (outside context_profile).
# Only sessions that look like 8-turn research runs (>=6 turn-end rows).
print("\n=== turn-end payload (research-run sessions) ===")
for date in sorted(by_run):
    if len(by_run[date]) < 6:
        continue
    for ts, cp, r in sorted(by_run[date], key=lambda x: x[0]):
        llm = r.get("llm_calls"); tok = r.get("tokens"); cost = r.get("cost_usd")
        ld = r.get("llm_duration_ms")
        tpr = (ld / llm / 1000.0) if llm and ld else None
        print(f"{ts} turn={r.get('turn_id','')[:8]} stage={r.get('stage')} llm={llm} "
              f"tokens={tok} cost={cost} ctx_est={cp.get('estimated_tokens')} "
              f"toolres={cp.get('tool_result_chars')} hist={cp.get('history_chars')} "
              f"ms/llm_call={tpr and round(tpr,1)}")
