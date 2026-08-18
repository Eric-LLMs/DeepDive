# DeepDive Architecture Design

> This document is the single source of truth (SSOT) for DeepDive. Every technical decision,
> module boundary, and deployment topology is governed here.

> **Implementation status:** this document is the SSOT, but not every part is implemented yet.
> The status tables below mark what runs today versus what is designed-only, so it is clear what
> to fill in when extending the business.

## Implementation Status

### Implemented (runs today)

| Area | What exists |
|----|------|
| Vocabulary subdomain | domains / terms / sentences / matches / materials / chunks (6 tables) |
| Hybrid search | pgvector (semantic) + tsvector (keyword) + RRF fusion |
| Agent runtime | `AgentKernel` composition root: cache-boundary `CacheBoundaryAssembler` (3 zones + `snapshot_key`) + deferred-tool `ToolGateway` + dual-track `MemoryService` (PG tsvector/pgvector RRF) + skill catalog + READ-only `Sandbox`, over `ReactLoopAgent` step loop + plugin `ToolRuntime` |
| Retrieval | RAG pipeline (rewrite → recall → RRF → rerank); `in_process` default, gRPC service available |
| Model services | TEI embedding (BGE-M3), Kokoro TTS, LiteLLM gateway (all Docker) |
| Async enrichment | gateway + arq worker split; `jobs` table is the source of truth; frontend polls `GET /jobs/{id}` |
| Session memory | PG-backed `sessions` / `messages` / `session_events` + deferred embed+summary finalize |
| Migrations | numbered SQL files (`migrations/*.sql`) + asyncpg runner (replaces Alembic) |
| Chat | agent loop with tool use, SSE streaming |
| Auth / RBAC | opaque `login_tokens` login credentials (hashed `dd_` user + Tokens-page API tokens; **admin console login is stateless** — signed `cc_` session token, never persisted) + `access_tokens` per-user LLM-key grants + `user_roles` (regular/pro/vip/admin/anonymous) + role quota + `/auth/*` login |
| Per-role LLM channels | `role_credentials` (role ↔ `llm_credentials` N:M); login pins a random active channel to the token, chat routes through it with failover. The Tokens page disables a user's access to a key per (user, channel); a user with no usable key degrades to the anonymous tier (guest quota) instead of losing login |
| Admin console | single-file SPA at `/admin` with 4 modules (Providers / Roles / Users / Tokens): credential/model/routing CRUD, role↔channel bindings, wallet topup, per-user usage + transactions. The Tokens module splits into *LLM Keys* (the per-user key-grant matrix, masked `sk-***` + copy) and *Login Credentials* (who can sign in, each shown as a masked sha256 fingerprint) |
| Billing | `llm_models` (per-1k pricing) + `user_wallets` + `wallet_transactions` + `llm_credentials` + `credential_models`; cost calc + atomic wallet deduct |
| Settings in DB | `app_settings` key/value JSONB (admin credential + LLM provider config + tiers), written by the admin console |
| Guest access | anonymous chat via the `anonymous` role's channels, with per-day Redis limit (`guest_daily_limit`), 429 → prompt login |

### Designed, not yet implemented

| Area | Gap |
|----|------|
| Multi-tenancy isolation | PostgreSQL RLS not enabled (app-level isolation by `user_id` only) |
| Subscriptions | recurring plan billing not created (pay-as-you-go wallet exists) |
| Observability | audit_logs / ai_call_logs / activity_logs / job_logs not created |
| Video / document learning | media → chunks pipeline (ingestion / transcription / chunking) designed only |
| Reranking | in-process cross-encoder exists but disabled (`reranker_model` empty); TEI reranker not wired |
| Edge gateway | Traefik skeleton only; dev runs FastAPI directly on the host |

## 1. Product Positioning

DeepDive is an "AI learning workbench" unified by a single abstraction:

- **Vocabulary learning**: domain vocabulary + example sentences + definitions + TTS + images + star ratings
- **Video / document learning**: media → timestamped/paginated text chunks → searchable, annotatable
- **AI chat assistant**: interactive Q&A with tool use (RAG + vocabulary lookup + sandboxed file/network tools)
- **Unifying principle: everything is a text chunk**

## 2. Tech Stack

| Layer | Choice | Rationale |
|----|------|------|
| Frontend | Vite + React 18 + TypeScript | SPA; talks to the backend via REST/SSE only |
| Edge API | FastAPI | Async, native SSE/WebSocket, auto OpenAPI |
| Internal comms | gRPC (grpcio) + protobuf + buf | retrieval service boundary; HTTP/2, typed contracts |
| Gateway | Traefik v3 | edge HTTP + gRPC routing, one entrypoint |
| Database | PostgreSQL + pgvector + tsvector | one DB for relational + vector + full-text |
| ORM | SQLAlchemy 2.0 (async) | native async |
| Config | pydantic-settings | env vars + type validation |
| Cache / queue | Redis + arq | result caching, async enrichment |
| LLM gateway | LiteLLM Proxy | legacy default client; pinned channels call their provider directly (`llm_credentials.base_url` + `api_key`) |
| Embedding | BGE-M3 (dim 1024) via TEI | multilingual, moderate dim |
| Reranking | BGE-reranker cross-encoder | post-recall precision |
| Retrieval (RAG) | query rewrite → pgvector + tsvector recall → RRF fusion → rerank | hybrid keyword + semantic search |
| Agent | `AgentKernel` (cache-boundary prompt + deferred tool loading + dual-track memory + sandbox) over `ReactLoopAgent` + plugin `ToolRuntime` | controllable, testable, plugin-based |
| MCP | FastMCP | optional external exposure of the tool runtime (`core/infrastructure/mcp.py`) |
| Speech | STT: faster-whisper / Whisper API; TTS: local Kokoro-82M | |

**Language boundary**: backend in Python (AI deps), frontend in TypeScript. The boundary is
API-only: REST/SSE at the edge, gRPC between internal services, HTTP to model services.

## 3. Repository Structure (Monorepo)

