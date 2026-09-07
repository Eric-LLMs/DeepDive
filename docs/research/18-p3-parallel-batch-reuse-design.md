# P3 Parallel / Batch / Reuse Refactor Design

Status: **design proposal — awaiting human review. No code was touched.**
Data basis: Run 7 (baseline, pre-P3) and Run 9 (first run after P3-1 `verify_batch` + P3-2 `fetch cache`/single-flight shipped), both reconstructed from `data/audit.jsonl` (`tool-call-detail` + `turn-end`) and the task's `run_events.json` (`run_seq` 7 / 9, task `26d220bf`, progressive mode, both auto-settled to PUBLISH).

---

## 1. Executive Summary

- **Half of the P3 agenda already shipped and demonstrably works.** Run 9 vs Run 7: tool calls 198 → 124, tokens 3.61 M → 1.79 M, cost $0.565 → $0.289, format-retry errors 14 → 1. The verify→mutate double-anchor chain is **gone in production** (Run 9: 5 `verify_batch`, **0** single `verify`, **0** `mutate_node`). Server-side fetch Σ-time is 259 s → 95 s.
- **The bottleneck has relocated: 86 % of Run 9 wall clock is LLM round-trips** (1 238 s of 1 441 s; 77 LLM calls averaging 16.1 s, ≈ 224 k tokens/turn of growing context). Tool I/O is now only 12 %. A P3 that only parallelizes tools cannot move the wall clock further.
- **The dominant waste left is orchestration, not mechanism.** The deterministic skip basis (`get_state` digest with `pending` + `evidence_fingerprint`) and the atomic batch writer (`verify_batch`, item-level rejects, one revision bump) exist, but **neither `deep_research.skill.md` nor `auto_turn_prompt` mentions `verify_batch` or `pending`** — the skill text still prescribes "one fetch (≤3) then one `verify` per claim" plus `mutate_node` for citations, i.e. the exact retail pattern P3-1 was built to kill. Run 9 used `verify_batch` anyway (schema discovery), but inconsistently (one batch in EVIDENCE; re-fetch/re-read of cached pages in later stages).
- Recommended program, in ROI order: **P3-4** deterministic-pending orchestration (skill + prompt + digest hints — the Vertical Slice, §16), **P3-5** fetch fan-out & chunk caps, **P3-6** unified single-writer graph commit (close the unlocked `_save_graph` race), **P3-7** context-size control for the LLM wall (the new dominant cost). All of it inside `plugins/research` + skill/prompt text; **`packages/workflow` and `packages/agent` stay frozen**.
- Explicitly **rejected** in §6: an LLM-level "verdict cache" keyed by model+prompt. The committed-verdict ledger already exists (`_verify_fps` + `pending`); re-verdicting unchanged evidence is a *skip* decision, not a *cache lookup*, and a model-keyed verdict cache adds correctness risk for no observed duplicate compute.

## 2. Run 7 Bottleneck Evidence (Observed)

Methodology (per `logs/_profile_run7_timing.py`): all durations from the loop's own measurement fields (`duration_s`, `llm_duration_ms`, `tool_duration_ms`, per-call `duration_ms`); `ts` used only for stage attribution; wall ≠ llm + tools + overhead is reported per turn, never assumed. Run 7 window = `ts < 2026-09-07T04:31` filtered by the run-7 auto-turn ids (the raw byte-offset boundary also contains later runs — filter is mandatory).

| Metric (Run 7, Observed) | Value |
|---|---|
| tool calls | **198** (exact) |
| tokens | **3,610,101** |
| cost | **$0.5651** (over the $0.20–0.30 budget) |
| wall (Σ turn `duration_s`) | **1,814 s (30.2 min)** |
| single `verify` | **52** (EVIDENCE 25 / EXECUTE 17 / EXPLAIN 10); ~27 of them re-verify already-anchored claims |
| `mutate_node` | 49 (EVIDENCE 18 / EXECUTE 15 / EXPLAIN 16) — mostly the citations/strength leg of the double-anchor chain |
| `research_scrape fetch` | **19 calls, Σ 259.1 s, mean 13.6 s, max 21.7 s**, all serial |
| schema-format retries | **14 errors** (verify findings×5 stringified, mutate missing `node_id`×7 / `patch`×2) |
| fetch-cache receipts | none — FetchStore shipped *after* Run 7; duplicate-URL count only reconstructable from `sanitized_args` |

