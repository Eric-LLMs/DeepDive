# 03 — ResearchProject Contract

> **Normative.** Supersedes `02-domain-model.md` §2.1 where more specific.

## 1. Identity and ownership

- `project_id` (uuid, immutable PK) is minted by the system at create; never supplied by a
  client.
- `owner_id` is resolved from `request_user` (`core/infrastructure/request_context.py`),
  **never from client input**. A guest (`request_user is None`) cannot own a project.
- Collaboration (future) flows through the existing workspace/ACL primitives; until then a
  project is personal.

## 2. Lifecycle states

```text
CREATED ──▶ IN_PROGRESS ──▶ COMPLETED ──▶ ARCHIVED
              │   ▲            │
              ▼   │            ▼
           BLOCKED            (terminal; ARCHIVED is the only end state)
```

| State | Meaning | Entered by |
|---|---|---|
| `CREATED` | Row exists, intake not finished | `research_project create` |
| `IN_PROGRESS` | At least one stage active | legal `transition_stage` |
| `BLOCKED` | Waiting on gate/approval/dependency | legal transition or gate FAIL on high-risk item |
| `COMPLETED` | PUBLISH finished; `FINAL_REPORT` promoted | legal transition of PUBLISH |
| `ARCHIVED` | Read-only historical | `research_project archive` |

`ARCHIVED` is terminal: no stage transitions, no new artifacts, no graph writes. Reads are
allowed.

## 3. Embedded ResearchIntent

Captured at intake and refined at FRAME:

```yaml
user_goal:            string          # required
research_question:    string|null     # set at FRAME; required before DESIGN/EXECUTE
target_audience:      string|null
constraints:          string[]        # e.g. "must use only uploaded data", "no sandbox"
success_criteria:     string[]        # user-defined done conditions
```

Intake rule (from the reference research protocol): **ask only what materially changes the
design; infer what can be safely inferred.**

## 4. Policies

### 4.1 `approval_policy`

```yaml
require_approval_on:  string[]   # subset of: research_question, design, sample, estimator,
                                 # claim_strength, data_source, publication
default:              ["research_question", "design", "sample", "estimator",
                       "claim_strength", "data_source", "publication"]
timeout_seconds:      int        # how long a PENDING approval waits (default 120,
                                 # aligned with settings.approval_timeout_seconds)
```

All six high-risk mutation classes are on by default. See `11-approval-policy.md`.

### 4.2 `budget_policy`

```yaml
max_cost_usd:         float|null   # null = platform default (settings.max_budget_per_turn_usd)
max_steps:            int|null     # cap on loop steps for the project
max_parallel_agents:  int          # default 1; sub-agents beyond this are queued
```

Exceeding `max_cost_usd` or `max_steps` puts the project `BLOCKED` and raises an approval
request before any further execution.

## 5. Creation flow (`research_project create`)

1. Validate title/intent/profile against schema; resolve `owner_id` from `request_user`.
2. Mint `project_id`; insert `research_projects` row (`CREATED`).
3. Create the cloud-drive folder scope `research/<project_id>/` (logical; actual durable
   assets are created lazily on first `promote_to_drive`).
4. Create the scratch root `{research_scratch_dir}/{owner_id}/{project_id}/`.
5. Materialize the initial stage rows for the profile's `activated_stages` (see `09`).
6. Return the project handle: `project_id`, drive path, scratch path, profile.

## 6. Resume and crash recovery

- `research_project open`/`resume` rehydrates state from the DB (not from a JSON file).
- On open, the service scans for `ResearchExecution.status == RUNNING` that are older than
  a stale threshold (no heartbeat) and marks them `ABANDONED`, then rolls back the owning
  stage to `READY` — never silently to `COMPLETED`.
- Recovery never skips gates: an aborted EXECUTE requires re-running the Evidence Gate
  before advancing.

## 7. Access control

- Owner and (future) collaborators see a project; all other reads are denied.
- All entity writes assert `project_id` ownership inside the same transaction.
- Drive-backed artifacts inherit the existing `asset_visible_expr` visibility rules.

## 8. Mutations that are never automatic

Changing `research_question`, `design_register`, `sample`, `estimator`, `claim_strength`,
`data_source`, or triggering `publication` after those objects exist **requires an
approved `ResearchApproval`** (see `11`). The agent may propose; it may not execute without
approval.