```
deepdive/
├── apps/
│   ├── api/                      # package `api` (FastAPI gateway: REST/SSE + job enqueue)
│   │   ├── main.py               # uvicorn apps.api.main:app (REST/SSE endpoints + GET /jobs/{id})
│   │   ├── auth.py               # opaque-token auth (require_admin / require_user + stateless console session signing)
│   │   ├── admin/                # admin console SPA (single-file index.html: Providers / Roles / Users / Tokens)
│   │   ├── deps.py               # DI assembly (capability seam + agent kernel wiring + plugins)
│   │   ├── tools/                # gateway tools, auto-discovered by `_tool.py` modules (rag_search / translate / web_search)
│   │   └── schemas.py            # Pydantic request/response models
│   ├── worker/                   # arq worker (executes async enrichment jobs)
│   │   ├── settings.py           # WorkerSettings (functions / redis / startup clients)
│   │   ├── tasks.py              # tts / image_fetch / explain / generate_media / ... job functions
│   │   └── main.py               # run_worker entrypoint
│   ├── retrieval/                # retrieval service (gRPC), run: python -m apps.retrieval.main
│   │   ├── server.py             # RetrievalService servicer (proto -> RAGPipeline)
│   │   └── main.py               # grpc.aio.server entrypoint
│   ├── web/                      # Vite + React frontend (TS)
│   └── desktop/                  # Electron workbench (file tree + media viewer + chat; proxies API to the backend)
├── packages/
│   ├── agent/                    # package `agent`: kernel + DI + loop + runtime + memory + skills + prompt
│   │   ├── kernel.py             # AgentKernel composition root (core tools + sandbox guard + zone sections)
│   │   ├── system_prompt.py      # PromptZone + CacheBoundaryAssembler (inject / snapshot_key / refresh_dynamic)
│   │   ├── tool_gateway.py       # ToolCatalog + ToolVisibilityPolicy + ToolGateway + tool_search meta-tool
│   │   ├── tool_permissions.py   # ToolPermission (READ/WRITE/NETWORK) + classify_permissions
│   │   ├── sandbox.py            # Sandbox permission gate (default READ-only; ASK w/o approver → deny)
│   │   ├── fs_tools.py           # resident read_file / edit_file / bash (workspace-rooted; escape rejected)
│   │   ├── skills.py             # Skill + SkillRegistry + SkillCatalog + skill meta-tool (lazy load)
│   │   ├── memory/               # base/file (memdir store) + retrieval (RRF fusion) + service (memory tools)
│   │   ├── loop.py               # ReactLoopAgent step loop (gateway-aware, per-step dynamic diff)
│   │   ├── runtime.py            # ToolRuntime lifecycle (pre-execute → guard → execute → post-execute)
│   │   └── plugins/              # plugin manager + built-in tool_audit
│   ├── rag/                      # package `rag`: retrieval DAG + build_pipeline factory
│   ├── core/                     # package `core`: config + domain/application/ports/infrastructure
│   │   └── infrastructure/memory_retrieval.py  # PG tsvector + pgvector session-recall channels
│   └── shared/proto/retrieval/   # generated protobuf/gRPC stubs (import name `retrieval.v1`)
├── data/
│   └── soul.md                   # agent identity persona (STATIC_PREFIX source)
├── migrations/                   # numbered SQL migrations (applied by init_db.py; replaces Alembic)
├── proto/retrieval/v1/retrieval.proto   # RetrievalService contract
├── buf.yaml / buf.gen.yaml             # proto lint / breaking / codegen
├── scripts/gen_proto.sh                # grpc_tools.protoc (or buf) codegen
├── scripts/init_db.py                  # apply migrations/*.sql (same runner as the app lifespan)
├── scripts/setup.sh                    # host setup (venv / deps / proto)
├── scripts/start_desktop.sh            # one-click launch: infra + uvicorn (port 8300) + Electron workbench
├── deploy/
│   ├── traefik/                  # gateway static + dynamic config
│   ├── retrieval/Dockerfile      # retrieval service image
│   ├── worker/Dockerfile         # arq worker image
│   └── litellm/config.yaml
├── tests/                        # pytest (di / jobs / loop / memory / memory_rrf / prompt_engine / rrf / system_prompt / tool_gateway / tool_runtime)
├── .github/workflows/ci.yml      # buf lint + pytest
└── docker-compose.yml            # data + model services + worker + retrieval + traefik
```

> `packages/agent`, `packages/rag`, `packages/core`, and `apps/api` are independent top-level
> packages (import names `agent` / `rag` / `core` / `api`); no nested `deepdive` package layer.
> Generated proto stubs live under `packages/shared/proto` and are imported as
> `retrieval.v1.retrieval_pb2` (a real package on the editable-install path, no `sys.path` hack).

## 4. Layered Architecture (Hexagonal + Capability Seam)

```
apps/api  (FastAPI)             → translation: HTTP/SSE ↔ usecases; injects providers via deps.py
     │
     ▼
packages/core/application       → usecase orchestration (business rules)
     │
     ▼
packages/core/ports             → interfaces (Repository / LLMPort / TTSPort / VectorPort / Retriever)
     │
     ▼
packages/core/infrastructure    → concrete implementations (postgres / openai / tts / pgvector / grpc)
```

- **Technical capability** (horizontal): `agent/`, `rag/`, `infrastructure/{llm,tts,vector,images,mcp,retrieval_grpc}`
- **Business subdomain** (vertical): `vocabulary`, `materials`, `assistant`
- **Dependencies point inward**: `domain`/`application` depend on no framework; `ports` define
  interfaces; `infrastructure` implements them; `apps/api` injects them in `deps.py`.
- **Capability seam**: cross-cutting capabilities (retrieval) are provided by *name* and required
  by *name*; the provider (in-process vs gRPC) is chosen at assembly time, invisible to consumers.

## 5. Agent Module

The `agent` package is the pluggable agent runtime, composed by an **`AgentKernel`** root. The
kernel wires five pieces around a **`ReactLoopAgent`** step loop:

- a **cache-boundary prompt** (`CacheBoundaryAssembler`) — three zones whose stable head is
  byte-identical across requests so the provider reuses its prefix cache;
- **deferred tool loading** (`ToolGateway`) — the prompt carries a compact catalog + the
  `tool_search` meta-tool; full schemas are mounted on demand;
- **dual-track memory** (`MemoryService`) — PG tsvector + pgvector recall fused by RRF, exposed
  as `memory_search` / `memory_save` tools (writes need human confirmation);
