# 06 — ResearchExecution Contract

> **Normative.** Supersedes `02-domain-model.md` §2.4 where more specific.

## 1. Purpose

Every sub-agent dispatch and every sandbox computation produces an **immutable audit
record**: what ran, with what inputs, by what agent/skill, at what cost, and with what
outcome. This is what makes a project reproducible and answers "who ran this result, when,
and how?".

## 2. Record (normative)

```yaml
execution_id:          uuid          # PK, immutable
project_id:            uuid
stage_id:              uuid|null     # owning ResearchStage
skill_id:              string|null   # skill name, when a skill drove it
agent_name:            string        # main or sub-agent identity
inputs:                object        # {artifact_ids: [], params: {}} — JSONB
outputs:               object        # {artifact_ids: [], result_summary: {...}} — JSONB
tools_used:            string[]      # tool names actually invoked
environment:           object        # {image: str|null, python_version: str|null, ...}
started_at:            datetime
finished_at:           datetime|null
status:                enum(RUNNING, SUCCESS, FAILED, ABANDONED)
cost_usd:              float         # tracked from LLM/tool usage where available
error_info:            object|null   # {kind, message, trace_ref}
parent_execution:      uuid|null     # nested runs (sub-agents)
```

Invariant: a terminal record (`SUCCESS`/`FAILED`/`ABANDONED`) is **never edited or
deleted**. Corrections create a new execution.

## 3. Lifecycle

```text
RUNNING ──▶ SUCCESS
   │  └──▶ FAILED        (recorded error)
   └────▶ ABANDONED      (stale heartbeat / crash recovery / user abort)
```

- `RUNNING` is written **before** the work starts (not after), so a crash leaves evidence.
- On success, outputs reference the produced artifact ids.
- On failure, `error_info` captures the reason; the owning stage returns to `READY` or
  `BLOCKED`, never `COMPLETED`.

## 4. Write-to-disk rule (mandatory for sub-agents)

A sub-agent must:

1. read its specified input artifacts;
2. perform the task;
3. write results to disk as artifacts (`research_artifact write_scratch` / promote);
4. report back to the main agent **≤ 10 lines**: `STATUS`, `DONE`, `OUTPUT (artifact ids)`,
   `KEY FINDINGS`, `RISKS`, `NEXT`.

The main agent holds only state + pointers; it must not re-ingest large payloads into
context. This mirrors the existing kernel `run_subagent` contract.

## 5. Sandbox execution (`execute_sandbox_script`)

- Only permitted when the project profile allows `sandbox_allowed` (see `09`); a
  `reasoning_only` profile rejects sandbox calls.
- Runs via the existing `bash` tool / docker sandbox; the environment is recorded
  (`image`, `python_version`, pinned deps) so results are reproducible.
- All sandbox intermediate files live in scratch (`storage_scope=scratch`) unless
  promoted.
- The sandbox never has network access unless the session granted it (existing sandbox
  permission semantics).

## 6. Cost and budget enforcement

- `cost_usd` is accumulated from LLM/tool usage where the runtime exposes it; the
  main-agent loop already enforces `settings.max_budget_per_turn_usd`.
- The project `budget_policy` (see `03`) caps total cost/steps/parallelism. Crossing a cap
  blocks the project and raises an approval request before further execution.

## 7. Crash recovery

- `research_project resume` finds `RUNNING` executions with no heartbeat beyond a stale
  threshold, marks them `ABANDONED`, and returns their stage to `READY` (never silently
  forward).
- Recovery does not skip gates: the stage's gate must re-run before the next transition.

## 8. Invariants

1. Every artifact has a producing execution (or `created_by` for intake files).
2. Terminal records are immutable; `outputs` are only written before terminal status.
3. Sub-agent reports are ≤10 lines and reference artifact ids, not inline content.
4. Sandbox calls always record `environment`; a `reasoning_only` profile never issues one.
