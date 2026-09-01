# DeepDive Evolution Roadmap

> **Purpose.** A 3–5 year engineering roadmap for scaling DeepDive from its current
> 1-process, ~15k-line modular monolith toward a multi-tenant, high-concurrency platform with
> third-party plugins. This is **forward design, not current implementation**. Each section
> states what exists today, what the target shape is, and the concrete trigger for doing it —
> so we never pay the cost of a microservice or a sandbox before a real requirement demands it.
>
> Status markers: ✅ implemented · 🔧 partially implemented · 🧭 designed, not yet built

## 0. Three governing principles

| Principle | Meaning | Anti-pattern |
|---|---|---|
| **Modular Monolith First** | Enforce boundaries *inside* one process; split processes only when a real deployment need appears | K8s / service discovery / distributed transactions from day one |
| **Boring Tech, Sharp Seams** | Keep PG / Redis / arq; define precise `Protocol` contracts at the seams so replacing a provider is O(1) | Rolling your own queue or vector store |
| **Tenant Context as Ambient** | Tenant identity flows through the whole stack via `ContextVar`; no layer passes it explicitly, every layer can validate it | `tenant_id` threaded through every function signature |

The current codebase already follows these (DI capability seam `ctx.provide/resolve`, the
hexagonal `packages/core`, the gRPC retrieval seam). The roadmap extends them, it does not
invent a new philosophy.

## 1. Module evolution: monolith → independently deployable domains

### 1.1 Stage map

```text
Stage A (today)            Stage B (6–15 engineers)      Stage C (15+, real isolation need)
──────────────             ────────────────────────      ────────────────────────────────────
Modular monolith           Per-package pyproject          True service extraction
single pyproject.toml      independent package versions   only domains with a deploy need
one process                one process, package-scoped    separate process + gRPC
import-linter not yet      import-linter in CI            retrieval (done) → worker → research
```

Trigger to move A→B: **two people editing the same package in the same week, twice.**
Trigger to move B→C: **a real scaling or isolation driver** (a tenant's research DAG
monopolizing the worker, a separate team owning retrieval).

### 1.2 Package boundary enforcement (Stage B, CI-enforced)

✅ `packages/core` is already hexagonal (`domain/ports/application/infrastructure`);
`packages/agent` depends only on `core.config` (7 files) — verified.

🧭 Add `importlinter.toml` and a CI step (`lint-imports` after pytest). This turns the
de-facto discipline into a hard gate:

```toml
[importlinter]
root_packages = ["core", "agent", "rag", "api", "evidence"]

# agent may not reach into core internals
[[importlinter.contracts]]
name = "agent-decoupled-from-core-infra"
type = "forbidden"
source_modules = ["agent"]
forbidden_modules = ["core.infrastructure", "core.application", "core.domain"]

# core is a kernel: nothing may import from below it
[[importlinter.contracts]]
name = "core-is-kernel"
type = "forbidden"
source_modules = ["core"]
forbidden_modules = ["agent", "rag", "api", "evidence"]

# layering: api → {agent, rag, evidence} → core
[[importlinter.contracts]]
name = "layered-architecture"
type = "layers"
layers = ["api", "agent | rag | evidence", "core"]
```

✅ Already done in this cycle: the worker no longer imports `api.deps`; both apps share the
composition in `apps/api/agent_factory.py`, so the worker process stops dragging in FastAPI.

### 1.3 Plugin lifecycle standardisation

✅ `PluginManager` today: DI resolution (`Context`/`Fiber`), dependency topology, hot reload,
validation.

🧭 Add an explicit lifecycle state machine so a plugin can run migrations, drain, and clean up
predictably — the missing piece for third-party plugins:

```python
class PluginPhase(Enum):
    DISCOVERED = auto(); VALIDATED = auto(); INSTALLING = auto()
    ACTIVE = auto(); DRAINING = auto(); SUSPENDED = auto(); REMOVING = auto(); REMOVED = auto()

class PluginLifecycleHook(Protocol):
    async def on_install(self) -> None: ...    # migrations, seed data, first activation
    async def on_activate(self, ctx) -> None: ...
    async def on_drain(self, timeout_s: float) -> None: ...
    async def on_remove(self) -> None: ...
```

### 1.4 Plugin sandbox & communication (three isolation tiers)

```text
Tier 1 IN_PROCESS   trusted first-party (research, toolkit)   DI + ToolRuntime permission gate   (✅ today)
Tier 2 SUBPROCESS   community plugins                          subprocess + JSON-RPC/MCP stdio    (🧭)
Tier 3 REMOTE       enterprise connectors                      gRPC + mTLS/API key                (✅ gRPC precedent)
```

Tier 2 is a hard boundary: a subprocess plugin gets rlimits/cgroup, no direct DB/Redis, and
talks only through an MCP-over-stdio capability proxy (`plugins/research/plugin.py` already
shows the factory + capability-injection pattern this builds on).

### 1.5 Skill governance

✅ Implemented this cycle: `SkillScopeEnforcer` hard-enforces a skill's `allowed_tools` as a
scoped allowlist (deny outside the union of declared allowlists + a small core meta-tool set).

🧭 Extend the `SkillManifest` with governance fields when third-party skills arrive:

```python
@dataclass(frozen=True)
class SkillManifest:
    name: str; description: str; keywords: list[str]
    allowed_tools: list[str]                # ✅ enforced today
    required_capabilities: list[str]        # 🧭 declared DI capabilities
    max_steps: int = 10                     # 🧭 loop hard cap
    cost_budget_usd: float = 0.50           # 🧭 per-run budget
    tenant_scope: str = "session"           # 🧭 session | workspace | global
```