- a **skill catalog** — SKILL.md skills advertised as a one-line index, body lazy-loaded via the
  `skill` meta-tool;
- a **read-only sandbox** (`Sandbox`) — a monotonic permission gate (READ / WRITE / NETWORK) that
  denies anything the session lacks permission for.

Beneath the kernel, a Cordis-style **DI state machine** wires plugins into a shared `Context`,
and the append-only session log is an optional collaborator. `AgentKernel.run(...)` mirrors the
`ReactLoopAgent.run` signature, so the API's `/chat` handler is unchanged.

### 5.1 DI state machine — `Context` / `Fiber`

- A `Context` resolves named capabilities lazily via attribute access (`ctx.retrieval` →
  `ctx.resolve("retrieval")`).
- Each plugin is a `Fiber` declaring `inject` (capabilities it needs) and `provides` (what it
  exports). States: `PENDING → LOADING → ACTIVE`; a mount error moves it to `FAILED` rather than
  silently stalling. `DISPOSED` / `UNLOADING` cover teardown.
- `Context.provide(name, value)` registers an external capability (immediately resolvable);
  `Context.plugin(...)` / `Context.service(...)` register fibers.
- `_settle()` is a topological fixpoint: it activates any `PENDING` fiber whose deps are all
  `ACTIVE`, so dependency order falls out of the state machine (replacing the old
  `_drain_pending` loop).
- `Service` is an optional base for class-based providers (`provide`/`inject` + `start`/`stop`).

### 5.2 Agent loop — `ReactLoopAgent`

`run(user_msg, history, …)` fires `agent/session-start`, assembles the prompt, then steps until a
final answer or `max_steps`, closing with `agent/session-end`. Each step is one LLM call
(`AgentLLMPort.chat`) plus execution of any returned tool calls. Concurrency-safe tools are
batched in parallel (`asyncio.gather`, capped by `max_parallel_tool_calls`); the rest run as
serial barriers. A tool whose execution `concludes_turn` stops the loop early. Returns
`AgentResult {messages, final_answer}`.

When built through `AgentKernel`, each step's model-visible tool array comes from
`ToolGateway.visible_schemas(context)` (core resident tools + whatever `tool_search` has mounted +
any scope allowlist, minus the denylist). The prompt's dynamic suffix is re-rendered per step via
`CacheBoundaryAssembler.refresh_dynamic(context)`; if it is unchanged from the previous step the
system message is not resent, and the byte-stable static head is reused as-is.

### 5.3 System prompt — `CacheBoundaryAssembler` (three-zone, cache-boundary)

Sections register with an `order` plus a `zone` and merge ascending within it. The zones are the
**cache-boundary contract**:

| zone | content | stability |
|---|---|---|
| `PromptZone.STATIC_PREFIX` | SOUL.md identity (`data/soul.md`) + compact tool catalog + compressed skill catalog | byte-identical across requests → the provider reuses its prefix cache |
| `PromptZone.PROJECT_CONTEXT` | CLAUDE.md / AGENTS.md project rules | stable per project |
| `PromptZone.DYNAMIC_SUFFIX` | per-step session memory brief + any `inject()` content | re-rendered every step |

`assemble()` returns a `PromptAssembly {static_prefix, project_context, dynamic_suffix, tools,
variables}`; the static/project render is cached, and only the dynamic suffix is recomputed.
`refresh_dynamic(context)` recomputes just that zone for each loop step.

- `inject(text, *, name)` — session-scoped persistent content that survives across steps
  (aligned with `agent.inject()`); cleared on the next `begin_session()`.
- `snapshot_key()` — `sha256(static + project)[:16]`; the observable identity of the stable head,
  making prefix-cache hit rate measurable.
- `render_prompt(assembly)` — joins the zones with a fixed `CACHE_BOUNDARY` marker
  (`"\n\n<CACHE_BOUNDARY/>\n\n"`) between the stable head and the dynamic suffix.

A section's text may be static or an async callable over the assemble context (used for on-demand
memory/skill retrieval). `{{name}}` placeholders interpolate from registered variables. The legacy
flat `SystemPrompt` (no zones, no boundary) still renders for backward compatibility.

### 5.4 Memory, skills, sessions

- **Memory** — dual-track, orchestrated by `MemoryService`:
  - **Long-term file memory** — `MemoryStore` protocol (`load`/`save`/`list`/`search`) +
    `FileMemoryStore` (claude-code memdir style: `MEMORY.md` index + one frontmatter `.md` per
    memory, description-weighted keyword recall). `Memory` records carry a `type` in
    {`user`,`feedback`,`project`,`reference`} and a staleness caveat via `age_days`.
  - **Session memory** — PostgreSQL-backed recall (`core/infrastructure/memory_retrieval.py`):
    `PgKeywordRecaller` (tsvector `to_tsvector('english', text)`, deterministic, no vectors —
    `fts_config` is a constructor param so zhparser/jieba can be swapped in for CJK) +
    `PgVectorRecaller` (pgvector cosine over `messages.embedding`). `RRFMemoryRetriever` fuses the
    two via `rag.rank.rrf.rrf_fusion`; a vector-channel failure degrades to tsvector-only —
    **never a silent empty**.
  - **Memory as tools** — `memory_search` (RRF-fused recall) and `memory_save` (writes require
    `confirmed=True`, a human-confirmation gate). At `begin_session()` the `MEMORY.md` head is
    loaded as the dynamic-suffix session brief.
- **Skills** — `Skill` (Markdown instructions + frontmatter + keywords) registered in a
  `SkillRegistry` (`register` / `relevant(query)` keyword scoring / `from_dir` for `*.skill.md`).
  `SkillCatalog.render()` emits a compressed one-line directory (name + truncated description,
  XML-escaped, within a character budget) into the STATIC_PREFIX; the `skill` meta-tool
  lazy-loads the full SKILL.md body on demand and reports `allowed_tools`.
- **Sessions** — `SessionLog` is an append-only event stream
  (`session-start` / `session-end` / `llm-call` / `tool-call` / `tool-result`), serializable to
  JSONL for audit.

## 6. Tool Runtime

The Agent core implements a plugin-based tool runtime in Python. The essentials:

