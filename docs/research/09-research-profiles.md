# 09 — Research Profiles

> **Normative.** Profile is two-dimensional: **Method Profile × Output Profile**.
> "How we research" and "what we deliver" are orthogonal.

## 1. Dimensions

**Method Profile** (how):

```text
literature    — survey/synthesize existing knowledge
empirical     — estimate effects from data, identification discipline
theoretical   — formal/model reasoning
qualitative   — text/interview/case analysis
mixed         — several of the above
```

**Output Profile** (what):

```text
memo                — short scoped write-up
literature_review   — survey + synthesis matrix, verified citations
proposal            — research plan (question/design/data plan)
research_report     — structured findings report
paper               — full manuscript, claim-governed, reproducible
replication_report  — reproduction/validation of prior work
```

## 2. Profile → stage activation matrix

| Method \\ Output | memo | literature_review | proposal | research_report | paper | replication_report |
|---|---|---|---|---|---|---|
| **literature** | D F E W | D F E W RV | D F E Dg | D F E X Epl W RV | D F E Dg X Epl W RV Rp | D F E X Rp |
| **empirical** | D F E Dg X Epl W | D F E Dg X Epl W RV | D F E Dg | D F E Dg X Epl W RV | **D F E Dg X Epl W RV Rp** | D F E Dg X Rp |
| **theoretical** | D F E W | D F E W RV | D F E Dg | D F E W RV | D F E Dg W RV Rp | D F E Dg Rp |
| **qualitative** | D F E W | D F E W RV | D F E Dg | D F E X Epl W RV | D F E Dg X Epl W RV Rp | D F E X Rp |
| **mixed** | D F E X Epl W | D F E X Epl W RV | D F E Dg | D F E X Epl W RV | D F E Dg X Epl W RV Rp | D F E X Rp |

Legend:

```text
D  = DISCOVER     F  = FRAME     E  = EVIDENCE   Dg = DESIGN
X  = EXECUTE      Epl = EXPLAIN  W  = WRITE      RV = REVIEW
Rp = REPRODUCE    P  = PUBLISH   (P is always the terminal stage)
```

Empty cells mean the corresponding stages are `SKIPPED` for that profile.

## 3. Required gates per profile

| Profile family | Gates required |
|---|---|
| `literature` / `theoretical` / `qualitative` (non-paper) | EVIDENCE_GATE (when EXECUTE runs), QUALITY_GATE (when REVIEW runs) |
| `literature` × `paper` | DESIGN_GATE, EVIDENCE_GATE, CLAIM_GATE, QUALITY_GATE |
| `empirical` (all outputs except memo) | DESIGN_GATE, EVIDENCE_GATE, CLAIM_GATE, QUALITY_GATE |
| `mixed` × `memo`/`literature_review` | EVIDENCE_GATE |
| `replication_report` | EVIDENCE_GATE, QUALITY_GATE |

`research_gate` only evaluates gates for materialized stages; a gate on a skipped stage is
not run.

> **Gate applicability is conditional on the activated stage in the selected profile.** If a
> profile does not activate a guarded stage (e.g., `literature × memo`, which skips formal
> `EXECUTE` and `REVIEW`), the corresponding gate is **not required**. A gate fires only for
> a stage the profile materializes.

## 4. Allowed execution mode

| Profile | `allowed_execution` |
|---|---|
| `empirical`, `mixed` | `sandbox_allowed` (Python via `bash` sandbox, when configured) |
| `literature`, `theoretical`, `qualitative` | `reasoning_only` (sandbox calls rejected) |
| any × `memo` | `reasoning_only` |

`research_run execute_sandbox_script` checks `allowed_execution` and rejects
`sandbox_allowed == false` with a structured error.

## 5. Profile invariants

1. A profile is fixed at project create and is itself a high-risk mutation to change
   (requires approval).
2. FRAME is never skipped: every profile runs DISCOVER → FRAME.
3. PUBLISH is always the terminal stage; every profile ends at PUBLISH.
4. `research_question` is required before any `DESIGN`/`EXECUTE` stage.
5. The profile matrix above is the normative source for `activated_stages` /
   `required_gates`; the implementation materializes stage rows from it.

## 6. MVP profile set (Phase 1)

Only these are implemented in the MVP (see `17-mvp-scope.md`):

| Method | Output |
|---|---|
| `literature` | `memo`, `literature_review`, `research_report` |
| `mixed` | `memo`, `literature_review`, `research_report` |

All other profile cells are out of MVP scope (stages/gates designed for, not built).
