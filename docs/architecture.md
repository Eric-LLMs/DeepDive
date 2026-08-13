# DeepGloss Architecture Design

> This document is the single source of truth (SSOT) for the DeepGloss project. All technical decisions, data models, and module boundaries are governed by this document.

> **Implementation status:** the codebase currently implements the **vocabulary** subdomain end-to-end — domains, terms, example sentences, hybrid search, TTS, image fetching, AI enrichment (explain / definition / syntax analysis), and a native function-calling Agent with RAG/MCP scaffolding. The **video / document** materials pipeline, multi-tenant auth/roles, billing, and internal gRPC are designed but not yet implemented.

## 1. Product Positioning (Requirements)

DeepGloss has evolved from a "domain-specific English vocabulary learning assistant" into an "AI learning workbench":

- **Vocabulary learning** (original capability): domain vocabulary + example sentences + definitions + TTS + images + star ratings
- **Video learning**: video → timestamped text chunks → searchable, annotatable
- **Document learning**: PDF/text → paginated text chunks → searchable, annotatable
- **AI chat assistant** (new): interactive Q&A like Gemini + voice chat
- **Unifying principle: everything is a text chunk**

## 2. Tech Stack

| Layer | Choice | Rationale |
|----|------|------|
| Frontend | **Vite** + **React 18** + TypeScript | Lightweight SPA; UI unified in TS, talks to the backend via REST/SSE only |
| Backend API | **FastAPI** | Async, native SSE/WebSocket streaming, auto OpenAPI |
| Database | **PostgreSQL** + **pgvector** + **tsvector** | One database solves relational, vector, and full-text search, avoiding the distributed tax of ES/Qdrant/ChromaDB |
| ORM | **SQLAlchemy 2.0** (async) | Native async, type-friendly |
| Config | **pydantic-settings** | Environment variables + type validation |
| Cache / queue | **Redis** (Streams + cache) + **arq** | AI result caching, async enrichment, rate limiting |
| LLM gateway | **LiteLLM Proxy** | Unified key management, routing, cost tracking |
| Embedding | **BGE-M3** (dim 1024) | Chinese-friendly, multilingual, moderate dimensionality |
| Reranking | **BGE-reranker** (cross-encoder) | Post-recall reranking to improve hit precision |
| Agent framework | **Native function-calling loop** (no LangChain/LangGraph) | Controllable, testable, avoids framework lock-in |
| MCP | **FastMCP** | Tool registration + bidirectional integration |
| Speech | STT: faster-whisper / Whisper API; TTS: existing TTS + content-hash caching | |
| Internal comms | External REST/SSE + internal gRPC | Browsers can't speak gRPC; internal services use gRPC for speed |

**Language boundary**: backend in Python (hosting the AI dependencies: BGE-M3 / pgvector / torch / speech processing), frontend in TypeScript (React). The boundary is API-only: both sides interact only via REST/SSE (and internal gRPC), with no shared runtime.

## 3. Repository Structure (Monorepo)

```
deepgloss/                        # repo root
├── apps/
│   ├── api/                      # package `api` (FastAPI backend)
│   │   ├── __init__.py
│   │   ├── main.py               # entry: uvicorn api.main:app
│   │   ├── deps.py               # dependency assembly (wires core usecases/ports/implementations)
│   │   └── schemas.py            # Pydantic request/response models
│   ├── web/                      # Vite + React frontend (TS)
│   └── desktop/                  # Electron thin shell (placeholder, empty)
├── packages/
│   ├── core/                     # package `core` (domain services, pure Python, zero framework deps)
│   │   ├── config.py             # config (pydantic-settings)
│   │   ├── domain/               # entities, value objects
│   │   ├── application/          # usecase orchestration
│   │   ├── ports/                # interfaces (Repository / LLMPort / TTSPort / VectorPort)
│   │   ├── infrastructure/       # implementations (postgres / openai / tts / pgvector)
│   │   ├── rag/                  # retrieval module (rewrite → recall → fusion → rerank)
│   │   └── agent/                # agent module (loop + tools + plugins + memory + skills)
│   └── shared/                   # shared types, constants, protocol definitions (placeholder, empty)
├── docs/
│   ├── architecture.md           # this document
│   └── images/                   # README images
├── scripts/                      # init_db.py / setup.sh
├── .github/workflows/            # CI/CD (placeholder, empty)
├── docker-compose.yml            # Postgres+pgvector / Redis
└── pyproject.toml
```