- a tool is a **typed definition** (`define_tool`), not a bare function;
- the **lifecycle** is a middleware waterfall with **decisions as return values**, not exceptions;
- **monotonic guards** can only deny (never allow);
- **registration is reversible** (every `register`/`guard`/`on` returns a disposer).

### 6.1 Typed tool definition — `define_tool`

```
define_tool(name, description, parameters, output, execute, destructive,
            is_concurrency_safe, permission)
  → ToolDefinition
```

- `permission`: optional explicit `{READ, WRITE, NETWORK}` class; `None` → `classify_permissions`
  infers it (destructive → WRITE, file/network-hinting params → WRITE/NETWORK, else READ).
- `parameters`: JSON Schema for the tool args (OpenAI function-calling format).
- `output = ToolOutput(schema, render)` — the **canonical value** is validated against `schema`
  (`jsonschema`); `render(args, value) -> [ContentBlock]` produces the **model-visible content**.
  The two are deliberately separated (`output.schema` + `output.render`).
- `execute(args, exec)`: the body. The returned `ToolDefinition.execute` wraps it:
  validate args (`ToolArgsError`) → run body → validate output (`ToolOutputError`).

### 6.2 Lifecycle — `ToolRuntime.execute`

```
tools/pre-execute   (waterfall; base = allow)
  ├─ deny → fail fast
  └─ ask  → approval handler (missing → degrade to deny)
guard                (monotonic, deny-only; a reason string blocks)
tools/execute        (waterfall; base = dispatch body)
tools/post-execute   (waterfall; base = accept)
tools/result         (serial observer)
```

- `PreToolDecision = allow | deny(reason) | ask(reason?)` — returned by pre-execute listeners.
- `PostToolDecision = accept | block(feedback)` — returned by post-execute listeners.
- `guard(fn)` where `fn(exec) -> str | None`: returning a reason denies and **cannot** be flipped
  back to allow by a later listener (monotonic).
- `ToolExecutionResult = ToolExecutionSuccess(value, content, meta) | ToolExecutionFailure(error, content)`.
- `EventBus` provides `waterfall` (middleware chain, short-circuit by not calling `next()`),
  `serial`/`emit` (read-only observers), all with disposer-based `on`/`observe`.

### 6.3 Plugins

`Plugin = {name, description, tools, skills, listeners, guards, inject, provides}`. `PluginManager.register` mounts
tools→`ToolRuntime.register`, guards→`ToolRuntime.guard`, listeners→`EventBus.on/observe`,
skills→`SkillRegistry`, collecting disposers so `unregister` rolls back cleanly. Built-in
`tool_audit` demonstrates both deny (pre-execute listener) and guard (monotonic) plus result audit.

### 6.4 What is deliberately *not* implemented

These runtime mechanisms are intentionally out of scope for the Python runtime:
a microkernel (Loader / patch-layer boot), two-queue Inbox,
`AsyncLocalStorage` initiator tracking, Code Mode (`run_code`), and scoped per-agent registration.
DeepDive uses a small `EventBus` + `SessionLog` (append-only session events) and Cordis-style
`Context`/`Fiber` DI instead.

### 6.5 Deferred loading, permissions & sandbox

**`tool_permissions.py`** — `ToolPermission {READ, WRITE, NETWORK}` and
`classify_permissions(defn)`: an explicit `permission` on the `ToolDefinition` wins; otherwise
`destructive` or write-hinting parameters → WRITE, url/http/network hints → NETWORK, else READ.
`ToolDefinition.permissions` is the effective (post-classify) class.

**`tool_gateway.py`** — deferred tool loading for the 1000-tool scaling problem (prompt bloat):
- `ToolCatalog` — a compact `name + blurb` index (no schemas); `render_index()` emits `- name:
  blurb` lines within a character budget (blurbs truncated per-line); `search(query)` does
  word-level scoring over name/blurb/permission tags.
- `ToolVisibilityPolicy` — per-request scope: `allow(name)` / `deny(name)` / `present_as(mode,
  names)`, each returning a disposer for rollback; `deny` beats both `allow` and a mounted tool.
- `ToolGateway` — `core_schemas()` (resident tools: `tool_search` / `skill` / `memory_search` /
  `memory_save`) + `visible_schemas(context)` = core ∪ mounted ∪ scope-allowlist − denylist;
  `mount(name)` pulls a tool's full schema into the visible set after the model asked for it via
  `tool_search`. The mounted set resets per session (`reset_session`).

**`sandbox.py`** — `Sandbox` holds `SandboxRule(permission, decision)` and exposes a monotonic
`guard()` (deny-only) used as the runtime's pre-execute gate: it computes the session's permitted
permissions (default **READ-only**), and any tool requiring WRITE / NETWORK is denied unless the
host granted it or a human approver confirms. `ASK` with no approver degrades to **deny**
(safe-by-default). It composes with `ToolRuntime`'s existing `approval` hook for human gates.

**`fs_tools.py`** — the resident filesystem/shell tools: `read_file` (READ), `edit_file` (WRITE),
`bash` (WRITE + NETWORK). All file access is rooted at `settings.workspace_dir` and path escape is
rejected (`_resolve`). The desktop workbench's "generate media" flow (`/media/generate` → worker
`generate_media`) stays a separate HTTP+job pipeline, not an agent tool.

## 7. Capability Seam (Definition / Provider / Consumer)

```
ports/retrieval.py  Retriever Protocol (retrieve(query, top_k, filters) -> [{id,text,score,meta}])
        ▲                              ▲
        │ ctx.provide("retrieval", …)  │ implement
        │                              │
   deps.py (assembly)          RAGPipeline (in-process)  |  GrpcRetriever (gRPC client)
```

The `rag_search` tool calls `ctx.resolve("retrieval")` (a `Context` capability seam). `deps.py`
registers the concrete provider via `ctx.provide("retrieval", …)` based on `settings.retrieval_mode`:

| `retrieval_mode` | provider | notes |
|---|---|---|
| `in_process` (default) | `RAGPipeline` | full RAG DAG inside the API process |
| `grpc` | `GrpcRetriever` | thin gRPC client → retrieval service |

Switching modes never touches the tool code — it only changes what `ctx.provide()` injects.

## 8. Distributed Topology

