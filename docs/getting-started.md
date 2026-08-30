# Getting Started

## 1. Prerequisites

- **Docker Desktop** — runs PostgreSQL, Redis, and the model services (embedding / rerank / TTS / LLM gateway).
- **Conda** (Miniconda or Anaconda) — the backend runs in a `deepdive` env.
- **Node.js 18+** — for the web frontend (optional; the API runs without it).
- **Git**.

## 2. Clone the repository

```bash
git clone https://github.com/Eric-LLMs/DeepDive.git
cd DeepDive
```

## 3. Create & activate the conda environment

```bash
conda create -n deepdive python=3.11 -y
conda activate deepdive
```

## 4. Configure environment

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Fill in `LLM_UPSTREAM_KEY` (your real OpenAI-compatible key). Every other variable has a working local-dev default — see the [configuration reference](configuration.md).

## 5. Install backend dependencies

```bash
pip install -e ".[dev]"     # runtime + test tooling (== pip install -r requirements-dev.txt)
pip install -e ".[rag]"     # optional: RAG semantic search (pulls torch / sentence-transformers)
```

## 6. Start infrastructure (data + model services)

```bash
docker compose up -d postgres redis embedding tts llm-gateway worker
```

The first start downloads the models (BGE-M3, Kokoro-82M) into Docker volumes — allow a few minutes. The LLM gateway routes the virtual model `deepdive-chat` to `LLM_UPSTREAM_MODEL` using `LLM_UPSTREAM_KEY`.

> Skip `embedding` if you don't use semantic search, and `tts` if you don't need audio — the API degrades gracefully.
>
> **Docker Desktop (Windows) memory note:** the TEI embedding service needs ~9 GB during BGE-M3 warmup. If Docker's WSL2 backend has only ~8 GB (the default on a 16 GB host), the container gets OOM-killed. Raise the limit in `%UserProfile%\.wslconfig`, e.g. `[wsl2]\nmemory=12GB\nswap=4GB`, then run `wsl --shutdown` and restart Docker Desktop.

## 7. Initialize the database (SQL migrations)

```bash
python scripts/init_db.py     # applies migrations/*.sql in order (creates all tables incl. jobs)
# or run a single script directly with psql:
psql -d deepdive -f migrations/0001_init.sql
```

## 8. Run the API

```bash
uvicorn apps.api.main:app --reload
```

Open http://localhost:8300/docs for the interactive API documentation.

> The **worker** (async enrichment) runs as a docker-compose service (see step 6). To run it on
> the host instead: `arq apps.worker.settings.WorkerSettings`.

## 9. (Optional) gRPC retrieval service

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

## 10. Run the tests

```bash
pytest
```

## 11. Run the web frontend (optional)

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:5273. The Vite dev server proxies `/api`, `/audio`, and `/images` to the backend.

## 12. Run the desktop workbench (Electron, optional)

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

## One-click launchers (pick by environment)

Both scripts print a `[n/N]` banner before each step so you can see where they are. Each step is
skipped when its target is already up (backend, Docker, infra), so re-running is fast and safe.
First run on a fresh machine also installs Docker and the Python/Node deps automatically.

| Environment | Script | What it does |
|---|---|---|
| **Windows desktop** (local PC client) | `bash scripts/start_desktop.sh` | Auto-installs Docker Desktop if missing → starts **all** dependency services (postgres, redis, embedding, tts, llm-gateway, worker) → ensures the Python venv → starts the backend (boot seeds `admin`/`admin`) → opens the Electron workbench. |
| **Linux server** (browser access) | `bash scripts/start_server.sh` | Auto-installs Docker Engine if missing → starts **all** dependency services (postgres, redis, embedding, tts, llm-gateway, worker) → ensures the Python venv → starts the backend (boot seeds `admin`/`admin`) → builds and serves the React web UI at `http://<server-ip>:5273`. |

The default `admin` / `admin` account is seeded on first boot and ready to sign in from the start.

---

# How to Use