## 2b. Run 9 Evidence (post P3-1/P3-2, the control point)

| Metric (Run 9, Observed) | Value | vs Run 7 |
|---|---|---|
| tool calls | **124** | −37 % |
| tokens / cost | **1,790,649 / $0.2886** | −50 % / −49 % |
| wall | **1,441 s (24.0 min)** | −21 % |
| **LLM phase** | **1,238 s = 86 % of wall** (77 calls, mean 16.1 s; ≈224 k tok/turn) | now dominant |
| tool phase | 179 s = 12 % | no longer the problem |
| `verify_batch` / `verify` / `mutate_node` | **5 / 0 / 0** | double-anchor eliminated in behavior |
| fetch | **7 calls, Σ 94.9 s**, incl. **two same-step fetches gathered in parallel** (turn `5ed657ae` step 4) | fan-out is feasible on today's flags |
| errors | **7** (reddit 403 ×3, read-discipline ×2, artifact-not-found ×1, missing `action` ×1) — schema-retry errors ≈ **0** | P3-3 coercion worked |
| terminal | auto-settle at QUALITY_GATE (`scorecard` — known legacy: gate reads `project["scorecard"]`, nothing writes it) | out of P3 scope, tracked |

## 3. Current Execution Dependency Graph

Within one EVIDENCE auto-turn today (observed Run 9 + code facts):

```
get_state (digest: claims + pending + evidence_fingerprint)   ← mechanical skip basis EXISTS
   ▼
per-claim loop, model-driven, SERIAL:
   web_search → pick URLs → fetch (≤3 URLs; asyncio.gather inside the call)
   → model reads snippets (same context)            ← verdicts are produced by the MAIN model
   → verify_batch or verify (deterministic commit)     in agent-loop steps, not per-claim LLM calls
   → [retail residual] mutate_node for citations       ← skill text still prescribes this shape
   ▼
next claim ...  → gate check → transition_stage
```

Key structural facts:
- **Turns are strictly serial** within a run (chained arq jobs; `drive_iteration` one execution at a time — by design, §19.3).
- **Steps are serial** inside a turn; the only in-step parallelism is the frozen loop's adjacent safe-tool gather (`loop.py:637-675`, window 10). `research_scrape` is the only research tool with `concurrency_safe=True` (`plugin.py:4643`); `research_evidence` is a whole-tool barrier (`plugin.py:3980-3987` — per-tool flags are all the frozen loop offers, P3-1 accepted calibration).
- **Cross-turn context is wiped** except the DB chat messages; the model re-derives state from `get_state`/`get_handoff`/snapshot — the 27 re-verifies of Run 7 lived here.
- **Unlocked writers**: `record_node`/`mutate_node`/`link_edge`/`invalidate_downstream`/single `verify` all persist via `_save_graph` (`plugin.py:2203/2226/2273/2285/3534`) — atomic file replace but **no portalocker, no revision CAS, no bump**; only `verify_batch` and asset/state paths go through `atomic_update_project` (`plugin.py:531-608`).

## 4. Parallelization Candidates (I/O × Rate Limit × Single-Flight)

| Work item | Today | Parallel-safe? | Rate limit | Single-flight? | Candidate action |
|---|---|---|---|---|---|
| `scrape fetch` (URL → clean text) | batch ≤3, gather inside one call; cross-step serial | yes — proven same-step gather (`research_scrape` safe flag) | per-site politeness; provider timeouts 30 s; FlightRegistry lease = timeout×(redirects+1)+5 | **yes**, process-local generation-fenced (`fetch_cache.py:339-483`) | **P3-5**: instruct same-step fan-out of k×fetch(≤3) for a whole pending batch; optional cap 3→4/5 behind a token guard (keep body budget: 3×`max_chars` is the current de-facto window bound) |
| `web_search` | serial (flag `None` → barrier; 13 calls, Σ 28.3 s) | pure network I/O, stateless | ddg/bing soft-throttle at ~1–2 QPS sustained | not yet | **P3-5a**: flag `is_concurrency_safe=True` on `apps/api/tools/web_search_tool.py` (translation layer, not frozen engine); aggregate provider already fetches engines concurrently internally |
| `search_social` | serial (6 calls, Σ 34.0 s) | yes, but reddit **403'd 3× in Run 9** — a live rate limit | per-platform | worth it (same query twice across turns wastes the quota) | **P3-5b**: safe flag + short TTL result cache keyed query+platform; treat 403 as terminal-for-run (stop retrying within the run — Run 9 retried reddit 3×) |
| `rag_search` | serial (1 call, 7.9 s) | read-only | none local | duplicate-query flight merge | low priority (volume tiny in EVIDENCE) |
| `verify_batch` commit | one CAS per call | **must stay serial** — single writer by design | — | — | do not parallelize (constraint 4) |
| Drive materialization on cache HIT | one `atomic_update_project` per fetch result (`_merge_cloud_assets` → L741) | serial (CAS) | — | — | fold per-turn asset merges into ONE commit per turn (**P3-6**, see §12) |

