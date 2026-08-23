# 🧠 DeepDive

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepDive** is an AI-powered learning workbench — a monorepo spanning a FastAPI backend, a React web UI, and an Electron desktop workbench. It unifies **vocabulary learning**, **video/document learning**, and an **AI chat assistant** around a single abstraction: *everything is a text chunk*.

The engine is a native function-calling **agent** — an `AgentKernel` composition root that wires a cache-boundary prompt (a byte-stable static head so the provider reuses its prefix cache), deferred tool loading, dual-track memory (PostgreSQL tsvector + pgvector fused by RRF), a skill catalog, and a read-only sandbox around a `ReactLoopAgent` step loop. A **hybrid search engine** finds context even when exact keywords are missing, and a **RAG pipeline** (rewrite → recall → RRF → rerank) grounds every chat answer.

Model inference never runs inside the API: embedding (**BGE-M3** via TEI) and **TTS (Kokoro)** are separate Docker services, and retrieval can be extracted behind a capability seam into its own **gRPC service**. **LLM calls** go directly to the provider channel pinned to the session (managed in the admin console), with the LiteLLM gateway as the legacy fallback. Async enrichment (TTS, images, explanations, session finalize) runs on an arq **worker** off the request path.

Also included: multi-user accounts with per-role quotas, **per-role LLM channels** (role ↔ credential bindings with one channel pinned per login), pay-as-you-go **billing** with atomic wallet deduction, a self-contained **admin console** (Providers / Roles / Users / Tokens / Tools config), **self-service accounts** (email-verified registration, password reset, editable profiles with avatars), and a desktop workbench with a file tree, a multi-format media viewer, and one-click video screenshots.

---

## 🏗️ Architecture at a glance

```mermaid
flowchart LR
    subgraph Frontends
        web[React Web UI]
        desk[Electron Workbench]
        lt[Local Tools]
    end
    subgraph Backend
        api["FastAPI gateway · AgentKernel<br/>HTTP/SSE entry · usecases · job enqueue"]
        wk[arq worker<br/>TTS / images / explanations / finalize]
        rs[Retrieval service<br/>RAG pipeline · gRPC]
    end
    subgraph Data
        pg[(PostgreSQL<br/>pgvector + tsvector)]
        rd[(Redis<br/>cache / queue)]
    end
    subgraph Model services
        emb[TEI embedding · BGE-M3]
        tts[Kokoro TTS]
        llm[LiteLLM gateway<br/>→ upstream LLM]
    end

    web --> api
    desk --> api
    api --> rd
    rd --> wk
    api --> pg
    wk --> pg
    api --> rs
    rs --> pg
    api --> emb
    api --> tts
    api --> llm
    wk --> tts
    rs --> emb
```

**Agent kernel** — the core, composed in and running inside the FastAPI gateway process:

```mermaid
flowchart TB
    kernel[AgentKernel] --> loop[ReactLoopAgent step loop]
    loop --> prompt[CacheBoundaryAssembler<br/>cache-boundary prompt]
    loop --> tools[ToolGateway<br/>deferred tool loading]
    loop --> skills[SkillCatalog · SKILL.md]
    loop --> memory[MemoryService<br/>PG tsvector + pgvector RRF]
    tools --> sandbox[Sandbox<br/>READ / WRITE / NETWORK]
```

**Memory — two independent tracks, orchestrated by `MemoryService`** (writes go to the local
memdir, recall reads the database; the tracks never cross):