All surfaces talk to the same backend. Start with the **desktop workbench** — the local-first client
is the primary way to study; the **web console** covers browser-based learning, the **cloud drive**
holds your files, and the **admin console** is for operators.

## 🖥️ Desktop workbench (local client)

A standalone local workbench — the file tree, multi-format viewer, video screenshots, and subtitles work **without the backend**:

- **Open Workspace** to browse any local folder; the viewer plays video (with subtitles), audio, images, PDF (with annotations), and text/code, and previews Word/Excel/PowerPoint in-window with pure-JS renderers (`.docx`/`.xlsx`/`.xls`/`.csv`/`.tsv`/`.pptx`, plus `.doc` as extracted text and images) — only `.ppt` opens in the OS app.
- Switch the sidebar source from **💻 Local** to **☁️ Cloud** to browse your cloud drive — **My Drive**, shared **workspaces**, and **🗑 Trash**, with **list / grid** views, per-folder **search**, **✏ Edit** mode + batch actions (Download / Share / Rename / Move / Trash), workspace **⚙ Manage**, and a **Query Repo** column (**＋ Import to Knowledge** / ✓ In Knowledge). Open and edit text notes in the built-in Markdown editor (**Edit / Preview** toggle, **Save**, `Ctrl+S`), or watch PDFs, video, images, and audio stream through the in-window viewer. Changes are saved straight to the server and show up in the web console.
- Take one-click video **screenshots** and **Generate PPT / Generate Book** from the current material.
- **Chat** streams answers with a collapsible **💭 thinking** panel (dock to bottom/side or float as a window); session history and search live in the sidebar. Bubbles render **Markdown + KaTeX math**; hover a bubble for **Copy / Read / Delete / Edit / Import to Knowledge** — editing a question re-asks it (the turn and everything after are removed, then a fresh answer streams in); **Read** speaks the message aloud via streaming TTS. A reply's **Import to Knowledge** binds it to its question as one query-repository chunk, and the header **⋯** menu organizes the whole session (the LLM groups each distinct question into its own chunk). An imported pair or session turns its import button into a persistent **✓ Imported** (disabled) state that survives session switches and app restarts. Click a session's title to **rename** it, or **delete** it from the sidebar. The input is a multi-line textarea (Enter sends, Shift+Enter for a new line); a **Generate** toolbar above it opens the generate dialog (Mind Map / Slides / Summary) — pick this conversation or Cloud Drive files, the output folder, and an optional prompt & name; the job runs in the background and the artifact lands in the chosen Cloud Drive folder.
- **Sign in** from the account menu (register new accounts / reset passwords; guests chat anonymously up to the daily limit); ⚙️ **Settings** covers theme, font size, window & display, update checks, help, and about.
- The account menu deep-links straight to the **web console**, the **Cloud Drive**, and — for admins — the **admin console**, signed in automatically via SSO.

Chat, session history & search, media generation, sign-in, and the **☁️ Cloud** drive panel all need the backend on `localhost:8300`; the desktop main process forwards `/api`, `/audio`, and `/images` to it.

## 🖥️ Web console (Learning Platform)

1. **Create Domain**: Navigate to *Import Data* → *Domain Management* to start a new topic (e.g. "AI Research Papers").
2. **Import Terms**: Switch to *Import Vocabulary*. Upload your vocabulary CSV or paste text directly.
3. **Build Corpus (Two Layers)**:
   - **Layer 1 (SQL)**: Import sentences for exact keyword matching.
   - **Layer 2 (Vector)**: Import raw text and index embeddings to enable semantic search.
4. **Interactive Study**: Navigate to *Study Mode* and click the **📖 View** icon to deep-dive — generate TTS audio, view AI definitions, record and compare your pronunciation, get context-aware sentence translations, view contextual images, navigate via Next/Prev, and save the best context to your database.
5. **Library Governance**: Navigate to *Manage Vocabulary* to sort globally, refine definitions with click-to-edit, and toggle term visibility.
6. **Chat Assistant**: Ask questions and get RAG-grounded, streamed answers.
7. **My Account**: check your wallet balance, daily usage, usage logs (per model / channel / tool), and transactions; edit your profile and avatar.