## 5. LLM Batch Candidates (Canonical Scenario Alignment)

The canonical pipeline (parallel fetch → pack context → **one LLM round** → verdicts+findings → `verify_batch` atomic commit) maps onto today's reality with **one deliberate divergence**: verdicts are produced by the main agent model inside the loop (there is no separate server-side verdict LLM — verify/verify_batch are pure deterministic code, `plugin.py:3732`). So "one LLM call per batch" is achieved **per agent step**, not per HTTP tool call: the batch unit is *how many claims the model adjudicates before it makes the commit call*.

Batch candidates, ranked by observed round-trips (Run 9 EVIDENCE: 1 verify_batch / 7 fetch / 21 total calls; Run 7: 25 verify + 18 mutate / 19 fetch):

| Batch unit | Mechanism | Status |
|---|---|---|
| verdict commit | `verify_batch` ≤ 8 items, explicit `item_id`, per-item reject | **shipped, under-guided** |
| citations/strength patch | verify_batch item carries `citations`+`strength` in the same commit | shipped — Run 9 proves it kills mutate |
| fetch URLs | ≤3 per call, N calls in one step | fan-out = prompt-level (P3-5) |
| whole pending set | deterministic chunking — **below** | missing |

**Token-Aware Chunking (P3-4, plugin/batch.py, pure functions, no LLM):** the orchestrator (code) computes the pending list from `_verify_fps` + fingerprints (already exact, `get_state` L1962-1976) and emits **suggested chunks**: greedily pack pending claims by `len(evidence_context_estimate)` so that per chunk `Σ claims×(claim+findings) ≤ chunk_token_budget(input)` and `items ≤ min(8, remaining)`; e.g. 8 pending with mixed fan-out → 3+3+2. Never a Cartesian 8×13 dump: each claim's chunk carries only its own 1-3 candidate sources. The **model still makes the verdict** — the chunker only decides *what is safe to pack into one round*. ID mapping is already explicit (`item_id` + `claim:{id}`; the frozen loop's reject of missing `item_id` is per-item, `plugin.py:3796-3798`).

**verify_batch partial success (shipped semantics to preserve):** structural errors reject the offending item only; per-URL provenance rejects land in the item's `rejected` list; disk write is all-or-nothing inside the one CAS; all-skipped ⇒ zero write, zero bump (constraints 5/6, `plugin.py:3750-3756`). Item-level retry = re-issue only rejected items in the next call (already expressible; needs prompt guidance in P3-4).

## 6. Result Reuse / Cache Candidates (Keys, Freshness, Commit Consistency)

| Fact class | Store | Key composition | Compliant with "no global revision binding"? |
|---|---|---|---|
| cleaned page body | FetchStore (disk CAS, global) | `sha256(canonical_url ⊕ fetch_policy_fingerprint ⊕ PARSER_VERSION)` — **no project, no revision, no tenant**; content_hash verified on read (self-heal); policy excludes `text_target` so the body, never a view, is cached | **yes (shipped)** |
| committed verdict ledger | `_verify_fps` in driver checkpoint | key = claim node id, value = `evidence_fingerprint` (`source_url⊕kinds⊕verdict╨facts⊕excerpt⊕len⊕status`, order-insensitive) — written **only on successful commit** ⇒ COMPUTED/COMMITTED boundary already enforced | yes — this is the authoritative reuse record |
| page summaries/judgments | — none — (model-side, same context) | n/a | n/a |

