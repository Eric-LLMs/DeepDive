# 17 — MVP Scope (Phase 1)

> **Normative.** The MVP is the shortest defensible loop. Everything designed in this suite
> but not listed here is **out of MVP scope** and must be explicitly deferred — not
> half-built.

## 1. MVP loop

```text
Research Inbox → Project → DISCOVER → FRAME → EVIDENCE → SYNTHESIZE
→ EVIDENCE GATE → Research Report → Cloud Drive → RAG
```

Mapped onto the stage machine:

| MVP stage | Stage machine stage | Notes |
|---|---|---|
| Inbox → Project | — | `research_project inbox_add` / `inbox_promote` |
| DISCOVER | DISCOVER | RAG + web sweep |
| FRAME | FRAME | research_question, scope |
| EVIDENCE | EVIDENCE | sources verified, literature matrix |
| SYNTHESIZE | EXECUTE (synthesis variant) | structured reasoning, no sandbox |
| EVIDENCE GATE | EVIDENCE_GATE | mechanical + light LLM |
| Research Report | WRITE | report artifact, claim-governed-lite |
| Cloud Drive + RAG | PUBLISH | FINAL_REPORT → drive → ingest |

## 2. Profiles in scope

`literature` / `mixed` × `memo` / `literature_review` / `research_report`.
All other profile cells (`empirical`, `theoretical`, `qualitative`, `paper`,
`replication_report`, `proposal` outputs) are **designed but not implemented** in MVP.

## 3. Entities/tables in scope (subset)

| Table | MVP usage |
|---|---|
| `research_projects` | full |
| `research_inbox` | full |
| `research_artifacts` | full (scratch + promote_to_drive) |
| `research_entities` | limited kinds: `Question, Hypothesis, Source, Result, Evidence, Claim, Decision, Risk, Gate` |
| `research_links` | limited kinds: `motivates, uses, produces, supports, derived_from, depends_on, cites, invalidates` |
| `research_sources` | full (verification) |
| `research_executions` | full (dispatch_subagent + record; no sandbox) |
| `research_stages` | DISCOVER, FRAME, EVIDENCE, EXECUTE(synthesis), WRITE, PUBLISH |
| `research_gates` | EVIDENCE_GATE only |
| `research_approvals` | full (mutation + override) |

Not in MVP: `design_register`, `sample_audit`, `diagnostics`, `robustness`, `manuscript`
claim anchoring, referee simulation, replication pack, context router.

## 4. Tools in scope

`research_project`, `research_artifact`, `research_state`, `research_evidence`,
`research_gate` (EVIDENCE_GATE), `research_run` (`dispatch_subagent` + `record` only —
sandbox disabled). Skills call existing `web_search`, `search_social`, `rag_search`.

## 5. Explicitly out of scope (MVP)

| Area | Deferred to |
|---|---|
| DESIGN stage + DESIGN_GATE + design lock | Phase 2 |
| Sandbox execution (`execute_sandbox_script`) | Phase 2 |
| `sample_audit`, `codebook`, diagnostics, robustness | Phase 2 |
| Full graph kinds (Variable, Analysis, Execution-as-node, Table/Figure, Paragraph) | Phase 2 |
| Claim anchoring (number ↔ ledger) + CLAIM_GATE | Phase 3 |
| Adversarial review + QUALITY_GATE scorecard | Phase 3 |
| REPRODUCE stage + replication pack | Phase 3 |
| Context Router (project/personal/literature/web routing) | Phase 4 |
| Research dashboard UI, proactive inbox listener | Phase 4 |
| Collaborative research workspace | Phase 4 |

## 6. MVP acceptance criteria

1. A chat idea → inbox item → promoted project in one session.
2. `DISCOVER → FRAME → EVIDENCE → SYNTHESIZE` produces verified sources, a matrix, and a
   claim-linked synthesis in the drive folder `research/{project_id}/`.
3. EVIDENCE_GATE blocks a report whose sources are `unverified` or whose claims have no
   evidence nodes.
4. FINAL_REPORT is promoted to the drive and becomes RAG-searchable.
5. Two users' projects/drives remain fully isolated.
6. The success criterion of `00` holds for the **implemented subset**: a fresh engineer can
   extend toward Phase 2 from `docs/research/` without redefining semantics.
