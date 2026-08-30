# 08 — Workflow Templates

> **Normative.** This document fixes the *shape* every stage, artifact, skill, and handoff
> must conform to. Content specifics belong to the skills; structure belongs here.

## 1. Stage template

Every research stage is a DAG node with this contract:

```yaml
name:                 DISCOVER | FRAME | EVIDENCE | DESIGN | EXECUTE | EXPLAIN |
                      WRITE | REVIEW | REPRODUCE | PUBLISH
activated_by_profile: bool
dependencies:         string[]          # stage names
skill:                string            # the skills/*.skill.md that implements it
required_inputs:      string[]          # artifact_types
produced_artifacts:   string[]          # artifact_types written
gate_on_exit:         string|null       # DESIGN_GATE | EVIDENCE_GATE | CLAIM_GATE | QUALITY_GATE
entry_anywhere_ok:    bool              # can the user start here given upstream artifacts
```

## 2. Skill contract (skills/research_*.skill.md)

Skills use the existing frontmatter format (`packages/agent/frontmatter.py`:
`---\nkey: value\n---`):

```yaml
---
name: research_<stage>        # e.g. research_literature, research_evidence
description: >-
  One-paragraph purpose. "Use when ..." trigger conditions.
keywords: research, literature, ...
allowed_tools: web_search, search_social, rag_search, research_project,
               research_artifact, research_state, research_evidence,
               research_gate, research_run, read_file, write_file
---
```

Body sections (mandatory, in order):

1. **When to use** — trigger; the router skill dispatches here only when inputs are present.
2. **Inputs** — which artifacts must already exist (verify, don't trust).
3. **Procedure** — the steps; each step names the tool actions it uses.
4. **Outputs** — artifact types to write and where (scratch/drive).
5. **Gate before advancing** — the exit gate name and the evidence bundle it requires.
6. **Handoff** — the ≤10-line summary to leave for the next stage.

Skills are **capability skills**: method logic is decoupled from stages (e.g. a DID-check
skill is usable from DESIGN and from REVIEW; a literature-matrix skill is usable from
EVIDENCE and from WRITE). The router skill (`research_router`) performs intake + profile
selection + dispatch only.

## 3. Artifact template

```yaml
artifact_type:        corpus | sources | literature_matrix | references | research_question |
                      design_register | estimand | identification | risk_ledger | dataset |
                      codebook | sample_audit | analysis | result | diagnostics | robustness |
                      evidence | claim | exhibit | manuscript | referee_report | revision_plan |
                      replication | report | final_report | handoff
status:               DRAFT | VALIDATED | PROMOTED | FROZEN | SUPERSEDED | INVALID
producer:             the ResearchExecution that wrote it
```

Each artifact type that participates in claims must also create/update the corresponding
graph node (see `05`).

## 4. Handoff template (written at stage exit)

```yaml
current_stage:   string
status:          string
completed_artifacts: string[]   # artifact ids
key_decisions:   string[]       # decision node ids + one-line reasons
key_numbers:     string[]       # headline figures with artifact refs
open_risks:      string[]       # risk node ids
failed_checks:   string[]       # gate failures + reasons
next_action:     string
```

The next stage must verify artifacts exist before trusting the handoff (handoff is a
pointer, not evidence).

## 5. Evidence ledger / claim registry (readable projections)

These are generated markdown projections of the graph, regenerated on graph writes:

- `evidence_ledger.md` — one row per `Evidence` node: `claim_id, claim, claim_type,
  estimand, sample, estimate, se, result_file, robustness, exhibit, script, citation,
  allowed_strength, status`.
- `claim_registry.md` — one row per `Claim` node: `claim, claim_type, allowed_strength,
  supporting_evidence_ids, status`.
- `decision_log.md` — one row per `Decision` node: `decision_id, question, considered,
  rejected, selected, reason`.

Regeneration must be deterministic (a projection function of the graph), so the markdown
never diverges from the database.

## 6. Projection rules

1. Markdown projections are read-only for agents: agents read them; they write via the
   graph/tools, never by editing the markdown directly.
2. Any graph mutation invalidates and regenerates the affected projections.
3. A projection regenerated from the graph is the only acceptable "ledger" content.