## ☁️ Cloud Drive (top tab *☁️ Cloud Drive*)

- **My Drive + workspaces**: every account gets a private **My Drive**; click **＋ New workspace** in the folder tree to create a shared workspace, and manage its members (owner / admin / editor / viewer) from **⚙ Manage**.
- **Upload & folders**: **⬆ Upload** streams files in chunks (already-stored content uploads instantly, deduplicated by SHA-256); **＋ New folder** builds multi-level paths like `English/Vocab`. Files larger than 256 MB go through the desktop client.
- **Notes**: click any text file to open it in the built-in note editor — toggle **Preview** for rendered Markdown (a Mermaid `mindmap` note renders as an SVG tree of nodes + edges) and hit **Save** (or `Ctrl+S`) to write it back and re-index. Right-click in the file area for **📄 New text file** / **📁 New folder** / **📤 Upload** / **🗑 Delete**; a name already used in that folder is auto-renamed before the extension (`a.txt` → `a(1).txt`).
- **In-window Office previews**: clicking a Word/Excel/PowerPoint document (`.docx`, `.xlsx`/`.xls`, `.csv`/`.tsv`, `.pptx` and its slide-deck siblings) previews it right in the page with the same pure-JS renderers the desktop uses (**mammoth** for Word, **SheetJS** for spreadsheets — one tab per sheet — and a JSZip-based slide deck for PowerPoint); `.md`/`.txt` still open the note editor, and `.doc`/`.ppt` show a "can't preview" panel. Nothing downloads on click — use the **⬇ Download** button (or **↗ Open in new tab** for unrenderable formats).
- **Manage files**: toggle **✏ Edit** to multi-select, then download, open, share, rename, move (across workspaces / folders), or delete.
- **Share**: **🔗 Share** grants read/write to a specific user or creates a public link.
- **Search**: the search box fuzzy-matches file names and folder paths, scoped to a workspace or all of My Drive; jump straight to a result's folder.
- **Trash**: deleting a file moves it to **Trash** — restore, purge permanently, or **Empty Trash**; entries older than 30 days purge automatically. Deleting a workspace trashes its files and moves them to My Drive trash.
- **Query Repo column**: files are ingested for retrieval in the background — each file shows a badge (Pending → Parsing → Chunking → Embedding → Indexed) plus a **＋ Import to Knowledge** button to (re)ingest text-bearing files (PDF tables are read via vision); unsupported formats (audio/video/slides) show a disabled hint, and ingested files show a grey **In Knowledge** badge.

## 🔧 Admin console (`/admin`, default `admin` / `admin`)

Sign in as an operator to configure the whole instance from a single SPA:

- **Providers**: add OpenAI-compatible **Credentials** (base_url + api_key) per LLM channel, maintain the **Model Catalog** (per-1k prompt/completion pricing), set **Routing & Weights**, and smoke-test a channel in **Chat Test**.
- **Roles**: per-role quotas (daily/monthly requests, tokens, RPM, cost), default model, and the role ↔ credential channel bindings that decide which channels each role may use.
- **Users**: create accounts directly, browse users, and manage their key grants.
- **Tokens**: the per-user LLM key-grant matrix — which user may use which provider key (shown masked `sk-***`), with independent revoke/restore, plus a login-sessions view.
- **Tools config**: web-search provider + API key + engine id, **SMTP** (email verification / password reset), free-form key/value tool params, and a test-email button.
- **RAG**: a live console for the retrieval pipeline — **Test** a query and inspect every node's per-stage trace, **Chunking** previews split strategies (fixed / paragraph / sentence) with optional CJK keywords and contextual prefixes, **Nodes** edits the pipeline topology (add / remove / reorder / toggle nodes and their params), **Eval** runs the golden-set regression (Recall@k / Precision@k / MRR) to catch quality regressions, and **Repository** lists every non-file query-repository chunk (learning / chat sources) with per-chunk delete.