Consequences:
- **COMPUTED vs COMMITTED (constraint 8):** already holds — batch verdicts not committed are invisible to `pending`; no design change, add a test (§13).
- **Verdict freshness:** re-evaluation is triggered exactly when `evidence_fingerprint` changes or the gate predicate fails (`compute_pending`, `batch.py:126-134`) — anchored is a citations-field read-check, NOT frozen-forever; the "incremental delta" semantics the order asks for is already the shipped semantics.
- **Rejected — model/prompt-keyed LLM verdict cache:** the compute being cached would be *the main model's in-loop judgment*, which cannot be invoked or replayed outside the turn without a new orchestration engine (frozen-boundary violation) — while `pending=False` already skips the *commit* work for unchanged evidence and the P3-4 digest hint will tell the model to skip the *judgment* work too. A `claim_fingerprint`-based cross-task verdict store is also correctness-hostile (claims are task-relative; verdict quality depends on the task's whole context).
- **Open mismatch:** `claim_fingerprint` (`batch.py:63-74`) is defined + tested but **not used on the production path** (baselines key on claim node id). Either wire it into the pending rule for rename-stability or delete it — decision in §15 (Q3).
- **HIT cost:** a cache hit still pays drive materialization + one `atomic_update_project`/revision bump (×N URLs) — see P3-6 merge-per-turn.

## 7. Run 7 Duplicate Work Mapping → Run 9 Follow-up (fine-grained)

| Duplicate class (Run 7) | n | Run 9 residual | Eliminator | Phase |
|---|---|---|---|---|
| double-anchor (verify→mutate per claim) | ~18 chains | **0** — mutate gone | `verify_batch` citations patch | shipped ✓ |
| cross-turn re-verify of anchored claims | ~27 | fetch/re-read of already-cached pages persists (7 fetch incl. duplicates across steps; old-6.com URL fetched in EVIDENCE *and* re-read in WRITE) | `pending`-driven skip + digest verdict summary (§16) | **P3-4** |
| serial fetch | 19×~13.6 s | 2 same-step fetches observed ⇒ capability exists, guidance missing | prompt: one-step fan-out | **P3-5** |
| schema format retries | 14 | 1 (missing `action`) | P3-3 coercion ✓ | done ✓ |
| repeated reddit 403 retries in-run | (not tallied) | 3 in Run 9 | terminal-for-run signal in tool error + prompt rule | **P3-5b** |
| per-URL asset merge commits | 1 bump/URL | unchanged | merge into 1 commit/turn | **P3-6** |
| LLM round-trip growth (context) | 3.61 M tok | **1.79 M, 86 % wall** | bounded tool-result windows (scrape returns slice per n; findings excerpts), digest-first re-entry | **P3-7** |

## 8. Target Execution Architecture (EVIDENCE, end state)

```
auto-turn start
  ▼
get_state → digest {claims[], each: anchored, evidence_fingerprint,
                    pending, last_verdict_summary, chunk_hint}   ← deterministic (code)
  ▼
pending == []  ──yes──►  skip EVIDENCE work; verify the gate; move on  ← kills the re-verify storm
  │no
  ▼
model takes chunk_hint (e.g. 3 claims + their ≤3 candidate URLs each)
  ▼ one assistant step
  ├─ fetch(A) ┐
  ├─ fetch(B) ├─ gathered in ONE step (safe flag, ≤10)   ← parallel I/O; single-flight + FetchStore
  └─ fetch(C) ┘                                            dedup under flight
  ▼
model judges the whole chunk in THIS context (one round of thinking)
  ▼
verify_batch([{item_id, claim, findings, citations, strength} × ≤8])
  │  one atomic_update_project CAS: graph + _verify_fps + merged assets
  │  ONE revision bump; item-level rejects returned
  ▼
rejected items → next chunk retries ONLY them (never the whole batch)
  ▼
loop over chunk_hints → gate check → transition_stage
```

Commit path stays single-writer (constraint 4): all graph mutations inside the one CAS; network I/O and model reasoning run concurrently *before* it, never during it.

## 9. P3 Implementation Phases (re-sequenced against Run 9)

Run 9 changes the ranking the Run-7-era order implied:

