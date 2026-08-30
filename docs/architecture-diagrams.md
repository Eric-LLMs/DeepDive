# Architecture Diagrams

Per-module architecture diagrams and their mermaid source (for editing / regeneration via mermaid.ink).

![Architecture overview — frontends, backend, data, and model services](./images/architecture-overview.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

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

</details>

**Agent kernel** — the core, composed in and running inside the FastAPI gateway process:

![Agent kernel — the ReactLoopAgent composition root](./images/agent-kernel.png)

<details>
<summary>Mermaid source (for editing — regenerate via mermaid.ink)</summary>

```mermaid
flowchart TB
    kernel[AgentKernel] --> loop[ReactLoopAgent step loop]
    loop --> prompt[CacheBoundaryAssembler<br/>cache-boundary prompt]
    loop --> tools[ToolGateway<br/>deferred tool loading]
    loop --> skills[SkillCatalog · SKILL.md]
    loop --> memory[MemoryService<br/>PG tsvector + pgvector RRF]
    tools --> sandbox[Sandbox<br/>READ / WRITE / NETWORK]
```

</details>

> Agent kernel — design: [architecture.md §5 Agent Module](architecture.md#5-agent-module).

**Answering flow** — each turn runs the `ReactLoopAgent` step loop, which decides what to retrieve and
composes the reply. It calls the **`rag_search` tool** (runs the RAG node pipeline and returns `top_k`
chunks — the retrieval layer described in the RAG section) and, only when local material is
insufficient, the **`web_search` tool** (duckduckgo / tavily / bing / google) for up-to-date external
results; it then composes the answer from both.

**Memory — two independent tracks, orchestrated by `MemoryService`** (writes go to the local
memdir, recall reads the database; the tracks never cross):

![Memory architecture — two independent tracks](./images/memory-architecture.png)

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
> Design: [architecture.md §5.4 — Memory, skills, sessions](architecture.md#54-memory-skills-sessions).

**Prompt — cache-boundary assembly, compression, and deferred tool stubs** (architecture, design
features, and per-step process logic):

![Prompt — cache-boundary assembly, compression, and deferred tool stubs](./images/prompt-architecture.png)

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
> Design: [architecture.md §16 — Prompt Module](architecture.md#16-prompt-module).

**RAG & Query Repository — a config-driven, plug-and-play retrieval pipeline** over one unified search
corpus. A single query searches your cloud-drive files, Learning-Platform material, and chat Q&A
together. The pipeline's node list *is* the retrieval topology: compose, reorder, toggle, and tune every
stage from the admin console — live, no code, no restart. Feature walk-through in
[**Key Features**](docs/features.md); the design detail lives in
[§10 RAG Module (architecture.md)](architecture.md#10-rag-module-config-node-pipeline).

![RAG — config-driven node pipeline](./images/rag-architecture.png)

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
        QC["QueryCache<br/>Redis · key(query, filters, top_k,<br/>config_version, corpus_version)<br/>pure accelerator · failure falls through"]
        REWRITE["QueryRewriter<br/>multi-query expansion + HyDE answer"]
        VEC["VectorRecaller<br/>pgvector cosine · BGE-M3 via TEI<br/>source-aware · LEFT JOIN assets"]
        KW["KeywordRecaller<br/>tsvector FTS · CJK via jieba<br/>source-aware · READY only for files"]
        RRF["rrf_fusion · k=60<br/>fuses cross-scale scores"]
        CE["CrossEncoderReranker<br/>BGE-reranker · optional"]
        PE["ParentExpandNode<br/>leaf → parent · optional"]
        CRG["CrgCheckNode<br/>LLM relevance gate · optional"]
        OUT["top_k SearchHits<br/>ground the chat answer"]
        FB["rag_feedback<br/>query · hits · rating → golden eval set"]
        Q --> REWRITE
        Q -. cache hit → .-> QC
        QC -.-> OUT
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
        OUT -. rated → .-> FB
    end

    EMBED -. source-aware recall<br/>leaf-only · LEFT JOIN assets .-> VEC
    EMBED -. source-aware recall .-> KW
    EMBED -. ingest / re-index<br/>bumps corpus_version .-> QC

    %% ── ④ RAG pipeline config ─────────────────────────────────────
    subgraph CFG["RAG Pipeline Config · admin console RAG module"]
        NODES["Nodes<br/>add / remove / reorder / toggle<br/>per-node params · live, no restart"]
        CHUNKCFG["Chunking<br/>size / overlap · contextualization"]
    end
    NODES -. drives topology .-> RETR
    CHUNKCFG -. drives chunking .-> EMBED
```

</details>

> [architecture.md](architecture.md) is the single source of truth for the full design —
> tech stack, repository layout, agent-kernel internals, tool runtime, data model, and deployment
> topology (including what is implemented today vs. designed-only).

