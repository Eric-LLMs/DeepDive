# Phase 0 Review — Self-Assessment

> **Normative.** This file records the Phase 0 self-review against the success criterion,
> the consistency checklist, and the open questions that Phase 0.5/1 must resolve or
> explicitly defer.

## 1. Success criterion

> An engineer who did **not** participate in this design can implement Phase 0.5 (spike)
> and Phase 1 (MVP) **from `docs/research/` alone** and will not invent their own Project /
> Artifact / State / Graph / Gate semantics.

Self-assessment: **met for the MVP subset.** The suite fixes, unambiguously:

- **Entity schemas** — `02` §2 gives exact field names/types/enums for all 8 entities;
  per-entity docs supersede.
- **6 tool signatures** — `15` §2 gives parameter JSON Schemas; `15` §3 gives the
  `ResearchService` interface.
- **State machine** — `07` gives stages, statuses, legal transition table, gate placement.
- **Gate mechanics** — `10` gives deterministic check lists and verdict semantics.
- **Storage** — `13` gives the one-way flow and path mapping.

Known residual ambiguities are listed in §4 and are intentionally deferred (they do not
block the MVP).

## 2. Consistency checklist

- [x] 23 files present (20 md + 3 mmd) — `contract-matrix.md` §1.
- [x] Normative banner present in every document.
- [x] `Execution` graph node defined; canonical chain Dataset → Execution → Analysis → Result.
- [x] Edge set includes derived_from / generated_by / depends_on / cites / tests.
- [x] `transition_stage` documented as request-only with Dependency → Gate → Approval checks.
- [x] Human-governed: no self-approval, no-auto-modify, monotonic design-lock guard.
- [x] Three-layer one-way storage; RAG derived-only.
- [x] Two-dimensional profile (Method × Output).
- [x] DeepDive-native reuse map (`00` §3) — no parallel storage/scheduling system.
- [x] Stage names consistent across `07`, `08`, `09`, `17`.
- [x] Tool names/actions consistent across `02`, `15`, `17`.
- [x] Entity field names consistent across `02` and per-entity docs.
- [x] Gate names (DESIGN/EVIDENCE/CLAIM/QUALITY) consistent across `07`, `09`, `10`, `15`.
- [x] EVIDENCE_GATE two-tier split (Core mandatory for all profiles / empirical profile-specific)
      — `10` §3.1; the literature MVP passes on Core Checks without empirical audits.
- [x] ResearchApproval PENDING invariant (`approver_user_id`/`resolved_at` null while
      PENDING, set when resolved) consistent across `02` §2.8 and `11` §1/§6.
- [x] Artifact producer invariant (execution-produced vs user-intake) — `04` §5/§8.
- [x] Gate applicability conditional on the activated profile stage — `09` §3, `10` §2.
- [x] Diagram graph edge names/directions match `05` §3; no non-normative edge (e.g.
      `rejected_by`) remains.
- [x] `FRAME` mandatory in every profile; workflow diagram carries no FRAME skip edge.

## 3. Cross-check results

- **Entity ↔ table**: `02` §4 proposes `research_projects`, `research_inbox`,
  `research_artifacts`, `research_entities`, `research_links`, `research_sources`,
  `research_executions`, `research_stages`, `research_gates`, `research_approvals`.
  No table listed without a backing entity; no entity without a table.
- **Tool ↔ entity**: every tool's primary entity is covered in `15`; every entity has at
  least one tool that operates on it.
- **Gate ↔ stage**: gate placement in `07` matches the gate definitions in `10`.
- **Profile ↔ gate**: profile activation table (`09` §3) is consistent with the stage DAG
  (`07` §1) and MVP scope (`17`).
- **EVIDENCE_GATE ↔ profile**: the two-tier check split (`10` §3.1) is consistent with
  profile activation (`09` §3) — literature/MVP profiles pass on Core Checks only.

## 4. Open questions (scoped, non-blocking for Phase 0)

1. **Graph storage representation**: whether `research_entities`/`research_links` are
   fully relational rows vs. a per-project JSON graph + indexed accessors. `02` §4 and `05`
   assume relational rows; a Phase 0.5 spike should validate query patterns (lineage,
   impact analysis) against real RAG/PG behavior before the migration is frozen.
2. **Number-anchoring tolerance**: exact rounding/format policy for CLAIM_GATE extraction
   (regex scope, numerical tolerance). Configurable; default suggested in `10` §3.3.
3. **Approval surface**: whether approvals are created/answered through the existing
   chat-stream approval frames directly or through a dedicated `/api/research/approvals`
   endpoint. `11` §3 assumes reuse of the existing bridge; the spike confirms the wiring.
4. **Project-scoped retrieval filter**: how the `retrieval` capability expresses a
   project scope (folder/domain id). `14` §3 assumes a project filter; needs a spike on
   `QueryRepository`.
5. **Drive folder realization**: whether `research/{project_id}/` is created eagerly as a
   folder row or lazily on first promote. `03` §5 says lazy; `13` assumes folder_path
   exists at promote. Resolve in the spike.
6. **Skill packaging**: skills authored in `skills/` (dir, auto-loaded) vs. bundled in the
   plugin `skills` field. Both are supported by `PluginManager`/`SkillRegistry`; `08` §2
   and `15` §4 allow either. Decide at implementation time; keep the file layout flat.

## 5. Design-sources note

This suite is **DeepDive-native**: it reuses `PluginManager`/Cordis DI, `define_tool`,
`SkillRegistry`, `AgentKernel` `run_subagent`, `DriveService.save_artifact`,
`retrieval`/`QueryRepository`, the approval bridge, and `ContextVar` tenancy. The two
reference research systems were used as **design sources** (state machine, evidence-ledger
and claim-governance ideas, capability-skill decoupling) — not as code templates.

## 6. Phase 0 exit condition

Phase 0 is complete when this review passes and a fresh reader can implement the MVP
(Phase 1) without redefining semantics. The 23 files under `docs/research/` are the
deliverable; no code, config, or migration has been changed.
