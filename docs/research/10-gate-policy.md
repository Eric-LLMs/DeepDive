# 10 — Gate Policy

> **Normative.** Four hard gates: **DESIGN · EVIDENCE · CLAIM · QUALITY**. A gate is a
> pairing of a **mechanical checker** (deterministic) and an **LLM reviewer** (subjective),
> and its verdict is recorded on a `ResearchGate` row.

## 1. Verdicts

```text
PASS       all required checks green
FAIL       at least one blocking check red — stage returns to a legal predecessor
OVERRIDE   human-approved override only (ResearchApproval, never agent self-approval)
```

An `OVERRIDE` is recorded with its `override_approval_id` and a reason; it never silences
the failing checks — the failure stays visible in the gate record.

## 2. Gate → stage placement

| Gate | Guarded transition | Checks (mechanical) | Judgment (LLM) |
|---|---|---|---|
| **DESIGN_GATE** | DESIGN → EXECUTE (design lock) | design_register exists; estimand explicit; identification documented; sample rules stated; risk_ledger non-empty; profile requires this design | method defensible for the question; assumptions credible; no researcher-driven specification |
| **EVIDENCE_GATE** | EXECUTE → EXPLAIN | **Core** (all profiles): sources verified (`verification_status != unverified` used as fact); evidence items exist and link to a verified source; no `INVALID` upstream; claim-to-evidence draft links present. **Empirical-only** (profile-gated, see §3.1): sample_audit complete; diagnostics present; robustness present | results actually support the interpretation; for `empirical` profiles: robustness failures not hidden; claim strength appropriate |
| **CLAIM_GATE** | WRITE → REVIEW | every manuscript number anchors to a ledger row; no unregistered citation; every claim ≤ `allowed_strength`; no placeholder text | wording restrained; no association→causal upgrade; no overgeneralized subgroup |
| **QUALITY_GATE** | REVIEW → REPRODUCE/PUBLISH | scorecard present (7 dims × 10); no fatal flag; revision-round cap not exceeded | each dimension ≥ 7; total ≥ 80/100; release decision |

> **Gate applicability is conditional on the activated stage.** A gate fires only when it
> guards a stage the selected profile actually materializes. If a profile does not activate
> a guarded stage (e.g., `literature × memo` skips formal `EXECUTE` and `REVIEW`), the
> corresponding gate is **not required** (see `09` §3).

## 3. Mechanical checks are authoritative

The mechanical half must be deterministic and testable:

1. **Artifact-exists**: each required artifact row exists with a valid status (e.g.
   `PROMOTED`/`FROZEN` for drive-backed, `VALIDATED`/`PROMOTED`/`FROZEN` for scratch).
2. **State-consistent**: stage statuses form a legal prefix of the DAG; no stage
   `COMPLETED` with unsatisfied deps.
3. **Number-anchoring** (CLAIM_GATE): extract numbers from the manuscript, match each to an
   `Evidence` node's `result_file`/`estimate` (tolerance/rounding policy configurable),
   fail on orphans.
4. **Citation-registry** (CLAIM_GATE): every inline citation resolves to a `Source` node
   with `verification_status != unverified`; fail on dangling citations.
5. **Placeholder-scan**: reject `TODO`, `[待补充]`, `lorem`, empty tables, etc.
6. **Gate-dependency**: the gate's required upstream gates are `PASS`/`OVERRIDE`.

### 3.1 EVIDENCE_GATE: core vs profile-specific checks

EVIDENCE_GATE checks are split into two tiers. **Core Checks** are mandatory for **every**
profile, including the Literature MVP. **Profile-Specific Empirical Checks** activate only
when the project's Method Profile actually runs an empirical stage.

**Core Checks** (mandatory for all profiles):

1. **Source existence & verification**: every source used as fact has a `research_sources`
   row with `source_type` set and `verification_status != unverified` (`05` §4).
2. **Evidence linkage**: at least one `Evidence` node exists and is linked to a verified
   source via a `supports`/`derived_from` edge.
3. **Upstream integrity**: no upstream node feeding the evidence is `INVALID`.
4. **Claim-to-evidence draft links**: every draft claim destined for the manuscript has a
   `supports` link from an `Evidence` node (or a documented, audited gap).

**Profile-Specific Empirical Checks** (only when the `empirical` Method Profile runs — see
`09` §3/§4):

1. **Sample audit completeness**: `sample_audit.md` present, with inclusions/exclusions
   recorded.
2. **Diagnostics & specification checks**: model diagnostics and specification checks
   documented.
3. **Robustness checks present**: robustness / placebo / permutation checks recorded.

**Result**: in the **Literature Review MVP** (`literature` profile, no empirical stage), the
EVIDENCE_GATE **passes when the Core Checks are satisfied**, without requiring a sample
audit, regression diagnostics, or robustness files.

## 4. Evidence links

Each gate check records `evidence_links` — the graph node ids it inspected. A gate verdict
without evidence links is invalid (a gate is not an opinion, it is a verdict over evidence).

## 5. No-auto-modify rule

A gate that FAILs **does not modify anything**. The agent (or skill) receives:

```text
gate: DESIGN_GATE
status: FAIL
checks:
  - id: estimand_explicit     red
  - id: identification        red
explain_failure: "estimand not explicit; identification section empty"
suggested: "run DESIGN again producing design_register.md v2"
```

Re-execution is a human-orchestrated decision, never an automatic loop:

- On high-risk items (research_question, design, sample, estimator, claim_strength,
  data_source, publication), the project goes `BLOCKED` and raises a `ResearchApproval`
  request before any re-execution.
- On mechanical-only gaps (missing artifact, placeholder), the agent may fix the gap and
  re-run the gate, but the revision must be recorded as a new artifact version.
- Revision-round caps (from `budget_policy`) stop auto loops.

## 6. Gate service interface (`research_gate`)

```text
check(gate_name)          → PASS | FAIL + structured check report (writes ResearchGate row)
explain_failure(gate_name)→ human-readable explanation of each red check
request_override(gate_name, reason) → creates PENDING ResearchApproval; NEVER sets OVERRIDE
```

Only the approval resolution path (human) may turn an approved request into `OVERRIDE`.

## 7. Failure → stage mapping

| Gate FAIL | Stage returns to |
|---|---|
| DESIGN_GATE | DESIGN (`READY`) |
| EVIDENCE_GATE | EVIDENCE or DESIGN (depending on what's missing) |
| CLAIM_GATE | WRITE or EXECUTE (depending on the orphan source) |
| QUALITY_GATE | REVIEW or WRITE (weakest dimension determines target) |

Returning never marks the target stage `COMPLETED`; it becomes `READY` to re-run.