> Note: `packages/core` and `apps/api` are each independent Python packages (import names `core` and `api` respectively), no longer nesting a `deepgloss` package-name layer.

## 4. Layered Architecture (Hexagonal / Clean Architecture)

```
apps/api  (FastAPI)          → only "translation": HTTP/SSE/gRPC ↔ usecases
     │
     ▼
packages/core/application    → usecase orchestration (business rules)
     │
     ▼
packages/core/ports          → interface definitions (Repository / LLMPort / TTSPort / VectorPort)
     │
     ▼
packages/core/infrastructure → concrete implementations (postgres / openai / tts / pgvector)
```

**Organization: technical capability + business subdomain (dual-axis feature-folder)**

- **Technical capability** (horizontal, cross-cutting all subdomains): `agent/`, `rag/`, and `llm / embedding / tts / vector / mcp` under `infrastructure/`
- **Business subdomain** (vertical): `vocabulary`, `materials` (media/video/document), `assistant` (chat assistant)

**Core principle: dependencies point inward.** Domain logic (`domain` / `application`) depends on no framework; `ports` only define interfaces; `infrastructure` provides implementations. The upper `apps/api` injects implementations into usecases via dependency assembly (`deps.py`).

## 5. Architecture Principles  

1. **Small, testable loop**: the main flow uses an explicit `while` loop (not config nodes), each step independently unit-testable.
2. **Tool registry as single source of truth**: a tool is defined once, shared by Agent / RAG / MCP.
3. **Layered memory**: index (MEMORY.md) + topic files (frontmatter) + session/disk/vector layers.
4. **Hooks for extension**: main flow hard-coded, extension points injected via hooks, non-invasive to the core loop.
5. **Modular monolith**: deploy as a monolith first, keep module boundaries clear for a later service split.
6. **Language follows dependencies**: AI-heavy dependencies stay in the Python backend, UI in TS, boundary API-only.
7. **API-only boundary**: modules communicate only via interfaces, no shared implementation details.
8. **Everything is a text chunk**: all learning content (video/document/vocabulary) unified under the text-chunk abstraction.

## 6. RAG Module (Config-Node DAG)

Retrieval is a **directed acyclic graph (DAG)**, using "declarative node order + parameters"; each node class hand-writes its logic, factory functions assemble them:

```
rewrite → multi-recall → RRF fusion → rerank
```

- **Rewrite `query_rewrite.py`**: multi-query (multiple query variants) + HyDE (hypothetical document); LLM outputs JSON with `_strip_code_fence` fallback parsing.
- **Multi-recall `recall/`**: `VectorRecaller` (semantic, pgvector) + `KeywordRecaller` (keyword, tsvector FTS).
- **Fusion `rank/rrf.py`**: RRF (Reciprocal Rank Fusion), k=60.
- **Rerank `rank/cross_encoder.py`**: BGE-reranker, lazy loading, `asyncio.to_thread` to run the model.
- **Orchestration `pipeline.py`**: `RAGPipeline.retrieve(query, top_k, filters)` chains the whole pipeline.

**Why config nodes for RAG**: retrieval is a deterministic pipeline — stages are fixed, composable, and individually togglable (e.g., disable rewrite / rerank), which suits a declarative DAG.

## 7. Agent Module (Code Loop + Hooks + Plugins + Memory + Skills)

Referencing claude-code / openclaw: the Agent is the **brain**; the main flow is a hard-coded `while` loop; extension is injected via hooks (not config nodes).