```
                         ┌──────────────────────────────────────────────┐
 browser ── HTTP/SSE ──▶ │ Traefik (edge gateway)                        │
                         │   /api/*      → FastAPI gateway (REST/SSE)    │
                         │   retrieval   → retrieval service (gRPC h2c)  │
                         └──────────────────────────────────────────────┘
                                    │                        │
                          REST/SSE  │                        │ gRPC (plaintext HTTP/2)
                                    ▼                        ▼
                         FastAPI gateway (api)         retrieval service (gRPC)
                         - Agent loop                  - RAGPipeline (embed → recall → RRF → rerank)
                         - vocabulary usecases         - owns TEI/pgvector/FTS/rerank/rewrite
                         - enqueues enrichment jobs
                                    │
                                    │  enqueue (arq)
                                    ▼
                                 Redis ─────────────▶ worker (arq)
                                 (queue)              - TTS / image fetch / explain / definition
                                                      - syntax analysis / sentence indexing
                                                      - session finalize (embed + summary)
                                    │
                                    │    HTTP (OpenAI-compatible) to model services:
                                    ├───────────────▶ TEI embedding  (POST /embed)
                                    ├───────────────▶ Kokoro TTS     (/v1/audio/speech)
                                    └───────────────▶ LiteLLM gateway (/v1)
                                    │
                                    │    DB direct (no service in front); jobs table = job state
                                    ▼
                            PostgreSQL (pgvector + tsvector + jobs) via SQLAlchemy+asyncpg
```

- **Model inference never runs in the API/retrieval/worker process** — embedding/TTS are separate
  containers; model updates don't restart the app. Reranking is the exception: the cross-encoder
  loads in-process via `sentence-transformers` when `reranker_model` is set (disabled by default).
- **Pinned LLM channels call their provider directly** — a session's chat request builds a
  per-request OpenAI client from the channel's `base_url`/`api_key` (the shared client is never
  mutated). The LiteLLM gateway (`llm_base_url`, default `:4000`) is used only for roles with no
  bound channel and for the legacy `/config` route / enrichment summaries.
- **DB is accessed directly** (SQLAlchemy + asyncpg) by the gateway, worker, and retrieval
  service; no DB proxy service. Production scaling adds pgBouncer + read replicas.
- **Retrieval is the first extracted service** because it owns the heavy, model-coupled stack;
  the **worker is the second**, moving every enrichment job off the gateway's request path.

### 8.1 Async enrichment (job model)

Enrichment endpoints (`/tts`, `/image-fetch`, `/explain`, `/terms/definition`,
`/sentences/analyze`, `/domains/{id}/sentences/index`) enqueue a job and return `{job_id}`
immediately; the frontend polls `GET /jobs/{id}` until the worker marks the job
`succeeded`/`failed`. The PostgreSQL `jobs` table is the single source of truth for job state
(`queued → running → succeeded | failed`); Redis only carries the work to the worker (arq).
Chat sessions finalize the same way: the gateway flushes session events synchronously on
`close()`, then enqueues `session_finalize` to backfill message embeddings and write the summary.

## 9. Retrieval Service (gRPC)

Contract (`proto/retrieval/v1/retrieval.proto`, `package retrieval.v1`):

```proto
service RetrievalService {
  rpc Retrieve(RetrieveRequest) returns (RetrieveResponse);
  rpc Health(HealthRequest) returns (HealthResponse);
}
message RetrieveRequest { string query = 1; int32 top_k = 2; map<string,string> filters = 3; }
message RetrieveResponse { repeated SearchHit hits = 1; }
message SearchHit { string id = 1; string text = 2; double score = 3; string meta = 4; }
```

- `apps/retrieval/server.py` implements the servicer by delegating to `RAGPipeline`.
- `apps/retrieval/main.py` starts `grpc.aio.server()` on `RETRIEVAL_GRPC_ADDR`.
- `core/infrastructure/retrieval_grpc.py` (`GrpcRetriever`) maps proto `SearchHit` back to dicts.
- Codegen: `scripts/gen_proto.sh` (buf if present, else `grpc_tools.protoc`) → `packages/shared/proto/retrieval/v1/`.

## 10. RAG Module (Config-Node DAG)

Retrieval is a deterministic DAG (declarative node order + parameters), each node independently togglable:

```
rewrite → multi-recall → RRF fusion → rerank
```

- **rewrite** `query_rewrite.py`: multi-query expansion + HyDE (LLM JSON with code-fence fallback).
- **recall** `recall/`: `VectorRecaller` (pgvector cosine) + `KeywordRecaller` (tsvector FTS).
- **fusion** `rank/rrf.py`: Reciprocal Rank Fusion (k=60).
- **rerank** `rank/cross_encoder.py`: BGE-reranker, lazy-loaded, `asyncio.to_thread`.
- **orchestration** `pipeline.py`: `RAGPipeline.retrieve(query, top_k, filters)`.

## 11. Feature → Mechanism Map

| Feature | Mechanism |
|---|---|
| Define a tool | `define_tool(name, parameters, output=ToolOutput(schema, render), execute)` |
| Register / unregister | `ToolRuntime.register` returns a disposer; `PluginManager.unregister` rolls back |
| Intercept before a tool | `tools/pre-execute` waterfall listener returns `PreToolDecision` |
| Irreversibly block a tool | monotonic `ToolRuntime.guard(fn)` returning a reason string |
| Rewrite args / augment result | `tools/post-execute` returns `PostToolDecision.accept(value=..., content=...)` |
| Observe results without blocking | `EventBus.observe("tools/result", ...)` (serial) / `emit` (fire-and-forget) |
| Swap retrieval provider | `Context.provide("retrieval", …)` + `settings.retrieval_mode` |
| Session extension points | `agent/session-start` / `agent/session-end` observers in the loop |
| Stable prompt head for prefix cache | `CacheBoundaryAssembler` zones + `CACHE_BOUNDARY` marker + `snapshot_key()` |
| Load a tool schema on demand | `tool_search` meta-tool → `ToolGateway.mount(name)` |
| Scope tool visibility per request | `ToolVisibilityPolicy` `allow` / `deny` / `present_as` (disposers) |
| Gate a tool by session permission | `Sandbox.guard()` + `ToolPermission` (`classify_permissions`) |
| Recall / write memory as a tool | `memory_search` / `memory_save` (the latter requires `confirmed=True`) |
| Lazy-load a skill body | `skill` meta-tool over `SkillCatalog.render()` compressed index |

