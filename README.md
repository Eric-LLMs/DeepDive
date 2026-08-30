# <img src="docs/images/deepdive-logo.png" alt="DeepDive" width="40" valign="bottom" /> DeepDive

[English](README.md) · [中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepDive** is a **production-grade, multi-tenant AI learning platform** — a persistent tutor that learns from your materials, remembers how you learn, and helps you understand, research, and create.

Dive deeper: [**What you can do**](#what-you-can-do) explores the product, [**Engineering highlights**](#-engineering-highlights) breaks down the system, and [docs/architecture.md](docs/architecture.md) documents the full design.

## What is DeepDive?

DeepDive is a **multi-tenant AI learning platform** — a persistent tutor, your personal memory, and a searchable knowledge base in one self-hostable system.

**Why it's different:**

- **Learn with your material, not beside it** — select and discuss passages while reading or watching, with grounded explanations and step-by-step breakdowns.
- **Your material is the starting point, not the boundary** — when your sources aren't enough, the tutor autonomously searches the web and academic sources to expand your understanding.
- **Learning insights never disappear** — important insights become durable memory, while discussions turn into summaries, mind maps, and slides that flow back into your searchable workspace, so you can pick up where you left off on any device.

```text
Material → indexed → retrieved → transformed → artifact → searchable again
```

## What you can do

| Capability | What it lets you do |
|---|---|
| **Learn** | • Ask questions while reading or watching — PDFs, Office docs, video, audio, images, and more (all openable in the desktop client)<br>• Get step-by-step explanations and concept breakdowns<br>• Discuss a selected passage, page, or moment |
| **Research** | • Search across your files, notes, conversations, and sources<br>• Go to the web when your material isn't enough<br>• Synthesize multiple sources into a grounded answer |
| **Remember** | • Save durable insights and recall them in later sessions<br>• Keep long-term memory separate from conversation history<br>• Revisit bookmarks, notes, and saved spots |
| **Create** | • Summarize sessions, notes, and documents<br>• Generate mind maps and slide decks<br>• Turn conversations into reusable knowledge that flows back into search |
| **Collaborate** | • Share files and knowledge in workspaces<br>• Study the same material as a team with roles and permissions |

## Demo

> **Agent-tutor flow:** open a paper or video → ask while learning → retrieve relevant passages → search the web when needed → discuss and clarify → save an insight → summarize the session → recall it later. Recorded demo coming soon.

---

## 🔧 Engineering highlights

DeepDive implements a controllable agent runtime rather than delegating orchestration to a rigid framework. Core architectural decisions and their production-grade implementations:

- **Agent orchestration is explicit and controllable.** A `ReactLoopAgent` step loop orchestrates model invocations and tool execution through a hot-reloadable, dependency-injected skill catalog and plugin runtime; a typed sandbox strictly gates every tool execution across `READ` / `WRITE` / `NETWORK` permissions.
- **Durable memory is decoupled from session history.** Two independent tracks share a single prompt boundary: the agent writes durable long-term memory via file-backed storage, while the system manages episodic session memory in PostgreSQL (`tsvector` + `pgvector` fused via RRF with recency decay). Hierarchical history compaction (flat token window) keeps context bounded without losing it.
- **Reliability is built-in, not bolted on.** Hard timeouts and exponential-backoff retries absorb transient upstream LLM errors, bounded by a per-turn cost budget. Tool safety is enforced via a Redis pub/sub human-approval gate (deny on timeout), plan mode, bounded subagents, shadow-git checkpoints for state rollback, and a resource-capped, network-isolated Docker sandbox.
- **Retrieval is configuration, not code.** A modular node pipeline — vector + keyword recall, RRF fusion, cross-encoder rerank, parent expansion, and CRAG relevance checks — can be reconfigured, reordered, or toggled live via the admin console without service restarts. It includes chunking previews, golden-set evaluation (`Recall@k`, `Precision@k`, `MRR`), Redis query caching (keyed by query + config + corpus version, auto-invalidated on re-index), and vision-LLM transcription for PDF tables. A single node failing degrades to the surviving channels instead of breaking the chat, and user feedback is logged to a golden evaluation dataset.
- **Prompts and tools are cache-friendly by design.** A byte-stable prompt head (system identity + one-line tool index) paired with a dynamic per-step tail maximizes LLM prefix caching to slash latency and token costs, with a measurable cache identity. Tools use deferred loading: lightweight stubs are mounted first, and full parameter schemas are fetched only upon invocation.
- **Storage is a unified knowledge substrate, not an isolated silo.** Private drives and shared team workspaces (featuring `Owner` / `Admin` / `Editor` / `Viewer` RBAC, member management, and append-only activity logs) sit atop a SHA-256 content-addressed object store with reference-counted deduplication, 8 MB resumable chunked uploads, multi-level folders, 30-day trash retention, per-file ACLs, public share links, and integrated multi-format previews. The drive doubles as the shared data and working directory for content processing, retrieval, and agent workflows.
- **Authorization is enforced at resource boundaries.** Roles, tenant boundaries, upstream LLM provider credentials, model catalogs, and routing weights are managed from a unified admin console. It features a per-user key-grant matrix with masked credentials (`sk-***`), SMTP for email verification / password reset, and stateless signed admin sessions.
- **Local-first client with self-hostable infrastructure.** The Electron workbench supports offline file workflows (file tree, multi-format viewer, video frame capture); large media is processed on the client and the resulting artifacts submitted back to the server, while most content-processing workloads run server-side. The complete backend stack — PostgreSQL/pgvector, Redis, TEI embeddings, Kokoro TTS, and LiteLLM gateway — deploys seamlessly via `docker-compose`, keeping your data fully within your infrastructure.

Full design: [docs/architecture.md](docs/architecture.md).

## ✅ Implementation status

| Area | Status |
|---|---|
| Agent Runtime | ✅ Implemented |
| Dual-track Memory | ✅ Implemented |
| Configurable Retrieval | ✅ Implemented |
| Async Job System | ✅ Implemented |
| Cloud Drive & Workspaces | ✅ Implemented |
| Auth / RBAC / ACL | ✅ Implemented |
| Usage & Model Routing | ✅ Implemented |
| Self-hosted AI Services | ✅ Supported |

See [docs/architecture.md §Implementation Status](docs/architecture.md#implementation-status) for the full matrix and planned capabilities.

---

## 🏗️ Architecture at a glance

![Platform architecture — tenants & workspaces, access layer, core application (agent runtime · dual-track memory · configurable RAG · cloud workspace · processing), self-hosted data & AI services](./docs/images/deepdive-architecture-platform-diagram.png)

---

## 🧩 Architecture & Flows

Per-module architecture and flow logic (agent kernel · memory · prompt · RAG): [docs/architecture-diagrams.md](docs/architecture-diagrams.md).

---

## 🧰 Tech Stack

Which technologies DeepDive uses and why each was chosen — see [docs/architecture.md §2 Tech Stack](docs/architecture.md#2-tech-stack).

## 📂 Repository Structure

How the monorepo is laid out and what each module owns — see [docs/architecture.md §3](docs/architecture.md#3-repository-structure-monorepo).

## 📚 Documentation

- [docs/architecture.md](docs/architecture.md) — full system design (single source of truth): tech stack, repository layout, agent-kernel internals, tool runtime, data model, deployment, and the implemented-vs-designed matrix.
- [docs/architecture-diagrams.md](docs/architecture-diagrams.md) — per-module architecture diagrams and mermaid source.
- [docs/getting-started.md](docs/getting-started.md) — full manual setup, one-click launchers, and the desktop/web/admin walkthrough.
- [docs/configuration.md](docs/configuration.md) — environment-variable reference.
- [docs/features.md](docs/features.md) — full feature walk-through (desktop workbench, chat assistant, RAG & query repository, study mode, cloud drive, roles & billing).

---

## 🚀 Quick Start

### Option A — One-click launcher (recommended)

| Environment | Script |
|---|---|
| Windows desktop | `bash scripts/start_desktop.sh` |
| Linux server | `bash scripts/start_server.sh` |

Each script installs Docker if missing, starts the data + model services, ensures the Python environment, seeds the default `admin` / `admin` account, and launches the workbench (desktop) or web UI (server).

### Option B — Manual local development

```bash
git clone https://github.com/Eric-LLMs/DeepDive.git
cd DeepDive
conda create -n deepdive python=3.11 -y && conda activate deepdive
cp .env.example .env            # fill in LLM_UPSTREAM_KEY
pip install -e ".[dev]"         # + pip install -e ".[rag]" for semantic search
docker compose up -d postgres redis embedding tts llm-gateway worker
python scripts/init_db.py
uvicorn apps.api.main:app --reload     # http://localhost:8300/docs
```

### Option C — Self-hosted LLM

The LiteLLM gateway routes the virtual model `deepdive-chat` to any OpenAI-compatible upstream (`LLM_UPSTREAM_BASE`). Point it at a self-hosted server (vLLM / Ollama / …) to run the whole AI stack on your own hardware, or at an external provider — no code change.

Full manual steps, environment variables, and the desktop/web/admin walkthrough: [docs/getting-started.md](docs/getting-started.md) · [docs/configuration.md](docs/configuration.md).

---

## 📝 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
