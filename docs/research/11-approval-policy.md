# 11 — Approval Policy

> **Normative.** Research OS is **human-governed**: agents propose, humans approve.
> No agent may self-approve anything.

## 1. ResearchApproval record

```yaml
approval_id:          uuid
project_id:           uuid
gate_name:            string|null     # for gate overrides
mutation_type:        string|null     # one of the high-risk classes below
status:               PENDING | APPROVED | REJECTED
requester_agent:      string          # the agent/skill that requested
approver_user_id:     uuid|null       # human approver; null while PENDING, set at resolution
decision_reason:      string|null
risk_assessment:      object|null     # impact analysis from the graph (STALE/INVALID scope)
created_at:           datetime
resolved_at:          datetime|null
```

Invariant: an agent may create a `PENDING` record; only a human approver (or the timeout
resolution below) changes status to `APPROVED`/`REJECTED`. While `PENDING`, both
`approver_user_id` and `resolved_at` are `null`; once `APPROVED`/`REJECTED`, both are set.

## 2. High-risk mutation classes (default on)

```text
research_question   — changing the falsifiable question
design              — changing design_register / estimand / identification after design lock
sample              — changing the estimation sample (inclusions/exclusions)
estimator           — replacing the primary estimator or specification
claim_strength      — upgrading a claim's allowed strength
data_source         — adding/removing a source that a claim depends on
publication         — any external submission action
```

The project `approval_policy.require_approval_on` may narrow the list; the default is all
seven. Narrowing is itself a project-level, human-audited change.

## 3. Enforcement path

```text
agent action (tool call / transition)
   ↓
mutation classifier (plugin guard, monotonic deny-only)
   ↓ high-risk?
   ├── no  → allowed
   └── yes → PENDING ResearchApproval
             ↓
        human approval via DeepDive approval bridge (approvals.py ASK path)
             ├── APPROVED → execute
             └── REJECTED / timeout → BLOCKED, no execution, reason recorded
```

Mapping onto DeepDive today:

- The research plugin registers a **monotonic guard** (`ToolRuntime.guard`) that denies
  the six/seven mutation classes once the relevant object exists, unless a matching
  `APPROVED` `ResearchApproval` is present in the request scope.
- `ResearchApproval` resolution is surfaced through the existing human-in-the-loop approval
  bridge (`apps/api/routers/chat.py` + `ApprovalStore` + `POST /approvals/{id}`), so the
  user sees the request inline in the chat stream and answers it there.
- Timeout semantics follow `settings.approval_timeout_seconds`: an unanswered request
  degrades to `REJECTED` (matching the sandbox ASK → DENY degradation), never to APPROVED.

## 4. Design lock

- `research_gate(DESIGN_GATE)` PASS locks the design: after the lock, any change to
  `design_register` / `estimand` / `identification` / `sample` / `estimator` requires an
  `APPROVED` approval.
- The guard is monotonic: once the design is locked, there is no path that silently unlocks
  it or rewrites the register without an approval row.
- Exploratory runs are allowed *as executions that never become the primary evidence*; a
  decision node records them as `exploratory`, and their results cannot upgrade to `VALID`
  primary evidence without a verified re-check.

## 5. Gate overrides

- `request_override(gate_name, reason)` creates a `PENDING` approval only. It never sets a
  gate `OVERRIDE`.
- On `APPROVED`, the gate row becomes `OVERRIDE` with `override_approval_id`; the failing
  checks remain visible.
- Overrides are recorded in `decision_log.md` and surfaced in `FINAL_REPORT.md`.

## 6. Approval invariants

1. Agents create `PENDING`; humans resolve. No self-approval.
2. An unanswered approval degrades to `REJECTED` on timeout, never `APPROVED`.
3. A `REJECTED` mutation leaves the project `BLOCKED` with the reason recorded; the agent
   may propose an alternative, which creates a new approval.
4. Every override and every design-lock change is audited (approval row + decision node).
5. Gate override ≠ evidence override: overriding a gate never reclassifies `INVALID`
   evidence as `VALID`.
6. A `PENDING` approval has `approver_user_id == null` and `resolved_at == null`; a
   resolved approval (`APPROVED`/`REJECTED`) has both set.
