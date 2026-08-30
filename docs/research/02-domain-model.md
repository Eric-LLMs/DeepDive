# 02 — Domain Model

> **Normative.** Behavior not explicitly permitted here is prohibited by default. See
> `00-architecture-overview.md`. This document is the single reference for the eight core
> entities and their database mapping. Per-entity contracts (`03`–`06`, `07`, `09`–`11`)
> supersede this overview where they are more specific.

## 1. The eight core entities

```text
ResearchProject     the domain center — identity, intent, policies, status
ResearchArtifact    first-class versioned artifact with lineage
ResearchGraph       Project-scoped entities + typed links, STALE/INVALID cascade
ResearchExecution   immutable audit record for every run
ResearchStage       DAG-based stage model
ResearchGate        four hard gates (DESIGN / EVIDENCE / CLAIM / QUALITY)
ResearchProfile     Method × Output profile
ResearchApproval    human intervention record
```

## 2. Entity reference (normative fields)

Every entity below is defined precisely in its own contract; this section fixes the field
**names and types** so the whole suite is consistent.

### 2.1 ResearchProject

```yaml
project_id:            uuid          # PK, immutable
owner_id:              uuid          # owning user (never from client input)
title:                 string        # 1..200 chars
status:                enum(CREATED, IN_PROGRESS, BLOCKED, COMPLETED, ARCHIVED)
method_profile:        enum(literature, empirical, theoretical, qualitative, mixed)
output_profile:        enum(memo, literature_review, proposal, research_report, paper, replication_report)
intent:                ResearchIntent
approval_policy:       object        # see 11-approval-policy.md
budget_policy:         object        # max_cost_usd, max_steps, max_parallel_agents
created_at:            datetime
updated_at:            datetime
deleted_at:            datetime|null # soft delete
```

`ResearchIntent`:

```yaml
user_goal:            string        # what the user wants out of this
research_question:    string        # falsifiable question once FRAME runs
target_audience:      string|null
constraints:          string[]      # time, data, scope limits
success_criteria:     string[]      # how the user will judge success
```

### 2.2 ResearchArtifact

```yaml
artifact_id:          uuid          # stable across versions, PK of the logical artifact
version:              int           # >= 1, immutable once written
parent_version:       int|null      # version this was derived from
project_id:           uuid
artifact_type:        enum(corpus, design, dataset, source, analysis, result, table, figure,
                          claim, evidence, manuscript, handoff, report, replication, other)
storage_scope:        enum(scratch, drive)
storage_path:         string        # relative path inside the storage scope
checksum:             string        # sha256 hex of content
generated_by_execution: uuid|null   # ResearchExecution id
derived_from:         uuid[]        # artifact ids
supports_claims:      uuid[]        # claim node ids
status:               enum(DRAFT, VALIDATED, PROMOTED, FROZEN, SUPERSEDED, INVALID)
idempotency_key:      string|null   # unique per producer, dedupes writes
created_by:           uuid          # acting user (or execution)
created_at:           datetime
meta:                 object        # per-type extra metadata (JSONB)
```

Invariant: `(artifact_id, version)` is unique. A `FROZEN` version is read-only; mutations
must create a new version with `parent_version` set.

### 2.3 ResearchGraph

```yaml
# Entity (node)
node_id:              uuid          # PK
project_id:           uuid          # graph is strictly Project-scoped
kind:                 enum(Question, Hypothesis, Source, Dataset, Variable, Design,
                          Execution, Analysis, Result, Evidence, Claim, Table, Figure,
                          Paragraph, Decision, Risk, Gate)
name:                 string
status:               enum(VALID, STALE, INVALID)   # cascade semantics, see 05
data:                 object        # kind-specific payload (JSONB)
artifact_id:          uuid|null     # node backed by an artifact, when any

# Link (edge)
link_id:              uuid          # PK
project_id:           uuid
source_id:            uuid          # node_id
target_id:            uuid          # node_id
kind:                 enum(motivates, uses, transformed_by, produces, supports, appears_in,
                          invalidates, overrides, derived_from, generated_by, depends_on,
                          cites, tests)
```

Invariant: `(source_id, target_id, kind)` unique per project. No cross-project edges.

### 2.4 ResearchExecution

```yaml
execution_id:         uuid          # PK, immutable
project_id:           uuid
stage_id:             uuid|null
skill_id:             string|null
agent_name:           string        # main or sub-agent identity
inputs:               object        # artifact refs / params (JSONB)
outputs:              object        # artifact refs produced (JSONB)
tools_used:           string[]
environment:          object        # {image?, python_version?, ...}
started_at:           datetime
finished_at:          datetime|null
status:               enum(RUNNING, SUCCESS, FAILED, ABANDONED)
cost_usd:             float
error_info:           object|null
parent_execution:     uuid|null
```

