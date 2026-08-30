# Contract Matrix — Research OS

> **Normative.** Single cross-reference for entities × tools × gates × documents. A gap
> here is a contract gap. File count: 20 Markdown/contract files + 3 Mermaid diagrams = 23.

## 1. Document list (23 files)

| # | File | Role |
|---|---|---|
| 1 | `00-architecture-overview.md` | orientation, philosophy, success criterion |
| 2 | `01-product-spec.md` | mission, personas, outputs, MVP |
| 3 | `02-domain-model.md` | 8 entities + DB mapping + invariants |
| 4 | `03-research-project.md` | ResearchProject contract |
| 5 | `04-research-artifact.md` | ResearchArtifact contract |
| 6 | `05-research-graph.md` | ResearchGraph contract |
| 7 | `06-research-execution.md` | ResearchExecution contract |
| 8 | `07-workflow-state-machine.md` | 10-stage DAG + legal transitions |
| 9 | `08-workflow-templates.md` | stage/artifact/skill/handoff templates |
| 10 | `09-research-profiles.md` | Method × Output matrix |
| 11 | `10-gate-policy.md` | four gates, mechanical checks, no-auto-modify |
| 12 | `11-approval-policy.md` | human-in-the-loop, no self-approval |
| 13 | `12-provenance-model.md` | lineage, anchoring, invalidation |
| 14 | `13-storage-model.md` | three-layer one-way storage |
| 15 | `14-research-context.md` | RAG context layer + router |
| 16 | `15-cordis-plugin-contract.md` | the 6 tools + ResearchService |
| 17 | `16-permission-model.md` | permissions, sandbox, tenancy |
| 18 | `17-mvp-scope.md` | MVP loop + explicit out-of-scope |
| 19 | `contract-matrix.md` | this file |
| 20 | `phase-0-review.md` | self-review + open questions |
| 21 | `diagrams/c4-container.mmd` | container diagram |
| 22 | `diagrams/workflow-state.mmd` | state machine diagram |
| 23 | `diagrams/research-graph.mmd` | graph model diagram |

## 2. Entity × document

| Entity | Defined in | Normative superseded by |
|---|---|---|
| ResearchProject | `02` §2.1, `03` | `03` |
| ResearchArtifact | `02` §2.2, `04` | `04` |
| ResearchGraph | `02` §2.3, `05` | `05` |
| ResearchExecution | `02` §2.4, `06` | `06` |
| ResearchStage | `02` §2.5, `07` | `07` |
| ResearchGate | `02` §2.6, `10` | `10` |
| ResearchProfile | `02` §2.7, `09` | `09` |
| ResearchApproval | `02` §2.8, `11` | `11` |

## 3. Entity × tool × gate

| Tool | Entities it operates on | Gates it serves |
|---|---|---|
| `research_project` | Project, Inbox | — |
| `research_artifact` | Artifact, (Graph nodes via support) | promote validates PROMOTED status |
| `research_state` | Stage | requires gate PASS before transition |
| `research_evidence` | Graph, Source, Projections | feeds all gates' evidence_links |
| `research_gate` | Gate, Approval | DESIGN / EVIDENCE / CLAIM / QUALITY |
| `research_run` | Execution, Artifact | sandbox blocked unless profile allows |

## 4. Gate → checks → document

| Gate | Mechanical checks | Document |
|---|---|---|
| DESIGN_GATE | register/estimand/identification/risk non-empty | `10` §2–3 |
| EVIDENCE_GATE | Core (all profiles): sources verified, evidence links, no INVALID upstream, claim-draft links; empirical-only (profile-gated): sample audit, diagnostics, robustness | `10` §2–3, §3.1 |
| CLAIM_GATE | number anchoring, citations registered, allowed_strength | `10` §2–3, `12` §3 |
| QUALITY_GATE | scorecard 7×10, no fatal, revision cap | `10` §2–3 |

## 5. Storage flow → document

| Flow | Contract |
|---|---|
| scratch → drive (promote_to_drive, save_artifact) | `04`, `13` |
| drive → RAG (asset_ingest) | `13`, `14` |
| drive delete → index removal | `13` §4, `14` §5 |
| graph → readable projections | `08` §5–6 |

## 6. Frozen-correction checklist (must all be present)

- [ ] Normative-contract banner in every doc (§ governing principle) — see `00` §banner.
- [ ] 23-file count — confirmed in §1 above and `00` §7.
- [ ] `Execution` is a graph node type — `02` §2.3, `05` §2 (canonical chain Dataset →
      Execution → Analysis → Result).
- [ ] Extended edge set (derived_from, generated_by, depends_on, cites, tests added) —
      `02` §2.3, `05` §3.
- [ ] `transition_stage` is legal-transition-only (Dependency → Gate → Approval) — `02`
      §5, `07` §4, `15` §2.3.
- [ ] No-auto-modify / human-governed — `10` §5, `11`, `16` §4.
- [ ] Three-layer one-way storage — `13`.
- [ ] Two-dimensional profile — `09`.
- [ ] DeepDive-native reuse map — `00` §3.
- [ ] EVIDENCE_GATE two-tier split: Core mandatory for all profiles; empirical checks only
      when `empirical` runs — `10` §3.1.
- [ ] ResearchApproval PENDING invariant: `approver_user_id`/`resolved_at` null while
      PENDING, set when resolved — `02` §2.8, `11` §1/§6.
- [ ] Artifact producer invariant: execution-produced ⇒ `generated_by_execution` non-null;
      user-intake ⇒ `created_by` non-null, `generated_by_execution` null — `04` §5/§8.
- [ ] Gate applicability conditional on the activated profile stage — `09` §3, `10` §2.
- [ ] Diagram graph edge names/directions match `05` §3 — `diagrams/research-graph.mmd`.

## 7. Open questions

Deferred to `phase-0-review.md` §4; each is scoped and non-blocking for Phase 0.
