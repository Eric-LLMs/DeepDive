# Configuration Reference

Environment variables and their defaults. Model inference never runs inside the API process — LLM channels are managed in the admin console (Providers → Credentials); each is an OpenAI-compatible `base_url` + `api_key`, and a role bound to a channel routes chat straight to that provider (no code change). Web-search (provider + key + google engine id) and SMTP (host, credentials, TLS, enabled) are likewise configured in the admin console (Tools config) rather than `.env`. Swapping embedding/TTS models = change `--model-id` in `docker-compose.yml` and the matching `*_BASE_URL` / dim in `.env` — no business-code change.

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
| `SKILLS_DIR` | `skills` | `SKILL.md` skills directory at the repo root (version-controlled; lazy-loaded via the `skill` tool) |
| `PLUGINS_DIR` | `plugins` | plugin directory (`*/plugin.py`, version-controlled; auto-discovered at startup) |
| `MEMORY_NOTE_MAX_CHARS` | `4000` | `memory_save` content length cap (guardrail) |
| `MEMORY_RECALL_TOP_K` | `5` | proactive recall hits injected into the prompt memory section |
| `HISTORY_MAX_MESSAGES` / `HISTORY_KEEP_MESSAGES` | `40` / `20` | chat history length that triggers compaction / most-recent messages kept after compaction |
| `SESSION_EVENTS_RETENTION_DAYS` / `RETENTION_CRON` | `30` / `17 4 * * *` | daily worker cron purges `session_events` (audit log) older than this many days / its 5-field cron schedule |
| `WORKER_CONCURRENCY` / `WORKER_JOB_TIMEOUT` | `10` / `3600` | arq worker max concurrent jobs / per-job timeout (seconds); large-file RAG ingest needs a generous budget (PDF parse + embed) |
| `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_API_KEY` / `WEB_SEARCH_ENGINE_ID` | `tavily` / `""` / `""` | web-search provider (`duckduckgo` \| `tavily` \| `bing` \| `google`) + API key + google engine id; normally managed in admin → **Tools config**, these flat keys mirror that namespace |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USERNAME` / `REDDIT_PASSWORD` | `""` ×4 | `search_social` plugin reddit OAuth (free *script* app creds from `reddit.com/prefs/apps`); all four set → official `oauth.reddit.com` API, otherwise anonymous `search.json` (often 403) |
| `X_BEARER_TOKEN` | `""` | `search_social` plugin `x` platform — X API v2 bearer token; without it the `x` adapter raises a clear error |
| `OBJECT_STORE_ROOT` | `data/objects` | cloud-drive blob store root (SHA-256 content-addressed, ref-counted) |
| `DRIVE_CHUNK_SIZE` | `8MB` | cloud-drive upload chunk size |
| `DRIVE_MAX_CHUNKS` | `1024` | max chunks per upload session (cap on single-file size) |
| `DRIVE_MAX_FILE_SIZE` | `0` (unlimited) | per-file size limit for the cloud drive (bytes; `0` = no limit) |
| `INGEST_CHUNK_CHARS` / `INGEST_CHUNK_OVERLAP` | `1200` / `150` | text chunking window / overlap used when a cloud-drive file is ingested for RAG |
| `TOOLKIT_MAX_INPUT_TOKENS` / `TOOLKIT_MAX_FILE_BYTES` | `12000` / `20MB` | toolkit generation (mind map / slides / summary): per-file map-reduce digest budget / per-file size cap — files over the cap are refused and greyed out in the picker |
| `EMBED_BATCH_SIZE` | `16` | embedding batch size for indexing cloud-drive files into pgvector |

See [docs/architecture.md](architecture.md) for the full topology.
