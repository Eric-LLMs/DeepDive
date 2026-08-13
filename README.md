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
- **Self-Healing Logic**: Automatically deduplicates legacy "dirty data" in matches to ensure UI stability.

### 💬 AI Chat Assistant
- **Native Function-Calling Agent**: An explicit loop (no LangChain/LangGraph) that calls tools and folds results back into the conversation.
- **RAG Retrieval**: query rewrite → multi-recall (vector + keyword) → RRF fusion → rerank.
- **MCP Tool Integration**: Tools are defined once and shared by the Agent, RAG, and MCP (FastMCP).
- **SSE Streaming**: Real-time token streaming to the frontend.

---

## 🚀 Getting Started

### 1. Prerequisites

- **Python 3.11+**
- **Node.js 18+** (for the web frontend)
- **Docker** (for PostgreSQL + Redis)

### 2. Clone the Repository

```bash
git clone https://github.com/Eric-LLMs/DeepGloss.git
cd DeepGloss
```

### 3. Configure Environment

Create a `.env` file from the template and fill in your keys:

```bash
cp .env.example .env
```

Key variables:

```env
# Database / Redis
DATABASE_URL=postgresql+asyncpg://deepgloss:deepgloss@localhost:5432/deepgloss
REDIS_URL=redis://localhost:16379/0

# LLM (OpenAI-compatible)
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini

# Embedding
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024

# TTS (leave TTS_API_KEY/TTS_BASE_URL empty to fall back to the LLM config, or use edge-tts)
TTS_MODEL=tts-1-hd
TTS_VOICE=alloy
```

### 4. Start the Database (PostgreSQL + pgvector + Redis)

```bash
docker compose up -d
```

### 5. Install Backend Dependencies

```bash
pip install -e ".[dev]"

# (optional) RAG semantic search — pulls in sentence-transformers + torch
pip install -e ".[rag]"

python scripts/init_db.py
```

### 6. Run the API

```bash
uvicorn api.main:app --reload
```

Open http://localhost:8000/docs for the interactive API documentation.

### 7. Run the Web Frontend

```bash
cd apps/web
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api`, `/audio`, and `/images` to the backend.

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
