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
only after its outputs exist. The DAG is always the full ten-stage chain — no profile removes
stages. Under the default `literature` profile, DESIGN / EXPLAIN / REPRODUCE are light
pass-throughs: do the stage's minimal honest work (or nothing material for that stage) and
call `transition_stage` to advance; in progressive mode a failed guarding gate is recorded as
a diagnostic and the move is granted. Only an `empirical` project (create it with
`profile: "empirical"`) does substantive EXECUTE work via `research_run execute_sandbox_script`.
A transition that reports `transition: "ADVANCED"` has committed and **ends the current turn**
automatically — the next turn opens at the new stage, so after a successful transition, do not
re-do the previous stage's collection or verification work.

## Tool contract

Every `research_*` tool takes an `action` argument pinned to a fixed enum — use ONLY the
literal verbs listed below; anything else is rejected before it reaches the handler. The
tool schema's `Supported actions` / `Constraints` text is the authority; this section is
the at-a-glance map.

| Tool | Actions (the complete legal set) |
|---|---|
| `research_project` | `create` · `resume` · `snapshot` · `archive` |
| `research_artifact` | `write_scratch` · `promote_to_drive` · `read` · `create_version` · `diff` |
| `research_state` | `get_state` · `get_handoff` · `transition_stage` |
| `research_evidence` | `record_node` · `mutate_node` · `invalidate_downstream` · `adjudicate` · `verify` · `verify_batch` |
| `research_gate` | `check` · `explain_failure` · `request_override` · `resolve_override` |
| `research_run` | `record_execution` · `finish_execution` · `execute_sandbox_script` |
| `research_scrape` | `save_scrape` · `fetch` · `fetch_materials` · `read` |

Hard boundaries:

- **No manual graph edges.** There is no link / edge / lineage action — `adjudicate` /
  `verify` / `verify_batch` write Sources/Evidence nodes and claim edges themselves. Do not
  invent
  verbs like `list`, `get`,
  `status`, `show`, `inspect`, `query`, `add_node`, `lineage`, `transitions` — none exist.
- **`bash` / `read_file` / `tool_search` do not exist in this workflow.** Never attempt
  them; your tools are already mounted and listed above. There is nothing to search for,
  nothing to shell out to, and scratch files are read only through `research_scrape read` /
  `research_artifact read`.
- **Batch per chunk, not per claim.** EVIDENCE runs the deterministic pipeline
  `pending → one research_evidence adjudicate call per chunk`
  (the EVIDENCE PROTOCOL in step 5) — never a per-claim fetch/verify/mutate retail chain.
  The fetch, representative-chunk extraction, batched adjudication, and the single
  verify_batch commit all happen server-side inside that one action.
  One `write_scratch` with the full text per artifact
  (it auto-versions — do not stream incremental writes).
- **One `get_handoff` before each `transition_stage`**, and `ADVANCED` ends the turn —
  never re-issue a transition for a stage you already left, and never call
  `query`-style discovery loops on `research_state` (`get_state` already returns the stage).

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