## 12. Data Model

> **Migration note:** the schema is defined **only** in `migrations/*.sql` — applied in filename
> order by `init_db()` via asyncpg and tracked in the `schema_migrations` table (no Alembic, no
> `create_all`). This document does not repeat the DDL; the migration files are the single source
> of truth. Table names below are the implemented ones (`sessions`, `messages`, `jobs` …), not the
> earlier design names (`conversations`, `job_logs` …). The current surface spans
> `0001_init.sql` … `0007_console_tokens_stateless.sql`; each file is written as one idempotent unit
> (final shape in a single pass, no repeated ALTERs).

The core learning + chat tables that run today (`migrations/0001_init.sql`):

- **domains** — `id`, `name` (unique), `created_at`.
- **materials** — `id`, `type` (`domain` | `video` | `document`), `title`, `source_url`, `meta` (JSONB), `created_at`.
- **users** — `id`, `created_at`; the auth columns come from `0002_auth.sql` (see §12.3).
- **terms** — `id`, `domain_id` (FK → `domains`), `word`, `definition`, `frequency`, `star_level`, `audio_hash`, `image_paths` (JSONB), `is_active`.
- **sentences** — `id`, `domain_id` (FK → `domains`), `origin_source`, `content_en` (unique), `content_cn`, `audio_hash`, `cn_explanation`, `embedding` (vector(1024)).
- **chunks** — `id`, `material_id` (FK → `materials`), `seq`, `content_en`, `content_cn`, `meta` (JSONB), `embedding` (vector(1024)).
- **sessions** — `id`, `user_id` (FK → `users`), `created_at`, `closed_at`, `summary`.
- **messages** — `id`, `user_id`, `session_id` (FK → `sessions`), `role` (`user` | `assistant` | `tool`), `text`, `embedding` (vector(1024)), `created_at`.
- **session_events** — `id`, `session_id` (FK → `sessions`), `seq`, `type`, `timestamp`, `payload` (JSONB).
- **matches** — `id`, `term_id` (FK → `terms`), `sentence_id` (FK → `sentences`), `cn_explanation`.
- **jobs** — `id`, `type`, `status`, `payload` (JSONB), `result` (JSONB), `error`, `created_at`, `started_at`, `completed_at`.

Multi-tenancy is carried by `user_id` on `sessions` / `messages` and the auth/billing tables;
PostgreSQL RLS is the isolation strategy (see §13). Runtime access is via the SQLAlchemy 2.0
async models in `packages/core/infrastructure/db.py`.

### 12.1 Indexes and Retrieval

Hybrid recall is computed in code, not stored in schema:

- **Keyword (tsvector)** — `to_tsvector('english', …) @@ websearch_to_tsquery` evaluated at query
  time over `chunks.content_en` / `messages.text` (`packages/rag/recall/keyword.py`,
  `packages/core/infrastructure/memory_retrieval.py`); no stored tsvector column.
- **Semantic (pgvector)** — cosine search over the `embedding vector(1024)` columns.
- **Indexes** — the migrations currently define no explicit FTS / vector index; the design
  proposes a GIN index on a generated `fts` column and an ivfflat (or HNSW for large data) index
  on `embedding`.

### 12.2 Billing and Logs

- **Implemented** — the billing surface in `migrations/0004_billing.sql` + `0005_roles_credentials.sql`:
  `llm_credentials`, `llm_models`, `credential_models`, `role_credentials`, `user_wallets`,
  `wallet_transactions` (documented in §12.3).
- **Design-only (not created)** — `subscriptions`, `credit_ledger`, `audit_logs`, `ai_call_logs`,
  `activity_logs`, `job_logs` (job state lives in the implemented `jobs` table).

### 12.3 Implemented auth, RBAC & billing schema

The multi-user + billing surface (`migrations/0002_auth.sql` … `0007_console_tokens_stateless.sql`, plus the
planned `0008` that splits login credentials into `login_tokens`). Fields below mirror the migration DDL exactly;
`TEXT` columns are plain `TEXT`, money is `NUMERIC`, time is `TIMESTAMPTZ`, JSON is `JSONB`.

- **users** — identity + credentials. Columns: `id` (UUID PK), `username` (TEXT, unique where
  non-null — legacy anonymous rows keep it NULL), `password_hash` (TEXT, stdlib pbkdf2),
  `display_name` (TEXT), `is_active` (BOOLEAN, default true), `role_id` (TEXT FK → `user_roles`
  `ON DELETE RESTRICT`, default `'regular'`), `meta` (JSONB, default `{}`), `created_at`
  (TIMESTAMPTZ, default now()), `updated_at` (TIMESTAMPTZ). The flat `tier` column existed in 0002
  and was dropped by 0003 when roles landed.
- **user_roles** — quota + model + feature permissions, the tier definition. Columns: `role_id`
  (TEXT PK), `role_name` (TEXT), `daily_request_limit` (INT, default 50), `monthly_request_limit`
  (INT, default 1500), `daily_token_limit` (BIGINT), `rpm_limit` (INT), `monthly_cost_limit`
  (NUMERIC(12,6)) — each `-1` = unlimited — plus `default_model` (TEXT, empty = the active
  provider's model), `models` (TEXT[], **legacy** allowed-model ids — the routing source is now
  `role_credentials`, this field is kept only for compatibility display), `features` (JSONB, e.g.
  `{"chat": true}`), `is_active` (BOOLEAN, default true), `created_at`. Seeded roles: `regular`
  (50/day), `pro` (500/day), `vip` (−1/unlimited), `admin` (−1/unlimited) from 0003, and
  **`anonymous`** (guest tier, 20/day) from 0005.
- **role_credentials** — N:M binding **role → LLM channel**; this is what decides which provider
  key a role may use (VIP/pro bind expensive channels, `regular` / `anonymous` the cheap ones).
  Columns: `role_id` (TEXT FK → `user_roles` CASCADE), `credential_id` (UUID FK → `llm_credentials`
  CASCADE), `is_active` (BOOLEAN, default true), `created_at`; PK `(role_id, credential_id)`, plus
  an index on `credential_id`.
