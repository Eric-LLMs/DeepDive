# 15 — Cordis Plugin Contract

> **Normative.** The research tool is **one Cordis-style plugin** exposing **six thin
> tools** over a `ResearchService`. The plugin is discovered by `PluginManager.discover`
> (`plugins/research/plugin.py` exporting `PLUGIN`), built by a factory
> `register_research_plugins(manager, ctx, llm)` wired in `apps/api/deps.py` (mirroring
> `register_toolkit_plugins`).

## 1. Capabilities the plugin injects/uses

The factory captures these from the DI `Context` (provided in `deps.py`):

| Capability | Provides | Used for |
|---|---|---|
| `drive` (new) | `DriveService` | `save_artifact`, read asset bytes, visibility |
| `storage` (existing) | object store | scratch/object byte reads |
| `session_factory` (existing) | DB session | repository construction |
| `retrieval` (existing) | RAG retrieve | project/personal context recall |
| `request_user` (ContextVar) | acting user | ownership resolution |
| kernel `run_subagent` | sub-agent dispatch | `research_run` |

`deps.py` additions: `ctx.provide("drive", get_drive_service())`, call
`register_research_plugins(manager, ctx, llm)`, and `kernel.gateway.policy.allow(...)` the
six research tools (resident, no `tool_search` hop).

## 2. The six tools (parameter contracts)

Each is `define_tool(name, description, parameters, output, execute, is_concurrency_safe,
permission)`. Parameters below are OpenAI function-calling JSON Schema `properties`.

### 2.1 `research_project`

```jsonc
{
  "action": { "enum": ["create", "open", "resume", "snapshot", "archive",
                       "inbox_add", "inbox_promote"] },
  // create:
  "title": { "type": "string" },
  "intent": { "type": "object" },            // ResearchIntent
  "method_profile": { "enum": ["literature", "empirical", "theoretical", "qualitative", "mixed"] },
  "output_profile": { "enum": ["memo", "literature_review", "proposal",
                               "research_report", "paper", "replication_report"] },
  // open/resume/snapshot/archive:
  "project_id": { "type": "string", "format": "uuid" },
  "snapshot_tag": { "type": "string" },
  // inbox:
  "kind": { "enum": ["idea", "question", "paper", "dataset", "observation", "hypothesis"] },
  "content": { "type": "string" },
  "source_note": { "type": "string" }
}
```

`create` returns `{project_id, drive_folder, scratch_root, profile}`. `resume` runs crash
recovery (§6 of `03`, §7 of `06`).

### 2.2 `research_artifact`

```jsonc
{
  "action": { "enum": ["read", "write_scratch", "promote_to_drive", "create_version", "diff"] },
  "artifact_id": { "type": "string", "format": "uuid" },
  "version": { "type": "integer" },
  "path": { "type": "string" },              // scratch-relative logical path
  "content": { "type": "string" },           // for write_scratch / create_version
  "artifact_type": { "type": "string" },
  "idempotency_key": { "type": "string" },   // required for write_scratch
  "meta": { "type": "object" }
}
```

Guarantees: idempotent writes; checksum verified on promote; `FROZEN` read-only.

### 2.3 `research_state`

```jsonc
{
  "action": { "enum": ["get_state", "transition_stage", "get_handoff"] },
  "project_id": { "type": "string", "format": "uuid" },
  "stage_id": { "type": "string", "format": "uuid" },
  "target_status": { "enum": ["READY", "IN_PROGRESS", "COMPLETED", "SKIPPED"] }
}
```

`transition_stage` is **request-only**: the service runs Dependency Check → Gate Check →
Approval Check and rejects illegal transitions with a structured reason (`07` §4). The
agent cannot set `BLOCKED`/`PENDING` directly.

### 2.4 `research_evidence`