![Memory architecture — two independent tracks](./docs/images/memory-architecture.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

```mermaid
flowchart TB
    %% Invariants: thinking never persisted · messages never deleted by compaction/retention ·
    %% memory_save writes the memdir only (never PG) · vector-channel failure → tsvector-only (never silent empty) ·
    %% superseding marks a memory, never deletes it (audit file kept) · cosmetic failures never block the main flow

    P[prompt memory section]

    subgraph trackA["Track A · local file memory — agent-writable, never the DB"]
        FILES[MEMORY.md index<br/>one frontmatter .md per memory<br/>type: user · feedback · project · reference<br/>superseded entries unindexed · files kept as audit]
        WRITE[memory_save · guardrailed write<br/>kebab-case · ≤4000 chars · type whitelist · write-then-readback<br/>importance 1-10 · supersede-in-place via status]
        SEARCH[memory_search · keyword × importance<br/>name ×3 · desc ×2 · body ×1<br/>superseded entries excluded]
        BRIEF[begin_session → MEMORY.md head<br/>first 200 lines as session brief · Lane-1 always on]
        WRITE --> FILES
        SEARCH --> FILES
        BRIEF --> P
    end

    subgraph trackB["Track B · session memory — PostgreSQL, system-written · agent read-only"]
        MSGS[(messages<br/>text · pgvector · tsvector · created_at)]
        SESS[(sessions<br/>title · summary · closed_at)]
        EVTS[(session_events<br/>audit log · JSONB payload<br/>compaction summaries persist here)]
        KW[tsvector keyword recall<br/>to_tsvector english<br/>fts_config swappable for CJK]
        VEC[pgvector semantic recall]
        RRF[RRF fusion + recency decay<br/>30-day half-life · 1.0× recent<br/>0.68× at 30d · 0.55× at 90d]
        COMPACT[compact_history · hierarchical recap<br/>over 40 msgs → keep latest 20<br/>L2 prior-window summaries coarse<br/>L1 current summary · failure still truncates]
        FINAL[worker finalize<br/>backfill embeddings · summary · auto-title<br/>failure-robust · cosmetic failures never block]
        SWEEP[retention cron · 04:17 daily<br/>purge session_events > 30d<br/>audit log only · L2 recap fades · messages kept]
        MSGS --> KW
        MSGS --> VEC
        KW --> RRF
        VEC --> RRF
        MSGS --> COMPACT
        MSGS --> FINAL
        COMPACT --> P
        COMPACT --> EVTS
        EVTS --> SWEEP
        FINAL --> SESS
    end

    USER[user message]
    GATE[trigger gate · should_recall<br/>memory-seeking phrases or short elliptical<br/>else only the Lane-1 brief]
    USER --> GATE
    GATE -- seeking --> RRF
    GATE -- neutral --> P
    RRF --> P
    P --> AGENT[ReactLoopAgent step loop]
```

</details>

> **Memory invariants** — thinking is never persisted; `messages` are never deleted by compaction
> or retention; `memory_save` writes the memdir only (never the DB); a vector-channel failure
> degrades to tsvector-only (never a silent empty); superseding marks a memory `superseded` and
> keeps the file as an audit trail — it is never deleted; the recall gate skips only the deep
> recall on non-memory-seeking turns (the Lane-1 brief always injects); cosmetic failures
> (summary / title / compaction) never block the main flow.

**Prompt — cache-boundary assembly, compression, and deferred tool stubs** (architecture, design
features, and per-step process logic):

![Prompt architecture — cache-boundary assembly](./docs/images/prompt-architecture.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

```mermaid
flowchart TB
    %% Prompt module: cache-boundary assembly + compression + deferred tool stubs.
    %% Invariants: the CACHE_BOUNDARY separator is internal-only and never rendered; the stable
    %% head (STATIC_PREFIX + PROJECT_CONTEXT) is byte-identical so the provider prefix cache is
    %% reused; only the DYNAMIC_SUFFIX re-renders per step and is skipped when unchanged; the
    %% per-message snip trims the request snapshot only, keeping the persistence copy raw; mounted
    %% tools appear as stable defer_loading stubs, with the full schema riding in the tool_search
    %% result.

    subgraph input["Input"]
        HIST["chat history + new user message"]
        AUTO["compact_history · token-aware char budget<br/>over prompt_max_chars → keep latest 20<br/>inject L1 current + L2 coarse recap"]
        HIST --> AUTO
    end

    subgraph zones["Design · cache-boundary zones"]
        SP["STATIC_PREFIX · byte-identical across requests<br/>SOUL.md identity + compact tool catalog<br/>+ compressed skill catalog"]
        PC["PROJECT_CONTEXT · stable per project<br/>DEEPDIVE.md via read_project_context<br/>capped at 8k chars · empty → zone dropped"]
        DS["DYNAMIC_SUFFIX · re-rendered per step<br/>memory brief · Lane-1 always on<br/>recalled memory · gated by should_recall<br/>+ agent.inject content"]
        SP --> HEAD["stable head"]
        PC --> HEAD
    end

    subgraph render["render_prompt"]
        HEAD --> RENDER
        DS --> RENDER
        RENDER["zones joined by blank lines<br/>CACHE_BOUNDARY is internal-only · never sent"]
        RENDER --> KEY["snapshot_key · sha256 of static + project<br/>16 hex · cache identity measurable"]
    end

    subgraph perstep["Process · per step"]
        RENDER --> SNIP["per-message snip · prompt_message_max_chars<br/>request snapshot trimmed · persistence keeps full text"]
        AUTO --> SNIP
        SNIP --> REQ["LLM request · system + snipped messages"]
        REQ --> REFRESH["refresh_dynamic · recompute only DYNAMIC_SUFFIX<br/>unchanged → system not re-sent · head reused"]
        REQ --> TOOLS["visible tools · core full schemas<br/>+ defer_loading stubs · name + description · empty params<br/>full schema via the tool_search result"]
        REFRESH --> REQ
    end
```

</details>

> **Prompt invariants** — the `CACHE_BOUNDARY` separator is internal-only and never rendered to the
> model; the stable head (STATIC_PREFIX + PROJECT_CONTEXT) is byte-identical across requests so the
> provider prefix cache is reused; only the DYNAMIC_SUFFIX re-renders per step and is skipped when
> unchanged; the per-message snip trims the request snapshot only, keeping the persistence copy raw;
> mounted tools appear as stable defer_loading stubs, with the full schema riding in the tool_search
> result.

> [docs/architecture.md](docs/architecture.md) is the single source of truth for the full design —
> tech stack, repository layout, agent-kernel internals, tool runtime, data model, and deployment
> topology (including what is implemented today vs. designed-only).

---

## ✨ Key Features

### 💬 AI Chat Assistant
- **Agentic Kernel** (`AgentKernel`): a composition root wiring a cache-boundary prompt assembler, deferred tool loading, dual-track memory, a skill catalog, and a read-only sandbox around a `ReactLoopAgent` step loop.
- **Cache-Boundary Prompt**: the system prompt is partitioned into three zones — a byte-stable static prefix (SOUL.md + compact tool/skill catalog), project context, and a per-step dynamic suffix — so the provider's prefix cache reuses the stable head across steps. Project conventions are loaded from the workspace's `DEEPDIVE.md` into their own stable zone; `snapshot_key()` makes the cache identity measurable.
- **Deferred Tool Loading**: the prompt carries only a compact `name + blurb` catalog plus the resident `tool_search` meta-tool; matched tools appear in the visible set as stable `name + description` stubs (defer_loading style) so the cached tools array never churns, and each tool's full schema is returned in the `tool_search` result. The prompt window is also bounded by a per-message snip plus a token-aware autocompact that fires on a character budget.
- **Dual-Track Memory**: session recall fuses PostgreSQL tsvector (keyword) + pgvector (semantic) via RRF, recency-weighted so newer messages win near-ties; when the embedding service is offline it degrades to tsvector-only — never a silent empty. `memory_search` / `memory_save` are tools; `memory_save` writes guardrailed notes (kebab-case key, length-capped content, closed type taxonomy) to the local memory directory while the session stays read-only. Proactive recall injects top hits for your question into the prompt, and long conversations are auto-compacted into an LLM summary (bounded token window).
- **Skill Catalog**: skills (SKILL.md) are advertised as a one-line compressed index; the full instructions are lazy-loaded through the `skill` meta-tool.
- **Read-Only Sandbox**: every tool call is gated by session permissions (READ / WRITE / NETWORK); file tools are rooted at the workspace dir and path escape is rejected. Writes and network access need an explicit grant or human approval.
- **RAG Retrieval**: query rewrite → multi-recall (vector + keyword) → RRF fusion → rerank.
- **SSE Streaming**: Real-time token streaming to the frontend.

### 🤖 AI-Powered Interactive Study
- **Seamless Navigation**: Switch instantly between words using **"⬅️ Prev"** and **"Next ➡️"** buttons without closing the dialog.
- **Hybrid Search Engine**: Combines PostgreSQL full-text search (exact keyword) and pgvector (semantic). If an exact sentence isn't found, it finds the most semantically similar sentence (e.g. searching "GQA" finds sentences about "Group Query Attention").
- **Context-Aware Explanations**: Uses LLMs to translate sentences and explain *exactly* what a term means within that specific context.
- **Auto-Fetch Definitions**: If a term lacks a definition, the system automatically calls the LLM in the background to fetch a precise English definition and Chinese translation.
- **Visual Context for Professional Vocabulary**:
  - **Multi-Dimensional Image Search**: Grasp complex or abstract terms instantly. The system automatically scrapes Google Images (with Bing as a seamless fallback) using a combined 3-tier strategy: *Term alone*, *Term + Definition*, and *Term + Contextual Sentence*.
  - **Asynchronous Loading & Randomized Regeneration**: Images load via a non-blocking UI mechanism so you can study text while images fetch in the background. Click **Regenerate** to sample a new set from a broader candidate pool.
  - **Local Image Caching**: Once saved, images are downloaded to a local cache and linked via relative paths for zero-latency loads and offline availability.
- **Built-in Mic Widget**: Record your own voice directly in the browser and compare it with the generated TTS audio for pronunciation practice.
- **Audio & Pronunciation**: Generate high-quality TTS audio for words and full sentences on the fly, with local audio caching to save API costs and speed up loading.
- **Importance Rating**: Rate terms from 1 to 5 stars (⭐⭐⭐⭐⭐) to prioritize your learning.

### ☁️ Cloud Drive (Personal & Shared File Storage)
- **My Drive + shared workspaces**: every user gets a private **My Drive**, plus shared **workspaces** (owner / admin / editor / viewer roles) for team files. Files are stored in a per-user object store with **SHA-256 content-addressing**, so identical files deduplicate to a single physical blob (ref-counted, freed only when the last reference is purged).
- **Instant upload & resumable chunks**: uploading an already-stored file short-circuits to **instant upload** (no bytes transferred); large files stream in **8 MB chunks** (`upload_sessions` tracks received chunks) with a progress bar and safe re-upload.
- **First-class folders**: multi-level folders (`folders` rows with full `/`-paths) live side by side with files, and the file manager tree combines them. Create/rename/delete folders; renaming or deleting a folder rewrites the path prefix of every file and sub-folder beneath it.
- **Move anywhere**: files move across workspaces / My Drive freely (drag-and-drop is the planned UX; the API + batch bar support it today).
- **Fuzzy search with suggestions**: a client-side, case-insensitive fuzzy matcher (prefix > substring > folder-path > subsequence scoring) with a live suggestion dropdown; you can scope a search to a workspace or search all of My Drive.
- **Trash & retention**: deleting a file moves it to the trash — bytes are kept (no ref-count release) until you **Restore**, **Delete permanently**, or **Empty Trash**. Trash auto-purges entries older than **30 days** (lazy sweep on list). Deleting a workspace trashes all its files and moves assets to My Drive trash.
- **Sharing & permissions**: per-file ACLs — grant read/write to specific users or create a **public link** (shareable without an account). Visibility is computed with three channels (ownership, workspace membership, ACL) so owner/admin/editor/viewer each see exactly what they should.
- **Member management & audit**: workspace owners/admin add/edit/remove members and grant the `admin` role (**only the owner** can grant admin/owner); every mutation is recorded in an append-only **activity log** (workspace members can read it; admin/owner manage it).

### 👥 Multi-User, Roles & Billing
- **Admin console** (`/admin`): a self-contained single-file SPA with five modules — **Providers** (credentials + model catalog + routing weights), **Roles**, **Users**, **Tokens**, and **Tools config** (web-search provider, SMTP, free-form key/value tool params, test email). A default `admin`/`admin` credential is seeded into the DB on first boot; console login is stateless (a signed session token, never persisted), so it never pollutes the tokens table.
- **Per-role LLM channels**: every role binds the provider channels it may use (`role_credentials`); each login pins one random active channel to the token, and chat routes through it per-request with failover.
- **LLM key management**: Tokens → LLM Keys manages the per-user key-grant matrix — which user may use which provider key; keys are shown masked (`sk-***`) with one-click copy, and a user's access to a key can be revoked or restored independently of their login.
- **Self-service accounts**: users **register** themselves (username + email + password); an **email-verification** link gates the first sign-in, and **forgot-password** sends a one-time reset link. After signing in, users edit their profile (display name, username, contact email, phone, avatar) and change their password — all from the web and desktop clients. Admins can still create accounts directly.
- **Email & SMTP**: verification / reset mail is sent over SMTP configured in the admin **Tools config** page. When SMTP is not configured, the one-time link is returned to the client instead and shown inline (dev mode), so registration still works locally.
- **User accounts**: users (and the web/desktop console) log in to receive an opaque token. Roles (`regular` / `pro` / `vip` / `admin` / `anonymous`) carry per-role quota (daily/monthly requests, tokens, RPM, cost) and an optional default model.
- **Server-managed config**: LLM provider keys, the model catalog, and the admin credential live in PostgreSQL (`app_settings`) — not `.env` or repo files — and are edited from the admin console.
- **Pay-as-you-go billing**: per-model pricing (prompt/completion per 1k tokens), a cash wallet per user, and an append-only ledger with a `balance_after` snapshot. Chat usage is priced and debited atomically (always the catalog model price). Every usage log records the **serving channel** too, so the admin can aggregate cost per provider key.
- **Guest access**: anonymous users can chat without an account, capped per day (`guest_daily_limit`, default 10); exceeding the cap prompts them to sign in.

### 📥 Smart Data Ingestion
- **Domain Management**: Organize your learning materials into isolated domains.
- **Flexible Import**: Import vocabulary (with frequencies) and contextual sentences via CSV/Excel/TXT uploads or manual entry.
- **Intelligent Deduplication**: Automatically skips existing terms during import (case-insensitive) to maintain a clean database.
- **Vector Indexing**: One-click generation of embeddings for your corpus using the industrial-grade **BGE-M3** model (stored in pgvector) to enable semantic search.

### 📖 Minimalist & Powerful Study Mode
- **Client-side Pagination & Sorting**: Lightning-fast UI with in-memory pagination. Sort vocabulary by Word (A-Z), Frequency, or Importance Level (Stars).
- **Advanced Filtering**: Filter your study list by specific domains or star levels.
- **Real-time Search**: Instantly find terms in your current list with a responsive search bar.
- **View Definitions**: Clean UI using a "📖 View" popover to see definitions without leaving the list.

### 🛠️ Efficient Library Governance
- **Efficient Toggles**: Instantly enable/disable terms with visual feedback.
- **Click-to-Edit**: Definitions display as clean labels and expand into editors only when clicked, preventing accidental edits.
- **Unified Visuals**: Star levels are managed via intuitive icon pickers (⭐) instead of raw numbers.
- **Transactional Page Commits**: Commit all modifications on a single page with one click for high-speed bulk updates while maintaining data integrity.
- **Global Operation Flow**: Perform global sorting across the entire database and save changes page-by-page.
- **Self-Healing Logic**: Automatically deduplicates duplicate matches to keep the UI stable.

### 🖥️ Desktop Workbench (Electron)
- **Workspace file tree**: open any local folder as your workspace (the last one is restored on the next launch); add files from anywhere, delete with one click, and fuzzy-search the tree.
- **Sessions search**: search your chat history by content — matching snippets are highlighted in the results.
- **Multi-format viewer**: video (with subtitles), audio, images, PDF (with annotations), and text/code; Office files open in your OS default app. One-click video **screenshots** and **Generate PPT / Generate Book**.
- **Subtitles**: a sibling `.srt`/`.vtt`/`.lrc` is auto-detected, or pick one manually; enable/disable and style it (size, color, background, position), and your settings are remembered.
- **Streaming chat**: answers stream in with a collapsible **💭 thinking** panel; dock the chat to the bottom or side, or float it as a window.
- **Native menus & settings**: everything you'd expect — open/switch workspaces, zoom & font size, fullscreen, Help & Feedback, About. ⚙️ Settings covers theme, display, updates, help, and about.

---

## 🚀 Getting Started (from zero)

### 1. Prerequisites

- **Docker Desktop** — runs PostgreSQL, Redis, and the model services (embedding / rerank / TTS / LLM gateway).
- **Conda** (Miniconda or Anaconda) — the backend runs in a `deepdive` env.
- **Node.js 18+** — for the web frontend (optional; the API runs without it).
- **Git**.

### 2. Clone the repository

```bash
git clone https://github.com/Eric-LLMs/DeepDive.git
cd DeepDive
```

### 3. Create & activate the conda environment

```bash
conda create -n deepdive python=3.11 -y
conda activate deepdive
```

### 4. Configure environment

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Fill in `LLM_UPSTREAM_KEY` (your real OpenAI-compatible key). Every other variable has a working local-dev default — see the [configuration reference](#-configuration-reference) below.

### 5. Install backend dependencies

```bash
pip install -e ".[dev]"     # runtime + test tooling (== pip install -r requirements-dev.txt)
pip install -e ".[rag]"     # optional: RAG semantic search (pulls torch / sentence-transformers)
```

### 6. Start infrastructure (data + model services)

```bash
docker compose up -d postgres redis embedding tts llm-gateway worker
```

The first start downloads the models (BGE-M3, Kokoro-82M) into Docker volumes — allow a few minutes. The LLM gateway routes the virtual model `deepdive-chat` to `LLM_UPSTREAM_MODEL` using `LLM_UPSTREAM_KEY`.

> Skip `embedding` if you don't use semantic search, and `tts` if you don't need audio — the API degrades gracefully.
>
> **Docker Desktop (Windows) memory note:** the TEI embedding service needs ~9 GB during BGE-M3 warmup. If Docker's WSL2 backend has only ~8 GB (the default on a 16 GB host), the container gets OOM-killed. Raise the limit in `%UserProfile%\.wslconfig`, e.g. `[wsl2]\nmemory=12GB\nswap=4GB`, then run `wsl --shutdown` and restart Docker Desktop.

### 7. Initialize the database (SQL migrations)

```bash
python scripts/init_db.py     # applies migrations/*.sql in order (creates all tables incl. jobs)
# or run a single script directly with psql:
psql -d deepdive -f migrations/0001_init.sql
```

### 8. Run the API

```bash
uvicorn apps.api.main:app --reload
```

Open http://localhost:8300/docs for the interactive API documentation.

> The **worker** (async enrichment) runs as a docker-compose service (see step 6). To run it on
> the host instead: `arq apps.worker.settings.WorkerSettings`.

### 9. (Optional) gRPC retrieval service

The default `retrieval_mode` is `in_process` (RAG runs inside the API). To run retrieval as a separate gRPC service:

```bash
bash scripts/gen_proto.sh        # generates retrieval.v1 stubs into packages/shared/proto/
python -m apps.retrieval.main    # starts the gRPC server on localhost:15051
```

Then set `RETRIEVAL_MODE=grpc` in `.env` and restart the API:

```bash
RETRIEVAL_MODE=grpc uvicorn apps.api.main:app --reload
```

With `RETRIEVAL_MODE=grpc` the `rag_search` tool routes through the retrieval service over gRPC — the capability seam swaps the provider, so no tool code changes.

### 10. Run the tests

```bash
pytest
```

### 11. Run the web frontend (optional)

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api`, `/audio`, and `/images` to the backend.

### 12. Run the desktop workbench (Electron, optional)

The desktop app is a standalone **learning workbench** with its own renderer (not the React web UI):
open a local folder as your workspace, browse a multi-format viewer (video with subtitles, audio,
images, PDF with annotations, text/code, Office files), take video screenshots, and chat with
streamed answers and a collapsible thinking panel.

```bash
bash scripts/start_desktop.sh    # one-click: infra (postgres/redis) + backend + workbench
# or manually:
cd apps/desktop
npm install
npm start                        # opens the workbench window
```

`scripts/start_desktop.sh` probes the backend at `http://localhost:8300/health` first, starts the
infra + `uvicorn apps.api.main:app --port 8300` in the background if it is down (pid in
`data/uvicorn.pid`), then opens the Electron window.

The file tree, viewer, video screenshots, and subtitles work **without the backend**. Chat
(streaming), session history & search, "生成 PPT / 生成书" (media generation), and sign-in / profile
need the backend running on `localhost:8300` — the Electron main process forwards `/api`, `/audio`,
and `/images` to it. ⚙️ Settings covers **theme**, **font size**, **update checks**, **help**, and
**about**. The login dialog supports **registering a new account** and **password reset**; once
signed in you can edit your profile and avatar. Guests can also chat anonymously (limited to
`guest_daily_limit` per day).

### One-click launchers (pick by environment)

Both scripts print a `[n/N]` banner before each step so you can see where they are. Each step is
skipped when its target is already up (backend, Docker, infra), so re-running is fast and safe.
First run on a fresh machine also installs Docker and the Python/Node deps automatically.

| Environment | Script | What it does |
|---|---|---|
| **Windows desktop** (local PC client) | `bash scripts/start_desktop.sh` | Auto-installs Docker Desktop if missing → starts **all** dependency services (postgres, redis, embedding, tts, llm-gateway, worker) → ensures the Python venv → starts the backend (boot seeds `admin`/`admin`) → opens the Electron workbench. |
| **Linux server** (browser access) | `bash scripts/start_server.sh` | Auto-installs Docker Engine if missing → starts **all** dependency services (postgres, redis, embedding, tts, llm-gateway, worker) → ensures the Python venv → starts the backend (boot seeds `admin`/`admin`) → builds and serves the React web UI at `http://<server-ip>:5173`. |

The default `admin` / `admin` account is seeded on first boot and ready to sign in from the start.

---

## ⚙️ Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://deepdive:deepdive@localhost:15432/deepdive` | PostgreSQL + pgvector |
| `REDIS_URL` | `redis://localhost:16379/0` | cache / queue |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `""` / `http://localhost:4000/v1` / `deepdive-chat` | legacy default client (LiteLLM gateway); active channels + keys live in the DB and are managed in the admin console |
| `LLM_UPSTREAM_MODEL` / `LLM_UPSTREAM_BASE` / `LLM_UPSTREAM_KEY` | `openai/gpt-4o-mini` / `https://api.openai.com/v1` / `sk-xxx` | real upstream LLM (consumed by the gateway container) |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `http://localhost:18080` / `BAAI/bge-m3` / `1024` | TEI embedding service |
| `TTS_BASE_URL` / `TTS_MODEL` / `TTS_VOICE` / `TTS_VOICE_ZH` | `http://localhost:18880/v1` / `kokoro` / `am_michael` / `zm_yunxi` | Kokoro-FastAPI TTS service (auto-switches to the Chinese voice for CJK text) |
| `RETRIEVAL_MODE` / `RETRIEVAL_GRPC_ADDR` | `in_process` / `localhost:15051` | capability seam: `in_process` or `grpc` |
| `WORKSPACE_DIR` | `.` | agent filesystem-tool root (`read_file` / `edit_file` / `bash`; path escape rejected) |
| `MEMORY_DIR` | `data/memory` | file memory directory (`MEMORY.md` index + one frontmatter `.md` per memory) |
| `SKILLS_DIR` | `data/skills` | `SKILL.md` skills directory (lazy-loaded via the `skill` tool) |
| `MEMORY_NOTE_MAX_CHARS` | `4000` | `memory_save` content length cap (guardrail) |
| `MEMORY_RECALL_TOP_K` | `5` | proactive recall hits injected into the prompt memory section |
| `HISTORY_MAX_MESSAGES` / `HISTORY_KEEP_MESSAGES` | `40` / `20` | chat history length that triggers compaction / most-recent messages kept after compaction |
| `SESSION_EVENTS_RETENTION_DAYS` / `RETENTION_CRON` | `30` / `17 4 * * *` | daily worker cron purges `session_events` (audit log) older than this many days / its 5-field cron schedule |
| `WORKER_CONCURRENCY` / `WORKER_JOB_TIMEOUT` | `10` / `300` | arq worker max concurrent jobs / per-job timeout (seconds) |
| `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` / `WEB_SEARCH_ENGINE_ID` | `tavily` / `""` / `""` | web-search provider (`duckduckgo` \| `tavily` \| `bing` \| `google`) + API key + google engine id; normally managed in admin → **Tools config**, these flat keys mirror that namespace |
| `OBJECT_STORE_ROOT` | `data/objects` | cloud-drive blob store root (SHA-256 content-addressed, ref-counted) |
| `DRIVE_CHUNK_SIZE` | `8MB` | cloud-drive upload chunk size |
| `DRIVE_MAX_CHUNKS` | `1024` | max chunks per upload session (cap on single-file size) |
| `DRIVE_MAX_FILE_SIZE` | `0` (unlimited) | per-file size limit for the cloud drive (bytes; `0` = no limit) |
| `INGEST_CHUNK_CHARS` / `INGEST_CHUNK_OVERLAP` | `1200` / `150` | text chunking window / overlap used when a cloud-drive file is ingested for RAG |
| `EMBED_BATCH_SIZE` | `16` | embedding batch size for indexing cloud-drive files into pgvector |

Model inference never runs inside the API process. LLM channels are managed in the admin
console (Providers → Credentials); each is an OpenAI-compatible `base_url` + `api_key`, and a role
bound to a channel routes chat straight to that provider (no code change). Web-search (provider +
key + google engine id) and **SMTP** (host, credentials, TLS, enabled) are likewise configured in
the admin console (**Tools config**) rather than `.env`. Swapping embedding/TTS
models = change `--model-id` in `docker-compose.yml` and the matching `*_BASE_URL` / dim in `.env` —
no business-code change. See [docs/architecture.md](docs/architecture.md) for the full topology.

---

## 💡 How to Use

1. **Create Domain**: Navigate to *Import Data* → *Domain Management* to start a new topic (e.g. "AI Research Papers").
2. **Import Terms**: Switch to *Import Vocabulary*. Upload your vocabulary CSV or paste text directly.
3. **Build Corpus (Two Layers)**:
   - **Layer 1 (SQL)**: Import sentences for exact keyword matching.
   - **Layer 2 (Vector)**: Import raw text and index embeddings to enable semantic search.
4. **Interactive Study**: Navigate to *Study Mode* and click the **📖 View** icon to deep-dive — generate TTS audio, view AI definitions, record and compare your pronunciation, get context-aware sentence translations, view contextual images, navigate via Next/Prev, and save the best context to your database.
5. **Library Governance**: Navigate to *Manage Vocabulary* to sort globally, refine definitions with click-to-edit, and toggle term visibility.
6. **Chat Assistant**: Ask questions and get RAG-grounded, streamed answers.

---

## 📝 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