## 2. Multi-tenancy & data isolation

### 2.1 Four defense layers

| Layer | Mechanism | Status |
|---|---|---|
| 1. Application predicate | automatic `tenant_id`/`owner_id` WHERE injection (`asset_visibility_sql` pattern) | ✅ |
| 2. Row-level security | PG RLS as a *second opinion* if an app-layer bug ever drops a WHERE | 🧭 |
| 3. Storage namespace | object key `/tenant/{sha256_prefix}/{sha256}`, per-tenant quota | 🧭 |
| 4. Vector namespace | metadata filter today → pgvector partition by tenant for big tenants | 🧭 |

Layer 1 stays the primary isolation; layer 2 is defense-in-depth, not a replacement.

### 2.2 TenantContext

🧭 A single ambient identity object replaces ad-hoc `get_request_user_id()` reads:

```python
@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str; user_id: str
    roles: frozenset[str]; permissions: frozenset[str]
    quota_tier: str = "free"
    trace_id: str = ""
    delegated_by: str | None = None          # agent acting on behalf of user

_tenant_ctx: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)
def get_tenant_id() -> str: ...              # raises TenantContextMissing if unset
```

Injected once in API middleware; the worker and every tool read it from the `ContextVar`.

### 2.3 RLS (Layer 2, optional — Stage B)

```sql
ALTER TABLE assets ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_assets ON assets
  USING (owner_id = current_setting('app.user_id')::uuid
         OR workspace_id IN (
           SELECT workspace_id FROM workspace_members
           WHERE user_id = current_setting('app.user_id')::uuid));
-- session var set per-connection in a SQLAlchemy checkout listener
```

## 3. High-concurrency async pipelines

### 3.1 Job system today

✅ arq worker + PostgreSQL job rows, with cancellation / retry / idempotency / ingest lock /
dead-letter / incremental import. Jobs: ingest, generation, research steps, session finalize.

### 3.2 Target: research DAG as a persistent state machine

🧭 The research workflow (`docs/research/07`) is already a DAG of stages with gates. When
deep-research runs longer than a worker lifetime, persist the DAG execution and resume from
checkpoint instead of redoing completed stages:

```text
saga_instances (state machine, persisted)      saga_steps (per-stage)
───────────────                                ────────────────
id UUID PK                                     id UUID PK
project_id FK → research_projects              instance_id FK
state JSONB (stage DAG + gate results)         step_type TEXT   -- discover/frame/evidence/...
current_stage TEXT                             step_state TEXT  -- RUNNING/OK/FAILED/SKIPPED
checkpoint JSONB (produced artifact ids)       retry_count INT
created_at / updated_at                        last_error TEXT
```

Rules:
- **Idempotent stages**: every `research_*` tool already takes `idempotency_key` — reuse it
  so a retried step replays the identical record.
- **Distributed lock**: one lock per project (Redis `SET NX EX`), not per job — a DAG can
  have many steps, but only one executor drives a project's transitions.
- **Backpressure**: worker `max_jobs` + per-tenant concurrency quota; a saturated tenant
  queues instead of starving others.
- **Resume**: worker crash mid-DAG → next run reads `saga_instances`, marks RUNNING steps as
  retryable, and re-enters at the first incomplete stage.

## 4. Observability & evaluation

### 4.1 Evals for retrieval + agent + memory

✅ RAG has a quality loop (Recall@k / Precision@k / MRR / golden set, live preview).
🧭 The two gaps:

| Target | Gap today | Mechanism to add |
|---|---|---|
| RAG | golden set exists, not wired to CI on ingest changes | `scripts/eval_rag.py` → scheduled eval job, regression gate |
| **Memory** | `RRFMemoryRetriever` has **no** metric — recall quality is invisible as history grows | same golden-set machinery over memory queries (hit@k, MRR); snapshot per release |
| **Agent trajectory** | turns are audit-logged but not scored | record plan→tool→outcome per turn; score against a rubric (task success, tool misuse, unnecessary steps, budget burn) |

### 4.2 Tracing & cost audit

✅ `TraceContext` (trace_id/turn_id), `TurnSpan`, per-turn `cost_usd`, JSONL audit trail.
🧭 Converge on OpenTelemetry: emit the existing spans as OTel traces; keep the JSONL trail as
the audit-of-record. Add a token/cost ledger table keyed by `(tenant_id, user_id, trace_id)`
so usage reports become a query over a ledger, not an aggregation over logs.

## 5. Sequencing

| When | Do |
|---|---|
| Now (done this cycle) | allowed_tools enforcement; worker decoupled from `api.deps`; `deep_research` skill; this roadmap |
| When a second engineer touches core | split `db.py` → `models/`, `drive_service.py`/`drive_repositories.py` by domain (P0 hygiene, no behavior change) |
| When deep-research runs > worker lifetime | persistent saga state machine (§3.2) + research artifacts → PG |
| When research conclusions must be recalled | wire `promote_to_drive` → RAG projection end-to-end (already marked RAG_PENDING) |
| When 3rd-party plugins arrive | plugin lifecycle (§1.3) + Tier-2 subprocess sandbox (§1.4) |
| When a real multi-tenant / scaling pain appears | RLS (§2.3), storage/vector namespaces, package-level CI isolation (§1.2) |
| When memory recall quality matters to retention | Memory evals (§4.1) — schedule this before it hurts |
