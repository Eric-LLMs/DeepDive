# 00 — Research OS: Architecture Overview

> **Normative.** This document is part of the DeepDive Research OS contract set
> (`docs/research/`). Every document in this directory is a **normative contract, not an
> implementation suggestion**. Any behavior not explicitly permitted by a contract is
> considered **prohibited by default**. Where two documents conflict, the more specific
> document wins and the conflict must be reported in `phase-0-review.md`.

## 1. Purpose

DeepDive is a multi-tenant AI learning platform. It already ships an agent kernel
(`AgentKernel`), a Cordis-style plugin runtime (`PluginManager`), a lazy `SkillRegistry`,
a per-user cloud drive (`DriveService`), a unified RAG retrieval capability (`retrieval` /
`QueryRepository`), an arq async job system, and `ContextVar`-based tenant isolation.

The **Research OS** is the product layer that turns a research request into a rigorous,
artifact-based research project delivered into the user's cloud drive and searchable via
RAG. It is not a paper pipeline: it is a general **research operating system** that
orchestrates both lightweight synthesis outputs (literature review, research memo,
research report) and rigorous empirical studies (design lock, evidence gates, claim
governance, peer-review simulation, reproducibility).

## 2. Design philosophy

> **Artifact-first** — the source of truth is persisted, versioned domain objects, never
> an LLM context window.
>
> **Graph-backed** — from research question, hypothesis, literature, dataset, execution,
> result, evidence to manuscript text, everything is connected in a **Project-scoped**
> dependency graph supporting bidirectional lineage and cascade invalidation.
>
> **Event-auditable** — every sub-agent dispatch or sandbox computation produces an
> immutable `ResearchExecution` audit record.
>
> **Human-governed** — design lock, core-assumption changes, and gate overrides require
> explicit human confirmation. Agents never self-approve.

The system center is the **`ResearchProject`** domain entity, not `workflow_state.json`.
`workflow_state.json` (or its equivalent runtime projection) is only a projection of the
project's current runtime state; the authoritative record lives in the database.

## 3. Layered architecture

Four decoupled layers, each mounted through existing DeepDive machinery. `Stage ≠ Skill`:
stages (control), agents (executors), capability skills (method logic), and tools
(actions) are independent.

```text
Workflow Engine   → 10-stage DAG state machine, legal transitions, handoffs, gate sequence
Agent Layer       → main agent holds pointers + state; sub-agents write to disk and return
                    ≤ 10-line summaries (write-to-disk rule)
Capability Skills → pure method logic as skills/*.skill.md (lit matrix, DID checks, referee)
Tool Layer        → 6 stateless action tools in the research Cordis plugin, thin wrappers
                    over ResearchService domain services
```

Reuse map (mandatory — do not build parallel infrastructure):

| Concern | DeepDive primitive |
|---|---|
| Plugin mount / hot reload / validation | `PluginManager` + `Plugin` (packages/agent/plugins) |
| Tool definition | `define_tool` / `ToolOutput` (packages/agent/tools/definition.py) |
| Skills | `SkillRegistry.from_dir` + `skills/*.skill.md` frontmatter (packages/agent/frontmatter.py) |
| Sub-agent dispatch | kernel `run_subagent` tool |
| Human approval | `ToolRuntime` approval bridge + `approvals.py` ASK path |
| Durable file storage | `DriveService.save_artifact` → assets/global_objects + object store |
| RAG retrieval | `retrieval` capability (`rag_search`), `QueryRepository`, `asset_ingest` |
| Tenant isolation | `request_user` ContextVar + `asset_visible_expr` / `chunk_visible_expr` predicates |
| Async heavy work | `TaskQueue` / arq worker (extended later) |

## 4. Core entities (8)

The domain model (see `02-domain-model.md` and the per-entity contracts) is:

1. `ResearchProject` — the domain center; identity, intent, policies, status. (`03`)
2. `ResearchArtifact` — first-class versioned artifact with lineage. (`04`)
3. `ResearchGraph` — Project-scoped entities + typed links with STALE/INVALID cascade. (`05`)
4. `ResearchExecution` — immutable audit record for every run. (`06`)
5. `ResearchStage` — DAG stage model. (`07`)
6. `ResearchGate` — four hard gates. (`10`)
7. `ResearchProfile` — Method × Output profile. (`09`)
8. `ResearchApproval` — human intervention records. (`11`)

## 5. Workflow and gates (summary)

```text
DISCOVER → FRAME → EVIDENCE → DESIGN ─[DESIGN GATE]→ EXECUTE ─[EVIDENCE GATE]→
EXPLAIN → WRITE ─[CLAIM GATE]→ REVIEW ─[QUALITY GATE]→ REPRODUCE → PUBLISH
```

- Stages form a **DAG**; profiles activate/skip stages; users may enter at any stage with
  sufficient upstream artifacts.
- `transition_stage()` is **not an arbitrary mutation**: every transition must pass
  Dependency Check → Gate Check → Approval Check (see `07`).
- Gates never auto-modify. A FAIL on a high-risk item stops and asks the user (see `10`, `11`).

## 6. Storage model (summary)

Three-layer, one-way flow (see `13`):

```text
Scratch (ephemeral)  →  Cloud Drive (durable Source of Record)  →  RAG (derived index)
```

The **database** (research tables + graph) is the system Source of Truth. RAG is a pure
derived projection and may never reverse-mutate the drive or metadata.

## 7. Diagram index

| Diagram | File | What it shows |
|---|---|---|
| Container | `diagrams/c4-container.mmd` | Clients → API gateway → research plugin → stores/worker |
| State machine | `diagrams/workflow-state.mmd` | 10 stages, 4 gates, fail-back loops, profile skips |
| Graph | `diagrams/research-graph.mmd` | Node types and typed edges across layers |

## 8. Document index and read order

1. `00-architecture-overview.md` (this file) — orientation.
2. `01-product-spec.md` — what the product does and for whom.
3. `02-domain-model.md` — the 8 entities at a glance + DB mapping.
4. `03`–`06` — per-entity contracts (Project, Artifact, Graph, Execution).
5. `07`–`08` — state machine and workflow/stage templates.
6. `09`–`11` — profiles, gate policy, approval policy.
7. `12`–`14` — provenance, storage, research context (RAG).
8. `15`–`16` — Cordis plugin/tool contract, permission model.
9. `17` — MVP scope.
10. `contract-matrix.md` — cross-reference; `phase-0-review.md` — self-review and open questions.

## 9. Success criterion (acceptance test for Phase 0)

> An engineer who did **not** participate in this design can implement Phase 0.5 (spike)
> and Phase 1 (MVP) **from `docs/research/` alone** and will not invent their own Project /
> Artifact / State / Graph / Gate semantics.

Phase 0 is complete when the success criterion holds and `phase-0-review.md` passes its
consistency checklist.
