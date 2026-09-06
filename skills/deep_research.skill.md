---
name: deep_research
description: Run a full multi-source research workflow — plan, search, gather evidence, cross-verify claims, synthesize, and publish a cited report to the drive. Use for substantive questions that need several sources and verification, not a quick answer.
keywords: research, deep research, investigate, sources, evidence, claims, citations, report, literature, study, synthesis
allowed_tools: research_project, research_artifact, research_state, research_evidence, research_gate, research_run, research_scrape, rag_search, web_search, search_social
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
   **Capture the raw source text first.** Whenever a `web_search` / `search_social` hit returns
   usable material, call `research_scrape` with `action: "save_scrape"` (pass `source`, `url`,
   `query`, `content` = the page text/markdown) BEFORE condensing it into notes — the file
   lands in the run's `temp/vN/scrape/` folder. It is silent: no message comes back, and it is
   not a substitute for note-taking. Then write the condensed candidate list to an artifact
   `corpus.md` via `research_artifact write_scratch` with `project_id` +
   `artifact_id: "corpus.md"`.

4. **FRAME — pin the question and scope.** Write `research_question.md` (the falsifiable
   question you will answer) and `scope.md` (what is in and out of bounds). Changing the
   question later needs an approval — so get it right here.

5. **EVIDENCE — verify and record (batch mode).** Work claim by claim, but *wholesale* per
   claim instead of one source at a time:
   - First record the claim as a graph node: `research_evidence record_node` with
     `node: {id: <claim id>, type: "Claim", label}`. `verify` anchors to that claim id and
     never creates a claim itself.
   - Pick 1–3 of the strongest candidate source URLs for the claim (from `web_search` /
     `search_social` hits you have not captured yet) and fetch them in ONE call:
     `research_scrape` with `action: "fetch"` and `urls: [≤3 URLs]`. The service fetches the
     batch concurrently (SSRF-guarded), cleans each page to its core text, files a full draft
     under `temp/vN/scrape/`, and returns a snippet per URL carrying its `canonical_url`,
     `content_status` (`usable` / `empty` / `interstitial`), `full_char_len` and the text.
   - Judge each returned page from its snippet. A page whose content supports the claim gets
     verdict `supports`; one that contradicts it `contradicts`; a login/empty/JS-only shell is
     unusable (do not verify it as a source); a page that genuinely neither supports nor
     contradicts gets `neutral`.
   - Then verify the whole claim in ONE call: `research_evidence` with `action: "verify"`,
     `claim: {id: <the claim id you recorded>}` and
     `findings: [{url: <canonical_url returned by fetch>, verdict: "supports"|"contradicts"|"neutral",
     source_label?, facts?: [...], excerpt?}]`. The service only turns a finding into a
     verified Source/Evidence when **its own server-side fetch ledger** for this run confirms
     the page was fetched-ok and usable — `neutral` and unusable pages never become claim
     edges. Run another `fetch` batch (≤3 URLs) only if the claim still needs more sources.
   - Do NOT hand-record Source/Evidence nodes or hand-link claim edges in EVIDENCE —
     `verify` writes them idempotently (re-verifying the same claim+URL is an upsert, never a
     duplicate). `record_node` is only for the Claim (and any non-source concept you want in
     the graph).
   - Keep a source ledger in `sources.md`; mark each source's authority and independence.
   - Deliberately search for disagreement — a missing contradiction is weaker evidence
     than an active search that found none.

6. **Cross-verify claims.** For contested or load-bearing claims, check a gate with
   `research_gate check` (gate_name as appropriate). If a gate fails, call
   `research_gate explain_failure` to see why; do not silently proceed past a failed gate.
   Never request a gate override on your own judgment alone — surface it for the user.
   In a `progressive` project a failed gate is recorded and lets you continue instead — see
   “Progressive mode” below, and never call `request_override` there.

7. **Synthesize (EXECUTE).** Compare sources, resolve contradictions explicitly (state both
   sides), and rank evidence. Produce the reasoning that turns evidence into a conclusion.
   Record the audit trail with `research_run record_execution` / `finish_execution` so the
   project's provenance is complete.

8. **WRITE — draft the report.** Write the full report to an artifact (e.g. `draft.md`):
   - Lead with the direct answer, then the reasoning and the evidence trail.
   - Cite each substantive claim to a source inline; separate your synthesis from what
     sources actually say.
   - Ground each citation in the full page, not the search snippet: before you quote or cite a
     fetched source, read its full draft back with `research_scrape` `action: "read"` passing
     the `canonical_url` (or `asset_id`) that `fetch` returned for it. The draft holds detail
     the returned snippet was too short to carry.
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

## Progressive mode (`execution_mode = progressive`)

Some projects run in `progressive` mode — read it from the `research_project resume/snapshot`
reply and behave accordingly. Gate checks still run exactly as in strict mode, but a FAILED
gate **records a diagnostic and does not block the workflow**:

- In progressive mode, call `research_state transition_stage` normally. If the guarding gate
  has not passed, the system automatically records the failed checks into project diagnostics
  and advances the stage (`granted: True`) — do not retry the same transition merely because
  the gate failed (the stage has already moved on, so a second call is an illegal transition).
  Do not pad or fake evidence to force a pass.
- **Never stall inside a stage.** Each stage must hand off to the next. If ~2-3 retrieval
  attempts (rag_search / web_search / search_social) for the *current* stage keep coming back
  empty or redundant, stop gathering and call `research_state transition_stage` to advance —
  the un-passed guarding gate's failed checks land in the project diagnostics and the
  transition is granted. Do not keep re-searching the same stage turn after turn hoping for a
  lucky hit; the diagnostics progressive writes are the honest record of what could not be
  sourced, and the report's "Known gaps / unverified items" section surfaces them. In
  strict mode, use `research_gate explain_failure` and only request an override when a real
  human decision is genuinely required.
- **Never call `research_gate request_override` in progressive mode.** Overrides are the
  human-approval path for strict runs; in progressive mode one would only park the task
  awaiting a human that should not be needed.
- Do not bypass the state machine in any other way: stay on the legal
  `DISCOVER → FRAME → EVIDENCE → DESIGN → EXECUTE → EXPLAIN → WRITE → REVIEW → REPRODUCE →
  PUBLISH` chain and never skip a stage.
- When you write the report, surface what could not be verified. Read the recorded
  diagnostics (via `research_project snapshot`), and close the report with a
  **"Known gaps / unverified items"** list — one entry per diagnostic (gate + stage + the
  failed checks), each clearly labeled `unverified`, so the user knows exactly what to supply.

## Output style

- Answer in the user's terms, then the supporting evidence with inline citations.
- For long research, summarize the verdict up front and put the full evidence trail in the
  published artifact.
- If the evidence is thin or one-sided, say so plainly rather than padding the conclusion.