| Phase | Content | Why here |
|---|---|---|
| **P3-4** | pending-driven EVIDENCE orchestration: digest gains `chunk_hint` + per-claim verdict summary; skill/auto_turn_prompt rewritten to wholesale-batch (read `pending`, `verify_batch` first-class, same-step fetch fan-out, item-level retry, 403 terminal rule) | highest ROI, near-zero risk, no new machinery — the mechanisms are already shipped but orphaned; §16 slice |
| **P3-5** | I/O fan-out hardening: `web_search`/`search_social` concurrency flags; fetch cap 3→5 behind body-budget check; in-run dedup of search queries | fetch is 53 % of remaining tool phase (95/179 s) |
| **P3-6** | commit unification: route `record_node`/`mutate_node`/`link_edge`/`invalidate_downstream` through `atomic_update_project` (graph as `extra_files`); batch per-turn asset merges into one commit | closes the real race (§12) + removes N−1 revision bumps/turn; must come after P3-4 so batch-first is the norm when the locks land |
| **P3-7** | LLM-wall control: bounded tool-result windows, digest-first re-entry text, per-stage excerpt budgets (`context_profile` telemetry already records the sizes) | addresses the now-dominant 86 % — largest remaining lever, most open-ended, last |
| **P3-8 (maybe)** | cross-turn `sources.md` becomes a machine-parseable ledger the digest injects (URLs + verdicts already materialized) | only if P3-7 shows re-read pressure in profiles |

## 10. Files to Change

**Editable (research plugin + prompt/skill layer only):**
- `plugins/research/plugin.py` — `get_state` digest extension (P3-4); fetch cap constant `plugin.py:4341-4345` (P3-5); `_merge_cloud_assets` batching + `_save_graph`→CAS migration (P3-6, L2203/2226/2273/2285/3534 callers)
- `plugins/research/batch.py` — pure chunker `suggest_chunks(pending, graph, budget)` (P3-4); optional `claim_fingerprint` decision (Q3)
- `plugins/research/fetch_cache.py` — read path unchanged; optional flight-key dedup for search (P3-5b)
- `plugins/research/workflow_adapter.py` — `auto_turn_prompt` wholesale-EVIDENCE paragraph (L506-521) (P3-4)
- `skills/deep_research.skill.md` — EVIDENCE section rewrite: verify_batch-first, pending-skip, fan-out, item retry (P3-4)
- `apps/api/tools/web_search_tool.py` — `is_concurrency_safe=True` (P3-5; translation layer)
- `plugins/research/monitor.py` — MUTATING_ACTIONS already contains `verify_batch`; asset-merge batching may change `project_revision` cadence (P3-6, no client change: SSE hints are already coalesced)
- tests: `tests/test_research_plugin.py`, `tests/test_research_fetch_cache.py`, new `tests/test_research_chunker.py`

**FROZEN (constraint 6) — verified untouched by every phase above:**
`packages/workflow/*` (all nine modules), `packages/agent/engine/loop.py`, `.../engine/runtime.py`, `packages/agent/tools/definition.py`, `.../tool_gateway.py`, `packages/agent/skills/registry.py`, `packages/agent/security/sandbox.py`. All new pools/batching/caching live in the plugin/adapter/skill layer; the fan-out in P3-5 uses only the existing per-tool flag + gather semantics.

## 11. API / Schema Changes

1. **`get_state` response** (additive, deterministic):
```json
"claims": [{ "id": "...", "label": "...", "anchored": true,
             "evidence_fingerprint": "sha256:…", "pending": false,
             "last_verdict": {"source_count": 2, "mixed": false},   // NEW, summary only
             "chunks": [["c1","c2","c3"], ["c4","c5"]]              // NEW, pending-only, ≤8/chunk, budget-packed
}]
```
   `chunks` computed by `batch.suggest_chunks` from the same fingerprint inputs the pending rule uses — replay-stable, no LLM.
2. **`verify_batch`** — schema unchanged (≤8 items; `item_id` required except the tolerated-patch form observed in Run 9 `p1`/no-id items: **make `item_id` strictly required** so rejects can be cited by id — one-line validation move, error text names the missing id). Response unchanged: per-item `committed | skipped_unchanged | rejected{reason}` + one `project_revision`.
3. **`research_scrape fetch`** — `urls` max 3 → max 5 (P3-5) with body budget invariant `n_urls × max_chars ≤ 5×30k` guard; duplicate-URL-in-batch error message gains the offending URL.
4. **`search_social` error contract** — on 403/rate-limit return `{"terminal_for_run": true, "hint": "do not retry this platform this run"}` (deterministic string the prompt rule keys on).
5. No changes to any tool output *envelope* (shared object schema, P3-3 discipline retained).