```jsonc
{
  "action": { "enum": ["record_node", "link_edge", "query_lineage", "invalidate_downstream"] },
  "project_id": { "type": "string", "format": "uuid" },
  // record_node:
  "node_kind": { "enum": ["Question","Hypothesis","Source","Dataset","Variable","Design",
                          "Execution","Analysis","Result","Evidence","Claim","Table",
                          "Figure","Paragraph","Decision","Risk","Gate"] },
  "payload": { "type": "object" },
  // link_edge:
  "source_id": { "type": "string", "format": "uuid" },
  "target_id": { "type": "string", "format": "uuid" },
  "edge_kind": { "enum": ["motivates","uses","transformed_by","produces","supports",
                          "appears_in","invalidates","overrides","derived_from",
                          "generated_by","depends_on","cites","tests"] },
  // invalidate_downstream:
  "node_id": { "type": "string", "format": "uuid" },
  "reason": { "type": "string" }
}
```

Project-scoped only; writes audit rows; regenerates readable projections.

### 2.5 `research_gate`

```jsonc
{
  "action": { "enum": ["check", "explain_failure", "request_override"] },
  "project_id": { "type": "string", "format": "uuid" },
  "gate_name": { "enum": ["DESIGN_GATE", "EVIDENCE_GATE", "CLAIM_GATE", "QUALITY_GATE"] },
  "reason": { "type": "string" }             // for request_override
}
```

Mechanical checks are deterministic (see `10` §3). `request_override` only creates a
`PENDING` approval.

### 2.6 `research_run`

```jsonc
{
  "action": { "enum": ["dispatch_subagent", "execute_sandbox_script", "record"] },
  "project_id": { "type": "string", "format": "uuid" },
  "skill_id": { "type": "string" },
  "inputs": { "type": "object" },            // artifact ids / params
  "script_path": { "type": "string" },       // for execute_sandbox_script
  "environment": { "type": "object" }        // {image, python_version, ...}
}
```

Writes an immutable `ResearchExecution`; enforces write-to-disk rule (`06` §4) and profile
execution mode (`09` §4).

## 3. ResearchService (domain service behind the tools)

The plugin is a thin layer; all logic lives in `ResearchService`:

```python
class ResearchService:
    # lifecycle
    async def create_project(owner, title, intent, method, output) -> ProjectHandle
    async def open_project(project_id, user) / resume_project(project_id, user)
    async def snapshot_project(project_id, tag, user) / archive_project(project_id, user)
    async def inbox_add(user, kind, content, source_note) / inbox_promote(item_id, user)
    # artifact
    async def write_scratch(...) / promote_to_drive(...) / create_version(...) / diff(...)
    # state
    async def get_state(project_id) / transition_stage(request) / get_handoff(stage_id)
    # graph
    async def record_node(...) / link_edge(...) / query_lineage(...) / invalidate_downstream(...)
    # gates
    async def check_gate(project_id, gate_name) -> GateReport
    async def explain_failure(project_id, gate_name) / request_override(...)
    # execution
    async def dispatch_subagent(...) / execute_sandbox_script(...)
```

Every public method asserts project ownership, records audit, and resolves the acting user
from `request_user` — never from client input.

## 4. Plugin shape

```python
PLUGIN = Plugin(
    name="research",
    description="Research OS: governed artifact-first research workflow.",
    tools=[research_project, research_artifact, research_state, research_evidence,
           research_gate, research_run],
    guards=[design_lock_guard, approval_guard],     # monotonic deny-only
    skills=[...],                                    # or rely on skills/ dir
    provides={"research_service": service},          # named service for other plugins
    inject=["drive", "storage", "session_factory", "retrieval"],
)
```

The guard set enforces: no design mutation after design lock, no high-risk mutation without
an `APPROVED` approval, no `transition_stage` to illegal targets.

## 5. Tool invariants

1. Every tool is project-scoped and ownership-checked.
2. Writes are idempotent (idempotency_key) or versioned; no silent overwrite of `FROZEN`.
3. No tool performs LLM judgment — gates are mechanical; judgment lives in skills.
4. `research_run` sandbox execution is blocked when `allowed_execution=reasoning_only`.
5. Tools never set approval/gate statuses; they only create `PENDING` requests.
