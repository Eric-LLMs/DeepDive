# 07 — Workflow State Machine

> **Normative.** Supersedes `02-domain-model.md` §2.5 where more specific.

## 1. Stages form a DAG, not a linear list

```text
DISCOVER → FRAME → EVIDENCE → DESIGN ─[DESIGN GATE]→ EXECUTE ─[EVIDENCE GATE]→ EXPLAIN
→ WRITE ─[CLAIM GATE]→ REVIEW ─[QUALITY GATE]→ REPRODUCE → PUBLISH
```

- Profiles activate a subset of stages (`activated_by_profile`).
- Stages may be `SKIPPED` when the profile does not require them and no upstream dependency
  is unsatisfied.
- Users may **enter at any stage** with sufficient upstream artifacts (e.g. "here is a clean
  dataset" → EVIDENCE/DESIGN; "here is a draft" → REVIEW). Entry never skips the required
  gates for the entered point.

## 2. Stage status (normative)

| Status | Meaning |
|---|---|
| `PENDING` | Not yet eligible; an upstream dependency is not satisfied |
| `READY` | Dependencies satisfied; may start |
| `IN_PROGRESS` | Work is being executed (execution row `RUNNING`) |
| `BLOCKED` | Awaiting a gate, an approval, a dependency, or budget resolution |
| `COMPLETED` | All outputs produced and required gates passed for this stage |
| `SKIPPED` | Not required by the profile / user decision |

## 3. Stage definitions (inputs → outputs → gate → handoff)

| Stage | Inputs required | Outputs (artifacts) | Gate before advance |
|---|---|---|---|
| **DISCOVER** | intent, profile | `corpus.md` (candidate sources), `discovery_log.md` | none |
| **FRAME** | intent, corpus | `research_question.md`, `scope.md` | none (question change = approval) |
| **EVIDENCE** | research_question | `sources.md` (verified), `literature_matrix.md`, `references.bib` | none |
| **DESIGN** | question, sources | `design_register.md`, `estimand.md`, `identification.md`, `risk_ledger.md` | **DESIGN GATE** (lock) |
| **EXECUTE** | design, dataset, sources | `analysis/*`, `results/*`, `diagnostics/*`, `robustness/*` | — |
| **EXPLAIN** | results | `mechanisms.md`, `heterogeneity.md`, `bounds.md` | — |
| **WRITE** | results, exhibits | `manuscript/draft.md`, `exhibits/*` | — |
| **REVIEW** | draft, evidence | `referee_report.md`, `revision_plan.md` | — |
| **REPRODUCE** | everything | `replication/README.md`, `run_all.sh`, `environment.*`, `data_availability.md` | — |
| **PUBLISH** | final report | `FINAL_REPORT.md` → drive | — |

Gates are evaluated by `research_gate` **at the boundary**: DESIGN GATE after DESIGN,
EVIDENCE GATE after EXECUTE, CLAIM GATE after WRITE, QUALITY GATE after REVIEW.

## 4. Legal transitions (the `transition_stage` contract)

`transition_stage(stage_id, target_status)` is **not an arbitrary mutation**. An agent may
only *request* a transition; the state machine evaluates:

```text
1. Dependency Check — all stage.dependencies are COMPLETED or SKIPPED;
   the source stage is READY/IN_PROGRESS and target is a legal successor in the DAG.
2. Gate Check — the gate(s) guarding this transition are PASS (or OVERRIDE with an
   approved ResearchApproval).
3. Approval Check — if the transition implies a high-risk mutation (research_question,
   design, sample, estimator, claim_strength, data_source, publication), a PENDING/APPROVED
   ResearchApproval must exist.
```

Outcome:

- all three pass → apply the transition, update `workflow_state` projection, write a handoff
  artifact for the stage;
- any fails → **reject** with a structured reason (`gate_fail`, `dependency_fail`,
  `approval_pending`); the stage becomes `BLOCKED` when the blocker is a gate/approval on a
  high-risk item.

The legal transition table (source → target):

```text
DISCOVER → FRAME → EVIDENCE → DESIGN → EXECUTE → EXPLAIN → WRITE → REVIEW → REPRODUCE → PUBLISH
DISCOVER → EVIDENCE   (profile skip of FRAME is not allowed; FRAME is always required)
EVIDENCE → EXECUTE    (when DESIGN is SKIPPED by profile)
any → BLOCKED         (gate/approval/dependency wait)
BLOCKED → READY       (on gate re-run PASS or approval)
any → COMPLETED       (only via the stage's own exit transition)
```

## 5. Handoffs

At every stage boundary the service writes a handoff artifact:

```yaml
current_stage:     string
status:            string
completed_artifacts: string[]
key_decisions:     string[]
key_numbers:       string[]
open_risks:        string[]
failed_checks:     string[]
next_action:       string
```

A handoff is **a pointer, not evidence**: the next stage must verify the actual artifacts
before trusting it.

## 6. Entry anywhere

`research_state open/resume` rehydrates stages from the DB. If a stage's inputs already
exist as verified artifacts, the stage is marked `READY` and the pipeline may begin there.
Entering mid-pipeline never marks upstream gates as passed retroactively.

## 7. Profile skips

`activated_by_profile` decides whether a stage is even materialized. Skipping rules:

- A `literature`/`mixed` profile for `literature_review`/`memo` skips `DESIGN`, `EXPLAIN`,
  and `REPRODUCE`; `EXECUTE` runs as **synthesis** (no sandbox).
- An `empirical` profile requires `DESIGN` + `EXECUTE` (sandbox allowed) + `REPRODUCE`.
- `research_gate` only ever evaluates gates for stages that were materialized and reached
  their boundary.