## 12. Failure / Concurrency / Consistency Model

- **The existing race to close (P3-6):** `_save_graph` writers hold no project lock and no CAS — today they are saved only by per-tool serialization in the loop; two processes (API + worker) or a future safe-mapping mistake races on `graph.json` (last-writer-wins whole-file loss). Migration: route them through `atomic_update_project(extra_files=["graph.json"])`; readers of `project_revision` (SSE monitor, drive CAS) see *more* bumps, all legal; `record_node` idempotent replay semantics preserved (id-keyed early-return inside the CAS body).
- **Single-writer commit:** every graph mutation completes inside one critical section; a logical batch = one revision transition (verified for `verify_batch`, L3891-3893; extended to asset merges in P3-6: one `_merge_cloud_assets` per turn, or a `pending_assets` buffer flushed by the next commit).
- **Partial failure:** item-level rejects never fail the batch (already); `ExecutionFailed`/transient retry re-mints `execution_id` so the ledger never double-records (workflow core, unchanged); FetchStore/Flight failures degrade to a plain miss, never fail a fetch batch (`fetch_cache.py:292-293`); flight-owner failure broadcasts, no negative caching.
- **Dirty-cache prevention:** COMMITTED-only reuse — `_verify_fps` written inside the same CAS as the graph (or the CAS aborts, so a failed commit cannot leak a baseline); regression test in §13.
- **Cancel/lease interplay:** fetch fan-out and batch commits remain *inside* one agent step of one turn; lease loss still drops (never retried) per §19.5; no new long-running background tasks.

## 13. Test Plan

1. **Chunker (pure):** determinism (same graph+fingerprints ⇒ same chunks), budget respect (Σ estimate ≤ cap, items ≤8), rename-sensitivity vs `claim_fingerprint` decision, empty-pending ⇒ no chunks.
2. **Digest:** `pending=False` claims excluded from chunks; legacy no-baseline ⇒ pending (P3-1 calibration retained: first verify_batch stamps, replays are zero-write zero-bump).
3. **verify_batch strictness:** missing `item_id` now rejected per-item, others still commit.
4. **Concurrent-write regression (P3-6):** two-process test hammering record_node + verify_batch over the same project ⇒ no lost graph mutations (would fail today; passes post-migration), revision monotonic, `_verify_fps` never ahead of graph content (CAS-failure injection).
5. **Dirty-cache drill:** verify_batch commit failure (injected RevisionConflictError) ⇒ `_verify_fps` unchanged, next digest still pending=True.
6. **Fetch fan-out:** N same-step fetches ≤ window; duplicate URL across two parallel calls ⇒ exactly one network hit (single-flight), both get results; 403-terminal error text surfaces unchanged.
7. **Asset-merge batching:** k fetch hits in one turn ⇒ one `_merge_cloud_assets` commit, one bump; SSE client coalescing unaffected (throttle exists, ~300 ms).
8. **Behavioral A/B gate (single-variable discipline per project rule):** one real EVIDENCE-heavy task before/after P3-4; acceptance = verify-family calls ↓, mutate=0 holds, tokens/turn not increased >10 %, cost within $0.20–0.30; Run-9 numbers are the frozen control.

## 14. Theoretical Performance Model

**Observed (Run 9):** wall 1 441 s = LLM 1 238 + tools 179 + gaps ≈24; 124 calls / 8 turns.
**Derived (arithmetic on observed, not measured):**
- P3-4 skip: removes the pending-excluded round-trips — Run 9 still spent EXECUTE-stage verify_batch×4 + reread fetches on already-settled evidence; conservatively −15 tool calls, −2 to −3 LLM steps/turn ⇒ −10 % wall (≈120–150 s), cost −$0.02–0.04.
- P3-5 fan-out: 7 fetch × 13.6 s serial = 94.9 s → ≈ max-per-step ≈ 35–45 s ⇒ −50 s tool phase (−3.5 % wall) — visible only after P3-4 stops over-fetching.
- P3-7 (context): llm 16.1 s/call at ~224 k tok/turn context; halving growth via result-window caps is the only lever that attacks the 86 % term — plausibly −20–30 % wall, marked **Hypothesis** (needs measurement).
**Four-dimension gate (constraint 11):** every phase must show Round-trips ↓, Wall ↓ or flat, **Tokens/cost not materially ↑** (batch packing shares one context across claims — token cost per claim falls; the risk is chunking too wide, bounded by the input-budget rule), Correctness = (gates + provenance discipline unchanged, verified by §13).
All projections flagged Derived/Hypothesis are **theoretical optimization space**, not commitments.

