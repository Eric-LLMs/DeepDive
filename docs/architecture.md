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
| Agent runtime | Cordis-style DI (`Context`/`Fiber`) + layered `SystemPrompt` + `ReactLoopAgent` step loop + plugin `ToolRuntime` |
| Retrieval | RAG pipeline (rewrite → recall → RRF → rerank); `in_process` default, gRPC service available |
| Model services | TEI embedding (BGE-M3), Kokoro TTS, LiteLLM gateway (all Docker) |
| Async enrichment | gateway + arq worker split; `jobs` table is the source of truth; frontend polls `GET /jobs/{id}` |
| Session memory | PG-backed `sessions` / `messages` / `session_events` + deferred embed+summary finalize |
| Migrations | Alembic (replaces `create_all` + manual `ALTER`) |
| Chat | agent loop with tool use, SSE streaming |

### Designed, not yet implemented

| Area | Gap |
|----|------|
| Auth / multi-tenancy | users / roles / user_roles tables + PostgreSQL RLS not created |
| Billing | subscriptions / credit_ledger not created |
| Observability | audit_logs / ai_call_logs / activity_logs / job_logs not created |
| Video / document learning | media → chunks pipeline (ingestion / transcription / chunking) designed only |
| Reranking | in-process cross-encoder exists but disabled (`reranker_model` empty); TEI reranker not wired |
| Edge gateway | Traefik skeleton only; dev runs FastAPI directly on the host |

## 1. Product Positioning

DeepDive is an "AI learning workbench" unified by a single abstraction:

- **Vocabulary learning**: domain vocabulary + example sentences + definitions + TTS + images + star ratings
- **Video / document learning**: media → timestamped/paginated text chunks → searchable, annotatable
- **AI chat assistant**: interactive Q&A with tool use (RAG + vocabulary lookup + external MCP)
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
| LLM gateway | LiteLLM Proxy | unified routing / keys / cost |
| Embedding | BGE-M3 (dim 1024) via TEI | multilingual, moderate dim |
| Reranking | BGE-reranker cross-encoder | post-recall precision |
| Retrieval (RAG) | query rewrite → pgvector + tsvector recall → RRF fusion → rerank | hybrid keyword + semantic search |
| Agent | Cordis-style DI (`Context`/`Fiber`) + `ReactLoopAgent` step loop + layered `SystemPrompt` + plugin tool runtime | controllable, testable, plugin-based |
| MCP | FastMCP | tool exposure + bidirectional integration |
| Speech | STT: faster-whisper / Whisper API; TTS: local Kokoro-82M | |

**Language boundary**: backend in Python (AI deps), frontend in TypeScript. The boundary is
API-only: REST/SSE at the edge, gRPC between internal services, HTTP to model services.

## 3. Repository Structure (Monorepo)

```
deepdive/
├── apps/
│   ├── api/                      # package `api` (FastAPI gateway: REST/SSE + job enqueue)
│   │   ├── main.py               # uvicorn api.main:app (REST/SSE endpoints + GET /jobs/{id})
│   │   ├── deps.py               # DI assembly (capability seam + tools + plugins + prompt)
│   │   ├── tools.py              # gateway built-in tools (rag_search / translate)
│   │   └── schemas.py            # Pydantic request/response models
│   ├── worker/                   # arq worker (executes async enrichment jobs)
│   │   ├── settings.py           # WorkerSettings (functions / redis / startup clients)
│   │   ├── tasks.py              # tts / image_fetch / explain / ... job functions
│   │   └── main.py               # run_worker entrypoint
│   ├── retrieval/                # retrieval service (gRPC), run: python -m apps.retrieval.main
│   │   ├── server.py             # RetrievalService servicer (proto -> RAGPipeline)
│   │   └── main.py               # grpc.aio.server entrypoint
│   ├── web/                      # Vite + React frontend (TS)
│   └── desktop/                  # Electron shell (placeholder)
├── packages/
│   ├── agent/                    # package `agent`: DI + loop + runtime + plugins + memory + skills + prompt
│   ├── rag/                      # package `rag`: retrieval DAG + build_pipeline factory
│   ├── core/                     # package `core`: config + domain/application/ports/infrastructure
│   └── shared/proto/retrieval/   # generated protobuf/gRPC stubs (import name `retrieval.v1`)
├── migrations/                   # Alembic migrations (env.py + versions/)
├── proto/retrieval/v1/retrieval.proto   # RetrievalService contract
├── buf.yaml / buf.gen.yaml             # proto lint / breaking / codegen
├── scripts/gen_proto.sh                # grpc_tools.protoc (or buf) codegen
├── deploy/
│   ├── traefik/                  # gateway static + dynamic config
│   ├── retrieval/Dockerfile      # retrieval service image
│   ├── worker/Dockerfile         # arq worker image
│   └── litellm/config.yaml
├── tests/                        # pytest (di / system_prompt / loop / memory / rrf / jobs / tool_runtime)
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

The `agent` package is the pluggable agent runtime. Three pieces compose a turn: a Cordis-style
**DI state machine** wires plugins into a shared `Context`, the **`ReactLoopAgent`** runs the
tool-use loop, and a layered **`SystemPrompt`** assembles the model context. Memory, skills, and
the append-only session log are optional collaborators injected into the loop.

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

### 5.3 System prompt — layered `SystemPrompt`

Prompt sections register with an `order` and merge ascending, so persona / tool guidance /
memory / skills each contribute independently without knowing one another. Order conventions:
harness identity `-100` < persona `0` < tool guidance `100` < memory `200` < skills `250`.
A section's text may be static or an async callable over the assemble context (used for on-demand
memory/skill retrieval). `{{name}}` placeholders interpolate from registered variables;
`render_prompt()` drops empty sections and joins the rest with blank lines.

### 5.4 Memory, skills, sessions

- **Memory** — `MemoryStore` protocol (`load`/`save`/`list`/`search`) + `FileMemoryStore`
  (claude-code memdir style: `MEMORY.md` index + one frontmatter `.md` per memory,
  description-weighted keyword recall). `Memory` records carry a `type` in
  {`user`,`feedback`,`project`,`reference`} and a staleness caveat via `age_days`.
- **Skills** — `Skill` (Markdown instructions + frontmatter + keywords) registered in a
  `SkillRegistry` (`register` / `relevant(query)` keyword scoring / `from_dir` for `*.skill.md`).
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
define_tool(name, description, parameters, output, execute, destructive, is_concurrency_safe)
  → ToolDefinition
```

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

