# 16 — Permission Model

> **Normative.** Research tools are gated by DeepDive's existing permission and approval
> machinery; tenancy is enforced by the existing `ContextVar` + visibility predicates.
> Research OS adds **no parallel security layer**.

## 1. Tool permission classes

The six research tools declare `permission` per `ToolPermission` (`READ`/`WRITE`/`NETWORK`):

| Tool | Permission | Notes |
|---|---|---|
| `research_project` | `{READ, WRITE}` | creates rows, folders, scratch |
| `research_artifact` | `{READ, WRITE}` | scratch/drive writes; promote via `save_artifact` |
| `research_state` | `{READ, WRITE}` | transitions are request-only (guarded) |
| `research_evidence` | `{READ, WRITE}` | graph + projections + audit |
| `research_gate` | `{READ, WRITE}` | writes gate rows; `request_override` → PENDING approval |
| `research_run` | `{READ, WRITE}` | sub-agent/sandbox; sandbox needs the session's network grant |

The default session is **READ-only** (existing `Sandbox`). A session/user that has not been
granted `WRITE` gets DENY/ASK for every research tool — consistent with how WRITE tools work
today.

## 2. Sandbox and network

- `execute_sandbox_script` runs via the existing `bash` tool / docker sandbox. The sandbox
  has no network unless the session granted it (existing `bash_sandbox_network` semantics).
- Sandbox execution additionally requires `allowed_execution=sandbox_allowed` from the
  project profile; otherwise the call is rejected structurally.

## 3. Tenancy and isolation

- Acting user = `request_user` ContextVar (`get_request_user_id()`), set per request by the
  `/chat` router. Never from client input.
- All `research_*` queries filter by ownership/project scope; drive-backed content reuses
  `asset_visible_expr` (owner ∪ workspace member ∪ ACL, incl. public links).
- Guests (`request_user is None`) cannot create or access research projects.
- Scratch roots are `{scratch_dir}/{owner_id}/{project_id}` — no cross-tenant filesystem
  collision on the shared agent workspace.

## 4. Approval gating (human-in-the-loop)

- High-risk mutations (the seven classes in `11` §2) pass through the plugin **guards**
  (monotonic deny-only) which require a matching `APPROVED` `ResearchApproval`.
- Approval resolution uses the existing approval bridge (`approvals.py` ASK path + chat
  stream `approval-request` frames + `POST /approvals/{id}`).
- Timeout degrades PENDING → REJECTED (mirroring ASK → DENY); never auto-APPROVE.

## 5. Budget enforcement

- Per-turn cost is already capped by `settings.max_budget_per_turn_usd` (agent loop aborts).
- Project-level `budget_policy` (`max_cost_usd`, `max_steps`, `max_parallel_agents`) is
  enforced by `research_run`: crossing a cap blocks the project and raises an approval
  request before further execution (`03` §4.2, `06` §6).

## 6. Skill scoping

Skills declare `allowed_tools` (frontmatter); the sandbox may scope an allowlist per skill,
so a `reasoning_only` skill cannot invoke `execute_sandbox_script` even if the tool exists.

## 7. Invariants

1. No research action outside the acting user's own projects.
2. No WRITE without an approval/session grant when required.
3. No agent sets approval or gate verdict statuses (only creates `PENDING` requests).
4. No cross-tenant read or write via shared scratch paths.
5. Guests have no research projects.
