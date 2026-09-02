---
name: deep_research
description: Run a full multi-source research workflow — plan, search, gather evidence, cross-verify claims, synthesize, and publish a cited report to the drive. Use for substantive questions that need several sources and verification, not a quick answer.
keywords: research, deep research, investigate, sources, evidence, claims, citations, report, literature, study, synthesis
allowed_tools: research_project, research_artifact, research_state, research_evidence, research_gate, research_run, rag_search, web_search, search_social
---

# Deep Research Procedure

Use this when the user wants a well-supported, cited answer that requires gathering and
verifying multiple sources — not a single lookup. The workflow runs inside a Research OS
project, so every source, claim, and artifact is persisted, auditable, and (on publish)
retrievable later through RAG.

> **Resuming an existing task?** If this turn carries a research handoff — you are told the
> task already exists, or you already have a `project_id` — **do NOT create a new project**.
> Call `research_project` with `action: "resume"` and that `project_id` first, then continue
> from the project's current stage through to PUBLISH. Creating a second project for an
> existing task is the single most common mistake; the task's `project_id` is the one you
> must keep using for every `research_*` call.

## Core loop

```text
Clarify → Plan → DISCOVER → FRAME → EVIDENCE → Synthesize → WRITE → REVIEW → PUBLISH
```

Each stage maps to a `research_state` stage; advance with `research_state transition_stage`
only after its outputs exist. The default `literature` profile skips DESIGN/EXPLAIN/REPRODUCE
and runs EXECUTE as *synthesis* — no sandbox script. If the user needs empirical work,
create the project with `profile: "empirical"` and use `research_run execute_sandbox_script`.

## Steps

1. **Clarify and plan.** Restate the question precisely; if it embeds a false premise,
   correct it first. Break a broad ask into sub-questions — one focused search per
   sub-question beats one vague query.

2. **Create the project — only if none exists yet.** `research_project` with
   `action: "create"`, a `name`, and a `profile` (default `"literature"`). Keep the returned
   `project_id` for every later call. Skip this step entirely when resuming (see the note
   above) and use `action: "resume"` with the existing `project_id` instead.

3. **DISCOVER — collect candidate sources.** Search in parallel across channels:
   - `rag_search` — the user's own imported corpus (papers, notes, documents).
   - `web_search` — official docs and recent, authoritative pages.
   - `search_social` — lived experience and current community discussion (useful for
     fast-moving topics, but treat anecdotes as opinions, not facts).
   Write the candidate list to an artifact `corpus.md` via `research_artifact write_scratch`
   with `project_id` + `artifact_id: "corpus.md"`.

4. **FRAME — pin the question and scope.** Write `research_question.md` (the falsifiable
   question you will answer) and `scope.md` (what is in and out of bounds). Changing the
   question later needs an approval — so get it right here.

5. **EVIDENCE — verify and record.** For each material claim:
   - Record the claim as a graph node: `research_evidence record_node` with
     `node: {id, type: "claim", label, status: "CANDIDATE"}`.
   - Link it to its source: `research_evidence link_edge` with `src: <source_id>`,
     `dst: <claim_id>`, `kind: "supports"` (or `"contradicts"` when a source disagrees).
   - Keep a source ledger in `sources.md`; mark each source's authority and independence.
   - Deliberately search for disagreement — a missing contradiction is weaker evidence
     than an active search that found none.

6. **Cross-verify claims.** For contested or load-bearing claims, check a gate with
   `research_gate check` (gate_name as appropriate). If a gate fails, call
   `research_gate explain_failure` to see why; do not silently proceed past a failed gate.
   Never request a gate override on your own judgment alone — surface it for the user.

7. **Synthesize (EXECUTE).** Compare sources, resolve contradictions explicitly (state both
   sides), and rank evidence. Produce the reasoning that turns evidence into a conclusion.
   Record the audit trail with `research_run record_execution` / `finish_execution` so the
   project's provenance is complete.

8. **WRITE — draft the report.** Write the full report to an artifact (e.g. `draft.md`):
   - Lead with the direct answer, then the reasoning and the evidence trail.
   - Cite each substantive claim to a source inline; separate your synthesis from what
     sources actually say.
   - Rate confidence per claim: **high** (multiple independent, specific sources agree),
     **medium** (one strong source, or several with gaps), **low** (thin or conflicting).
   - Name the remaining uncertainty explicitly — a good report states its gaps.

9. **REVIEW — self-check before publish.** Re-read the draft for unsupported assertions.
   If a claim is unverifiable, say "not verified" instead of hedging. Fix the draft with
   `research_artifact create_version` rather than leaving a broken version.

10. **PUBLISH.** Promote the final artifact with `research_artifact promote_to_drive`
    (`artifact_id` of the final report). Promotion marks the drive asset RAG_PENDING, so the
    projection worker indexes it — the report becomes retrievable in future `rag_search`
    queries (the knowledge flywheel).

## Output style

- Answer in the user's terms, then the supporting evidence with inline citations.
- For long research, summarize the verdict up front and put the full evidence trail in the
  published artifact.
- If the evidence is thin or one-sided, say so plainly rather than padding the conclusion.
