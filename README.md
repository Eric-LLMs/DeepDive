# 🧠 DeepGloss

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepGloss** is an AI learning workbench for domain-specific English learning. It focuses on **contextual learning** within specific domains (e.g. "Stanford CS336 Lectures", "Legal English", "Medical Terms"): import vocabulary and example sentences, automatically fetch definitions, generate Text-to-Speech (TTS) audio, retrieve contextual images, and get context-aware AI explanations. A **hybrid search engine** (PostgreSQL keyword + pgvector semantic search) finds relevant example sentences even when exact keywords are missing, and a **native function-calling agent** powers interactive Q&A with RAG and MCP tooling.

---

## 📸 Screenshots

**1. Clean & Modern Vocabulary List**

Seamlessly sort, search, and view inline definitions via hover popovers without leaving the page.

![Vocabulary List](docs/images/listpage_demo.png)

**2. Interactive Study Dialog**

Practice pronunciation with the built-in mic widget, compare with native TTS, visualize abstract concepts with automatically fetched contextual images, and get AI-powered contextual explanations. The system retrieves sentences via both keyword match and semantic vector search.

![Practice Dialog](docs/images/practice_demo.png)

**3. Smart Data Import Center**

Manage domains and import vocabulary, raw corpus (SQL), and semantic embeddings (VectorDB) with intelligent deduplication in one place.

![Data Import](docs/images/data_upload_demo.png)

**4. Efficient Library Governance**

Toggle terms with smooth switches, rate importance with star icons, click to edit definitions, and commit page-level changes transactionally.

![Manage Vocabulary](docs/images/manage_vocab_demo.png)

---

## ✨ Key Features

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

### 🛠️ Efficient Library Governance
- **Efficient Toggles**: Instantly enable/disable terms with visual feedback.
- **Click-to-Edit**: Definitions display as clean labels and expand into editors only when clicked, preventing accidental edits.
- **Unified Visuals**: Star levels are managed via intuitive icon pickers (⭐) instead of raw numbers.
- **Transactional Page Commits**: Commit all modifications on a single page with one click for high-speed bulk updates while maintaining data integrity.
- **Global Operation Flow**: Perform global sorting across the entire database and save changes page-by-page.
- **Self-Healing Logic**: Automatically deduplicates duplicate matches to keep the UI stable.

### 💬 AI Chat Assistant
- **Native Function-Calling Agent**: An explicit loop that calls tools and folds results back into the conversation.
- **RAG Retrieval**: query rewrite → multi-recall (vector + keyword) → RRF fusion → rerank.
- **MCP Tool Integration**: Tools are defined once and shared by the Agent, RAG, and MCP (FastMCP).
- **SSE Streaming**: Real-time token streaming to the frontend.

---

## 🚀 Getting Started (from zero)

### 1. Prerequisites

- **Docker Desktop** — runs PostgreSQL, Redis, and the model services (embedding / rerank / TTS / LLM gateway).
- **Conda** (Miniconda or Anaconda) — the backend runs in a `deepgloss` env.
- **Node.js 18+** — for the web frontend (optional; the API runs without it).
- **Git**.

### 2. Clone the repository

```bash
git clone https://github.com/Eric-LLMs/DeepGloss.git
cd DeepGloss
```

### 3. Create & activate the conda environment

```bash
conda create -n deepgloss python=3.11 -y
conda activate deepgloss
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
docker compose up -d postgres redis embedding tts llm-gateway
```

The first start downloads the models (BGE-M3, Kokoro-82M) into Docker volumes — allow a few minutes. The LLM gateway routes the virtual model `deepgloss-chat` to `LLM_UPSTREAM_MODEL` using `LLM_UPSTREAM_KEY`.

> Skip `embedding` if you don't use semantic search, and `tts` if you don't need audio — the API degrades gracefully.
>
> **Docker Desktop (Windows) memory note:** the TEI embedding service needs ~9 GB during BGE-M3 warmup. If Docker's WSL2 backend has only ~8 GB (the default on a 16 GB host), the container gets OOM-killed. Raise the limit in `%UserProfile%\.wslconfig`, e.g. `[wsl2]\nmemory=12GB\nswap=4GB`, then run `wsl --shutdown` and restart Docker Desktop.

### 7. Initialize the database

```bash
python scripts/init_db.py
```

### 8. Run the API

```bash
uvicorn api.main:app --reload
```

Open http://localhost:8000/docs for the interactive API documentation.

### 9. (Optional) gRPC retrieval service

The default `retrieval_mode` is `in_process` (RAG runs inside the API). To run retrieval as a separate gRPC service:

```bash
bash scripts/gen_proto.sh        # generates retrieval.v1 stubs into packages/shared/proto/
python -m apps.retrieval.main    # starts the gRPC server on localhost:15051
```

Then set `RETRIEVAL_MODE=grpc` in `.env` and restart the API:

```bash
RETRIEVAL_MODE=grpc uvicorn api.main:app --reload
```

The `rag_search` tool now calls the retrieval service over gRPC instead of the in-process RAG pipeline — no tool code changes (the capability seam does the swap).

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

---

## ⚙️ Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://deepgloss:deepgloss@localhost:5432/deepgloss` | PostgreSQL + pgvector |
| `REDIS_URL` | `redis://localhost:16379/0` | cache / queue |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `sk-local-gateway` / `http://localhost:4000/v1` / `deepgloss-chat` | the LiteLLM gateway the API talks to |
| `LLM_UPSTREAM_MODEL` / `LLM_UPSTREAM_BASE` / `LLM_UPSTREAM_KEY` | `openai/gpt-4o-mini` / `https://api.openai.com/v1` / `sk-xxx` | real upstream LLM (consumed by the gateway container) |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `http://localhost:8080` / `BAAI/bge-m3` / `1024` | TEI embedding service |
| `TTS_BASE_URL` / `TTS_MODEL` / `TTS_VOICE` | `http://localhost:8880/v1` / `kokoro` / `am_michael` | Kokoro-FastAPI TTS service |
| `RETRIEVAL_MODE` / `RETRIEVAL_GRPC_ADDR` | `in_process` / `localhost:15051` | capability seam: `in_process` or `grpc` |

Model inference never runs inside the API process. Swapping a model = change `--model-id` in `docker-compose.yml` and the matching `*_BASE_URL` / dim in `.env` — no business-code change. See [docs/architecture.md](docs/architecture.md) for the full topology.

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
