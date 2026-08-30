# 12 — Provenance Model

> **Normative.** Provenance is the property that every research fact can be traced:
> Claim → Evidence → Result → Analysis/Execution → Dataset/Script → Source → Raw data.

## 1. Provenance chain

```text
Raw source (Source node, verification_status)
   → Dataset (transformed_by) → Execution (generated_by) → Analysis (produces) → Result
   → Evidence (supports) → Claim (appears_in) → Paragraph in manuscript
```

Each hop is either a graph edge or an artifact lineage field. A claim with any missing hop
is `STALE`/unverified, not `VALID`.

## 2. Mechanisms that guarantee provenance

1. **Checksums** — every artifact carries a SHA-256; promotion verifies bytes against the
   record.
2. **Versioning** — `(artifact_id, version)` immutable; changes always create new versions
   with `parent_version`; `FROZEN` bytes are read-only.
3. **Executions** — every artifact has a producing `ResearchExecution`; every execution is
   an immutable audit record with inputs/outputs/tools/environment/cost.
4. **Graph edges** — lineage is queryable (`query_lineage`) and supports impact analysis.
5. **Readable projections** — `evidence_ledger.md` / `claim_registry.md` / `decision_log.md`
   are deterministic projections of the graph, regenerated on writes.

## 3. Number anchoring

A number in the manuscript is anchored when it resolves to an `Evidence` node's
`estimate` + `result_file` (with a configurable rounding tolerance). The CLAIM_GATE
mechanical check enforces this; an unanchored number blocks the gate.

## 4. Citation integrity

- Every citation resolves to a `Source` node.
- `verification_status` must be at least `content_verified` before the citation may support
  a factual claim.
- Citations found to be fabricated or wrong are marked `INVALID`; all dependent claims and
  paragraphs cascade to `STALE`/`INVALID` via `invalidate_downstream`.

## 5. Invalidation semantics (STALE vs INVALID)

| State | Condition | Recovery |
|---|---|---|
| `VALID` | consistent with all upstream evidence | — |
| `STALE` | upstream changed; epistemic uncertainty | re-run the owning stage's gate |
| `INVALID` | method refuted / evidence overturned (recorded reason) | new evidence or human decision |

- Editing an upstream node → downstream `STALE` (default), not `INVALID`.
- `INVALID` requires an explicit cause (a `Decision`/`Risk` node with an `invalidates` edge
  or a gate FAIL with a recorded reason).
- Impact analysis returns the full `STALE`/`INVALID` scope before a change; the approval's
  `risk_assessment` is computed from it.

## 6. Audit trail

- Every graph mutation, version creation, gate verdict, and approval resolution appends an
  audit row (who, when, project, node/artifact, reason). This is separate from the
  `workspace_activity` drive audit and from the agent turn audit (`data/audit.jsonl`); it is
  the research lineage audit.

## 7. Reproducibility (REPRODUCE stage)

A project is reproducible when, for every headline result:

```text
data source recorded and verified       → data_availability.md
dataset construction described           → codebook.md / sample_audit.md
execution environment pinned             → environment.* (image, versions)
analysis script or reasoning record kept → 04_analysis/code + result files
run instructions exist                    → run_all.sh / README.md
```

`REPRODUCE` produces `08_replication/` (README, run_all.sh, environment, data_availability).
A project is `COMPLETED` only when PUBLISH promotes these; the manuscript alone never
completes a project.

## 8. Invariants

1. No unverified number or citation enters the manuscript (CLAIM_GATE).
2. No `INVALID` node is cited as `VALID` by a later stage.
3. Deletion of a drive asset marks the artifact `INVALID`/`SUPERSEDED` (never silent
   disappearance of the record).
4. Restoring `VALID` always requires a verified re-check (a gate re-run or new evidence).
