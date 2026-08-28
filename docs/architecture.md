# DeepDive Architecture Design

> This document is the single source of truth (SSOT) for DeepDive. Every technical decision,
> module boundary, and deployment topology is governed here.

> **Implementation status:** this document is the SSOT, but not every part is implemented yet.
> The status tables below mark what runs today versus what is designed-only, so it is clear what
> to fill in when extending the business.

## Table of Contents

- [Implementation Status](#implementation-status)
  - [Implemented (runs today)](#implemented-runs-today)
  - [Designed, not yet implemented](#designed-not-yet-implemented)
- [1. Product Positioning](#1-product-positioning)
- [2. Tech Stack](#2-tech-stack)
- [3. Repository Structure (Monorepo)](#3-repository-structure-monorepo)
- [4. Layered Architecture (Hexagonal + Capability Seam)](#4-layered-architecture-hexagonal--capability-seam)
- [5. Agent Module](#5-agent-module)
  - [5.1 DI state machine — `Context` / `Fiber`](#51-di-state-machine--context--fiber)
  - [5.2 Agent loop — `ReactLoopAgent`](#52-agent-loop--reactloopagent)
  - [5.3 System prompt — `CacheBoundaryAssembler` (three-zone, cache-boundary)](#53-system-prompt--cacheboundaryassembler-three-zone-cache-boundary)
  - [5.4 Memory, skills, sessions](#54-memory-skills-sessions)
- [6. Tool Runtime](#6-tool-runtime)
  - [6.1 Typed tool definition — `define_tool`](#61-typed-tool-definition--define_tool)
  - [6.2 Lifecycle — `ToolRuntime.execute`](#62-lifecycle--toolruntimeexecute)
  - [6.3 Plugins](#63-plugins)
  - [6.4 What is deliberately *not* implemented](#64-what-is-deliberately-not-implemented)
  - [6.5 Deferred loading, permissions & sandbox](#65-deferred-loading-permissions--sandbox)
- [7. Capability Seam (Definition / Provider / Consumer)](#7-capability-seam-definition--provider--consumer)
- [8. Distributed Topology](#8-distributed-topology)
  - [8.1 Async enrichment (job model)](#81-async-enrichment-job-model)
- [9. Retrieval Service (gRPC)](#9-retrieval-service-grpc)
- [10. RAG Module (Config-Node Pipeline)](#10-rag-module-config-node-pipeline)
  - [10.1 Node contract](#101-node-contract)
  - [10.2 Context blackboard](#102-context-blackboard)
  - [10.3 Registry](#103-registry)
  - [10.4 Configuration](#104-configuration)
  - [10.5 Executor](#105-executor)
  - [10.6 Nodes](#106-nodes)
  - [10.7 Ingest side (runtime-configured chunking)](#107-ingest-side-runtime-configured-chunking)
  - [10.8 Query Repository — multi-source import](#108-query-repository--multi-source-import)
  - [10.9 Quality regression (P0)](#109-quality-regression-p0)
  - [10.10 Admin console](#1010-admin-console)
  - [10.11 Schema](#1011-schema)
- [11. Feature → Mechanism Map](#11-feature--mechanism-map)
- [12. Data Model](#12-data-model)
  - [12.1 Indexes and Retrieval](#121-indexes-and-retrieval)
  - [12.2 Billing and Logs](#122-billing-and-logs)
  - [12.3 Implemented auth, RBAC & billing schema](#123-implemented-auth-rbac--billing-schema)
  - [12.4 Business logic — per-user LLM-key assignment & the disable (Tokens module)](#124-business-logic--per-user-llm-key-assignment--the-disable-tokens-module)
  - [12.5 Session & message deletion](#125-session--message-deletion)
- [13. Multi-Tenancy and Deployment Strategy](#13-multi-tenancy-and-deployment-strategy)
- [14. Cloud Drive Module](#14-cloud-drive-module)
  - [14.1 Database](#141-database)
  - [14.2 Core logic](#142-core-logic)
  - [14.3 Permission management](#143-permission-management)
  - [14.4 REST surface](#144-rest-surface)
  - [14.5 Frontend](#145-frontend)
  - [14.6 Configuration](#146-configuration)
- [15. Desktop Workbench (Electron)](#15-desktop-workbench-electron)
- [16. Prompt Module](#16-prompt-module)
  - [16.1 Goals](#161-goals)
  - [16.2 Three-zone cache-boundary assembly](#162-three-zone-cache-boundary-assembly)
  - [16.3 Rendering and cache identity](#163-rendering-and-cache-identity)
  - [16.4 Project context loader](#164-project-context-loader)
  - [16.5 Compression pipeline](#165-compression-pipeline)
  - [16.6 Deferred tool loading (defer_loading stubs)](#166-deferred-tool-loading-defer_loading-stubs)
  - [16.7 Per-step process](#167-per-step-process)
  - [16.8 Configuration](#168-configuration)

## Implementation Status

### Implemented (runs today)

| Area | What exists |
|----|------|
| Vocabulary subdomain | domains / terms / sentences / matches / materials / chunks (6 tables) |
| Hybrid search | pgvector (semantic) + tsvector (keyword) + RRF fusion |
| Agent runtime | `AgentKernel` composition root: cache-boundary `CacheBoundaryAssembler` (3 zones + `snapshot_key`) + deferred-tool `ToolGateway` + dual-track `MemoryService` (PG tsvector/pgvector RRF) + skill catalog + READ-only `Sandbox`, over `ReactLoopAgent` step loop + plugin `ToolRuntime`; `ReliableLLM` timeout/retry (error taxonomy + cancellation) + per-turn cost budget; HITL approvals (memory / Redis pub-sub broker); `run_subagent` (bounded child turns); `plan` meta-tool; shadow-git checkpoints (`revert_to_checkpoint`); Docker `BashSandbox` backend |
| Retrieval | config-driven node pipeline (rewrite → recall → RRF → rerank, plus optional parent-expand / CRAG nodes; CJK + contextual + parent-child indexing); `in_process` default, gRPC service available (`AuthGuard` token gate / per-peer rate limit / tenant binding); admin RAG console + golden-set eval (Recall@k / Precision@k / MRR); Redis **query cache** (keyed by query/filters/top_k + config + corpus version); `POST /rag/feedback` golden-dataset recorder |
| Query repository | unified multi-source corpus: cloud-drive files (`source_type='file'`) + Learning-Platform sentences/articles (`'learning'`) + chat Q&A pairs / LLM-grouped whole-session imports (`'chat'`); `chunks.asset_id` nullable + `source_type`/`source_id`, source-aware recall (both recallers `LEFT JOIN assets`); PDF tool chain (body text + tables rendered to PNG → vision LLM, per-table skip on failure); admin RAG → **Repository** tab lists non-file chunks with delete |
| Model services | TEI embedding (BGE-M3), Kokoro TTS, LiteLLM gateway (all Docker) |
| Async enrichment | gateway + arq worker split; `jobs` table is the source of truth; frontend polls `GET /jobs/{id}`; daily `session_events` retention cron in `WorkerSettings.cron_jobs`; `run_agent_turn` job reuses the API's `AgentKernel` singleton for scheduled background turns; `toolkit_generate` runs the 5-stage toolkit pipeline (file mode → workspace output; session / cloud-file modes → caller's Cloud Drive, with a custom `prompt` + `name`) |
| Session memory | PG-backed `sessions` / `messages` / `session_events` + deferred embed+summary finalize + trigger-gated proactive recall (Lane-1 brief always on) + RRF recency weighting + importance-weighted file recall + supersede-in-place user directives + hierarchical history compaction (L2 coarse recap + L1 summary at `/chat`) + 30-day audit-event retention |
| Migrations | numbered SQL files (`migrations/*.sql`) + asyncpg runner (replaces Alembic) |
| Chat | agent loop with tool use, SSE streaming |
| Auth / RBAC | opaque `login_tokens` login credentials (hashed `dd_` user + Tokens-page API tokens; **admin console login is stateless** — signed `cc_` session token, never persisted) + `access_tokens` per-user LLM-key grants + `user_roles` (regular/pro/vip/admin/anonymous) + role quota + `/auth/*` login + **self-service accounts** (`/auth/register` with an email-verification gate, `/auth/forgot-password` + `/auth/reset-password`, editable `/auth/me` profile with avatar upload). Auth endpoints are Redis **rate-limited per client IP** (login/register/recovery, fixed window, fail-open); `enforce_secure_secrets` fails fast at startup when the legacy `JWT_SECRET` default is untouched |
| Per-role LLM channels | `role_credentials` (role ↔ `llm_credentials` N:M); login pins a random active channel to the token, chat routes through it with failover. The Tokens page disables a user's access to a key per (user, channel); a user with no usable key degrades to the anonymous tier (guest quota) instead of losing login |
| Admin console | single-file SPA at `/admin` with 5 modules (Providers / Roles / Users / Tokens / **Tools config**): credential/model/routing CRUD, role↔channel bindings, wallet topup, per-user usage + transactions. The Tokens module splits into *LLM Keys* (the per-user key-grant matrix, masked `sk-***` + copy) and *Login Credentials* (who can sign in, each shown as a masked sha256 fingerprint). The **Tools config** module edits the generic `tools` namespace (web-search provider, SMTP, free-form key/value params) with a one-click *Test email*; the Chat Test user picker is a fuzzy-autocomplete text box; a **RAG** module adds live pipeline testing (per-node trace), chunking preview, node-topology editing, and golden-set eval |
| Billing | `llm_models` (per-1k pricing) + `user_wallets` + `wallet_transactions` + `llm_credentials` + `credential_models`; cost calc + atomic wallet deduct |
| Settings in DB | `app_settings` key/value JSONB (admin credential + LLM provider config + tiers + the generic `tools` namespace for web search / SMTP + the `rag` pipeline config), written by the admin console |
| Cloud drive | per-user My Drive + shared workspaces: `global_objects` (SHA-256 dedup, ref-counted physical store) + logical `assets` + first-class `folders` (per scope) + `workspace_members` roles + `asset_acl` sharing + chunked `upload_sessions` + RAG `chunks` + no-FK `workspace_activity` audit; trash with 30-day lazy retention; roles owner > admin > editor > viewer; **text-note editing** (`GET/PUT /files/{id}/content` — read/in-place overwrite with re-dedup + RAG re-index), **collision-safe naming** (`name (1)` auto-suffix across files+folders), and **personal-scope `user_id` filtering** on every My Drive folder/asset operation; full file manager with an in-page Markdown note editor in the web console (see §14) that also previews Mermaid mind-map (`.mmd`) notes as an SVG tree |
| Guest access | anonymous chat via the `anonymous` role's channels, with per-day Redis limit (`guest_daily_limit`), 429 → prompt login |

### Designed, not yet implemented

| Area | Gap |
|----|------|
| Subscriptions | recurring plan billing not created (pay-as-you-go wallet exists) |
| Observability | audit_logs / ai_call_logs / activity_logs / job_logs not created |
| Video / document learning | media → chunks pipeline (ingestion / transcription / chunking) designed only |
| Reranking | in-process cross-encoder exists but disabled (`reranker_model` empty); TEI reranker not wired |
| GraphRAG node | graph-of-communities `graph_rag` node (LLM entity/relation extraction → community summaries → global/local search) designed only, see §10.6 |
| Edge gateway | Traefik skeleton only; dev runs FastAPI directly on the host |
| Retrieval-feedback UI | `POST /rag/feedback` + `rag_feedback` table exist (tests green), but no workbench/web UI calls the endpoint yet |

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
| Retrieval (RAG) | config-driven node pipeline: query rewrite → pgvector + tsvector recall → RRF fusion → rerank, plus optional parent-expand / CRAG nodes | hybrid keyword + semantic search; CJK queries segmented with jieba; topology + params editable live in the admin console |
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
│   │   ├── main.py               # uvicorn apps.api.main:app (composition root: lifespan, app, CORS, static mounts)
│   │   ├── auth.py               # opaque-token auth (require_admin / require_user + stateless console session signing)
│   │   ├── account_email.py      # email / verification helpers shared by auth + config routes
│   │   ├── admin/                # admin console SPA (single-file index.html: Providers / Roles / Users / Tokens / Tools / RAG)
│   │   ├── deps.py               # DI assembly (capability seam + agent kernel wiring + plugins)
│   │   ├── routers/              # functional routers: drive (files/folders/trash/users/workspaces), auth, admin, rag_admin, config, vocab, chat, sessions, jobs
│   │   ├── tools/                # gateway tools, auto-discovered by `_tool.py` modules (rag_search / translate / web_search)
│   │   └── schemas.py            # Pydantic request/response models
│   ├── worker/                   # arq worker (executes async enrichment jobs)
│   │   ├── settings.py           # WorkerSettings (functions / redis / startup clients)
│   │   ├── tasks.py              # tts / image_fetch / explain / generate_media / toolkit_generate / ... job functions
│   │   └── main.py               # run_worker entrypoint
│   ├── retrieval/                # retrieval service (gRPC), run: python -m apps.retrieval.main
│   │   ├── server.py             # RetrievalService servicer (proto -> RAGPipeline)
│   │   └── main.py               # grpc.aio.server entrypoint
│   ├── web/                      # Vite + React frontend (TS)
│   └── desktop/                  # Electron workbench (file tree + media viewer + chat; proxies API to the backend)
├── packages/
│   ├── agent/                    # package `agent`: kernel + DI + loop + runtime + memory + skills + prompt
│   │   ├── engine/               # Agent Kernel: kernel.py (AgentKernel composition root) + loop.py (ReactLoopAgent step loop) + loop_guard.py + context.py (AgentTurn) + decisions.py + runtime.py (ToolRuntime lifecycle) + events.py + sessions.py + telemetry.py
│   │   ├── prompt/               # system_prompt.py: PromptZone + CacheBoundaryAssembler (inject / snapshot_key / refresh_dynamic)
│   │   ├── tools/                # definition.py (ToolDefinition / define_tool) + tool_gateway.py (ToolCatalog + ToolGateway + tool_search) + tool_permissions.py (READ/WRITE/NETWORK) + fs_tools.py (read_file / edit_file / bash) + bash_sandbox.py + subagent.py + plan_tool.py + checkpoints.py + project_context.py
│   │   ├── skills/               # registry.py: Skill + SkillRegistry + SkillCatalog + skill meta-tool (lazy load)
│   │   ├── security/             # sandbox.py (permission gate: default READ-only; ASK w/o approver → deny) + approvals.py (HITL approval broker)
│   │   ├── llm/                  # llm_guard.py (ReliableLLM timeout/retry) + llm_errors.py (error taxonomy)
│   │   ├── memory/               # base/file (memdir store) + retrieval (RRF fusion) + service (memory tools)
│   │   ├── di.py                 # Cordis-style DI (Context / Fiber / Service)
│   │   ├── harness.py            # FakeLLM / assistant / tool_call test harness
│   │   ├── frontmatter.py        # SKILL.md frontmatter parser
│   │   └── plugins/              # plugin manager + built-in tool_audit
│   ├── rag/                      # package `rag`: config-driven retrieval pipeline
│   │   ├── pipeline/             # executor.py (enabled-node list = topology; degrade, never stop) + factory.py + pipeline_config.py (RagPipelineConfig / NodeConfig / ChunkingConfig) + context.py (PipelineContext blackboard + NodeTrace) + registry.py (node registry, name → class)
│   │   ├── query/                # query_rewrite.py (QueryRewriter) + cjk.py (jieba segmentation for the CJK keyword channel)
│   │   ├── nodes/                # pluggable pipeline nodes (one file per stage)
│   │   ├── recall/               # VectorRecaller (pgvector) + KeywordRecaller (tsvector / CJK)
│   │   ├── rank/                 # rrf_fusion + CrossEncoderReranker
│   │   ├── types.py              # shared pipeline types (SearchHit / ...)
│   │   ├── config_store.py       # app_settings["rag"] persistence + validation + cache
│   │   ├── eval.py               # golden-set regression (Recall@k / Precision@k / MRR)
│   │   └── query_cache.py        # retrieval response cache
│   ├── core/                     # package `core`: config + domain/application/ports/infrastructure
│   │   ├── infrastructure/mailer.py            # stdlib smtplib emailer (verification / reset / test)
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

> The tree is illustrative, not an exhaustive inventory: each directory shows a few
> representative files and the rest are collapsed — `apps/desktop`, `apps/web/src`,
> `packages/core/{domain,ports,infrastructure}`, `migrations/*.sql` and `tests/` account for
> most of the omitted files. `git ls-files` is the authoritative list.

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
  `tool_search` meta-tool; matched tools mount into the visible array as stable `name +
  description` stubs (defer_loading style), full schemas riding in the search result;
- **dual-track memory** (`MemoryService`) — PG tsvector + pgvector recall fused by RRF, exposed
  as `memory_search` / `memory_save` tools (guardrailed, READ-classified);
- a **skill catalog** — SKILL.md skills advertised as a one-line index, body lazy-loaded via the
  `skill` meta-tool;
- a **read-only sandbox** (`Sandbox`) — a monotonic permission gate (READ / WRITE / NETWORK) that
  denies anything the session lacks permission for.

Beneath the kernel, a Cordis-style **DI state machine** wires plugins into a shared `Context`,
and the append-only session log is an optional collaborator. `AgentKernel.run(...)` and
`AgentKernel.run_stream(...)` mirror the `ReactLoopAgent` signatures, so the API's `/chat` and
`/chat/stream` handlers stay thin.

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

`run_stream(...)` is the streaming twin: the same pipeline (prompt assembly, tool dispatch,
session-start/session-end hooks, persistent session memory) with the LLM call streamed. It yields
per-step events so a client renders the model's reasoning and answer incrementally —
`{"type": "thinking", "data"}` reasoning fragments and `{"type": "content", "data"}` answer
fragments as they arrive, `{"type": "tool", "data"}` before each tool dispatch, a
`step-answer` boundary after a step's final answer, and a terminal
`{"type": "done", "data": {answer, messages, usage}}`. If the generator is abandoned (client
disconnect) the `finally` block still closes the session memory, so nothing leaks.

The API's `POST /chat/stream` (SSE, `EventSourceResponse`) consumes `run_stream` through the
**same auth / quota / session / history path as `/chat`** — anonymous guest fallback and quota
checks, session creation (`create_session`), `compact_history`, deferred `session_finalize`
enqueue, and usage logging. It also resolves the turn's `user_message_id` / `assistant_message_id`
so the client can act on a single message, and it emits a `notice` event when a user with no
usable LLM key is degraded to the anonymous tier. Reasoning (`thinking`) is streamed but **never
persisted**, keeping the recall corpus clean.

When built through `AgentKernel`, each step's model-visible tool array comes from
`ToolGateway.visible_schemas(context)` (core resident tools + whatever `tool_search` has mounted +
any scope allowlist, minus the denylist). Stable tools (core + allowlisted) carry full schemas;
mounted tools appear as **deferred-loading stubs** (name + rich description, empty parameter
shape) so the cached array stays small and byte-stable across steps — the full parameter schema
reaches the model through the `tool_search` result instead. Each LLM call also applies a
**per-message snip** (`settings.prompt_message_max_chars`) to the request snapshot only; the
persistence copy keeps full content. The prompt's dynamic suffix is re-rendered per step via
`CacheBoundaryAssembler.refresh_dynamic(context)`; if it is unchanged from the previous step the
system message is not resent, and the byte-stable static head is reused as-is.

The API's `POST /chat/stream` forwards the agent's events **verbatim** as SSE `data:` lines, so the
wire protocol is exactly the stream above (`thinking` / `content` / `tool` fragments, a final
`done`) plus a verbatim **approval frame** when the agent blocks on human-in-the-loop approval.
The approval frame can never deadlock the stream: a sibling *pump* task consumes `run_stream` into an
unbounded `asyncio.Queue`, and the `ApprovalStore` sink pushes approval frames into the **same queue**,
so the generator only ever reads from the queue (a plain `async for` would stall awaiting
`POST /approvals/{id}`). On client disconnect the pump is cancelled and the loop's `except
CancelledError` logs `turn-cancelled`; a `done` sentinel guarantees the generator terminates even on
cancellation.

### 5.3 System prompt — `CacheBoundaryAssembler` (three-zone, cache-boundary)

Sections register with an `order` plus a `zone` and merge ascending within it. The zones are the
**cache-boundary contract**:

| zone | content | stability |
|---|---|---|
| `PromptZone.STATIC_PREFIX` | SOUL.md identity (`data/soul.md`) + compact tool catalog + compressed skill catalog | byte-identical across requests → the provider reuses its prefix cache |
| `PromptZone.PROJECT_CONTEXT` | the first existing `DEEPDIVE.md` under `settings.workspace_dir` (read by `read_project_context`, capped at `settings.project_context_max_chars`) | stable per project; empty when absent |
| `PromptZone.DYNAMIC_SUFFIX` | per-step session memory brief + any `inject()` content | re-rendered every step |

`assemble()` returns a `PromptAssembly {static_prefix, project_context, dynamic_suffix, tools,
variables}`; the static/project render is cached, and only the dynamic suffix is recomputed.
`refresh_dynamic(context)` recomputes just that zone for each loop step.

- `inject(text, *, name)` — session-scoped persistent content that survives across steps
  (aligned with `agent.inject()`); cleared on the next `begin_session()`.
- `snapshot_key()` — `sha256(static + project)[:16]`; the observable identity of the stable head,
  making prefix-cache hit rate measurable.
- `render_prompt(assembly)` — joins the stable head and the dynamic suffix with plain newlines.
  The `CACHE_BOUNDARY` marker (`"\n\n<CACHE_BOUNDARY/>\n\n"`) is an **internal-only separator**:
  it marks the token-position split for the prefix cache but is deliberately **never rendered**
  into the prompt, so the model never sees the literal.

A section's text may be static or an async callable over the assemble context (used for on-demand
memory/skill retrieval). `{{name}}` placeholders interpolate from registered variables. The legacy
flat `SystemPrompt` (no zones, no boundary) still renders for backward compatibility.

The **project context loader** (`agent/tools/project_context.py::read_project_context`) reads the first
existing convention file (`DEEPDIVE.md`) under the agent's workspace and
caps it at `settings.project_context_max_chars`; the kernel registers it into
`PromptZone.PROJECT_CONTEXT`, so project rules become part of `snapshot_key`'s cache identity and
reach the model on every turn. When no convention file exists the zone renders nothing, keeping the
prompt byte-identical to the no-context case.

The **compression pipeline** bounds the prompt at two levels: per-message **snip** — the loop caps
each message's content to `settings.prompt_message_max_chars` when building the LLM request (the
persistence copy stays raw); and **token-aware autocompact** — `compact_history` also fires on a
total-window character budget (`settings.prompt_max_chars`), so a few oversized messages trigger
compaction even below the message-count threshold.

The README's *Prompt* diagram ([mermaid source](../README.md)) visualizes the same contract end to
end — input compaction, the three cache-boundary zones feeding a stable head, `render_prompt`
producing the snapshot key, and the per-step process (snip → LLM request → `refresh_dynamic` /
visible-tool stubs).

### 5.4 Memory, skills, sessions

- **Memory** — dual-track, orchestrated by `MemoryService`:
  - **Long-term file memory** — `MemoryStore` protocol (`load`/`save`/`list`/`search`) +
    `FileMemoryStore` (claude-code memdir style: `MEMORY.md` index + one frontmatter `.md` per
    memory). `Memory` records carry a `type` in {`user`,`feedback`,`project`,`reference`},
    a **staleness caveat** via `age_days`, an **`importance`** score (1–10, default 5), and a
    **`status`** (`active` | `superseded`) with **`supersedes`** linking a replacement to its
    predecessor. Keyword recall is **importance-weighted** (`points × importance`), so curated
    high-salience notes surface ahead of incidental ones. `user`-type memories are the directive
    user model (write imperative `Always …` / `Never …` / `Prefer …` lines); saving with
    `supersedes` marks the old memory `status: superseded`, drops it from the index and recall,
    and keeps the file on disk as an audit trail — new values **supersede in place**, so a stale
    preference never resurface alongside its replacement. Files written before these fields
    existed parse with defaults.
  - **Session memory** — PostgreSQL-backed recall (`core/infrastructure/memory_retrieval.py`):
    `PgKeywordRecaller` (tsvector `to_tsvector('english', text)`, deterministic, no vectors —
    `fts_config` is a constructor param so zhparser/jieba can be swapped in for CJK) +
    `PgVectorRecaller` (pgvector cosine over `messages.embedding`). `RRFMemoryRetriever` fuses the
    two via `rag.rank.rrf.rrf_fusion`; a vector-channel failure degrades to tsvector-only —
    **never a silent empty**. The fused result is **recency-weighted**: an exponential decay over
    `messages.created_at` (recent ≈ 1.0×, 30 days ≈ 0.68×, 90 days ≈ 0.55×) re-sorts near-ties
    toward newer messages, so RRF structures the base ranking and recency breaks ties.
  - **Memory as tools** — `memory_search` (RRF-fused recall) and `memory_save` (guardrailed
    note-writing: kebab-case key, non-empty content capped at `MEMORY_NOTE_MAX_CHARS`, `type`
    restricted to the closed taxonomy, `importance` clamped to 1–10, optional `supersedes`;
    READ-classified so it writes only the local memdir without weakening the session's READ-only
    posture). At `begin_session()` the `MEMORY.md` head is loaded as the dynamic-suffix session
    brief (Lane-1, always on), and **proactive recall** (Lane-2) injects the top
    `MEMORY_RECALL_TOP_K` hits for the user's message into the suffix (computed once per run).
    Lane-2 is **gate-controlled**: `MemoryService.should_recall` is a cheap lexical prefilter
    (memory-seeking trigger words in `MEMORY_RECALL_TRIGGER_WORDS`, or queries at/below
    `MEMORY_RECALL_MIN_LEN` chars that are elliptical) — the expensive RRF query only runs on
    memory-seeking turns; every turn still gets the always-on Lane-1 brief.
- **Skills** — `Skill` (Markdown instructions + frontmatter + keywords) registered in a
  `SkillRegistry` (`register` / `relevant(query)` keyword scoring / `from_dir` for `*.skill.md`).
  `SkillCatalog.render()` emits a compressed one-line directory (name + truncated description,
  XML-escaped, within a character budget) into the STATIC_PREFIX; the `skill` meta-tool
  lazy-loads the full SKILL.md body on demand and reports `allowed_tools`.
- **Sessions** — `SessionLog` is an append-only event stream
  (`session-start` / `session-end` / `llm-call` / `tool-call` / `tool-result`), serializable to
  JSONL for audit. Long conversations are **compacted** at the `/chat` boundary: histories over
  `HISTORY_MAX_MESSAGES` are truncated to the recent `HISTORY_KEEP_MESSAGES`, the dropped prefix
  is folded into a conversation summary (reusing the session summary when one exists, otherwise a
  synchronous LLM summary via the same prompt as `finalize_session`), injected as a leading
  system message, and recorded as a `compaction` session event whose payload **persists the
  summary**. The injected recap is **hierarchical**: coarse summaries of prior compaction windows
  (each truncated, up to the latest 5) render as `## Earlier conversation (coarse)` ahead of the
  current window's `## Conversation summary` — L0 raw messages are never deleted, L1 is the
  latest window, L2 is the coarse prior-window recap, and L3 is the cross-session file memory —
  so distant turns are remembered in broad strokes while the token window stays flat. If the
  summary call fails the overflow is still dropped — a bounded window is preferred to an
  unbounded request. A fresh session is also **auto-titled** at creation from the first user
  message
  (`create_session`, `sessions.title`, 40-char cap). `GET /sessions?q=` filters a user's sessions
  by **content** — a case-insensitive `ILIKE` over title, summary, and message text
  (`list_sessions` outer-joins `messages` and de-dups) — and each result carries a `snippet`
  (the earliest matching message, truncated to 500 chars) so the client can show exactly where the
  match landed even when it is not in the title.

### 5.5 Reliability & per-turn budget

Every agent LLM call goes through :class:`~agent.llm.llm_guard.ReliableLLM` (wired once on the
kernel, wrapping any ``AgentLLMPort``):

- **hard timeout** — ``asyncio.wait_for`` (``settings.llm_timeout_seconds``, default 90 s); a hung
  provider stalls the turn no longer. For streams the timeout bounds only the *first token*; once
  deltas are flowing they forward untouched, so mid-stream failures surface to the loop as a step error.
- **retry with exponential backoff** — tenacity retries only *temporary* errors
  (``LLMTemporaryError``: timeout, 429, 5xx, connection hiccups) up to ``max_retries`` with
  exponential backoff capped at 15 s; fatal errors surface immediately.
- **error taxonomy** (:mod:`~agent.llm.llm_errors`) — ``classify`` maps any exception to
  ``LLMTemporaryError`` (retryable) or ``LLMFatalError`` (auth, bad request, unknown) without
  hard-coding an SDK; base-exception control flow (``CancelledError`` / ``KeyboardInterrupt`` /
  ``SystemExit``) is passed through unchanged and **never caught by a retry loop**.
- **cancellation** — an SSE disconnect aborts the underlying request at once; the loop logs
  ``turn-cancelled``, closes session memory, and re-raises, so a dropped client never leaks state.

The loop also enforces a **hard per-turn budget** (:class:`AgentTurn.max_budget_usd`, default
``settings.max_budget_per_turn_usd``). Each step accumulates ``usage`` on the turn; after the step
``estimate_cost_usd(turn.usage, model)`` prices it and `_budget_exceeded` aborts the loop once the
accumulated cost crosses the cap (an ``error`` event is streamed on the SSE path). Cost, usage, and
budget all live on the per-turn object — concurrent turns never share accounting.

### 5.6 Human-in-the-loop: approvals, subagents, plan mode, checkpoints

**Approvals** (:mod:`~agent.security.approvals`) — when the sandbox gates a tool to **ASK**, the
runtime calls the process-global :class:`ApprovalBridge` wired as ``ToolRuntime(approval=bridge)``.
The bridge reads the per-request :class:`ApprovalStore` bound to the current task via a contextvar
(so concurrent requests never share approval state), emits an ``approval-request`` SSE event with the
tool name / arguments / reason, and blocks on a decision future until
``settings.approval_timeout_seconds`` (timeout → deny). ``POST /approvals/{id}`` resolves it. The
:class:`ApprovalBroker` is **distributed**: state lives in Redis and resolutions wake the pending SSE
across nodes via Redis Pub/Sub; :class:`MemoryApprovalBroker` is the single-process dev/tests
fallback. A tool that ASKs with no approver bound **degrades to deny** — safe by default.

**Subagents** (:mod:`~agent.tools.subagent`) — the ``run_subagent`` tool spawns a *bounded child
turn*: a fresh ``AgentTurn`` (empty history) on the same runtime but with a filtered tool schema —
no recursive ``run_subagent`` and no parent meta-tools (``tool_search`` / ``plan`` /
``revert_to_checkpoint``) — capped at ``SUBAGENT_MAX_STEPS`` steps and ``max_subagent_depth``
nesting (tracked in a task-local ``ContextVar``). The child runs in its own task with a copied
context, so nested subagents on the shared kernel never interfere with the parent turn or each
other; the child's final answer returns as the tool result and its messages are discarded.

**Plan mode** (:mod:`~agent.tools.plan_tool`) — the ``plan`` meta-tool lets the model present an
explicit multi-step breakdown before acting: it emits a ``{"type": "plan", "data": {goal, steps}}``
event to the turn's progress sink (streamed as an SSE frame) and returns a confirmation. The loop
needs no change — ``plan`` is just another tool the model may choose.

**Checkpoints** (:mod:`~agent.tools.checkpoints`) — before each turn the kernel records a
**shadow-git snapshot** of ``settings.workspace_dir`` into an *out-of-tree* git-dir
(``settings.checkpoint_dir``), so ``revert_to_checkpoint`` / ``POST /checkpoints/{id}/revert`` can
roll the workspace back to a known-good state after a bad batch of agent file edits. The shadow repo
is initialized once with ignore rules in ``info/exclude`` (never a ``.gitignore`` in the user's
workspace); large media and derived artifacts never enter a snapshot, keeping each commit small.

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
  `memory_save`) + `visible_schemas(context)` = core ∪ mounted ∪ scope-allowlist − denylist.
  Stable tools (core + allowlisted) carry full schemas; a `mount(name)` after `tool_search` adds a
  tool as a **defer_loading stub** — name + rich description with an empty parameter shape — so the
  cached tools array stays small and byte-stable, and the full schema reaches the model through the
  `tool_search` result (`schema_of(name)` → `parameters`). The mounted set resets per session
  (`reset_session`).

**`sandbox.py`** — `Sandbox` holds `SandboxRule(permission, decision)` and exposes a monotonic
`guard()` (deny-only) used as the runtime's pre-execute gate: it computes the session's permitted
permissions (default **READ-only**), and any tool requiring WRITE / NETWORK is denied unless the
host granted it or a human approver confirms. `ASK` with no approver degrades to **deny**
(safe-by-default). It composes with `ToolRuntime`'s existing `approval` hook for human gates.

**`fs_tools.py`** — the resident filesystem/shell tools: `read_file` (READ), `edit_file` (WRITE),
`bash` (WRITE + NETWORK). All file access is rooted at `settings.workspace_dir` and path escape is
rejected (`_resolve`). The desktop workbench's "generate media" flow (`/media/generate` → worker
`generate_media`) stays a separate HTTP+job pipeline, not an agent tool: from a **local video** it
produces a PPT/PDF "book" — parse the subtitle track (SRT/VTT/LRC) → `ffmpeg` keyframe extraction at
subtitle timestamps → one slide per (frame, subtitle text) via `build_pptx` / `build_pdf` (CJK text
via the STSong-Light font), written under `media_output_dir`.

The sibling **toolkit pipeline** (`apps/api/tools/toolkit/`) powers the workbench's
**Generate Mind Map / Generate Slides / Summarize** — `POST /toolkit/generate` → worker
`toolkit_generate`. Every tool (`summary` / `mindmap` / `slides`) runs the same five stages:
**validate** (workspace-confined sources, existence, per-file size cap) → **ingest** (text
extraction + token-budget map-reduce: a single over-budget file is split into line-tracked
chunks, digest calls run under a concurrency cap of 4, and the chunk bound — 64 chunks ×
12000 chars per file — fails loudly instead of burning the worker timeout on serial LLM calls)
→ **generate** (structured JSON via JSON mode, jsonschema-validated, one corrective retry that
carries the concrete schema errors back into the prompt) → **render** (JSON → Mermaid `.mmd` /
Marp `.md` / summary Markdown / `.pptx`, never raw model-written markup) → **persist** (atomic,
collision-proof names). The endpoint accepts three modes, plus two per-run options — `name`
(output-stem override) and `prompt` (a per-task custom prompt **appended to** — never replacing
— the tool's default system prompt in stage 3, so the JSON/schema constraints stay intact):

```
┌─ Electron workbench ──────────────┐        ┌─ FastAPI gateway ────────────┐
│ Generate dialog                   │        │ POST /toolkit/generate        │
│  source tab: session | cloud files│        │   validate + ownership-check  │
│  output folder · prompt · name    │─enqueue─▶   enqueue TOOLKIT_GENERATE   │
│ pickCloudFiles (greys over-limit) │        │ GET /toolkit/prompts · /config│
└───────────────────────────────────┘        └──────────────┬───────────────┘
                                                            ▼ Redis (arq)
                             ┌──────────────────────────────┴──────────────┐
                             │ docker worker · toolkit_generate             │
                             │  stage temp sources → 5-stage pipeline       │
                             │  → DriveService.save_artifact                 │
                             └──────────────────────────────┬──────────────┘
                       ┌─────────────────────────────────────▼──────────────┐
                       │ file mode → workspace output_dir                    │
                       │ session / cloud-file mode → caller's Cloud Drive    │
                       └────────────────────────────────────────────────────┘
```

- **File mode** (`paths` + optional `output_dir`) generates from workspace files into the
  configured output dir.
- **Session mode** (`session_id` + `folder_path`): the worker reads the session's conversation
  (`load_session_detail` → `build_transcript`, user/assistant messages only), stages it as a
  temp source inside the workspace, runs the pipeline, then drops each artifact into the
  caller's Cloud Drive via `DriveService.save_artifact` (SHA-256 object-store put + asset row;
  `folder_path` or drive root; name `<name|safe title>_<tool>.<ext>`, auto-suffixed on
  collision). The temp transcript is deleted in `finally`, and stale ones are swept at worker
  startup.
- **Cloud-file mode** (`file_ids` + `folder_path`): the router ownership-checks every id
  (`DriveService.ensure_asset_readable`) and rejects files over the per-file size cap up front;
  the worker re-checks (defense in depth), downloads each file's bytes into a temp file inside
  the workspace — the **original extension preserved** so text/PDF/doc extraction works — then
  merges them with any `paths` into a **single pipeline run** (multiple files are ingested
  together and produce one combined artifact). Artifacts are saved back to the caller's Cloud
  Drive; temp files are deleted in `finally`.

Two read-only endpoints surface the pipeline's knobs to the clients: `GET /toolkit/prompts`
returns the default system prompts (the generate dialog shows them as the prompt placeholder)
and `GET /toolkit/config` returns the per-file size cap plus the supported-extensions set so
the desktop picker can grey out files a job would refuse. The frontend dialog and its
poll-until-terminal progress model are described in §15.

**`bash_sandbox.py`** — where the ``bash`` tool's commands actually run, two backends behind one
:class:`BashSandbox` protocol (`settings.bash_sandbox` = ``"docker"`` | ``"host"``, **default
``"docker"``**):
- :class:`DockerBashSandbox` — the **default production path**: each command runs in a fresh,
  one-shot container (docker-py, a hard dependency) with the workspace mounted read-write,
  **network disabled by default**, memory/CPU caps, and a call-level timeout. Real isolation
  lives here.
- :class:`HostBashSandbox` — a hardened **local-development-only** fallback (explicit opt-in via
  ``settings.bash_sandbox="host"``): the command runs on the host process, so it is **not a
  security boundary** — only a best-effort workspace-escape guard (:func:`assert_no_escape`),
  a hard timeout, and an output cap.

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
Finalize is **failure-robust**: the summary and auto-title are cosmetic — if either LLM call
fails, finalize still closes the session and persists embeddings (a `logger.warning` is the only
signal), so a dead provider can never strand a session in an open/unsaved state.
A daily retention cron (`prune_session_events`, registered in `WorkerSettings.cron_jobs`) sweeps
`session_events` older than `SESSION_EVENTS_RETENTION_DAYS` (30); only the audit log is purged —
`messages` (the recall corpus) and `sessions` (summaries) are deliberately kept.

**Background agent turns** — the `run_agent_turn` job (cron / scheduled activity) runs one agent
turn for a user/session in the worker. It **reuses the API's hardened `AgentKernel` singleton**, so
a scheduled turn gets exactly the same prompt assembly, recall, approvals, telemetry, and budget
guard as an interactive one — no second, drift-prone kernel construction in the worker. The answer
lands in the session like a normal chat message and `session_finalize` is deferred exactly as in the
interactive path (payload: `user_id` / `session_id` / `message`, plus optional `model` / `base_url` /
`api_key` to pin an LLM channel).

**Job lifecycle under retries** — `WorkerSettings.max_tries` (arq's retry budget) is mirrored to PG by
the `_run` wrapper (`apps/worker/tasks.py`): a job is marked `running` first, and **FAILED is written
only on the final attempt** (`attempt >= max_tries`). A non-terminal failure flips the row back to
`RUNNING` with an `error` note ("attempt N failed — retrying"), so PG never shows a false FAILED while
arq is still retrying. arq cancels jobs past `job_timeout` with `CancelledError` (a `BaseException`,
which a bare `except Exception` would swallow — the row would stay `running` forever); the wrapper
records the honest terminal state before re-raising. Terminal failures are also appended as a
best-effort **dead-letter marker** — one JSONL line on `audit_log_path` (`event: job_dead_letter`) —
since there is no `job_events` table.

**Per-asset ingest serialization** — `asset_ingest` jobs for the same asset (upload auto-enqueue,
cloud-drive "Import to Knowledge", admin reindex) are **serialized per asset** by an in-process
`asyncio.Lock` (`_asset_ingest_lock`): without it, two jobs' delete-by-asset + incremental insert
would interleave and delete each other's parent chunks mid-flight (`chunks_parent_chunk_id_fkey`).
The asset's `rag_status` walks `PARSING → CHUNKING → EMBEDDING → INDEXED/FAILED`; a cancelled or
failed job marks the asset `FAILED` so the UI never shows a stuck badge (cancellation is a
`BaseException`, so `except Exception` alone would leave the badge stuck).

**TTS streaming** — besides the `/tts` job, `GET /tts/stream` streams a transcript sentence by
sentence over SSE: each `segment` event carries a **cached WAV URL** (synthesized in-process against
the localhost Kokoro container), so the client plays sentence 1 while the later ones are still
generating; `error` / `done` frames terminate the stream.

**Toolkit generation jobs** — `toolkit_generate` legitimately runs many minutes (a large PDF's
map-reduce, then JSON generation) and is bounded by `WORKER_JOB_TIMEOUT` (1 h), so a generation
job is **never transient**. The desktop client reflects that: the generate dialog keeps its
Generate button disabled and polls `GET /jobs/{id}` every 2 s **until a terminal state** — it
imposes no client-side deadline — so a job that outlives the dialog keeps running and reports its
result when the user reopens the dialog or when the poll completes in the background.

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

Every `Retrieve` is gated by an :class:`AuthGuard` (in `apps/retrieval/server.py`) before reaching
the pipeline:

- **token gate** — a shared secret from gRPC metadata (`authorization: Bearer <token>`); an empty
  ``token`` disables auth so local development needs no secret. Wrong/missing token →
  ``UNAUTHENTICATED``.
- **per-peer rate limit** — a per-peer token bucket when ``rate_limit > 0`` (0 = unlimited);
  an exhausted bucket → ``RESOURCE_EXHAUSTED``.
- **tenant binding** — the pipeline only applies its owner / workspace / ACL visibility predicate
  when a ``user_id`` filter is present; a request without one reads across every tenant. The guard
  therefore **requires** a non-empty ``user_id`` (or an explicit ``guest=1`` marker) and rejects
  anything else with ``PERMISSION_DENIED``. A guest resolves to ``user_id=None``, which the recall
  nodes treat as public-link assets only.

``Health`` is deliberately unauthenticated (a liveness probe that leaks no data). On the client
side, `GrpcRetriever` stringifies the filter values (UUIDs → str) and attaches the same Bearer
token as metadata, normalizing a guest `user_id=None` to the ``guest=1`` marker on the wire.

## 10. RAG Module (Config-Node Pipeline)

Retrieval is a **config-driven node pipeline**: the enabled node list *is* the topology. Each stage
is a `Node` running against a shared `PipelineContext` blackboard; the executor creates nodes from
the configured name list, runs them in order, records a per-node trace, and **degrades rather than
stops** — one node failing appends an error and the pipeline continues downstream.

**Design invariants** — the pipeline degrades, it never stops: a failing node appends an error and the
downstream stages keep running; rewrite / HyDE failure falls back to the original query. If *every*
ranking channel fails, retrieval raises `RetrievalUnavailable` so the `rag_search` tool answers from
knowledge instead of returning a silent empty. Rerank is off until a `model_name` is configured; a CJK
query routes through jieba segmentation when the CJK flag is on. Retrieval is a capability seam —
`rag_search` calls `ctx.resolve("retrieval")`, so swapping the in-process `RAGPipeline` for the gRPC
retrieval service (`RETRIEVAL_MODE=grpc`) needs no tool change.

### 10.1 Node contract

The node contract lives in `rag/nodes/base.py`:

- `NodeStatus` — `OK` / `FAIL` / `SKIP` (`FAIL` records an error, the pipeline keeps going).
- `Node(name, display_name, stage, params_schema, run(ctx, deps) -> NodeStatus)`. `params_schema` is
  a JSON Schema that drives the admin-console node form; `deps` carries pipeline-level dependencies
  so a node is constructible from a name + params alone.

### 10.2 Context blackboard

The blackboard lives in `rag/pipeline/context.py`:

`PipelineContext` carries the `request` (query / top_k / filters), a typed `store` dict for stage
artifacts (`variants`, `rankings`, `fused`, `hits`, `quality`, …), a per-node `trace`
(`name / status / ms / out`), and an `errors` list. `set_out` / `get_out` let a node publish a
human-readable summary of what it produced for the console.

### 10.3 Registry

The registry lives in `rag/pipeline/registry.py`:

Name → class map with X-macro single-point registration (`registry._import_and_register`).
**Registering a node = one file in `rag/nodes/` + one line in the registrar.** Unknown names are
rejected at config-validation time, so a typo cannot silently produce a no-op pipeline.

### 10.4 Configuration

The config model and its persistence live in `rag/pipeline/pipeline_config.py` and `rag/config_store.py`:

- `RagPipelineConfig` = `nodes: list[NodeConfig]` (name / enabled / params) + `chunking`
  (strategy / chunk_chars / overlap) + flags `contextual` / `parent_child` / `cjk`.
- Stored as the `app_settings["rag"]` JSON blob; env settings seed the defaults on first boot.
  `config_store` validates against the registry + param schemas and caches the result in-process.
  Saving a new config clears the API's retriever lru_cache, so the next retrieval is built from the
  new topology — **no restart**.

### 10.5 Executor

The executor lives in `rag/pipeline/executor.py`:

`RAGPipeline.retrieve(query, top_k, filters)` keeps the pre-refactor contract (returns
`[{id, text, score, meta}]`) and runs every enabled node in order. Per-node failure degrades
(rewrite / HyDE failure falls back to the original query) and downstream still runs; if *every*
ranking channel fails it raises `RetrievalUnavailable` so the `rag_search` tool surfaces the
"answer from knowledge" notice instead of a silent empty. The admin console uses `RAGPipeline.trace()`
to read hits + per-node trace + errors.

### 10.6 Nodes

Default topology (behavior-identical to the pre-refactor DAG):

| node | file | what it does |
|---|---|---|
| `query_rewrite` | `nodes/query_rewrite.py` | multi-query variants + HyDE via `QueryRewriter`; falls back to the original query on LLM failure |
| `vector_recall` | `nodes/vector_recall.py` | embeds each variant / HyDE doc, pgvector cosine over `leaf` chunks, tenant- + domain-filtered |
| `keyword_recall` | `nodes/keyword_recall.py` | tsvector FTS; a CJK query is jieba-segmented and matched against `content_search` via `to_tsvector('simple', …)` |
| `rrf_fusion` | `nodes/rrf_fusion.py` | Reciprocal Rank Fusion (k=60) over all ranking channels |
| `cross_encoder` | `nodes/cross_encoder.py` | BGE-reranker (lazy, `asyncio.to_thread`); SKIPs while `model_name` is empty |

Optional nodes:

| node | file | what it does |
|---|---|---|
| `parent_expand` | `nodes/parent_expand.py` | small-to-big: leaf hit → parent-chunk text; sibling leaves deduped by `parent_chunk_id`, ordered by first-leaf position |
| `crg_check` | `nodes/crg_check.py` | simplified CRAG: LLM judges relevant / ambiguous / irrelevant; drops hits judged irrelevant |

**Per-node parameters** — each node declares a JSON `params_schema` + `default_params` that drive the
admin-console node form (a node without a schema shows an empty editor):

| node | param | type | default | meaning |
|---|---|---|---|---|
| `query_rewrite` | `n_variants` | int | `2` | extra query variants the LLM generates beyond the original; recall runs over every variant |
| `query_rewrite` | `hyde` | bool | `false` | also generate a hypothetical answer doc (HyDE), embedded and searched alongside the variants |
| `vector_recall` | — | — | — | no params; candidate count derives from the request `top_k` (`top_k × 2`) |
| `keyword_recall` | — | — | — | no params; candidate count derives from the request `top_k` (`top_k × 2`) |
| `rrf_fusion` | `k` | int | `60` | RRF smoothing constant: `score = Σ 1/(k + rank + 1)`, scale-free across recall channels |
| `cross_encoder` | `model_name` | str | `""` | BGE reranker model id; **empty string disables the stage** (SKIPs) |
| `parent_expand` | — | — | — | no params; its presence in the enabled node list *is* the on/off switch |
| `crg_check` | `max_evidence_chars` | int | `800` | per-hit text prefix length sent to the LLM judge |

**parent_child / parent_expand — an input/output pair, configured in different places.** They are two
*independent* knobs, not one flag. The **input** side is the top-level config flag `parent_child`
(`RagPipelineConfig.parent_child`, persisted in `app_settings["rag"]["parent_child"]`, toggled under
admin **RAG → Nodes → Chunking & enrichment**): when on, `build_chunks` calls `split_hierarchy`
(`core/infrastructure/ingest.py`) to write parent + leaf chunks, and recall searches leaves only. The
**output** side is the `parent_expand` node in the pipeline node list: when present *and* enabled it
replaces each leaf hit with its parent chunk's text; there is no boolean parameter — the executor's
`enabled_nodes` filter (`rag/pipeline/pipeline_config.py`) is what the row's enable switch / Remove toggles.
Combinations: `parent_child=off` + `parent_expand=on` is a pass-through (no parents exist to expand);
`parent_child=on` + `parent_expand=off` returns narrow leaf text; both on gives the full small-to-big
flow.

**Planned (designed, not yet implemented)** — `graph_rag`: a graph-of-communities node over the query
repository. Per chunk, an LLM extracts entities + relations; a co-occurrence graph is built, community
detection groups related chunks, and community summaries are written. Recall then runs two tracks: the
existing chunk-level search above, plus a graph track answering global questions from community
summaries (global / local search). It registers the same way as any node (one file in `rag/nodes/` +
one line in the registrar) and its params ride in `app_settings["rag"]` like every other node.

**Fusion vs. rerank** — `rrf_fusion` and `cross_encoder` solve *different* stages of ranking and are
not redundant. RRF is **rank-only fusion**: it never reads content, it aggregates the rank position of
every document across all recall channels via `score = Σ 1/(k + rank + 1)` (k=60), which lets vector
cosine and `ts_rank` scores of different scales fuse fairly. `cross_encoder` is **content-based
rerank**: it discards the RRF ordering and the stored chunk embedding, reconstructs a `(query, hit)`
pair for every candidate, and runs it through the BGE cross-encoder (query + doc concatenated through
one transformer) to get a relevance score, then re-sorts by that new score — it can overturn the RRF
order entirely. Because the cross-encoder reads real text interaction, it corrects "embedding-similar
but semantically unrelated" false positives that a pure rank fusion cannot see.

**Candidate count flow** — `vector_recall` / `keyword_recall` each fetch `top_k × 2` hits (the `×2`
headroom is for the fusion + rerank stages to cut half of it away). `rrf_fusion` merges and dedups the
full set — it does **not** truncate. `cross_encoder` (when `model_name` is configured) reranks all of
them. The only truncation point is the executor's final `ctx.final_hits()[:top_k]`.

**Hard dependency — do not remove `rrf_fusion` from the default topology.** The recall nodes only append
to `ctx["rankings"]`; they never write `ctx["hits"]`. `rrf_fusion` is the *only* default node that
produces `hits`, so removing it silently returns an empty result: every recall channel succeeds,
`rankings` is non-empty (so `RetrievalUnavailable` is *not* raised — that guard fires only when
rankings are entirely absent), and the executor returns `[]`. The minimal viable pipeline is
`vector_recall → rrf_fusion` (or `keyword_recall → rrf_fusion`); at least one fusion/rerank node that
writes `ctx["hits"]` must remain.

The `rag_search` tool's optional `domain` argument maps to `filters["domain_id"]` (→
`assets.domain_id`).

### 10.7 Ingest side (runtime-configured chunking)

The chunking + enrichment pipeline lives in `core/infrastructure/ingest.py`. `asset_ingest` reads the
current `RagPipelineConfig` at job time, so a chunking / enrichment config
change takes effect on the next `POST /admin/rag/reindex` (re-ingests every READY asset):

- **Strategies** — `fixed` (sliding window), `paragraph` (blank-line groups merged), `sentence`
  (sentence-boundary merge), `semantic` (embedding breakpoints; interface reserved).
- **Contextual enrichment** — each chunk gets an LLM-generated 50–100 token context prefix
  (`content_en = context + "\n" + raw`, raw kept in `meta["raw"]`); `asyncio.Semaphore(8)` bounds
  concurrency, and the raw chunk is kept on any LLM failure.
- **Parent/child indexing** — `split_hierarchy` emits parent chunks (`chunk_kind='parent'`) plus
  leaf chunks (`chunk_kind='leaf'`, `parent_chunk_id` → parent; parent ids are client-side UUIDs
  assigned before insert). Parents are context only: they get a zero-vector sentinel and are
  never vector-recalled (recall searches `leaf`; `parent_expand` fetches a parent by id), so large
  documents don't blow the embed budget. Leaves embed + insert in incremental `embed_batch_size`
  batches, so a worker timeout preserves already-committed chunks and a re-run re-does only the
  remainder.
- **CJK keywords** — `rag/query/cjk.py` lazily loads jieba; segmented tokens are stored in
  `chunks.content_search` and matched with `to_tsvector('simple', content_search) @@
  plainto_tsquery('simple', <segmented query>)` (GIN-indexed). English queries keep the original
  `english` FTS path unchanged.

### 10.8 Query Repository — multi-source import

The **query repository** is the unified retrieval corpus: the existing `chunks` table, extended so
content can arrive from three entries instead of only cloud-drive files. Recall is
**source-aware** — one query searches file, learning, and chat content together, still tenant-scoped.

**Schema** (migration `0011_query_repository.sql`): `chunks.asset_id` is now nullable; two new columns
`source_type TEXT NOT NULL DEFAULT 'file'` (`file` / `learning` / `chat`) and `source_id TEXT NULL`
(article id, sentence id, or chat session / Q&A id) tag non-file content, indexed by
`chunks_source_idx`. A new `articles` table (user / domain / title / content) backs the Learning-Platform
article entity. Non-file chunks store the importing owner in `user_id` and leave `workspace_id` NULL —
the owner is the visibility boundary (sharing / domain filters for learning/chat chunks are out of scope
for now).

**Import entries:**

| entry | UI trigger | processing logic | result |
|---|---|---|---|
| Cloud Drive | file row **＋ 加入查询仓库** (`POST /files/{id}/import-rag`) | worker `asset_ingest` → `extract_document_text` dispatch; `.pdf` runs the PDF tool chain | `source_type='file'`, `asset_id` set |
| Learning Platform | Import Data → sentence checkboxes / *Articles & Query Repo* (`POST /learning/import`, `POST /learning/articles/{id}/import`) | worker `learning_import` (batch) or the API (single article) reads rows → `build_chunks` → embed → insert | `source_type='learning'`, `source_id=<id>` |
| Chat (single pair) | desktop reply **Import to Knowledge** (`POST /chat/import`) | API binds the assistant reply to its preceding user question, `build_chunks` the Q&A text | `source_type='chat'`, `source_id=<user_message_id>` |
| Chat (whole session) | desktop session **⋯ → Import to Knowledge** (`POST /chat/import-session`) | worker `chat_session_import` → LLM `_segment_chat` groups turns (same question across turns merges; distinct questions split; each entry carries the covered transcript indexes), default per-turn grouping on failure | `source_type='chat'`, `source_id=<first covered user-message id>`, `kind='qa'`, `meta.covered` = covered user-message ids |

**Persistent import state** — the source of truth is a per-message `messages.imported_rag` flag (default 0,
set to 1 on import), returned by `GET /sessions/{id}` so the desktop renders each reply's **📥** button from
its own row — never from the question it happens to bind to, so deleting or re-grouping a message can't
spread the state to sibling pairs or hide it. `GET /chat/imported?session_id=` reads the same flags for the
click-time re-check: `qa_source_ids` lists every flagged user message, `session_imported` is true only when
*every* current user message is flagged, and `legacy_session_imported` marks a pre-flag whole-session import
(old `kind='session-qa'`) with no per-message data. On session load a one-time backfill
(`GET /sessions/{id}`) reconstructs flags from pre-flag chunk meta (whole-session `meta.covered` → all
messages when every current user message is covered, else the covered user messages; single-pair
`source_id` → that user message), so already-imported sessions keep their **✓ Imported** state after the
upgrade without a re-import.

**Chat storage granularity** — both chat imports store **Q&A pairs**, never raw messages and never a
single row per whole session. A single `/chat/import` binds an assistant reply to its preceding user
question, merges them into one text (`question\n\nanswer`), runs `build_chunks`, and inserts the
resulting 1..N rows under `source_id=<user_message_id>`. A whole-session import first groups the
transcript into Q&A entries (`_segment_chat` — same question across turns merges, distinct questions
split), then stores each entry the same way under a stable key `source_id=<first covered user-message id>`
(message-id-derived, so positional drift after message deletes / regroupings never collides with an old
chunk), with every chunk's `meta` tagged `kind='qa'`, `covered` (the user-message ids the entry answers),
and `session_id`. Long answers split into several leaf chunks (plus an optional `parent`) that share
one `source_id` — so one Q&A pair maps to one `source_id` but possibly many `chunks` rows.

Whole-session imports are **incremental and flag-driven — no delete-and-rebuild**: a Q&A entry is embedded
only when its span (the question plus its merged answer) is not yet fully `imported_rag`-flagged, so
re-importing a session never re-embeds content already in the repo (a regenerated answer — a fresh
assistant-message id — re-imports; an appended question imports; an untouched imported pair is skipped).
Each import replaces the pair's chunk keyed by its question id (`delete_by_source` then write), keeping
single-pair and whole-session imports idempotent and non-overlapping. Pre-flag legacy whole-session keys
(`<session_id>:<i>` and the bare `<session_id>`) are purged once on a legacy session's first re-import so it
converts cleanly without duplicating content. On success the imported pairs' messages get their
`imported_rag` flag set, and the corpus version is bumped so stale query-cache hits drop immediately.

**PDF tool chain** — processing is extracted into **tools** (the admin can toggle them like any tool)
wrapping pure functions in `core/infrastructure/pdf.py`:

1. `page.get_text("text")` extracts the body text page by page (PyMuPDF).
2. `page.find_tables()` detects tables (no torch / Table-Transformer); each table's bounding box is
   rendered to a PNG (`get_pixmap(clip=bbox)`).
3. Each table PNG goes to the vision LLM as an `image_url` content part (`OpenAILLM.chat` passes it
   through to any vision-capable model, e.g. `gpt-4o-mini`) → transcribed text. A per-table failure
   is logged and skipped — enrichment never fails the ingest.

The tools (`apps/api/tools/pdf_tools.py`: `pdf_extract_text_tool`, `pdf_table_to_text_tool`) resolve
the asset's bytes via `ctx.resolve("storage")` / `ctx.resolve("session_factory")`; the worker calls the
shared `extract_pdf_document` directly (same function body). `.docx` extracts paragraphs + table cells
via `python-docx`; `.txt`/`.md`/subtitles use the existing `extract_text` dispatch.

**Recall visibility** — both recallers `LEFT JOIN assets` instead of `JOIN`:

- `keyword_recall` / `vector_recall`: `AND (c.asset_id IS NULL OR a.file_status = 'READY')`; the
  domain filter applies only to file chunks (`c.asset_id IS NOT NULL AND a.domain_id = …`).
- Tenant isolation (`asset_visibility_sql`) already keys off `c.user_id` / `c.workspace_id`, so
  non-file chunks are visible to their owner automatically.
- `chunk_kind='leaf'` filtering is unchanged; parent/child, contextual, and CJK enrichment apply to
  every source identically (they operate on `Chunk` objects before insert).

**Idempotency** — every non-file import deletes the source's existing chunks first
(`delete_by_source`) then re-inserts, so re-importing after a config change or a partial failure is safe.

**Verification** — admin console **RAG → Repository** tab lists every non-file chunk (source badge,
title from `meta`, per-chunk delete via `DELETE /admin/rag/repository/{chunk_id}`); the **Test** tab
searches across all three sources; **Eval** runs the golden-set regression unchanged. Config flow
(Nodes / Chunking) is source-agnostic — it governs how any text is chunked.

### 10.9 Quality regression (P0)

The eval engine lives in `rag/eval.py`. Golden cases (`data/eval/golden.json`) pin a query to the
**asset ids** that should surface.
`run_golden_set(pipeline_factory, golden_path, top_k)` runs every case through one pipeline and
scores asset-level **Recall@k / Precision@k / MRR** — asset-level expectations make the metrics
insensitive to chunk-boundary changes. Driven by the admin **Eval** tab or `scripts/eval_rag.py`
(prints a table, writes `data/eval/results/<ts>.json`).

### 10.10 Admin console

The endpoints live in `apps/api/routers/admin.py` (+ `rag_admin.py` for the RAG module); the
console page in `apps/api/admin/index.html`. Six
`/admin/rag/*` endpoints (all `require_admin`) sit behind the **RAG** module's four tabs:

| endpoint | tab | purpose |
|---|---|---|
| `GET/POST /admin/rag/config` | Nodes | read / validate + persist the pipeline config; POST clears the retriever cache |
| `POST /admin/rag/test` | Test | run the configured pipeline → hits + per-node trace |
| `POST /admin/rag/chunk-preview` | Chunking | split a pasted text with a strategy (+ CJK / contextual), no DB writes |
| `POST /admin/rag/eval` | Eval | run the golden-set regression → metric table |
| `POST /admin/rag/reindex` | Nodes | re-ingest every READY asset under the current chunking config |

### 10.11 Schema

Defined in migration `0010_rag_pipeline.sql`:

`assets.domain_id UUID NULL REFERENCES domains(id) ON DELETE SET NULL` (+ `assets_domain_idx`);
`chunks.parent_chunk_id` (FK → `chunks.id` ON DELETE SET NULL), `chunks.chunk_kind` (default
`'leaf'`), `chunks.content_search TEXT NULL`; indexes on `parent_chunk_id` and GIN on
`to_tsvector('simple', content_search)`.

### 10.12 Query cache & retrieval feedback

**Query cache** (`rag/query_cache.py`) — a Redis cache in front of any `Retriever` (in-process
`RAGPipeline` or the gRPC client), keyed by `(query, filters, top_k, config-version, corpus-version)`:

- **config change** — the key embeds a hash of the current node config, so a new topology
  invalidates automatically (no explicit flush needed).
- **corpus change** — a `rag:corpus_version` Redis counter is bumped on every ingest / re-index
  (worker `asset_ingest` / `learning_import` / `chat_session_import`, admin `reindex`); the version
  is part of the key, so stale hits vanish as soon as the corpus moves.
- **degradation contract** — the cache is a pure accelerator: Redis down, a missing value, or a
  write failure all fall through to the wrapped retriever, so a retrieval is never failed by the
  cache. Disabled when `query_cache_ttl_seconds <= 0` or no Redis client is bound
  (`wrap_retriever` returns the inner retriever untouched).

**Incremental re-index** — `POST /admin/rag/reindex` re-ingests every READY asset under the current
chunking config. Per asset the rebuild is atomic (delete-by-asset + bulk insert) so a worker timeout
preserves already-committed chunks and a re-run redoes only the remainder; on completion the corpus
version bumps, dropping stale query-cache hits immediately.

**Retrieval feedback** (`rag_feedback` table, `RagFeedbackModel`) — `POST /rag/feedback` persists a
👍/👎 rating for the chunks behind an answer. Each row snapshots the query, the retrieved hits
(`id` / `score` / text), the rating, and an optional reason, so the corpus becomes a **golden
dataset** for future fine-tuning / eval without re-running retrieval. The endpoint and table are
implemented (with tests); a rating UI that calls it is not wired up yet.

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
| Stable prompt head for prefix cache | `CacheBoundaryAssembler` zones (internal `CACHE_BOUNDARY` separator, never rendered) + `snapshot_key()` |
| Load a tool schema on demand | `tool_search` meta-tool → `ToolGateway.mount(name)` (defer_loading stub) + `schema_of(name)` in the result |
| Inject project conventions | `read_project_context` → `PromptZone.PROJECT_CONTEXT` (DEEPDIVE.md, capped) |
| Bound the prompt window | per-message snip (`prompt_message_max_chars`) + char-budget autocompact (`prompt_max_chars`) |
| Scope tool visibility per request | `ToolVisibilityPolicy` `allow` / `deny` / `present_as` (disposers) |
| Gate a tool by session permission | `Sandbox.guard()` + `ToolPermission` (`classify_permissions`) |
| Recall / write memory as a tool | `memory_search` / `memory_save` (guardrailed, READ-classified; importance + supersede) |
| Lazy-load a skill body | `skill` meta-tool over `SkillCatalog.render()` compressed index |
| Reconfigure retrieval at runtime | `app_settings["rag"]` + `rag.config_store` (validated against the registry, cached; a save clears the retriever lru_cache) |
| Measure retrieval quality | `rag.eval` golden-set regression (asset-level Recall@k / Precision@k / MRR); admin **Eval** tab or `scripts/eval_rag.py` |

## 12. Data Model

> **Migration note:** the schema is defined **only** in `migrations/*.sql` — applied in filename
> order by `init_db()` via asyncpg and tracked in the `schema_migrations` table (no Alembic, no
> `create_all`). This document does not repeat the DDL; the migration files are the single source
> of truth. Table names below are the implemented ones (`sessions`, `messages`, `jobs` …), not the
> earlier design names (`conversations`, `job_logs` …). `migrations/0001_init.sql` is the single
> consolidated base schema (the squash of the original 0001–0008 development migrations; every
> statement is idempotent). On top of it, the incremental migrations `0002_auth_profiles.sql` …
> `0013_asset_acl_public.sql` layer later changes (self-service accounts + `verification_tokens`,
> usage-log channel, cloud-drive objects, vocabulary isolation, folders, workspace activity,
> memory-retention index, session title, RAG pipeline columns, the multi-source query
> repository, RAG retrieval feedback, and the public-link asset ACL). All are applied in
> filename order by `init_db()`.

The core learning + chat tables that run today (`migrations/0001_init.sql`):

- **domains** — `id`, `name` (unique), `created_at`.
- **materials** — `id`, `type` (`domain` | `video` | `document`), `title`, `source_url`, `meta` (JSONB), `created_at`.
- **users** — `id`, `created_at`; the auth columns are added by the consolidated schema (see §12.3).
- **terms** — `id`, `domain_id` (FK → `domains`), `word`, `definition`, `frequency`, `star_level`, `audio_hash`, `image_paths` (JSONB), `is_active`.
- **sentences** — `id`, `domain_id` (FK → `domains`), `origin_source`, `content_en` (unique), `content_cn`, `audio_hash`, `cn_explanation`, `embedding` (vector(1024)).
- **chunks** — the RAG chunk table (added by `0004_drive_objects.sql`): `id`, `asset_id` (FK →
  `assets` CASCADE, **nullable since `0011`**), denormalized `user_id` / `workspace_id` for filtered
  recall, `seq`, `content_en`, `content_cn`, `meta` (JSONB), `embedding` (vector(1024), HNSW-indexed).
  The `0010_rag_pipeline.sql` migration adds `parent_chunk_id` (FK → `chunks.id`, parent/child indexing),
  `chunk_kind` (`'leaf'` default | `'parent'`), and `content_search` (jieba-segmented CJK keywords,
  GIN-indexed). The `0011_query_repository.sql` migration makes non-file content first-class:
  `source_type` (`'file'` default | `'learning'` | `'chat'`, indexed) + `source_id`, and the new
  `articles` table (user / domain / title / content / created_at) for Learning-Platform study material.
- **sessions** — `id`, `user_id` (FK → `users`), `title`, `created_at`, `closed_at`, `summary`.
  `title` (`0009_session_title.sql`) is auto-set at creation from the first user message —
  whitespace-normalized and capped at 40 chars — so the sidebar shows a readable name while the
  deferred finalize job is still running; `PATCH /sessions/{id}` can rename it and an empty title
  resets it to `NULL` so auto-naming kicks in again.
- **messages** — `id`, `user_id`, `session_id` (FK → `sessions`), `role` (`user` | `assistant` | `tool`), `text`, `embedding` (vector(1024)), `created_at`.
- **session_events** — `id`, `session_id` (FK → `sessions`), `seq`, `type`, `timestamp`, `payload` (JSONB).
- **matches** — `id`, `term_id` (FK → `terms`), `sentence_id` (FK → `sentences`), `cn_explanation`.
- **jobs** — `id`, `type`, `status`, `payload` (JSONB), `result` (JSONB), `error`, `created_at`, `started_at`, `completed_at`.

Multi-tenancy is carried by `user_id` on `sessions` / `messages` and the auth/billing tables;
isolation is app-level predicates — `user_id` scoping plus the `visibility` tenant predicate, not RLS (see §13).
Runtime access is via the SQLAlchemy 2.0
async models in `packages/core/infrastructure/db.py`.

### 12.1 Indexes and Retrieval

Hybrid recall is computed in code over two channels:

- **Keyword (tsvector)** — the English path evaluates `to_tsvector('english', …) @@
  websearch_to_tsquery` at query time over `chunks.content_en` / `messages.text`
  (`packages/rag/recall/keyword.py`, `packages/core/infrastructure/memory_retrieval.py`). A CJK
  query is jieba-segmented (`rag/query/cjk.py`) and matched against the stored `chunks.content_search`
  column via `to_tsvector('simple', content_search) @@ plainto_tsquery('simple', <segments>)`.
- **Semantic (pgvector)** — cosine search over the `embedding vector(1024)` columns.
- **Indexes** — `0004_drive_objects.sql` adds an HNSW index on `chunks.embedding`;
  `0010_rag_pipeline.sql` adds a GIN index on `to_tsvector('simple', COALESCE(content_search, ''))`
  plus B-tree indexes on `chunks.parent_chunk_id` and `assets.domain_id`. `0008_memory_retention.sql`
  adds a plain B-tree index on `session_events(timestamp)` so the daily audit-event sweep's range
  DELETE stays fast.

### 12.2 Billing and Logs

- **Implemented** — the billing surface (in the consolidated `migrations/0001_init.sql`):
  `llm_credentials`, `llm_models`, `credential_models`, `role_credentials`, `user_wallets`,
  `wallet_transactions` (documented in §12.3).
- **Design-only (not created)** — `subscriptions`, `credit_ledger`, `audit_logs`, `ai_call_logs`,
  `activity_logs`, `job_logs` (job state lives in the implemented `jobs` table).

**Quota settlement (free-first)** — `authorize_usage()` (replacing the old hard `check_quota`)
returns the tier a request is charged to: **`free`** when within the role's daily/monthly/token
limits (counters incremented, wallet untouched), else **`paid`** — the wallet must hold at least
`wallet_gate_min_balance_usd` or the request gets **402**; the exact cost is then debited at
`_log_usage` (clamped to the balance, so an overflow undercharges at most one request). A
`-1` limit means unlimited (always free). **Anonymous guests** skip the wallet entirely: they are
identified by a **signed `gt_` HMAC token** (`sign_guest_token` / `verify_guest_token`, TTL
`guest_token_ttl_seconds`) minted on the first request and returned in the chat response — a
client-supplied `user_id` is never trusted — and capped by the `guest_daily_limit` Redis counter
(429; fail-open on a Redis outage). A signed-in user whose every LLM key is disabled degrades to
the anonymous tier for that request (guest quota + `anonymous` routing).

### 12.3 Implemented auth, RBAC & billing schema

The multi-user + billing surface (all part of the consolidated `migrations/0001_init.sql`, with the
self-service account tables/columns in `0002_auth_profiles.sql`): login credentials live in
`login_tokens`, and `access_tokens` is the per-user LLM-key grant matrix.
Fields below mirror the migration DDL exactly;
`TEXT` columns are plain `TEXT`, money is `NUMERIC`, time is `TIMESTAMPTZ`, JSON is `JSONB`.

- **users** — identity + credentials. Columns: `id` (UUID PK), `username` (TEXT, unique where
  non-null — legacy anonymous rows keep it NULL), `password_hash` (TEXT, stdlib pbkdf2),
  `display_name` (TEXT), `is_active` (BOOLEAN, default true), `role_id` (TEXT FK → `user_roles`
  `ON DELETE RESTRICT`, default `'regular'`), `meta` (JSONB, default `{}`), `created_at`
  (TIMESTAMPTZ, default now()), `updated_at` (TIMESTAMPTZ). The flat `tier` column from the early
  design was dropped in favour of `role_id`. Self-service profile/verification columns (from
  `migrations/0002_auth_profiles.sql`): `email` (TEXT, unique where non-null), `phone` (TEXT),
  `avatar` (TEXT — `/avatars/{user_id}.{ext}`, the uploaded file path served by a static mount),
  `email_verified` (BOOLEAN, default false — a non-null email blocks sign-in until verified).
- **verification_tokens** — one-time tokens for email verification (`kind='verify'`, TTL 24h) and
  password reset (`kind='reset'`, TTL 1h), stored hashed (sha256) and shown once. Columns: `id`
  (UUID PK), `user_id` (UUID FK → `users` CASCADE), `kind` (TEXT), `token_hash` (TEXT UNIQUE),
  `expires_at` (TIMESTAMPTZ), `used_at` (TIMESTAMPTZ — single-use), `created_at` (TIMESTAMPTZ).
- **user_roles** — quota + model + feature permissions, the tier definition. Columns: `role_id`
  (TEXT PK), `role_name` (TEXT), `daily_request_limit` (INT, default 50), `monthly_request_limit`
  (INT, default 1500), `daily_token_limit` (BIGINT), `rpm_limit` (INT), `monthly_cost_limit`
  (NUMERIC(12,6)) — each `-1` = unlimited — plus `default_model` (TEXT, empty = the active
  provider's model), `models` (TEXT[], **legacy** allowed-model ids — the routing source is now
  `role_credentials`, this field is kept only for compatibility display), `features` (JSONB, e.g.
  `{"chat": true}`), `is_active` (BOOLEAN, default true), `created_at`. Seeded roles: `regular`
  (50/day), `pro` (500/day), `vip` (−1/unlimited), `admin` (−1/unlimited), and **`anonymous`**
  (guest tier, 20/day).
- **role_credentials** — N:M binding **role → LLM channel**; this is what decides which provider
  key a role may use (VIP/pro bind expensive channels, `regular` / `anonymous` the cheap ones).
  Columns: `role_id` (TEXT FK → `user_roles` CASCADE), `credential_id` (UUID FK → `llm_credentials`
  CASCADE), `is_active` (BOOLEAN, default true), `created_at`; PK `(role_id, credential_id)`, plus
  an index on `credential_id`.
- **login_tokens** — the **login/API credential** (separate from the `access_tokens` key-grant matrix).
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
  `login_tokens_user_no_cred_uniq` on (user) where no channel is pinned). **Admin console logins
  are stateless** — `/admin/login` returns a signed
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
  `model_name` (TEXT), `credential_id` (UUID FK → `llm_credentials` SET NULL, from
  `migrations/0003_usage_channel.sql` — **the channel that served this request**; records which
  provider key ran the call), `tool` (TEXT), `prompt_tokens` / `completion_tokens` / `total_tokens`
  (INT, default 0; total is denormalized for dashboards), `cost_usd` (NUMERIC(12,6)), `created_at`;
  indexes on `(user_id, created_at)` and `(token_id, created_at)` (plus `credential_id` from 0003).
  **The `cost_usd` is always the catalog `llm_models` price** for the served model; `credential_id`
  only records *which channel* served it, so the admin can aggregate cost per channel (see the
  `GET /admin/usage/by-channel` aggregation). A request with no usable channel (anonymous/legacy
  fallback) logs `credential_id` NULL.
- **llm_credentials** — a provider channel; **one row = one "token"/key** the admin manages.
  Columns: `id` (UUID PK), `name` (TEXT), `base_url` (TEXT), `api_key` (TEXT), `is_active` (BOOLEAN,
  default true — the per-channel availability switch), `created_at`, `updated_at`. `name` is a
  **human label only — it has no routing or pricing semantics**: routing pins the row by `id`, and
  pricing never reads a channel name. A channel's
  displayed price is derived from its `credential_models` routes (single model) or a price range
  (multiple models).
- **llm_models** — model catalog with PAYG pricing. `name` (TEXT UNIQUE) is the display name
  referenced by roles; `provider_model_name` (TEXT) is the real model id sent upstream to the
  provider platform. Columns: `id` (UUID PK), `name`, `provider_model_name`, `description` (TEXT),
  `prompt_price_per_1k` (NUMERIC(12,6)), `completion_price_per_1k` (NUMERIC(12,6)), `is_active`,
  `created_at`. The chat path resolves a display name to `provider_model_name` before calling the
  provider, and `get_model_prices` matches either so pricing stays correct.
- **credential_models** — N:M routing (credential ↔ model): which credential serves which catalog
  model, with failover priority, load weight, and a free-text `note` describing the route's
  purpose (the upstream model id comes from the catalog entry, not the note). Columns:
  `credential_id` (UUID FK CASCADE), `model_id` (UUID FK CASCADE), `note` (TEXT), `priority` (INT,
  lower = preferred), `weight` (INT, load-balance weight), `prompt_price_per_1k` /
  `completion_price_per_1k` (NUMERIC(12,6), NULL = inherit `llm_models` price), `is_active`; PK
  `(credential_id, model_id)`. The source of each channel's model list and displayed price. The
  per-route price overrides are **display-only** — the admin console shows them, but billing never
  reads them: **user charging always uses the `llm_models` catalog price**, no matter which channel
  serves the request.
- **user_wallets** — cash wallet, one row per user. Columns: `user_id` (UUID PK FK CASCADE),
  `balance` (NUMERIC(14,6), default 0), `currency` (TEXT, default `'USD'`), `updated_at`.
- **wallet_transactions** — append-only ledger; `balance_after` is a snapshot, never recomputed.
  Columns: `id` (UUID PK), `user_id` (UUID FK CASCADE), `type` (TEXT: `'topup'` | `'llm_consume'` |
  `'refund'` | `'adjustment'`), `amount` (NUMERIC(14,6), +credit / −debit), `balance_after`
  (NUMERIC(14,6)), `description` (TEXT), `meta` (JSONB), `idempotency_key` (TEXT UNIQUE), `created_at`;
  index on `(user_id, created_at)`. Chat usage is priced via `compute_cost` and debited atomically
  (`UPDATE … WHERE balance >= cost`), so insufficient funds never overdraw.

Two auth details worth knowing: **the admin credential is mirrored into `users`** — on every boot the
startup guard (`apps/api/main.py`, `security.py`) upserts a `users` row matching
`app_settings['admin']`, so `admin/admin` can also sign in through `/auth/login` (the desktop client),
not just the stateless `/admin/login`. And **a password reset revokes every login token** for the user
(`/auth/reset-password` flips all their `login_tokens.is_active` false), so the old password stops
working immediately.

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

The model served by the chosen channel is resolved in this order: the **role's `default_model`**
(if the role sets one and the channel can serve it), else the channel's **preferred active route**
(`credential_models`, lowest `priority`), else the **first active model** in the catalog
(`llm_models.is_active`). There is no `settings.llm_model` global default anymore — that setting
only survives as the legacy `/config` client default.

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
   shared client is never mutated), and the model is the **role's `default_model`** (if set and
   served), else the channel's preferred active route, else the first active catalog model. The
   usage log records the served **model + channel** (`credential_id`), and billing stays the catalog
   model price — the admin aggregates cost per channel via `GET /admin/usage/by-channel`.
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

**No-channel is an explicit 503, never a silent fallback** — when the `anonymous` tier has no usable
channel either, the chat endpoint **blocks with HTTP 503** (message: ask the admin to configure a
channel) rather than falling back to the legacy global client: a silent fallback would hide a
misconfiguration behind a paywall that looks like normal usage. A signed-in user whose keys are all
exhausted still degrades to the anonymous tier, but the response carries a `notice` field telling
them they are running on the guest quota (and whether they hit `guest_daily_limit`).

### 12.5 Session & message deletion

Deleting a session is a **synchronous hard delete** — the request handler issues a single
`DELETE FROM sessions` (`DELETE /sessions/{id}`, `apps/api/routers/sessions.py`), and the database's
`ON DELETE CASCADE` (migration `0001_init.sql`) removes that session's `messages` and `session_events`
in the same statement. Deleting a single message (`DELETE /sessions/{id}/messages/{mid}`) is a plain
single-row `DELETE FROM messages`; nothing references `messages.id`, so there is no cascade and no
orphan risk. Because `messages.embedding` lives in the row, a deleted message also vanishes from the
memory-recall corpus (§5.4) automatically.

**The query repository is not cascaded.** Chat-imported knowledge lives in `chunks` with
`source_type='chat'` and a `source_id` that is a **plain string with no FK** to `sessions`/`messages`
(§10.8) — deleting a session or message leaves its imported Q&A chunks in place, still recallable by
the importing owner. Only `asset_id`-linked file chunks cascade (FK → `assets` `ON DELETE CASCADE`).
Deleting a chat and removing it from the RAG corpus are therefore separate operations today: a pair
already imported to knowledge is removed via the admin **RAG → Repository** per-chunk delete
(`DELETE /admin/rag/repository/{chunk_id}`) or by re-importing a changed source, not by deleting the
session.

## 13. Multi-Tenancy and Deployment Strategy

| Scenario | Strategy |
|------|------|
| B2C (multi-user) | Shared DB + app-level predicates (`user_id` scoping + `visibility` tenant predicate) |
| B2B (enterprise) | database-per-tenant |
| Read scaling | read replicas + pgBouncer connection pool |
| Edge vs internal | external REST/SSE via Traefik ↔ internal gRPC |
| Model scaling | separate model services (TEI/Kokoro/LiteLLM), independent scale-out |

---

## 14. Cloud Drive Module

Every user gets a **My Drive** (personal scope) plus any number of **workspaces** (shared
groups). A *file* is a logical **asset** that points at a physical, SHA-256-deduplicated
**global object**; folders are first-class rows scoped to a workspace or the personal drive;
the trash keeps soft-deleted assets for a retention window; and every mutation lands in a
no-FK audit trail.

Sources: `migrations/0004_drive_objects.sql`, `0006_folders.sql`, `0007_workspace_activity.sql`;
`packages/core/application/drive_service.py` (service), `packages/core/infrastructure/drive_repositories.py`
(SQL repos), `packages/core/infrastructure/visibility.py` (tenant predicate),
`apps/api/routers/drive.py` (REST), `apps/web/src/CloudDrive.tsx` (file manager).

### 14.1 Database

| Table | Purpose |
|------|------|
| `global_objects` | One physical file per SHA-256 (primary key), shared across all users. `ref_count` = how many logical assets point at it; the bytes are freed when the count reaches 0. `storage_key` shards as `{root}/objects/{sha[0:2]}/{sha[2:4]}/{sha}`. |
| `workspaces` | User-owned group (`owner_id`, `name`). Ownership is **not** a member row. |
| `workspace_members` | `(workspace_id, user_id)` PK + `role` (`admin` / `editor` / `viewer`). Membership is the sharing mechanism. |
| `folders` | One row per folder path inside a scope; `workspace_id` NULL = My Drive. `path` is the full `/`-separated relative path (`"English/Vocab"`), so ancestors are implicit — no parent FK. Uniqueness per scope via `folders_unique_ws` (partial) and `folders_unique_personal`. |
| `assets` | Logical file: `user_id` (owner), nullable `workspace_id`, `object_sha256` → `global_objects`, `name`, `folder_path`, `file_status` (`uploading/processing/ready/deleted`), `rag_status` (`pending/parsing/chunking/embedding/indexed/failed`), `domain_id` (nullable → `domains`, added by `0010_rag_pipeline.sql`, drives the RAG domain filter), `meta` JSONB, `deleted_at`. |
| `asset_acl` | Asset-level sharing: `(asset_id, grantee_user_id)` PK; `grantee_user_id` NULL = public link (`asset_acl_public_uniq` unique partial index). `permission` = `read` / `write`. |
| `upload_sessions` | Chunked-upload state: expected `sha256`, `size`, `chunk_size`, `num_chunks`, `received_chunks` (boolean array) → resumable uploads. |
| `chunks` | RAG chunks rebuilt with denormalized `asset_id` / `user_id` / `workspace_id` for filtered recall; `embedding vector(1024)` with an HNSW index; `0010_rag_pipeline.sql` adds `parent_chunk_id` + `chunk_kind` (parent/child indexing, recall searches `leaf` only) and `content_search` (jieba-segmented CJK keywords, GIN-indexed). |
| `workspace_activity` | Audit trail: `workspace_id`, `actor_user_id` / `actor_username`, `action` (e.g. `file.create`, `member.add`), `target_type` / `target_id` / `target_name`, `detail`. **No foreign keys by design** — an entry survives the deletion of the workspace / user it references. |

### 14.2 Core logic

- **Upload lifecycle** — `init_upload` → `store_chunk` → `complete_upload` (plus `abort_upload`
  and `chunk_status` for resume):
  1. `init_upload` validates the SHA-256 / size and, for a workspace target, membership. If a
     `global_objects` row already exists for the digest it **deduplicates** ("instant upload"):
     `ref_count` is bumped and the asset is created `READY` immediately, no bytes are sent.
     Otherwise an `upload_sessions` row is created with `chunk_size` (default 8 MB) and the
     client uploads chunks.
  2. `store_chunk` writes one chunk and flips its slot in `received_chunks`; `chunk_status`
     returns the missing-chunk list so a client can resume after a drop.
  3. `complete_upload` verifies every chunk is present, bumps `global_objects.ref_count`
     (creating the physical object on first upload), marks the asset `READY`, and enqueues the
     RAG ingest job.
- **Physical dedup & ref-count** — deleting is a **soft delete**: the asset row keeps its
  bytes, only `deleted_at` is set. `_purge_asset` decrements the object's `ref_count` and only
  removes the physical bytes (and the `global_objects` row) when the count hits 0, so a
  deduplicated file is freed exactly once. Order matters: the asset row is dropped *before* the
  object row (FK `assets.object_sha256` → `global_objects`), with a CAS so a concurrent upload
  that re-incremented is not clobbered.
- **Text notes (read / in-place update)** — text files (`.md`, `.txt`, code, data) can be read
  and rewritten without a re-upload. A text guard accepts a `text/*` MIME or a name matching
  `_TEXT_EXT_RE` (`.txt .md .markdown .text .log .json .csv .yaml .yml .toml .ini .xml .html .py
  .js .ts .jsx .tsx .c .h .cpp .hpp .java .go .rs .sh .bat .sql`); anything else is refused with
  415. `read_text` mirrors `download` (must be `READY` with bytes present) and returns the object
  decoded as UTF-8 (`errors="replace"`). `update_content` performs an **in-place overwrite**:
  `content` is UTF-8 encoded, SHA-256 digested, stored via `storage.put` +
  `objects.upsert_and_increment` (identical to `complete_upload`'s byte-store half), then
  `set_content_meta` **repoints the asset** to the new digest (object_sha256 / size / mime).
  The old object is retired in FK order — repoint first, then `decrement`, then
  `delete_if_zero` + `storage.delete` when its ref_count hits 0 — so a deduplicated note that
  other files share is never freed prematurely. The asset is marked `READY` / `RAG_PENDING` and
  the router re-enqueues `ASSET_INGEST`, which deletes and rebuilds the RAG chunks for the new
  text. A content-identical PUT is a no-op (same digest → log + return).
- **Collision-safe naming** — files and folders share **one namespace per directory** (the tree
  merges them), so a folder `docs` and a file `docs` in the same parent are ambiguous. Every
  mutating op — `init_upload`, `create_folder`, `rename_file`, `rename_folder`, `move_file`,
  `move_folder` — runs the target name through `_unique_name`, which calls `_name_taken` against
  both the `folders` row and the `assets` row at `parent_path/name` and returns the first free
  `stem(n)ext` variant (`a.docx` → `a(1).docx`, folders → `docs(1)`). Creating/moving/renaming
  into a busy directory therefore
  **never fails**; the caller surfaces the final name to the user. Personal (My Drive, workspace
  NULL) rows are scoped to `user_id`, so another user's same-named folder is not a clash.
- **Personal-scope isolation** — folder and asset subtree ops that used to take only
  `workspace_id` now take `user_id` too (`move_subtree`, `trash_subtree`, `delete_subtree`,
  `create`, `get_by_path` in both repos). A workspace scope still matches any member's rows; a
  personal (workspace NULL = My Drive) scope additionally restricts to `user_id = :me`, so one
  user's operations can never touch another user's same-named My Drive rows.
- **Folder semantics** — paths are full relative paths inside a scope; creating `English/Vocab`
  also upserts the `English` ancestor. Rename / move are **prefix rewrites** on both
  `folders.path` and `assets.folder_path` (`move_subtree`), so children follow automatically.
- **Trash & retention** — `delete_asset` moves a file to the trash (soft delete only, no
  ref-count change). Trash supports **restore** (to the original workspace, falling back to My
  Drive if that workspace is gone or the user is no longer a member) and **purge** (hard delete
  + ref-count drop). `list_trash` lazily **permanently deletes anything older than
  `TRASH_RETENTION_DAYS` (30)** — no background sweeper.
- **Workspace lifecycle** — deleting a workspace trashes every asset, nullifies their
  `workspace_id` (so the `assets` FK does not block the drop), then removes the workspace row;
  members and folders cascade. Trashed assets of a deleted workspace restore into My Drive.
- **Audit trail** — every mutation calls `_log(...)` writing who / what / when / target. The
  row has no FKs so it survives its subjects; the API lists it with actor/target fuzzy search,
  date bounds, and pagination (admin / owner only).

### 14.3 Permission management

Three channels grant access to an asset (`asset_visible_expr` in
`packages/core/infrastructure/visibility.py`, mirrored as raw SQL `asset_visibility_sql` for the
RAG recallers):

1. **Ownership** — `assets.user_id == me`.
2. **Workspace visibility** — `assets.workspace_id` in the workspaces I own **or** am a member
   of. The owner is *not* a `workspace_members` row, so the predicate unions owned workspaces
   into the visible set — without this the owner would not see files uploaded by members.
3. **Asset ACL** — a share row granting me, or a public link (`grantee_user_id IS NULL`).

Workspace roles (**owner > admin > editor > viewer**), where owner is implicit in
`workspaces.owner_id` and admin / editor / viewer are `workspace_members.role`:

| Operation | viewer | editor | admin | owner |
|---|---|---|---|---|
| List / download / open | ✓ | ✓ | ✓ | ✓ |
| Upload / edit / move / delete files; create / rename / delete folders | ✗ | ✓ | ✓ | ✓ |
| Open the **Manage** page (Members + Activity Logs) | ✗ | ✗ | ✓ | ✓ |
| Add, change role of, or remove **non-admin** members; view the log | ✗ | ✗ | ✓ | ✓ |
| Assign the `admin` role; modify or remove an existing admin | ✗ | ✗ | ✗ | ✓ |
| Rename / delete the **workspace** | ✗ | ✗ | ✗ | ✓ |

My Drive and the Trash are personal: the user always has full access there. Write gating
(`_can_write`) accepts owner / admin / editor or an ACL `write` grant; member-management and
log endpoints use a *manager* gate (owner or admin) plus a role whitelist
(`admin` / `editor` / `viewer`) and the owner-only rule for granting admin; workspace
rename / delete stays owner-only. The frontend mirrors these rules and **disables** (grays
out) buttons the current role may not press.

### 14.4 REST surface

The Vite dev proxy strips `/api`; the backend mounts the drive routers at the root.

| Area | Endpoints |
|------|-----------|
| Workspaces | `GET/POST /workspaces`, `PATCH/DELETE /workspaces/{id}` (owner), `GET/POST /workspaces/{id}/members` (manager), `PATCH/DELETE /workspaces/{id}/members/{uid}` (manager; admin members owner-only), `GET /workspaces/{id}/activity` (manager) |
| User lookup | `GET /users/search?q=` — resolve a username / user-id fragment to a UUID when adding members |
| Files | `POST /files/init-upload`, `GET /files`, `GET /files/{id}`, `PUT /files/{id}/chunks/{i}`, `GET /files/{id}/chunks`, `POST /files/{id}/complete`, `POST /files/{id}/abort`, `GET /files/{id}/download`, `GET /files/{id}/content` (read a text note), `PUT /files/{id}/content` (overwrite a text note; re-enqueues `ASSET_INGEST`), `PATCH /files/{id}` (rename), `DELETE /files/{id}` (→ trash), `POST /files/{id}/move`, `POST /files/{id}/share`, `DELETE /files/{id}/share/{grantee}`, `GET /files/{id}/shares`, `GET /files/{id}/ingest-status` |
| Folders | `GET/POST /folders`, `PATCH/DELETE /folders/{id}`, `POST /folders/{id}/move` (move a subtree to a new parent, cycle-refused) |
| Trash | `GET /trash`, `POST /trash/{id}/restore`, `DELETE /trash/{id}` (purge), `DELETE /trash` (empty) |

### 14.5 Frontend

`apps/web/src/CloudDrive.tsx` renders the file manager: a tree (**My Drive**, each workspace,
and **🗑 Trash**), list and grid views with folder rows (double-click to enter), a file-name
search with a scope dropdown (all files / a single workspace) and an autocomplete suggestion
list, a chunked upload with progress, and modals for Move / Share / Rename / New folder /
Manage. A **right-click context menu** offers New text file / New folder / Upload / Delete,
and a **note editor** opens any text file in place — an **Edit / Preview** toggle over a
`textarea`, with Preview rendering Markdown through the XSS-safe `renderMarkdown` (`markdown-it` with
`html:false` + a `validateLink` that blocks `javascript:`/`data:` schemes), or — for a Mermaid
mind-map note (`.mmd`, or text starting with `mindmap`) — an **SVG tree diagram** of nodes +
edges via `renderMindmap` (`apps/web/src/mindmap.ts`), **Save**
(`Ctrl+S`, disabled while clean), and a dirty-confirm on close. Saving calls
`PUT /files/{id}/content` and refreshes the row in place. In the **web console** Office documents
preview **in the browser page** instead of downloading: `apps/web/src/FilePreview.tsx` fetches the
bytes via `GET /files/{id}/download` and renders them with the same pure-JS renderers the desktop
app uses —
`.docx` through the vendored **mammoth** browser bundle (`window.mammoth`, loaded as a classic
script in `index.html`, output run through the same DOM sanitizer), `.xlsx`/`.xls` and the
delimited tables `.csv`/`.tsv` (decoded to UTF-8 so SheetJS auto-detects the delimiter) through
the npm **`xlsx`** dep (`XLSX.read` + `sheet_to_html`, one tab per sheet), and the PowerPoint
family (`.pptx`/`.ppsx`/`.potx`/…) through the vendored **JSZip** global with the DrawingML
slide-deck layout ported from `apps/desktop/renderer/pptxview.js`. `officeKindOf(name)` routes
by extension; `.csv`/`.tsv` are excluded from the note editor so they open as tables. `.doc` and
`.ppt` have no browser parser, so they show a **can't-preview** panel — nothing downloads on
click, only the **⬇ Download** (or **↗ Open in new tab**) buttons fetch the bytes. Remaining
binary files (images, PDF, video, audio) still open in a new tab.
A **New text file** modal creates a `.txt` note through the normal chunked-upload flow (the
usual instant-upload dedup applies if the same content is already stored). `App.tsx` opens on the **Cloud Drive** tab by
default with a global topbar (Settings + account chip). The **Manage** modal has two tabs —
**Members** (role-aware dropdowns, add-by-name with autocomplete, remove) and **Activity Logs**
(actor/target search, date range, pagination). Buttons are **disabled** (grayed) rather than
hidden when the current user's role forbids the action, so the permission model stays visible
without leaking state.

### 14.6 Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OBJECT_STORE_ROOT` | `data/objects` | root of the sharded physical object store |
| `DRIVE_CHUNK_SIZE` | `8388608` (8 MB) | upload chunk size used by `init_upload` |
| `DRIVE_MAX_CHUNKS` | `1024` | max chunks per upload (8 MB × 1024 ≈ 8 GB) |
| `DRIVE_MAX_FILE_SIZE` | `0` (unlimited) | max upload bytes |
| `INGEST_CHUNK_CHARS` | `1200` | RAG chunk target length (chars) |
| `INGEST_CHUNK_OVERLAP` | `150` | RAG chunk overlap (chars) |
| `EMBED_BATCH_SIZE` | `16` | embeddings per batch during ingest |

Trash retention is a code constant — `TRASH_RETENTION_DAYS = 30` in `drive_service.py`,
enforced lazily on `list_trash`.

## 15. Desktop Workbench (Electron)

The desktop app (`apps/desktop/`) is a standalone learning workbench with its **own vanilla-JS
renderer** (not the React web UI). It is deliberately decoupled from the backend: the file tree,
viewer, screenshots, and subtitles work offline; chat, sessions, media generation, sign-in /
profile, and the **My Drive cloud panel** need the FastAPI gateway on `localhost:8300`.

- **Main process** (`main.js`) — `contextIsolation: true` / `nodeIntegration: false` with a
  preload `contextBridge` (`window.desktopAPI`). It owns the app menu (**File** = Open
  Workspace… / Add File to Workspace; **View** = reload, zoom, Font Size… → Window & Display
  settings, fullscreen, DevTools; **Help** = Help & Feedback / About / DeepDive on GitHub), a
  custom `local://` protocol that streams local media/documents to the renderer, and an IPC
  surface: folder/file pickers, recursive `read-tree`, copy-into-workspace, delete-file and
  **delete-folder** (both workspace-rooted; folder delete is recursive and refuses the root),
  **create-folder** / **create-text-file** (collision-safe `stem(n)ext` naming, path-escape
  rejected), **move-path** (drag-and-drop), **cloud-cache**, text reads, PDF annotation
  sidecars (`read/save/embed-annotations`), video screenshot saving, subtitle pick/find,
  version/update check, and window prefs. **cloud-cache** streams `GET /files/{id}/download`
  with the session Bearer token into `temp/deepdive-cloud/{assetId}.{ext}` — the extension is
  whitelisted (`[a-z0-9]{1,10}`) so a hostile file name can't escape the cache directory, and
  the path is stable per asset so annotation sidecars survive re-opens. When the backend runs,
  the main process forwards `/api`, `/audio`, and `/images` to it (a zero-length request body is
  sent as an explicit `""` rather than a streamed body, which Chromium would abort).
- **Renderer** (`renderer/`) — an SPA served from `app://bundle/`:
  - **Sidebar** — a **Files** tab and a **Sessions** tab (server-side content search via
    `GET /sessions?q=` with the matching snippet highlighted). A **source switcher**
    (`workspace-source`: **💻 Local** / **☁️ Cloud**) picks which tree fills the sidebar. The
    local tree keeps the client-side fuzzy search and adds a **live suggestion dropdown**
    (`fuzzyScore` prefix > substring > path > subsequence scoring over the flattened tree; Enter
    opens the hit, arrows navigate, a hit expands its ancestor chain to reveal) plus a
    right-click context menu — New folder, New text file, Delete file / Delete folder (permanent,
    workspace-bounded). The last workspace folder is persisted in `localStorage` and re-opened on
    launch.
  - **Cloud Drive panel** (`clouddrive.js` + `viewer.js`) — the **☁️ Cloud** source is a full
    cloud-file manager aligned with the web console, over the same `/api/*` (Bearer token from
    `localStorage["deepdive_token"]`). A tree shows **My Drive**, every **workspace** (with its
    subfolders), and **🗑 Trash** at the bottom; selecting a node navigates the main area to that
    scope (`loc = root | workspace | folder | trash`). The main area is a **list / grid toggle**
    (☰ / ▦) with a **five-column table** — `Name | Size | RAG Status | Query Repo | Updated`
    (Trash: `Name | Deleted`) — directory rows double-click to enter, file rows click to open, and
    a per-scope **search** box filters by name. **✏ Edit** mode adds selection checkboxes and a
    **batch bar** (Download / Open / Share / Rename / Move / Trash; in Trash: Restore / Delete
    permanently / Empty Trash). A toolbar offers **⚙ Manage** (workspace members + activity logs,
    role-gated `canManage`), **＋ New folder**, **＋ New text**, and **⬆ Upload** (role-gated
    `canWrite`). The **Query Repo** column renders a status cell — `✓ In Knowledge` /
    `Importing…` / `Processing… (ETA)` / `＋ Import to Knowledge` / `Not supported` — driven by
    `ragCell(f)` + `ingestEtaSuffix`, with a 5 s `pollWhileWorking` re-poll. Clicking a
    `.md`/`.txt`/code row opens the in-window **note editor** (`#note-editor`); any other file is
    cached via `cloud-cache` and rendered by `Viewer.render` on the temp path, so PDFs, images,
    video, and audio play in window.
  - **Note editor** — an overlay with an **Edit / Preview** icon-button toggle and **Save**
    (`Ctrl+S`). Edit mode is a monospace `textarea`; Preview renders the draft through the
    vendored `markdown-it` + `katex` chat renderer (`renderMarkdown`, XSS-safe `validateLink`),
    or — for a Mermaid mind-map note (`.mmd` / `mindmap`-prefixed text) — as an **SVG tree of
    nodes + edges** via `renderMindmap` (same layout as the web's `mindmap.ts`).
    Save calls `PUT /files/{id}/content` and closes the dirty flag; a dirty close asks to
    discard. Because the server is the source of truth, a note saved here shows up in the web
    console (and vice versa) on refresh.
  - **Viewer** (`viewer.js`) — dispatches by extension: video, audio, image, PDF (pdf.js with a
    sidecar-annotation overlay), text/code, and Office documents previewed **in-window with
    pure-JS renderers** (no LibreOffice / OS app required): `.docx` via the vendored
    **mammoth** browser bundle (`mammoth.convertToHtml({ arrayBuffer })`, output run through a
    DOM sanitizer that drops `script`/`iframe`/`object`, `on*` handlers, and non-`image/*`
    `data:` / `javascript:` URLs); `.xlsx`/`.xls` and the delimited-text tables `.csv`/`.tsv`
    (decoded to UTF-8 so SheetJS auto-detects the delimiter) via vendored **SheetJS**
    (`XLSX.read` + `sheet_to_html`, one tab per sheet); PowerPoint via vendored **JSZip** +
    `pptxview.js` (a small DrawingML parser that reads `ppt/slides/slideN.xml`, lays out text
    shapes and `p:pic` images at absolute EMU-derived positions, and renders a prev/next slide
    deck). The whole PowerPoint family is the same OOXML zip, so `.pptx`/`.ppsx`/`.potx`/
    `.pptm`/`.ppsm`/`.potm` all route through it. Office bytes are read through the `read-file-bytes` IPC (Buffer → Uint8Array). Legacy binary `.doc` is extracted in the main process via **word-extractor** (`word-extract` IPC): its text is
    shown as paragraphs plus any embedded raster images (PNG/JPEG/GIF/BMP pulled from the raw
    bytes, deduped, rendered unpositioned — layout isn't preserved); unparseable `.doc` files
    and `.ppt` fall back to the OS default app. The toolbar carries a **✕** close button and **Esc** also dismisses the current
    document (both guarded so they never fire while typing in a field, in fullscreen, or with a
    modal overlay open); closing wipes `#viewer` and restores its empty "Select a file…" state,
    which also resets `state.path/kind/openPath`.
  - **Video subtitles** — auto-detects a sibling `.srt`/`.vtt`/`.lrc` via `find-subtitle`, or
    loads a user-picked file (**Add Subtitle**, picker defaulting to the video's folder). A
    **Subtitles** dropdown lists **Enable / Disable / Add / Subtitle Settings**; the style panel
    (size, color, background, position) is persisted to `localStorage`
    (`deepdive_subtitle_style`) and restored on the next launch.
  - **Chat** (`app.js`) — consumes the SSE `POST /chat/stream` endpoint
    (`EventSourceResponse`) with a collapsible **💭 thinking** block and incremental answer
    rendering; the pane docks bottom/right or floats as a draggable window. Splitter and
    floating-window drags use **pointer events + `setPointerCapture`**, so drag tracking continues
    even when the pointer passes over the `<video>` element. Sign-in / register / password-reset
    and profile/avatar editing are modal dialogs against `/auth/*`. The **input box** is a
    Gemini-style row — a **＋ attach** button, a **multi-line `<textarea>`** (3 rows by default;
    **Enter sends** the message, **Shift+Enter** inserts a new line), inline **🎤 / 🔊**
    buttons, and Send — with an attachment preview strip above it. Attach stages a pending
    attachment that rides on the next send: pick a file (OS picker → uploaded to the cloud
    drive), attach the currently-open cloud asset by id, or capture a **window screenshot**
    (`capture-window` IPC → base64 → uploaded); the API prefixes an `[Attached: …]` note to the
    user message so the agent's document tools can fetch the bytes by `asset_id`. The chat-header
    also carries a **⋯** session menu (pin / rename / **Import to Knowledge** / delete, plus
    **Generate Mind Map / Generate Slides / Summarize & Save Notes**) and a **hide** toggle that
    collapses the chat into a floating restore icon; a **Generate** toolbar above the input
    exposes the same three entries as one-click buttons.

    Every generation entry opens the **generate dialog** (see §6): pick the source — **this
    conversation** or **Cloud Drive files** (a checkbox-tree picker, `pickCloudFiles`, that greys
    out files over the `/toolkit/config` per-file cap or in an unsupported format) — then the
    output folder, an optional custom prompt, and an optional file name. Submit enqueues
    `POST /toolkit/generate` in session or cloud-file mode. While the job runs the dialog's
    Generate button greys out and the toolbar button becomes a steady **⏳**; the job is tracked
    in `toolkitJobs[tool]` and polled every 2 s until a terminal state (see §8.1), so closing the
    dialog never aborts the run — the status box live-updates while open and the toolbar recovers
    when the job finishes. On completion every produced artifact is listed with its folder, a
    **view output** link navigates the Cloud Drive to that folder (`openCloudFolder`), and the
    drive tree refreshes.
  - **Chat bubbles & message management** — every user/assistant bubble renders through the
    vendored `markdown-it` (`renderMarkdown`, `html:false` + XSS-safe `validateLink`) with a
    **KaTeX math plugin** (`$...$` inline, `$$...$$` display — block rule registered before
    `lheading`, inline rule after `escape`; `throwOnError:false` degrades an unparseable formula
    to its raw source instead of breaking the bubble). Streamed deltas re-render incrementally,
    so math/Markdown appears live. Each bubble carries a **Copy / Read / Delete / Edit /
    Import to Knowledge** action row (`buildMsgActions`): **Read** speaks the message aloud via
    the server-side streaming **Kokoro TTS** (`POST /tts/stream` → SSE) with a sequential
    segment queue (`speakMessage` / `voiceStop`, abortable and tagged by generation); **Delete**
    opens a per-turn selection to tick the question and/or answer, then issues
    `DELETE /sessions/{id}/messages/{mid}` per message; **Edit** (user messages only) follows
    the **edit = re-ask** model — the edited message and every later one are deleted server-side
    + DOM, then the question is re-sent through the stream so a fresh answer regenerates (there
    is no server-side message-edit endpoint; the client rewrites the history). **Import to
    Knowledge** writes the Q&A pair to the query repository (§10.8) and flips to a disabled
    **✓ Imported** once covered, driven by `GET /chat/imported` per user message. Sessions are
    **renamed** inline (click the chat-header title or a sidebar row → `PATCH /sessions/{id}`;
    empty title resets auto-naming) or **deleted** from the sidebar (`DELETE /sessions/{id}`);
    `GET /sessions/{id}/messages` reloads a session's bubbles.
  - **Settings** — a modal with five tabs: **Appearance** (theme), **Window & Display** (font
    size), **Updates** (GitHub release check), **Help & Feedback**, and **About**.

## 16. Prompt Module

This chapter gives a single, systematic overview of the prompt module — the three concerns the
kernel wires into every LLM request: **cache-boundary assembly** (a byte-stable head the provider
reuses in its prefix cache), **compression** (a bounded, token-aware window), and **deferred tool
loading** (a stable tool set whose full schemas ride below the cache boundary). The detail lives
in the component sections — §5.3 (assembler internals), §5.2 (loop integration), §6.5 (stub
permissions & sandbox) — and the README's *Prompt* diagram visualizes the whole flow end to end.

### 16.1 Goals

1. **Prefix-cache reuse** — the stable part of the system prompt is byte-identical across
   requests and steps, so providers hit their KV-cache prefix instead of re-processing the head.
2. **A bounded prompt window** — history is capped on both message count and total characters,
   and individual oversized messages are trimmed before they reach the request.
3. **A stable tool set** — the model-visible `tools` array never churns; new tools appear as
   stable stubs, never as freshly-injected full schemas.
4. **Measurable cache identity** — `snapshot_key()` exposes the stable head's hash so cache
   effectiveness is observable rather than assumed.

### 16.2 Three-zone cache-boundary assembly

`CacheBoundaryAssembler` (a `SystemPrompt` subclass) partitions sections into three `PromptZone`s,
merged ascending by `order` within each zone:

| zone | content | stability |
|---|---|---|
| `STATIC_PREFIX` | SOUL.md identity (`soul` section, `PERSONA_ORDER=0`) + compact tool catalog + compressed skill catalog (`HARNESS_IDENTITY_ORDER=-100` / `SKILLS_ORDER=250`) | byte-identical across requests → prefix-cache reuse |
| `PROJECT_CONTEXT` | workspace `DEEPDIVE.md` conventions (`PROJECT_CONTEXT_ORDER=-90`) | stable per project; renders nothing when absent |
| `DYNAMIC_SUFFIX` | session memory brief + proactive recall (`MEMORY_ORDER=200` / `+10`) + `inject()` content | re-rendered per step |

`assemble()` resolves the static and project zones once and caches them
(`_cached_static` / `_cached_project`), then renders the dynamic suffix. `refresh_dynamic(context)`
recomputes only the dynamic suffix on each loop step — its sections (memory brief, recall) plus any
injected content — so an unchanged suffix means the system message is not re-sent and the cached
head is reused. Recalled memory is gated by `MemoryService.should_recall` (Lane-2); the Lane-1
memory brief always injects.

### 16.3 Rendering and cache identity

`assemble()` returns a `PromptAssembly {static_prefix, project_context, dynamic_suffix, tools,
variables}`; `render_prompt(assembly)` joins the stable head (`static_prefix + project_context`)
and the dynamic suffix with plain `"\n\n"`. The `CACHE_BOUNDARY` constant (`"\n\n<CACHE_BOUNDARY/>\n\n"`)
is an **internal-only** separator: it documents the token-position split between head and suffix
but is deliberately **never rendered** — the model never sees the literal. `snapshot_key()` returns
`sha256(static + "\n\n" + project)[:16]`, the observable identity of the stable head, so the
project-context zone is part of the prefix-cache contract.

### 16.4 Project context loader

`agent/tools/project_context.py::read_project_context(workspace, *, files, max_chars)` reads the first
existing convention file (`DEEPDIVE.md` by default) under the agent's workspace, caps it at
`settings.project_context_max_chars` (appending a
`…(truncated)` marker), and returns `""` when none exists. The kernel registers a non-empty result
into `PromptZone.PROJECT_CONTEXT`; an empty zone renders nothing, keeping the prompt byte-identical
to the no-context case. The loader runs in `apps/api/deps.py` and feeds the value into the kernel,
so project rules reach the model on every turn and feed `snapshot_key()`.

### 16.5 Compression pipeline

The prompt window is bounded at two levels:

- **Per-message snip** — the loop (`_snip` / `_snip_messages`) caps each message's content at
  `settings.prompt_message_max_chars` when building the LLM request. It trims **only the request
  snapshot** (a shallow copy); the `messages` list the persistence layer keeps stays raw.
- **Token-aware autocompact** — `compact_history` fires when the message count exceeds
  `settings.history_max_messages`, **or** when it sits between `history_keep_messages` and the max
  while the total character budget `settings.prompt_max_chars` is exceeded. It keeps the latest
  `history_keep_messages` and folds the overflow into a hierarchical recap (L2 prior-window coarse
  summaries + L1 current summary, injected as a leading system message). Histories at or below
  `history_keep_messages` always pass through — nothing to drop, and oversized singles are handled
  by the snip. If the summary call fails the overflow is still dropped (bounded window beats an
  unbounded request).

### 16.6 Deferred tool loading (defer_loading stubs)

`ToolGateway.visible_schemas(context)` computes the model-visible tool set as
`core ∪ mounted ∪ scope-allowlist − denylist`, in deterministic registration order. Stable tools
(core + scope-allowlisted) carry **full schemas**; deferred tools mounted mid-run appear as stable
`defer_loading` stubs — `{name, description (≤ STUB_DESC_MAX=400), parameters: {properties: {}}}` —
so the cached `tools` array stays byte-stable across steps. A stub's full schema reaches the model
through the `tool_search` **result message** (below the cache boundary): the builtin `tool_search`
scores the catalog (name hits beat blurb hits), `mount()`s each match into the visible set, and
returns `parameters` from `gateway.schema_of(name)`. Execution is unaffected: `_dispatch` resolves
the tool by name from the runtime, so calling a stub runs the real tool, still behind the `Sandbox`
permission guard (a READ-only session cannot gain write tools by mounting them).

### 16.7 Per-step process

1. **Session start** — `kernel.run` begins the memory session; `ReactLoopAgent.run` then calls
   `assembler.begin_session()` (clearing prior `inject()` content) and `gateway.reset_session()`
   (clearing the mounted tool set).
2. **Assemble** — the three zones render; the static/project head is cached once.
3. **Compact** — `compact_history` bounds the history window by count and character budget.
4. **Snip** — the request snapshot is built as `system + snipped messages`; persistence keeps full text.
5. **Call** — the LLM sees `render_prompt(assembly)` plus the visible tools (full core schemas +
   stubs); a tool result may trigger `tool_search` → mount → richer visible set next step.
6. **Refresh** — `refresh_dynamic(context)` recomputes only the suffix; an unchanged suffix means
   the system message is not re-sent and the head is reused.
7. **Stream** — `run_stream` follows the same pipeline (assemble → snip → call → refresh) per step.

### 16.8 Configuration

| setting | default | role |
|---|---|---|
| `prompt_max_chars` | `120_000` | total-window character budget for token-aware autocompact |
| `prompt_message_max_chars` | `8_000` | per-message snip cap on the request snapshot |
| `project_context_files` | `["DEEPDIVE.md"]` | convention files tried in order |
| `project_context_max_chars` | `8_000` | cap on the project-context zone, with truncation marker |