```
plugins/                    # plugin system
├── hooks.py                #   HookEvent / HookContext / HookResult / Hook
├── base.py                 #   Plugin (packaging unit for hooks+tools+skills)
├── manager.py              #   PluginManager (registration + directory discovery + hook dispatch)
└── builtin.py              #   builtin plugins (e.g., tool_audit intercepts destructive tools)
memory/                     # layered memory
├── base.py                 #   MemoryStore abstraction (load/save/search)
└── file.py                 #   FileMemoryStore (MEMORY.md index + frontmatter topic files)
skills.py                   # Skill (Markdown-configured instructions, not code) + SkillRegistry
context.py                  # ContextBuilder (system + memory + relevant skill + history)
loop.py                     # Agent main loop + AgentLLMPort + AgentResult
harness.py                  # FakeLLM and other offline test utilities
tools.py                    # Tool + ToolResult + ToolRegistry + build_default_tools
```

**Core mechanisms:**

- **Loop**: `while` up to `max_steps` steps; each step calls `llm.chat(messages, tools)`, executes any `tool_calls` and feeds results back, otherwise ends. Native function-calling, no prompt+JSON.
- **Hook lifecycle**: `SESSION_START` → (`PRE_TOOL_USE` → execute tool → `POST_TOOL_USE`)×N → `SESSION_END`, plus `PRE_COMPACT`. `HookResult` has three verdicts: `continue` (allow) / `block` (deny) / `modify` (rewrite args).
- **Plugin = register ≠ execute**: `register()` only attaches hook/tool/skill schemas to the registry (cheap); handlers run only when the loop actually triggers them. Builtin plugins register explicitly; third-party plugins use directory discovery (scan `*/plugin.py` module-level `PLUGIN` objects).
- **Skill = Markdown config**: one `.skill.md` = frontmatter metadata (name/description/keywords) + instructions body. The Agent matches keywords to inject relevant skill instructions into context, then follows them. Skills are "recipes", not code.
- **Layered memory**: `FileMemoryStore` implements the claude-code memdir style — `MEMORY.md` as the index, each memory a frontmatter topic file; the interface abstracts as `MemoryStore`, later replaceable by session/vector layers.

## 8. Tool Registry (Single Source of Truth)

```
Tool Registry (single definition: name + JSON Schema + handler)
  ├─ Agent   consumes (function-calling → get_for_agent())
  ├─ RAG     consumes (rag_search tool)
  └─ MCP     consumes (FastMCP exposes → all())
```

A tool is defined once, shared by three consumers. `ToolResult` uniformly wraps `data / error / new_messages`; errors fold into readable results fed back to the LLM.

## 9. Data Model (Core Table DDL)

Unified multi-tenancy: all business tables carry `user_id`, enabling PostgreSQL RLS.

```sql
-- Users and authentication
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- Roles and permissions
CREATE TABLE roles (
    id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL      -- 'free' | 'pro' | 'admin'
);
CREATE TABLE user_roles (
    user_id UUID REFERENCES users(id),
    role_id UUID REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

-- Learning domain: vocabulary domain
CREATE TABLE domains (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Unified material: video / document / vocabulary domain are all one kind of learning material
CREATE TABLE materials (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    type       TEXT NOT NULL,        -- 'domain' | 'video' | 'document'
    title      TEXT NOT NULL,
    source_url TEXT,
    meta       JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Text chunk (core abstraction: everything is a text chunk)
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    material_id UUID REFERENCES materials(id),
    seq         INT NOT NULL,        -- order (video = timestamp order, document = page order)
    content_en  TEXT NOT NULL,
    content_cn  TEXT,
    meta        JSONB DEFAULT '{}',  -- timestamp / page etc.
    embedding   vector(1024),        -- pgvector, dimension follows the embedding model
    UNIQUE (material_id, seq)
);

-- Vocabulary (migrated original capability)
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

-- Example sentence (migrated original capability)
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

-- Match relation (term ↔ sentence, with AI explanation)
CREATE TABLE matches (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term_id       UUID REFERENCES terms(id),
    sentence_id   UUID REFERENCES sentences(id),
    cn_explanation TEXT
);

-- AI enrichment result cache (content-addressed)
CREATE TABLE enrichment (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_hash TEXT NOT NULL,       -- md5(input + model + voice + params)
    kind        TEXT NOT NULL,        -- 'tts' | 'image' | 'explanation' | ...
    payload     JSONB,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (content_hash, kind)
);

-- Conversations and messages (new AI assistant)
CREATE TABLE conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    title      TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id),
    role            TEXT NOT NULL,     -- 'user' | 'assistant' | 'system' | 'tool'
    content         TEXT NOT NULL,
    tool_calls      JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### 9.1 Indexes and Retrieval

```sql
-- Full-text search (tsvector)
ALTER TABLE chunks  ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content_en)) STORED;
CREATE INDEX ON chunks USING GIN (fts);