- **Model inference never runs in the API/retrieval/worker process** — embedding/TTS/LLM are
  separate containers; model updates don't restart the app. Reranking is the exception: the
  cross-encoder loads in-process via `sentence-transformers` when `reranker_model` is set
  (disabled by default).
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

## 12. Data Model (Core Table DDL)

> **Migration note:** the implemented schema is managed by **Alembic**
> (`migrations/versions/0001_init.py`); `init_db()` runs `alembic upgrade head` at startup
> (replacing the old `create_all` + manual `ALTER`). The implemented tables include `sessions`,
> `messages`, `session_events`, and `jobs` — which the design DDL below expresses as
> `conversations` / `messages` / `job_logs`. The DDL below is the full designed schema; some
> tables are design-only (auth / billing / observability).

Unified multi-tenancy: business tables carry `user_id`, enabling PostgreSQL RLS.

```sql
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE roles (
    id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE user_roles (
    user_id UUID REFERENCES users(id),
    role_id UUID REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE domains (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE materials (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    type       TEXT NOT NULL,        -- 'domain' | 'video' | 'document'
    title      TEXT NOT NULL,
    source_url TEXT,
    meta       JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    material_id UUID REFERENCES materials(id),
    seq         INT NOT NULL,
    content_en  TEXT NOT NULL,
    content_cn  TEXT,
    meta        JSONB DEFAULT '{}',
    embedding   vector(1024),
    UNIQUE (material_id, seq)
);

CREATE TABLE terms (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id),
    domain_id   UUID REFERENCES domains(id),
    word        TEXT NOT NULL,
    definition  TEXT,
    frequency   INT DEFAULT 0,
    star_level  INT DEFAULT 0,
    audio_hash  TEXT,
    image_paths JSONB DEFAULT '[]',
    is_active   BOOLEAN DEFAULT true
);

CREATE TABLE sentences (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID REFERENCES users(id),
    domain_id     UUID REFERENCES domains(id),
    origin_source TEXT,
    content_en    TEXT UNIQUE NOT NULL,
    content_cn    TEXT,
    audio_hash    TEXT,
    cn_explanation TEXT
);

CREATE TABLE matches (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term_id       UUID REFERENCES terms(id),
    sentence_id   UUID REFERENCES sentences(id),
    cn_explanation TEXT
);

CREATE TABLE enrichment (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (content_hash, kind)
);

CREATE TABLE conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    title      TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tool_calls      JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### 12.1 Indexes and Retrieval

```sql
ALTER TABLE chunks ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content_en)) STORED;
CREATE INDEX ON chunks USING GIN (fts);
CREATE INDEX ON chunks USING ivfflat (embedding vector_cosine_ops);
-- or HNSW for large data: CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
```

### 12.2 Billing and Logs

```sql
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    plan TEXT NOT NULL, status TEXT NOT NULL,
    started_at TIMESTAMPTZ DEFAULT now(), ends_at TIMESTAMPTZ
);
CREATE TABLE credit_ledger (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    amount BIGINT NOT NULL, balance_after BIGINT NOT NULL, reason TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE audit_logs (
    id BIGSERIAL PRIMARY KEY, user_id UUID, action TEXT, entity TEXT, entity_id UUID,
    before JSONB, after JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE ai_call_logs (
    id BIGSERIAL PRIMARY KEY, user_id UUID, model TEXT,
    prompt_tokens INT, completion_tokens INT, latency_ms INT, cost_micro BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE activity_logs (
    id BIGSERIAL PRIMARY KEY, user_id UUID, event TEXT, payload JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE job_logs (
    id BIGSERIAL PRIMARY KEY, job_type TEXT, status TEXT, input JSONB, error TEXT,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
```

## 13. Multi-Tenancy and Deployment Strategy

| Scenario | Strategy |
|------|------|
| B2C (multi-user) | Shared DB + PostgreSQL RLS row-level isolation |
| B2B (enterprise) | database-per-tenant |
| Read scaling | read replicas + pgBouncer connection pool |
| Edge vs internal | external REST/SSE via Traefik ↔ internal gRPC |
| Model scaling | separate model services (TEI/Kokoro/LiteLLM), independent scale-out |
