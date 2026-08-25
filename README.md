# <img src="docs/images/deepdive-logo.png" alt="DeepDive" width="40" valign="bottom" /> DeepDive

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepDive** is an **AI-powered learning workbench** for researchers, students, and lifelong learners — a **1-on-1 AI tutor** that walks you through anything you read or watch, from first skim to final review.

- **1-on-1 AI Tutoring**: Load a paper, book, lecture, or video and ask about anything — get explanations, related sources, and discussion until it clicks, even as the video plays.
- **Knowledge Synthesis**: Skim a 600-page book in minutes — get a structured summary or slide deck before you commit to reading it.
- **Note-Taking That Sticks**: The tutor records and organizes your insights as you study, ready when you review.
- **Learn Together**: Share files from the built-in cloud drive in shared workspaces and study the same material as a group.
- **One Place, All Forms**: A fast web app, a desktop workbench that works offline, and a fully self-hosted AI engine — your materials stay yours.

The engine is a native function-calling **agent** — an `AgentKernel` composition root that wires a cache-boundary prompt (a byte-stable static head so the provider reuses its prefix cache), deferred tool loading, dual-track memory (PostgreSQL tsvector + [pgvector](https://github.com/pgvector/pgvector) fused by RRF), a skill catalog, and a read-only sandbox around a `ReactLoopAgent` step loop. A **hybrid search engine** finds context even when exact keywords are missing, and a **config-driven RAG pipeline** (pluggable nodes — rewrite → recall → RRF fusion → rerank, plus optional CJK segmentation, contextual enrichment, and parent/child indexing) grounds every chat answer.

Model inference never runs inside the API: embedding (**[BGE-M3](https://huggingface.co/BAAI/bge-m3)** via [TEI](https://github.com/huggingface/text-embeddings-inference)) and **TTS ([Kokoro](https://github.com/hexgrad/kokoro))** are separate Docker services, and retrieval can be extracted behind a capability seam into its own **gRPC service**. **LLM calls** go directly to the provider channel pinned to the session (managed in the admin console), with the [LiteLLM](https://github.com/BerriAI/litellm) gateway as the legacy fallback. Async enrichment (TTS, images, explanations, session finalize) runs on an [arq](https://arq-docs.helpmanual.io/) **worker** off the request path.

Also included: multi-user accounts with per-role quotas, **per-role LLM channels** (role ↔ credential bindings with one channel pinned per login), pay-as-you-go **billing** with atomic wallet deduction, a self-contained **admin console** (Providers / Roles / Users / Tokens / Tools config), **self-service accounts** (email-verified registration, password reset, editable profiles with avatars), and a desktop workbench with a file tree, a multi-format media viewer, one-click video screenshots, and a built-in **cloud note editor** (Markdown edit + preview) that edits your My Drive straight from the app.

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

**Answering flow** — each turn runs the `ReactLoopAgent` step loop, which decides what to retrieve and
composes the reply. It calls the **`rag_search` tool** (runs the RAG node pipeline and returns `top_k`
chunks — the retrieval layer described in the RAG section) and, only when local material is
insufficient, the **`web_search` tool** (duckduckgo / tavily / bing / google) for up-to-date external
results; it then composes the answer from both.

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
        SWEEP[retention cron · scheduled daily<br/>purge session_events > 30d<br/>audit log only · L2 recap fades · messages kept]
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

![Prompt — cache-boundary assembly, compression, and deferred tool stubs](./docs/images/prompt-architecture.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

```mermaid
%%{init: {"flowchart": {"wrappingWidth": 500, "nodeSpacing": 40, "rankSpacing": 50}}}%%
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
        SNIP["per-message snip · prompt_message_max_chars<br/>request snapshot trimmed · persistence keeps full text"]
        AUTO --> SNIP
        SNIP --> REQ["LLM request · system + snipped messages"]
        REQ --> TOOLS["visible tools · core full schemas<br/>+ defer_loading stubs · name + description · empty params<br/>full schema via the tool_search result"]
        REQ --> EXEC["tool execution · dispatch via runtime<br/>concurrency-safe → parallel gather · else serial<br/>Sandbox permission-guarded"]
        EXEC --> COMMIT["tool result committed to messages<br/>role:tool + deferred contexts<br/>messages grows · next step reuses"]
        COMMIT --> REFRESH["refresh_dynamic · recompute only DYNAMIC_SUFFIX<br/>unchanged → system not re-sent · head reused"]
        REFRESH --> REQ
        RENDER --> REQ
    end
```

</details>

> **Prompt invariants** — the `CACHE_BOUNDARY` separator is internal-only and never rendered to the
> model; the stable head (STATIC_PREFIX + PROJECT_CONTEXT) is byte-identical across requests so the
> provider prefix cache is reused; only the DYNAMIC_SUFFIX re-renders per step and is skipped when
> unchanged; the per-message snip trims the request snapshot only, keeping the persistence copy raw;
> mounted tools appear as stable defer_loading stubs, with the full schema riding in the tool_search
> result.

**RAG & Query Repository — a config-driven, plug-and-play retrieval pipeline** over one unified search
corpus. A single query searches your cloud-drive files, Learning-Platform material, and chat Q&A
together. The pipeline's node list *is* the retrieval topology: compose, reorder, toggle, and tune every
stage from the admin console — live, no code, no restart. Feature walk-through in **Key Features** →
**RAG & Query Repository**; the design detail lives in [architecture.md](docs/architecture.md) §10.

![RAG — config-driven node pipeline](./docs/images/rag-architecture.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

```mermaid
flowchart TB
    %% ── ① sources → per-entry processing ───────────────────────────
    subgraph ENTRY["Three import entries"]
        DRIVE["Cloud Drive<br/>doc · pdf · txt · docx"]
        LEARN["Learning Platform<br/>sentences · articles"]
        CHAT["Chat<br/>reply pairs · whole sessions"]
        PDFT["PDF tools<br/>page text + tables → image → vision LLM text"]
        LEARNP["learning_import job<br/>sentences / articles → chunks"]
        CHATP["chat import<br/>LLM merges one question's turns · splits distinct Qs"]
        DRIVE --> PDFT
        LEARN --> LEARNP
        CHAT --> CHATP
    end

    %% ── ② query repository ────────────────────────────────────────
    subgraph REPO["Query Repository"]
        CHUNK["chunks table<br/>source_type file · learning · chat<br/>source_id · asset_id nullable · owner = importer"]
        EMBED["build_chunks + BGE-M3 embed → pgvector<br/>leaf-only · chunked per runtime config"]
        PDFT --> CHUNK
        LEARNP --> CHUNK
        CHATP --> CHUNK
        CHUNK --> EMBED
    end

    %% ── ③ retrieval node pipeline ─────────────────────────────────
    subgraph RETR["Retrieval — config-driven node pipeline"]
        Q["user query · top_k<br/>+ filters.user_id · tenant binding<br/>owner / workspace / ACL · guest → public links"]
        REWRITE["QueryRewriter<br/>multi-query expansion + HyDE answer"]
        VEC["VectorRecaller<br/>pgvector cosine · BGE-M3 via TEI<br/>source-aware · LEFT JOIN assets"]
        KW["KeywordRecaller<br/>tsvector FTS · CJK via jieba<br/>source-aware · READY only for files"]
        RRF["rrf_fusion · k=60<br/>fuses cross-scale scores"]
        CE["CrossEncoderReranker<br/>BGE-reranker · optional"]
        PE["ParentExpandNode<br/>leaf → parent · optional"]
        CRG["CrgCheckNode<br/>LLM relevance gate · optional"]
        OUT["top_k SearchHits<br/>ground the chat answer"]
        Q --> REWRITE
        REWRITE --> VEC
        REWRITE --> KW
        VEC --> RRF
        KW --> RRF
        RRF --> CE
        RRF -. no reranker .-> OUT
        CE --> OUT
        RRF -. optional extension .-> PE
        PE --> CRG
        CRG --> OUT
    end

    EMBED -. source-aware recall<br/>leaf-only · LEFT JOIN assets .-> VEC
    EMBED -. source-aware recall .-> KW

    %% ── ④ RAG pipeline config ─────────────────────────────────────
    subgraph CFG["RAG Pipeline Config · admin console RAG module"]
        NODES["Nodes<br/>add / remove / reorder / toggle<br/>per-node params · live, no restart"]
        CHUNKCFG["Chunking<br/>size / overlap · contextualization"]
    end
    NODES -. drives topology .-> RETR
    CHUNKCFG -. drives chunking .-> EMBED
```

</details>

> [docs/architecture.md](docs/architecture.md) is the single source of truth for the full design —
> tech stack, repository layout, agent-kernel internals, tool runtime, data model, and deployment
> topology (including what is implemented today vs. designed-only).

---

## ✨ Key Features

### 🖥️ Desktop Workbench (Local Client)
- **Workspace file tree**: open any local folder as your workspace (the last one is restored on the next launch); add files from anywhere, create folders and text files, delete files & folders with a right-click (permanent, workspace-bounded), and fuzzy-search the tree with live suggestions that jump straight to a match.
- **My Drive in the app**: the sidebar's **☁️ Cloud** source browses your cloud **My Drive** — open `.md`/`.txt` notes in the built-in Markdown editor (✏ Edit / 👁 Preview / 💾 Save, synced straight back to the server), and stream PDFs, images, video, and audio into the in-window viewer via a temp-cache download. Anything you save shows up in the web console on refresh.
- **Summarize**: condense a document/video into a summary or key-point outline.
- **Generate slides & mind maps**: turn the current material into slide decks and mind-map overviews.
- **Edit the material**: modify the loaded document in place.
- **Clip & discuss**: select a passage in the viewer, clip it, and start a discussion with the AI tutor about that selection.
- **Bookmarks**: mark pages, sections, or words to revisit — jump back to any saved spot anytime.
- **Learning notes**: capture and organize your notes alongside the material as you study, ready when you review.
- **Sessions search**: search your chat history by content — matching snippets are highlighted in the results.
- **Multi-format viewer**: video (with subtitles), audio, images, PDF (with annotations), and text/code. Word/Excel/PowerPoint (`.docx`/`.xlsx`/`.xls`/`.pptx`) preview **in-window** with pure-JS renderers — **mammoth** (Word), **SheetJS** (Excel), **JSZip** slide deck (PowerPoint) — no external apps required; older binary formats (`.doc`/`.ppt`) fall back to your OS default app. One-click video **screenshots** and **Generate PPT / Generate Book**. A toolbar **✕** (or **Esc**) dismisses the current document back to the empty state.
- **Subtitles**: a sibling `.srt`/`.vtt`/`.lrc` is auto-detected, or pick one manually; enable/disable and style it (size, color, background, position), and your settings are remembered.
- **Streaming chat**: answers stream in with a collapsible **💭 thinking** panel; dock the chat to the bottom or side, or float it as a window. Messages render **Markdown + math** (`$...$` / `$$...$$` formulas via KaTeX), and every bubble has **Copy / Read / Delete / Edit** actions — **Edit** (on a user question) re-asks it, dropping that turn and everything after before streaming a fresh answer; sessions can be **renamed** (click the title) or **deleted** from the sidebar.
- **Native menus & settings**: everything you'd expect — open/switch workspaces, zoom & font size, fullscreen, Help & Feedback, About. ⚙️ Settings covers theme, display, updates, help, and about.

### 💬 AI Chat Assistant
- **Agentic Kernel** (`AgentKernel`): a composition root wiring a cache-boundary prompt assembler, deferred tool loading, dual-track memory, a skill catalog, and a read-only sandbox around a `ReactLoopAgent` step loop.
- **Cache-Boundary Prompt**: the system prompt is partitioned into three zones — a byte-stable static prefix (SOUL.md + compact tool/skill catalog), project context, and a per-step dynamic suffix — so the provider's prefix cache reuses the stable head across steps. Project conventions are loaded from the workspace's `DEEPDIVE.md` into their own stable zone; `snapshot_key()` makes the cache identity measurable.
- **Deferred Tool Loading**: the prompt carries only a compact `name + blurb` catalog plus the resident `tool_search` meta-tool; matched tools appear in the visible set as stable `name + description` stubs (defer_loading style) so the cached tools array never churns, and each tool's full schema is returned in the `tool_search` result. The prompt window is also bounded by a per-message snip plus a token-aware autocompact that fires on a character budget.
- **Dual-Track Memory**: session recall fuses PostgreSQL tsvector (keyword) + pgvector (semantic) via RRF, recency-weighted so newer messages win near-ties; when the embedding service is offline it degrades to tsvector-only — never a silent empty. `memory_search` / `memory_save` are tools; `memory_save` writes guardrailed notes (kebab-case key, length-capped content, closed type taxonomy) to the local memory directory while the session stays read-only. Proactive recall injects top hits for your question into the prompt, and long conversations are auto-compacted into an LLM summary (bounded token window).
- **Skill Catalog**: skills (SKILL.md) are advertised as a one-line compressed index; the full instructions are lazy-loaded through the `skill` meta-tool.
- **Read-Only Sandbox**: every tool call is gated by session permissions (READ / WRITE / NETWORK); file tools are rooted at the workspace dir and path escape is rejected. Writes and network access need an explicit grant or human approval.
- **RAG Retrieval**: a config-driven node pipeline — query rewrite → multi-recall (vector + keyword) → RRF fusion → rerank, extensible with CJK segmentation, contextual enrichment, parent/child indexing, and domain filtering. Full feature walk-through in **RAG & Query Repository** below.
- **Query Repository**: one search corpus for cloud-drive files (PDF tables transcribed via vision), Learning-Platform sentences/articles, and chat Q&A.
- **SSE Streaming**: Real-time token streaming to the frontend.

### 🔎 RAG & Query Repository
- **Plug-and-play retrieval pipeline**: the retrieval flow is a composable list of stages. Default chain:
  *query rewrite → vector recall + keyword recall → RRF fusion → cross-encoder rerank*, plus two optional
  stages — *parent-expand* (widen a leaf hit to its parent chunk) and *relevance check* (drop chunks the
  LLM judges irrelevant). From admin **RAG → Nodes** you add / remove / reorder / enable / disable any
  stage and edit its parameters live — no code, no restart.
- **Three input channels, one searchable corpus**: cloud-drive files, Learning-Platform sentences &
  articles, and chat Q&A all feed a single query repository, so one query retrieves across all of them.
  Each entry is tagged by source; learning / chat content stays visible to the user who imported it,
  while files keep their usual sharing / ACL rules.
- **Import content & multiple text formats**: files get a **＋ Import to Knowledge** button and an
  "in knowledge" badge once indexed. Supported formats: plain text (`.txt` / `.md` / `.log` / `.json` /
  `.csv`), subtitles (`.srt` / `.vtt` / `.lrc`), Word (`.docx`), and PDF (`.pdf`). PDFs extract body text
  *and* detect tables, rendering each to an image the vision LLM transcribes (a failing table is skipped,
  never fatal). The Learning Platform lets you import saved sentences and write articles; chat lets you
  import a single reply (bound to its question) or organize a whole session — the LLM merges the same
  question's follow-up turns into one entry and splits distinct questions. Imported chat entries show a
  persistent **✓ Imported** state.
- **Configurable chunking**: pick a split strategy — `fixed` sliding window, `paragraph`, `sentence`, or
  `semantic` — and set chunk size / overlap. Optionally enable **contextual** enrichment (an LLM-written
  context prefix per chunk), **parent/child** indexing, and **CJK** keyword search (jieba). All of it is
  driven from the admin console and applied on re-index; a **Chunking preview** tab shows exactly how a
  strategy splits your text before you commit.
- **Parent/child (small-to-big), an input/output pair**: turn on parent/child **indexing** under chunking
  to store leaf chunks plus larger parent windows (retrieval searches the leaves), then add the
  **parent_expand** stage so a leaf hit returns its parent's fuller text. The admin **RAG → Repository**
  tab lists every imported non-file chunk with a source badge and per-chunk delete.
- **Test before you ship**: the admin RAG module has a **Test** tab that runs the configured pipeline
  stage by stage and shows a per-node trace, and a golden-set **Eval** regression that scores
  Recall@k / Precision@k / MRR against your expected answers.
- **Domain-scoped search**: the `rag_search` tool takes an optional `domain` argument — retrieval
  narrows to assets in that knowledge domain (file chunks only).
- **Re-index & safe re-import**: an admin **Reindex** button re-ingests every ready file under the
  latest chunking config; re-importing a source replaces its old chunks first — no duplicates.
- **Degrade, never break the chat**: when the retrieval stack is unavailable, the assistant answers
  from its own knowledge with a notice instead of erroring or retrying.

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
- **Notes & Markdown editing**: text files (`.md`, `.txt`, code, data) open in an in-page **note editor** — a **✏ Edit / 👁 Preview** toggle renders Markdown live, and **💾 Save** (`Ctrl+S`) rewrites the file in place (the bytes are re-deduplicated and RAG indexing re-runs). Rendering is XSS-safe: raw HTML is escaped and `javascript:`-style links are blocked.
- **In-window Office previews — web console and desktop**: both the **web console Cloud Drive** and the **desktop app** render Word/Excel/PowerPoint files in-window (`.docx`; `.xlsx`/`.xls`/`.csv`/`.tsv`; `.pptx`/`.ppsx`/`.potx`/… slide decks) with the same pure-JS renderers (**mammoth** for Word, **SheetJS** for spreadsheets — one tab per sheet — and a **JSZip**-based slide deck for PowerPoint). Clicking never downloads: only the **⬇ Download** button fetches the bytes. `.doc`/`.ppt` have no browser parser and show a **can't-preview** panel (Download / Open in new tab), while images, PDF, video, and audio open in a new tab.
- **Push files into the query repository**: text-bearing files (`.txt`/`.md`/`.pdf`/`.docx`/…; audio/video/slides excluded) show a **＋ Import to Knowledge** button — click it to ingest (or re-ingest under the latest RAG config), and an **In Knowledge** badge marks files already in the corpus.
- **First-class folders**: multi-level folders (`folders` rows with full `/`-paths) live side by side with files, and the file manager tree combines them. Create/rename/move/delete folders; renaming or deleting a folder rewrites the path prefix of every file and sub-folder beneath it.
- **Context menu & collision-safe naming**: right-click a file or folder for **📄 New text file**, **📁 New folder**, **📤 Upload**, and **🗑 Delete** (deleting a folder trashes its files first). Files and folders share one namespace per directory, so creating/moving/renaming into a busy spot auto-suffixes `(1)`, `(2)`, … before the extension (`docs` → `docs(1)`, `a.docx` → `a(1).docx`) instead of failing.
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
- **Query Repository import**: push saved sentences and written articles (Articles & Query Repo tab) into the unified search corpus alongside cloud-drive files and chat Q&A.

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
images, PDF with annotations, text/code, Office documents rendered in-window), take video screenshots, and chat with
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

All surfaces talk to the same backend. Start with the **desktop workbench** — the offline-first local
client is the primary way to study; the **web console** covers browser-based learning, the **cloud
drive** holds your files, and the **admin console** is for operators.

### 🖥️ Desktop workbench (local client)

A standalone local workbench — the file tree, multi-format viewer, video screenshots, and subtitles work **without the backend**:

- **Open Workspace** to browse any local folder; the viewer plays video (with subtitles), audio, images, PDF (with annotations), and text/code, and previews Word/Excel/PowerPoint in-window with pure-JS renderers (`.docx`/`.xlsx`/`.xls`/`.csv`/`.tsv`/`.pptx`, plus `.doc` as extracted text and images) — only `.ppt` opens in the OS app.
- Switch the sidebar source from **💻 Local** to **☁️ Cloud** to browse your My Drive — open and edit text notes in the built-in Markdown editor (✏ / 👁 Preview / 💾 Save, `Ctrl+S`), or watch PDFs, video, images, and audio stream through the in-window viewer. Changes are saved straight to the server and show up in the web console.
- Take one-click video **screenshots** and **Generate PPT / Generate Book** from the current material.
- **Chat** streams answers with a collapsible **💭 thinking** panel (dock to bottom/side or float as a window); session history and search live in the sidebar. Bubbles render **Markdown + KaTeX math**; hover a bubble for **Copy / Read / Delete / Edit** — editing a question re-asks it (the turn and everything after are removed, then a fresh answer streams in). A reply's **📥 Import Repo** action binds it to its question as one query-repository chunk, and the header **📥** organizes the whole session (the LLM groups each distinct question into its own chunk). An imported pair or session turns its **📥** into a persistent **✓ Imported** (disabled) state that survives session switches and app restarts. Click a session's title to **rename** it, or **delete** it from the sidebar.
- **Sign in** from the account menu (register new accounts / reset passwords; guests chat anonymously up to the daily limit); ⚙️ **Settings** covers theme, font size, window & display, update checks, help, and about.
- The account menu deep-links straight to the **web console**, the **Cloud Drive**, and — for admins — the **admin console**, signed in automatically via SSO.

Chat, session history & search, media generation, sign-in, and the **☁️ Cloud** My Drive panel all need the backend on `localhost:8300`; the desktop main process forwards `/api`, `/audio`, and `/images` to it.

### 🖥️ Web console (Learning Platform)

1. **Create Domain**: Navigate to *Import Data* → *Domain Management* to start a new topic (e.g. "AI Research Papers").
2. **Import Terms**: Switch to *Import Vocabulary*. Upload your vocabulary CSV or paste text directly.
3. **Build Corpus (Two Layers)**:
   - **Layer 1 (SQL)**: Import sentences for exact keyword matching.
   - **Layer 2 (Vector)**: Import raw text and index embeddings to enable semantic search.
4. **Interactive Study**: Navigate to *Study Mode* and click the **📖 View** icon to deep-dive — generate TTS audio, view AI definitions, record and compare your pronunciation, get context-aware sentence translations, view contextual images, navigate via Next/Prev, and save the best context to your database.
5. **Library Governance**: Navigate to *Manage Vocabulary* to sort globally, refine definitions with click-to-edit, and toggle term visibility.
6. **Chat Assistant**: Ask questions and get RAG-grounded, streamed answers.
7. **My Account**: check your wallet balance, daily usage, usage logs (per model / channel / tool), and transactions; edit your profile and avatar.

### ☁️ Cloud Drive (top tab *☁️ Cloud Drive*)

- **My Drive + workspaces**: every account gets a private **My Drive**; click **＋ New workspace** in the folder tree to create a shared workspace, and manage its members (owner / admin / editor / viewer) from **⚙ Manage**.
- **Upload & folders**: **⬆ Upload** streams files in chunks (already-stored content uploads instantly, deduplicated by SHA-256); **＋ New folder** builds multi-level paths like `English/Vocab`. Files larger than 256 MB go through the desktop client.
- **Notes**: click any text file to open it in the built-in note editor — toggle **👁 Preview** for rendered Markdown and hit **💾 Save** (or `Ctrl+S`) to write it back and re-index. Right-click in the file area for **📄 New text file** / **📁 New folder** / **📤 Upload** / **🗑 Delete**; a name already used in that folder is auto-renamed before the extension (`a.txt` → `a(1).txt`).
- **In-window Office previews**: clicking a Word/Excel/PowerPoint document (`.docx`, `.xlsx`/`.xls`, `.csv`/`.tsv`, `.pptx` and its slide-deck siblings) previews it right in the page with the same pure-JS renderers the desktop uses (**mammoth** for Word, **SheetJS** for spreadsheets — one tab per sheet — and a JSZip-based slide deck for PowerPoint); `.md`/`.txt` still open the note editor, and `.doc`/`.ppt` show a "can't preview" panel. Nothing downloads on click — use the **⬇ Download** button (or **↗ Open in new tab** for unrenderable formats).
- **Manage files**: toggle **✏ Edit** to multi-select, then download, open, share, rename, move (across workspaces / folders), or delete.
- **Share**: **🔗 Share** grants read/write to a specific user or creates a public link.
- **Search**: the search box fuzzy-matches file names and folder paths, scoped to a workspace or all of My Drive; jump straight to a result's folder.
- **Trash**: deleting a file moves it to **Trash** — restore, purge permanently, or **Empty Trash**; entries older than 30 days purge automatically. Deleting a workspace trashes its files and moves them to My Drive trash.
- **Query Repo column**: files are ingested for retrieval in the background — each file shows a badge (Pending → Parsing → Chunking → Embedding → Indexed) plus a **＋ Import to Knowledge** button to (re)ingest text-bearing files (PDF tables are read via vision); unsupported formats (audio/video/slides) show a disabled hint, and ingested files show a grey **In Knowledge** badge.

### 🔧 Admin console (`/admin`, default `admin` / `admin`)

Sign in as an operator to configure the whole instance from a single SPA:

- **Providers**: add OpenAI-compatible **Credentials** (base_url + api_key) per LLM channel, maintain the **Model Catalog** (per-1k prompt/completion pricing), set **Routing & Weights**, and smoke-test a channel in **Chat Test**.
- **Roles**: per-role quotas (daily/monthly requests, tokens, RPM, cost), default model, and the role ↔ credential channel bindings that decide which channels each role may use.
- **Users**: create accounts directly, browse users, and manage their key grants.
- **Tokens**: the per-user LLM key-grant matrix — which user may use which provider key (shown masked `sk-***`), with independent revoke/restore, plus a login-sessions view.
- **Tools config**: web-search provider + API key + engine id, **SMTP** (email verification / password reset), free-form key/value tool params, and a test-email button.
- **RAG**: a live console for the retrieval pipeline — **Test** a query and inspect every node's per-stage trace, **Chunking** previews split strategies (fixed / paragraph / sentence) with optional CJK keywords and contextual prefixes, **Nodes** edits the pipeline topology (add / remove / reorder / toggle nodes and their params), **Eval** runs the golden-set regression (Recall@k / Precision@k / MRR) to catch quality regressions, and **Repository** lists every non-file query-repository chunk (learning / chat sources) with per-chunk delete.

---

## 📝 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