-- Vector search (pgvector)
CREATE INDEX ON chunks USING ivfflat (embedding vector_cosine_ops);
-- or HNSW (for large data)
-- CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

-- Hybrid search: semantic (vector) + keyword (fts), RRF fusion
-- then reranked by the rerank model
```

### 9.2 Billing and Logs

```sql
-- Subscriptions
CREATE TABLE subscriptions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID REFERENCES users(id),
    plan       TEXT NOT NULL,          -- 'free' | 'pro'
    status     TEXT NOT NULL,          -- 'active' | 'canceled' | 'expired'
    started_at TIMESTAMPTZ DEFAULT now(),
    ends_at    TIMESTAMPTZ
);

-- Credit ledger (append-only, bank-style accounting)
CREATE TABLE credit_ledger (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID REFERENCES users(id),
    amount          BIGINT NOT NULL,        -- positive = top-up, negative = spend (smallest unit)
    balance_after   BIGINT NOT NULL,        -- balance after the entry
    reason          TEXT NOT NULL,          -- 'ai_call' | 'recharge' | 'refund'
    idempotency_key TEXT UNIQUE NOT NULL,   -- idempotency key, prevents double-charging
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Four log types
CREATE TABLE audit_logs (      -- audit logs (who changed what): longest retention, non-deletable
    id BIGSERIAL PRIMARY KEY,
    user_id UUID, action TEXT, entity TEXT, entity_id UUID,
    before JSONB, after JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE ai_call_logs (    -- AI call logs (cost analysis): token / latency / cost
    id BIGSERIAL PRIMARY KEY,
    user_id UUID, model TEXT, prompt_tokens INT, completion_tokens INT,
    latency_ms INT, cost_micro BIGINT, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE activity_logs (   -- activity logs (product analytics): short retention, aggregatable then discarded
    id BIGSERIAL PRIMARY KEY,
    user_id UUID, event TEXT, payload JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE job_logs (        -- job logs (async enrich / import)
    id BIGSERIAL PRIMARY KEY,
    job_type TEXT, status TEXT, input JSONB, error TEXT,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
```

## 10. Data Flows

### 10.1 Learning Content Import (video / document)

```
video URL / file
  → youtube-transcript-api / faster-whisper transcription
document PDF
  → PyMuPDF extraction
        │
        ▼
  chunking (video by timestamp, document by page)
        │
        ▼
  embedding (BGE-M3) → store in pgvector
        │
        ▼
  async enrich (translate / annotate / image) → Redis Stream worker → enrichment table (content-hash cache)
```

### 10.2 Interactive Q&A (RAG + Agent + Voice)

```
user (text / voice)
  → STT (when voice)
  → Agent native function-calling loop
        ├─ tool 1: RAG retrieval (hybrid search + RRF + rerank)
        ├─ tool 2: vocabulary lookup (terms / matches)
        ├─ tool 3: external MCP tools
        └─ direct answer
  → LLM (via LiteLLM gateway)
  → SSE streaming response
  → TTS (when voice, content-hash cache)
```

## 11. Multi-Tenancy and Deployment Strategy

| Scenario | Strategy |
|------|------|
| B2C (multi-user) | Shared DB + PostgreSQL RLS row-level isolation |
| B2B (enterprise) | database-per-tenant, independent DB |
| Read scaling | Read replicas + connection pool (pgBouncer) |
| Internal comms | External REST/SSE ↔ internal gRPC |