Invariant: once terminal (`SUCCESS`/`FAILED`/`ABANDONED`), the record is immutable.

### 2.5 ResearchStage

```yaml
stage_id:             uuid          # PK
project_id:           uuid
name:                 enum(DISCOVER, FRAME, EVIDENCE, DESIGN, EXECUTE, EXPLAIN, WRITE,
                          REVIEW, REPRODUCE, PUBLISH)
status:               enum(PENDING, READY, IN_PROGRESS, BLOCKED, COMPLETED, SKIPPED)
dependencies:         string[]      # stage names that must be COMPLETED/SKIPPED first
activated_by_profile: bool          # whether the current profile requires this stage
latest_handoff_artifact_id: uuid|null
```

### 2.6 ResearchGate

```yaml
gate_id:              uuid          # PK
project_id:           uuid
gate_name:            enum(DESIGN_GATE, EVIDENCE_GATE, CLAIM_GATE, QUALITY_GATE)
status:               enum(PASS, FAIL, OVERRIDE)
checked_at:           datetime|null
checked_by:           string        # tool + skill that ran the mechanical check
evidence_links:       uuid[]        # graph nodes the check was based on
override_approval_id: uuid|null     # set when status == OVERRIDE
meta:                 object
```

### 2.7 ResearchProfile

```yaml
method_profile:       enum(literature, empirical, theoretical, qualitative, mixed)
output_profile:       enum(memo, literature_review, proposal, research_report, paper,
                          replication_report)
activated_stages:     string[]      # stage names
required_gates:       string[]      # gate names that must PASS (or be OVERRIDDEN)
allowed_execution:    enum(reasoning_only, sandbox_allowed)
```

### 2.8 ResearchApproval

```yaml
approval_id:          uuid          # PK
project_id:           uuid
gate_name:            string|null   # when the approval is a gate override
mutation_type:        string|null   # when the approval is a high-risk mutation
status:               enum(PENDING, APPROVED, REJECTED)
requester_agent:      string        # agent that requested
approver_user_id:     uuid|null     # human approver; null while PENDING
decision_reason:      string|null
risk_assessment:      object|null
created_at:           datetime
resolved_at:          datetime|null
```

Invariant: `status == PENDING` ⇒ `approver_user_id is null` and `resolved_at is null`.
`status in (APPROVED, REJECTED)` ⇒ both are non-null. Only a human resolves an approval.

## 3. Relationships

```text
ResearchProject 1 ── n ResearchArtifact
ResearchProject 1 ── n ResearchExecution
ResearchProject 1 ── n ResearchStage
ResearchProject 1 ── n ResearchGate
ResearchProject 1 ── n ResearchApproval
ResearchProject 1 ── 1 ResearchGraph (nodes/links, all Project-scoped)
ResearchProfile   ── selects ──> ResearchStage activation + ResearchGate requirements
ResearchExecution ── produces ──> ResearchArtifact
ResearchArtifact  ── supports ──> Claim node (via ResearchGraph)
```

`workflow_state.json` (or the runtime state projection) is **not** an entity: it is a
cache of `ResearchStage` + `ResearchGate` rows rendered for the agent. The database is the
authoritative source.

## 4. Database mapping (Phase 0.5/1 — proposed `migrations/0014_research_os.sql`)

| Table | Backs | Notes |
|---|---|---|
| `research_projects` | ResearchProject (+ intent, policies as JSONB) | owner-scoped |
| `research_inbox` | Inbox items (idea/question/paper/...) | non-entity support table |
| `research_artifacts` | ResearchArtifact | unique `(artifact_id, version)` |
| `research_entities` | ResearchGraph nodes | kind + status + data JSONB |
| `research_links` | ResearchGraph edges | unique `(source_id, target_id, kind)` |
| `research_sources` | Source verification (sub-entity of Source node) | type + verification_status |
| `research_executions` | ResearchExecution | append-only once terminal |
| `research_stages` | ResearchStage | per-project stage rows |
| `research_gates` | ResearchGate | per-project gate rows |
| `research_approvals` | ResearchApproval | approval audit |

Isolation: every table carries `owner_id`/`project_id`; reads filter by ownership and (for
drive-backed content) reuse `asset_visible_expr`. Guest users (`request_user is None`) get
no research projects.

## 5. Global invariants

1. No entity references a project the acting user cannot access.
2. `ResearchArtifact.status == FROZEN` ⇒ no content mutation without a new version.
3. `ResearchExecution` terminal records are never edited.
4. `ResearchGraph` has no cross-project edges; graph ops are always `WHERE project_id = ?`.
5. A stage may only reach `COMPLETED` through a legal transition (Dependency → Gate →
   Approval, see `07`).
6. Agents never set `ResearchApproval.status`; only a human approver resolves it.