- **login_tokens** — the **login/API credential** (split out of `access_tokens` by 0008).
  Columns: `id` (UUID PK), `user_id` (UUID FK → `users` CASCADE; NULL = admin/API token),
  `name` (TEXT, human label), `token_hash` (TEXT UNIQUE — sha256 of the raw `dd_` token, shown once
  at mint and **never recoverable**; the admin console shows only a masked fingerprint), `role` (TEXT,
  default `'user'`; `'admin'` or `'user'`), `role_id` (TEXT FK → `user_roles`
  SET NULL, optional quota-role override), `credential_id` (UUID FK → `llm_credentials` SET NULL —
  the channel **pinned for this login**; the session routes through it), `expires_at` (TIMESTAMPTZ),
  `last_used_at` (TIMESTAMPTZ, refreshed on every authenticated request), `is_active` (BOOLEAN,
  default true — **login-credential validity only**), `created_at`. `expires_at` is set at login to
  `access_token_expire_minutes` (**default 7 days**, overridable via the `ACCESS_TOKEN_EXPIRE_MINUTES`
  env var) and is **not extended by per-request use** — once it passes, requests get 401 and the
  user must log in again. Rows are **unique per (user, pinned channel)**: a re-login on the same
  channel rotates `token_hash` and bumps the timestamps in place instead of inserting a new row
  (partial unique indexes `login_tokens_user_credential_uniq` on (user, credential), and
  `login_tokens_user_no_cred_uniq` on (user) where no channel is pinned — moved from the 0006/0007
  `access_tokens` indexes). **Admin console logins are stateless** — `/admin/login` returns a signed
  `cc_` HMAC session token held in the browser and never writes a row; only Tokens-page API tokens
  (hashed `dd_`) and user login tokens are persisted here.
- **access_tokens** — the **per-user LLM-key permission record** (the Tokens page "which key may
  this user use" matrix), no login-credential data. Columns: `id` (UUID PK), `user_id` (UUID FK →
  `users` CASCADE), `credential_id` (UUID FK → `llm_credentials` SET NULL), `is_active` (BOOLEAN,
  default true — **the key-grant switch**: off = this user is banned from this key), `created_at`.
  Unique per (user, credential). A row is created **lazily** the first time a key is assigned to the
  user (at login); the admin flips `is_active` to grant/revoke that key. `token_hash`, `expires_at`,
  `last_used_at`, `role`, `role_id` live on `login_tokens` — nothing here ever blocks a login
  (see §12.4 for the full business logic).
- **app_settings** — server-managed key/value store, the source for data that used to live in
  `.env` / `data/config.json`. Columns: `key` (TEXT PK), `value` (JSONB NOT NULL), `updated_at`.
  Holds the admin credential, LLM provider config, and tier overrides.
- **user_usage_counters** — O(1) quota accounting: atomic UPSERT per (user, period), deliberately
  not Redis and not a `COUNT` over logs. Columns: `user_id` (UUID FK CASCADE), `period_type`
  (TEXT, `'day'` | `'month'`), `period_start` (DATE), `request_count` (BIGINT), `token_count`
  (BIGINT), `updated_at`; PK `(user_id, period_type, period_start)`.
- **user_usage_logs** — append-only per-call audit. Columns: `id` (UUID PK), `user_id` (UUID FK SET
  NULL), `token_id` (UUID FK → `login_tokens` SET NULL), `role_id` (TEXT, snapshot at call time),
  `model_name` (TEXT), `tool` (TEXT), `prompt_tokens` / `completion_tokens` / `total_tokens` (INT,
  default 0; total is denormalized for dashboards), `cost_usd` (NUMERIC(12,6)), `created_at`;
  indexes on `(user_id, created_at)` and `(token_id, created_at)`.
- **llm_credentials** — a provider channel; **one row = one "token"/key** the admin manages.
  Columns: `id` (UUID PK), `name` (TEXT), `base_url` (TEXT), `api_key` (TEXT), `is_active` (BOOLEAN,
  default true — the per-channel availability switch), `created_at`, `updated_at`. A channel's
  displayed price is derived from its `credential_models` routes (single model) or a price range
  (multiple models).
- **llm_models** — virtual model catalog with PAYG pricing. Columns: `id` (UUID PK), `name` (TEXT
  UNIQUE, the virtual model name referenced by roles), `description` (TEXT),
  `prompt_price_per_1k` (NUMERIC(12,6)), `completion_price_per_1k` (NUMERIC(12,6)), `is_active`,
  `created_at`.
- **credential_models** — N:M routing (credential ↔ model): which provider model each credential
  actually serves, with failover priority and load weight. Columns: `credential_id` (UUID FK
  CASCADE), `model_id` (UUID FK CASCADE), `actual_model_name` (TEXT — the provider's model id for
  this credential), `priority` (INT, lower = preferred), `weight` (INT, load-balance weight),
  `prompt_price_per_1k` / `completion_price_per_1k` (NUMERIC(12,6), NULL = inherit `llm_models`
  price), `is_active`; PK `(credential_id, model_id)`. The source of each channel's model list and
  displayed price.
- **user_wallets** — cash wallet, one row per user. Columns: `user_id` (UUID PK FK CASCADE),
  `balance` (NUMERIC(14,6), default 0), `currency` (TEXT, default `'USD'`), `updated_at`.
- **wallet_transactions** — append-only ledger; `balance_after` is a snapshot, never recomputed.
  Columns: `id` (UUID PK), `user_id` (UUID FK CASCADE), `type` (TEXT: `'topup'` | `'llm_consume'` |
  `'refund'` | `'adjustment'`), `amount` (NUMERIC(14,6), +credit / −debit), `balance_after`
  (NUMERIC(14,6)), `description` (TEXT), `meta` (JSONB), `idempotency_key` (TEXT UNIQUE), `created_at`;
  index on `(user_id, created_at)`. Chat usage is priced via `compute_cost` and debited atomically
  (`UPDATE … WHERE balance >= cost`), so insufficient funds never overdraw.

### 12.4 Business logic — per-user LLM-key assignment & the disable (Tokens module)

The Tokens page manages **per-user LLM-key access** — two tables, one concern each. `login_tokens`
is the **login/API credential** (who can sign in); `access_tokens` is the **key-grant matrix**
(which LLM keys this user may use). The admin flips `is_active` on an `access_tokens` (user, key)
row to grant or revoke that key for that user — a key ban, nothing to do with login. The console
presents the two concerns as separate tabs — *LLM Keys* (user / role / model / masked key + copy,
model and user rows link to their detail) and *Login Credentials* (user / role / token fingerprint /
expiry / last login / revoke / delete) — each with its own person search and role filter.