5. **EVIDENCE — close each chunk in ONE atomic action.** This stage is governed by a
   strict operational protocol, not a suggestion. Work the pipeline
   `get_state → pending claims → ONE research_evidence adjudicate call per chunk`
   exactly as written:

   **EVIDENCE PROTOCOL (mandatory, 11 rules)**

   0. **Materials first.** If `get_handoff` / `get_state` shows a `materials_hint` (the task
      carries materials), start the stage with `research_scrape` `action: "fetch_materials"`
      (optional `names` filter) — it extracts every task material server-side into the run's
      provenance ledger under `material://` urls. Materials are first-class sources with the
      exact same standing as web pages: pass the returned `material://` urls to `adjudicate`
      alongside web urls, and cite them only after `research_scrape read` (same as web
      pages). `source_type` is identity only — it never changes how evidence is verified.
   1. **Pending first.** Start every EVIDENCE turn with `research_state get_state`: the
      `claims` digest marks each claim `pending` (true only when it has no valid
      committed verdict, or its evidence fingerprint moved since the last commit) and
      groups the pending ones by `chunk_hint`. The digest fields are hints for
      efficiency; the server re-checks the pending rule at commit time regardless.
   2. **No pending evidence, no adjudicate call.** If the digest lists no pending claims,
      do NOT call any `verify`-family action at all — finish the stage instead.
   3. **Reuse before you create.** A claim already in the digest keeps its exact `id`
      — `record_node` ONLY for claims not yet listed; never mint a shadow id for an
      existing claim. The `id` is the sole anchor; `label` is display-only.
   4. **One `adjudicate` call is the whole chunk.** Submit
      `research_evidence` `action: "adjudicate"` with `urls` = every candidate source
      URL for the chunk (any count — the server batches the fetches itself) and
      `claim_ids` = the chunk's pending ids. Server-side, in that ONE call: every page
      is fetched and saved, ONE deterministic representative chunk per page is
      extracted by Python (never an LLM summary), relevance+verdict are judged in a
      single batched LLM pass (split into more passes ONLY if the token budget is
      exceeded — invisible to you), and ALL verdicts commit in ONE atomic
      `verify_batch` transaction. Do NOT call `research_scrape fetch` for
      adjudication, do NOT judge snippets in the conversation, and do NOT hand-build
      a `verify_batch` in EVIDENCE (`verify`/`verify_batch` stay for manual paths).
   5. **The reply is a compact verdict summary.** `per_claim` lists each claim's
      supports / contradicts / insufficient URLs, plus `skipped_sources` (per-URL
      fetch failures), `dropped_rows` (invalid ids the model emitted — server-rejected)
      and `no_verdict_claims` (no source related to them). `insufficient` is
      relevance-only — it never creates a ticket edge.
   6. **Citations and strength go on BEFORE you adjudicate.** A Claim must carry its
      `citations` and canonical `strength` (`asserted | supported | confident |
      contested`) at `record_node` time. Patching the graph after a commit is the
      retired double-anchor chain and is forbidden.
   7. **Never re-adjudicate committed evidence.** Claims whose committed evidence is
      unchanged come back `skipped_unchanged` inside the commit with zero writes, and
      pages the run already fetched are reused from the ledger, never re-fetched.
   8. **Claim-level retry.** If `no_verdict_claims` names a claim (or the commit
      rejects one), gather BETTER source URLs and call `adjudicate` again with just
      that `claim_id` — never re-run the whole chunk for one claim.
   9. **Source-scoped failures are terminal for that source.** A 403, `empty`, or
      `interstitial` source is dead for this run: it appears under
      `skipped_sources`; do not retry it in-run and never
      let one page stall or kill the task — note the gap and proceed with the other
      sources. `search_social` signals the same contract with a `terminal_for_run`
      item in its results: when you see it, stop querying that platform this run.
   10. **Explicit ids, never positions.** Match every result by `claim_id` and source
       `url` / `canonical_url`, never by array order or label text.

   Supporting semantics the service enforces: adjudication anchors to already
   recorded claim ids and never creates claims; a page supporting a claim gets
   verdict `supports`, contradicting `contradicts`, genuinely related-but-thin
   `insufficient` (a neutral annotation, no ticket), and a
   login/empty/JS-only shell is unusable (never adjudicated) — only URLs
   the server-side fetch ledger confirms as fetched-ok and usable become edges, so
   your own content assertions carry no authority. Do NOT hand-record Source/Evidence
   nodes or hand-link claim edges; the commit writes them idempotently.
   - Keep a source ledger in `sources.md`; mark each source's authority and independence.
   - Deliberately search for disagreement — a missing contradiction is weaker evidence
     than an active search that found none.

6. **Cross-verify claims.** For contested or load-bearing claims, check a gate with
   `research_gate check` (gate_name as appropriate). If a gate fails, call
   `research_gate explain_failure` to see why. In **strict** mode, do not silently proceed
   past a failed gate and never request a gate override on your own judgment alone —
   surface it for the user. In **progressive** mode a failed gate is recorded and lets you
   continue instead — see “Progressive mode” below (that path is not "silently proceeding";
   the diagnostics are the honest record), and never call `request_override` there.

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
     These are report prose; the graph node's `strength` field uses the canonical set
     `asserted | supported | confident | contested` (see EVIDENCE above).
   - Name the remaining uncertainty explicitly — a good report states its gaps.

9. **REVIEW — self-check before publish, in ONE call.** Call
   `research_artifact review_draft` (pass the report's `artifact_id`) EXACTLY ONCE.
   The server puts the whole draft + claim graph in front of the model; it returns
   only `{"changes": [...]}` correction instructions (no document echo); Python
   pre-checks every anchor under the unique-match iron rule, applies them in a
   staging copy and commits atomically — all pass → one new version, any reject →
   nothing is written and the error lists the failed rows. On a rejection, fix ONLY
   the listed passages with a minimal `create_version` (never re-read the whole
   draft); on `new_version: null` the draft was fully supported — move on. If a
   claim is unverifiable, the draft must say "not verified" instead of hedging.

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
