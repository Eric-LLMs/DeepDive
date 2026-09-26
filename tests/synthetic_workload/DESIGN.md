# Synthetic User Workload + Cascade Shadow Evaluation — Design

Status: **offline evaluation asset only**. Nothing here touches the production
path: no dispatch, no Runtime, no config change (`chat_funnel_min_score=0.82`
untouched). The dataset is a permanent, versioned regression asset ("standard
exam paper"), not disposable test data.

## Why synthetic

Delveta has no historical real user queries. The workload is explicitly
`workload_design` / `synthetic_assumption` — proportions here are DESIGN
choices and must never be quoted as real user distribution.

## Evaluation chain (Shadow only)

```
Synthetic Query (per turn)
  → Matcher → Recall (raw scores, min_score=0 top_k=10)
  → ToolIntentModel → Adapter → Candidate Gate → Binder
  → STOP            (would_execute is a FLAG, never an action)
```

Phase D ships dataset + generator + stats only. Cascade Shadow runner is
Phase E+ (built on `funnel._run_nodes`, execution_mode="shadow"; Matcher-only
shadow stays as-is per constraint #7).

## Phase-A recon facts that shape the design (code-verified)

1. **The funnel never sees conversation history.** ToolIntentModel input
   discipline (`tool_intent/base.py:5-9`): query + TurnFacts + candidate cards,
   "No tools list, no skills, no conversation history". So per-turn funnel
   evaluation is well-defined: each turn is scored on (query, viewer facts).
   History influences the funnel ONLY through upstream-resolved `TurnFacts`.
2. **TurnFacts is the only session/viewer channel** (`contract.py:24-49`):
   has_viewer / viewer_asset_id / viewer_current_page / has_viewer_selection /
   has_attachment / has_turn_context. Dataset `viewer_context` maps onto these.
3. **The funnel does not guess from history** (golden case
   `sequence_followup_not_guessed`): anaphoric turns like `名字改成 papers`
   are EXPECTED-AGENT at current capability baseline — labeling them TOOL would
   measure a resolver that doesn't exist. Anaphora-heavy turns carry
   `notes: future_anaphora` so a later anaphora feature can flip the label via
   a dataset version bump (never silent edit, §22).
4. **Shadow can never trigger Runtime by construction**: the cascade produces
   routing metadata only (8.8); execution authority lives in the plan-stage
   ActionExecutor, which the shadow harness simply never invokes.
   `would_execute := (_run_nodes returned a certified TurnRequirements)` —
   recorded, discarded.
5. **Published capability universe (3)**: `cap-create-folder{name}`,
   `cap-add-term{term,domain[,definition]}`, `cap-pdf-extract-text{asset_id}`
   (asset_id resolved from viewer/attachment facts, bench `hit-pdf-*`).
   Every TOOL label uses one of these three — no phantom caps.

## Trajectory answers (user supplement, §11)

1. *Session state*: TurnFacts only (§A-1/2). The harness builds a ctx stub per
   turn exactly like `funnel._preview_ctx` does.
2. *Assistant response → next turn*: stays OUTSIDE the funnel; the dataset
   keeps canned assistant texts for realism/replay, but the shadow feed to the
   funnel is (user_query, viewer facts) — same as production.
3. *Viewer state*: dataset `viewer_context{kind,asset_id,page,selection}` →
   TurnFacts fields per turn.
4. *Virtual Shadow State*: per-session `virtual_state`, mutated ONLY by
   `expected_state_delta` of certified turns (folder names, added terms,
   extracted assets). Real workspace never touched. Referenced assets in later
   turns use stable virtual ids (`vfolder-research`, `ast-paper-nlp`).
5. *No Runtime*: §A-4. Harness has no executor, no session_factory write path.
6. *Storage*: `session_trajectories.jsonl` keeps the WHOLE session (turns are
   never flattened into unrelated singletons); each turn also mirrors into
   per-turn stats via stable `case_id = <session_id>-t<N>`.
7. *A/B replay*: identical dataset version + per-run `runs/<run_id>/…` output;
   comparison keyed by case_id (L1) and session_id+turn_id (L3).
8. *First divergence*: per-run record stores per-turn actual; the comparator
   reports `first_divergence_turn` and classifies it:
   intent | state | tool-result | context.

`expected_funnel` vocabulary: `TOOL` (chain certifies), `AGENT` (fail-open at
any stage), `AMBIGUOUS` (multi-intent: any single certification is wrong),
`ABSTAIN` (entry gate must veto — research/handoff/memory-demand lanes never
enter the cascade; distinguished from AGENT so stats don't blame the cascade
for a gate decision).

## Dataset layout (permanent DATA)

```
tests/synthetic_workload/
├── DESIGN.md  schema.json  generator.py  test_dataset.py
├── scenarios/*.yaml            # curated seeds (the authoring surface)
└── datasets/v1-pilot/          # generated artifacts — committed DATA
    ├── manifest.json           # version, counts, per-file sha256
    ├── query_cases.jsonl       # L1 superset (all single-turn cases)
    ├── tool_positive.jsonl     # L1 expected TOOL
    ├── hard_negative.jsonl     # L1 HN_* intent class
    ├── natural_workload.jsonl  # balanced_workload (designed mixture)
    ├── stress_workload.jsonl   # HN/ambiguous/drift/multiturn-heavy mixture
    └── session_trajectories.jsonl   # L2/L3 whole sessions
```

`data/` is gitignored (runtime caches) — hence DATA lives under tests/,
versionable and regression-importable. Generator is deterministic (no RNG,
no timestamps): same scenario YAML ⇒ byte-identical JSONL (verified by test).
Versioning discipline (§22): cases are never silently edited; new/changed
labels bump the dataset version and the manifest changelog.

## Metric plan (for Phase G/H, recorded here for schema completeness)

Per threshold bucket {0.55,0.58,0.60,0.62,0.65,0.70,0.75,0.82}, recomputed
offline from RAW recall scores (never re-embedded): TP/FP/FN/TN, Precision,
Recall, F1, FPR, fallback + abstain rates; sliced by scenario / intent /
viewer_relation / difficulty / capability / funnel-stage; FP taxonomy
① recall-FP ② model-blocked ③ binder-rejected ④ would_execute risk.
Synthetic labels ARE ground truth here (unlike live shadow where gold is
unknown) — that is precisely why this workload exists before real traffic.