**How a user is assigned an LLM key — the decision in two phases:**

Phase 1 — *at login* (`_pick_credential`; the chosen channel is pinned as
`login_tokens.credential_id` and rides along with the login token):

```
candidates = { role_credentials(role).is_active }     # role → channel bindings, switched on
           ∩ { llm_credentials.is_active }            # the channel itself, switched on
           − { channels with a disabled access_tokens  # per-user Tokens ban
               row for this user }
→ pick one at random and pin it; empty set → nothing pinned (credential_id NULL)
```

Phase 2 — *at chat* (`_resolve_chat_route`, per request):

```
1. pinned channel active AND not banned for the user  → use it directly
2. pinned channel disabled or banned                  → fail over: re-pick from the same
                                                        candidate set (Phase-1 set)
3. no pin (credential_id NULL)                        → pick fresh from the same candidate set
4. candidate set empty                                → anonymous tier: guest_daily_limit +
                                                        anonymous-role keys / legacy route
```

The model served by the chosen channel is its preferred active route (`credential_models`,
lowest `priority`), else the role's `default_model`, else the global default
(`settings.llm_model`).

**Where the pin lives** — `credential_id` is a column on the **`login_tokens` row** (the login
credential), *not* on the chat `sessions` row. Every request presents the token (Bearer header);
the backend loads that token's row and reads `credential_id` from it, so the pinned key rides
along with the login, not with the conversation. A re-login may pin a different key, and
`sessions` / `messages` never store which key served a message.

**Login lifetime & re-pin** — a login token expires after `access_token_expire_minutes`
(default 7 days); use does not extend it. Once it expires the token is dead (401) and the user
logs in again, which re-runs Phase 1 and **re-pins a key** — the new random pick may be the same
channel or a different one, and a key the admin disabled in the meantime is skipped. So: a key is
pinned *per login*, and re-login after expiry is the moment a newly-banned key gets dropped.

The rules that make this safe:

- **Login is never blocked.** Disabling a key only stops that key from being *assigned*; the user
  always signs in and receives a credential. Admin-console logins (`cc_` HMAC session tokens) are
  stateless and never touch these tables at all.
- **Key assignment skips banned keys.** At login `_pick_credential` lists the role's active
  channels and drops every channel the user has a disabled `access_tokens` grant for; one remaining
  channel is picked at random and pinned as `login_tokens.credential_id`. No usable key → nothing
  pinned (`credential_id` NULL) — a plain login token, still issued.
- **Chat never routes through a banned key.** A token pinned to a key whose `access_tokens` grant
  was later disabled fails over to another active channel of the same role the user is allowed. A
  token with no pin picks fresh from the role, again excluding banned keys.
- **No usable key → anonymous tier.** A logged-in user with zero usable keys still chats, but as an
  anonymous guest for that request: the Redis `guest_daily_limit` (default 10/day) is enforced, the
  request routes through the `anonymous` role's keys (or the legacy `/config` client when that role
  has none), and no usage/billing is recorded for it. This is the paywall — the anonymous tier is
  the free, rate-limited base.
- **Restoring access is a re-grant.** The admin re-enables the `access_tokens` row; the next login
  re-pins the key, reusing and rotating the same `login_tokens` (user, channel) row in place (one
  row per (user, channel), enforced by the partial unique indexes), so the table never grows with
  logins.

End-to-end flow for one chat request:

1. `POST /auth/login` — verify user → resolve `user.role_id` → active `role_credentials` ∩ active
   `llm_credentials`, **excluding channels the user has a disabled `access_tokens` grant for** →
   pick one randomly and pin it as `login_tokens.credential_id`. Guests resolve to the `anonymous`
   role instead (per-day Redis `guest_daily_limit`). If every candidate is disabled, nothing is
   pinned — **login still succeeds**.
2. Each chat request — `require_user` loads the `login_tokens` row, so `credential_id` comes along
   for free; the channel's `base_url` / `api_key` drive the model call (resolved per-request; the
   shared client is never mutated), and the model is the channel's preferred active route, else
   `role.default_model`, else the global default.
3. Pinned channel disabled (credential-level `is_active`, or a per-user Tokens ban on that key —
   its `access_tokens` grant is off) → fail over to another active channel of the same role the user
   is not banned from. An `admin` / legacy token without `credential_id` uses the legacy `/config`
   active provider.
4. A logged-in user with **no usable key** (every key banned, or the role has no bindings) degrades
   to the **anonymous tier** for this request: `guest_daily_limit` (Redis) + the `anonymous` role's
   keys / legacy route, with no usage/billing recorded. Full access returns when the admin re-enables
   a key.
5. Billing stays model-catalog-based (`get_model_prices` by model name) — the channel selects *which*
   model/key is used, the per-1k catalog price determines the cost. A degraded (anonymous-tier)
   request is not billed.

**When each table is written** — chat never writes either table; the writes are login-time,
request-time, and admin-only:

- `login_tokens` — written at **login** (insert on first login, or rotate `token_hash` + bump
  `expires_at` / `last_used_at` / `is_active=true` in place on a re-login for the same channel), on
  **every authenticated request** (refresh `last_used_at`), and by **admin** on the Tokens page
  (revoke → `is_active=false`, rename, extend, or delete the row). Expiry / revoke here only stops
  sign-in — it never touches key grants.
- `access_tokens` — written at **login** (lazily insert the (user, key) grant the moment a key is
  first assigned to the user) and by **admin** (flip `is_active` to grant/revoke the key; delete
  the row). Nothing here ever blocks a login.

## 13. Multi-Tenancy and Deployment Strategy

| Scenario | Strategy |
|------|------|
| B2C (multi-user) | Shared DB + PostgreSQL RLS row-level isolation |
| B2B (enterprise) | database-per-tenant |
| Read scaling | read replicas + pgBouncer connection pool |
| Edge vs internal | external REST/SSE via Traefik ↔ internal gRPC |
| Model scaling | separate model services (TEI/Kokoro/LiteLLM), independent scale-out |