## 15. Risks and Open Questions

- **Q1 — guide drift:** skill.md currently contradicts shipped capability ("one verify per claim", action table omits `verify_batch`). Any P3-4 change must keep skill text, tool-schema descriptions and `auto_turn_prompt` in one reviewable trio; add a parity test asserting the skill mentions `verify_batch`/`pending` (tests exist for `_LEGAL_NEXT` parity — same pattern).
- **Q2 — verdicts live in the main model:** "one LLM call per batch" is step-granular, not call-granular; a future request for true server-side batched judging would need a second LLM channel *inside the plugin* (allowed) — deliberately out of scope (quality, budget, and a second model loop to gate).
- **Q3 — dormant `claim_fingerprint`:** keying pending on node id is rename-fragile and cross-run-blind. Wire it (identity-stable pending) or delete it. Recommendation: wire into digest as `claim_fp` display-only this phase; revisit as key later (migration would one-time flip all baselines ⇒ a re-stamp batch — budget it honestly).
- **Q4 — safe-flag granularity:** the frozen loop is per-tool; `research_evidence` cannot become safe while it mixes `verify_batch` (CAS-ok) with `record/mutate` (unlocked) — P3-6 unifies the writes, only then may a read-only action split be reconsidered (still per-tool: would need a separate small tool, e.g. `research_state`-style — evaluate later).
- **Q5 — fetch cap raise vs upstream politeness:** 3→5 assumes per-site niceness is fine at ≤5 concurrent URLs/step; provider 429 budget unchanged; keep the hard ValueError.
- **R1:** revision-bump cadence changes (P3-6) alter SSE hint density — client coalesces; monitor `MUTATING_ACTIONS` list must include any new CAS path (it already missed things historically; audit in tests).
- **R2:** QUALITY_GATE/scorecard legacy bug auto-settles every progressive run — it will mask P3 behavior changes in E2E reads; keep it excluded from P3 acceptance (§2b note), but do not confuse "run reached PUBLISH" with "gates passed".
- **R3:** single-run evidence: numbers are n=1 per run (project iron rule: no re-runs without an approved proposal); all "improvements" in §14 are modeled from the two existing traces, not averaged.

## 16. Recommended First Implementation (Vertical Slice)

**"pending-driven batch EVIDENCE" — P3-4. Zero generic-engine change, zero new stores, zero schema migration.**

End-to-end closed loop:
1. **Input** — `get_state` digest adds per-claim `last_verdict` summary + deterministic `chunks` (new pure `batch.suggest_chunks`; ≤8/chunk, budget-packed, pending-only).
2. **Deterministic decision** — model instruction (skill + `auto_turn_prompt`, rewritten together): read `pending`; **skip everything when `pending` is empty**; else process exactly one suggested chunk per commit.
3. **Compute/skip** — fetch fan-out for the chunk's candidate URLs in one step (capability already exists — Run 9 proved same-step gather works); judge the chunk in-context; **`verify_batch` is the only commit verb**; rejected item-ids retried alone.
4. **Commit** — existing `verify_batch` CAS (graph + citations/strength + `_verify_fps` + one bump) — unchanged.
5. **Audit** — `tool-call-detail` already timestamps every leg; add nothing new; digest verdict summaries make the replay basis inspectable.
6. **Failure handling** — item rejects (existing), flight/cache degrade-to-miss (existing), cancel/lease semantics untouched.

Why this is the minimal complete slice: it attacks the largest residual observed class (cross-turn re-work + serial retail judging) using **only guidance text + one pure function on top of mechanisms already merged and tested**; it is verifiable against the Run 9 control numbers with a single E2E A/B (§13.8); and its failure mode is bounded — worst case the model ignores the guidance and behavior equals today's, so it can ship independently of P3-5/6/7.

**STOP — design delivered; awaiting human review. No code modified.**
