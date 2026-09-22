"""Application configuration.

Reads from environment variables / .env via pydantic-settings.
Field names map one-to-one to the environment variables in .env (case-insensitive).
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database / cache ──
    database_url: str = "postgresql+asyncpg://delveta:delveta@localhost:15432/delveta"
    redis_url: str = "redis://localhost:16379/0"

    # ── Worker / jobs ──
    worker_concurrency: int = 10         # arq max concurrent jobs
    worker_job_timeout: int = 3600       # arq per-job timeout (seconds) — large-file ingest (PDF parse + embed) can exceed a few minutes
    worker_max_tries: int = 1            # max arq retries per job; 1 = no retry (PG stays the honest terminal source)
    # Audit-event retention: a daily cron purges session_events older than this many days.
    # Only the audit log is swept; messages (the recall corpus) and sessions (summaries) stay.
    session_events_retention_days: int = 30
    retention_cron: str = "17 4 * * *"   # arq cron schedule for the purge (daily 04:17 local)

    # ── LLM (via LiteLLM gateway; the gateway routes the virtual model name) ──
    llm_api_key: str = ""
    llm_base_url: str = "http://localhost:14000/v1"
    llm_model: str = "delveta-chat"

    # ── TTS (Kokoro-FastAPI service, OpenAI-compatible /v1/audio/speech) ──
    tts_base_url: str = "http://localhost:18880/v1"
    tts_api_key: str = "not-needed"   # Kokoro-FastAPI ignores auth; the openai SDK needs a non-empty key
    tts_model: str = "kokoro"
    tts_voice: str = "am_michael"
    # Chinese voice (Kokoro zh pack, e.g. zm_yunxi / zf_xiaoni). Selected automatically
    # when the input text contains CJK characters; the English voice is used otherwise.
    tts_voice_zh: str = "zm_yunxi"

    # ── Embedding (TEI service) ──
    embedding_base_url: str = "http://localhost:18080"   # TEI /embed
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # ── RAG ──
    rag_query_rewrite: bool = True      # whether to rewrite/expand the query with the LLM before recall
    rag_multi_query_n: int = 2          # number of additional query variants to generate
    rag_hyde: bool = False              # whether to enable HyDE (hypothetical document)
    reranker_model: str = ""            # cross-encoder rerank model name; empty string disables reranking

    # ── Retrieval capability seam ──
    retrieval_mode: str = "in_process"  # "in_process" (RAGPipeline) | "grpc" (retrieval service)
    retrieval_grpc_addr: str = "localhost:15051"

    # ── STT (FunASR SenseVoice sidecar, OpenAI-compatible /v1/audio/transcriptions) ──
    # Runs as a docker-published sidecar reached over the host loopback, mirroring the TTS
    # mechanism above (the API gateway itself runs on the host, not inside the compose network).
    stt_base_url: str = "http://localhost:18881/v1"
    stt_api_key: str = "not-needed"   # the FunASR server ignores auth; the openai SDK needs a non-empty key
    stt_model: str = "sensevoice"     # FunASR alias → iic/SenseVoiceSmall (≈234M, zh/en friendly)
    stt_max_bytes: int = 10 * 1024 * 1024  # upload guard for POST /stt (a few minutes of speech)
    stt_timeout_seconds: float = 30.0      # client-side wall time per transcription request

    # ── Media (desktop workbench: keyframes → PPT / PDF book) ──
    media_output_dir: Path = Path("data/media_output")

    # ── Toolkit content generation (workspace files → slides / mindmap / summary) ──
    # Output root (relative to the workspace) + input guardrails. Since 2026-09-15 the
    # three tools generate from the FULL raw text: ``toolkit_max_input_tokens`` is a pure
    # one-shot capacity CHECK — at or below it the complete text goes to the generator in
    # ONE call; above it the pipeline enters the EXPLICIT big-document multi-call flow
    # (raw-grounded call per line-tracked batch + deterministic merge; the old map-reduce
    # digest pre-pass that fed a summary as sole input is removed for good).
    # 100K ≈ current channel's 128K window minus generation headroom;
    # chat-history compaction (``apply_compaction``) is a separate mechanism and unaffected.
    # ``toolkit_llm_timeout_s`` bounds each toolkit generation call (a full-context
    # Pass A / one-shot summary needs far more than the global 90s wall time).
    toolkit_output_dir: Path = Path(".output")
    toolkit_max_input_tokens: int = 100000
    toolkit_max_file_bytes: int = 20 * 1024 * 1024
    toolkit_llm_timeout_s: float = 300.0
    # Slides engine switch (2026-09-17): "direct" = one semantic LLM call + the local
    # deterministic compiler (apps.api.tools.toolkit.deck.generator); "legacy" = the
    # Brief chain (TEXT→VISUAL→REDUCE→SYNTHESIZE) kept whole as the escape hatch.
    # Overridable per request via the ``generation_mode`` job parameter.
    slides_generation_mode: str = "direct"

    # Batch generation (``complete``/``complete_json``) streams the response and
    # accumulates it, so the provider's ~300s non-stream gateway cutoff can never kill a
    # legitimate full-context generation; ``toolkit_llm_timeout_s`` becomes an
    # idle-between-chunks deadline. ``llm_disable_thinking`` sends the Qwen-compatible
    # ``enable_thinking: false`` flag on those calls — reasoning tokens are pure latency
    # for schema-validated JSON output. Interactive chat (``chat_stream``) keeps thinking.
    llm_disable_thinking: bool = True

    # Deck (content-to-slides) Pass C throughput knobs. Effective fan-out per deck =
    # min(configured, provider per-key cap, worker in-job cap, slide_count).
    deck_pass_c_concurrency: int = 8      # configured ceiling for parallel slide calls
    deck_provider_concurrency: int = 6    # conservative DeepSeek per-key concurrent-call cap
    deck_worker_concurrency: int = 8      # LLM calls one worker job may keep in flight
    deck_slide_timeout_s: float = 180.0   # per-attempt deadline; a timed-out page retries alone

    # ── Chat control plane (Phase 2: DIRECT fast path) ──────────────────────────
    # All OFF by default = dark launch: every turn keeps the legacy AgentExecutor
    # path byte-for-byte. Flip the master gate only after the understanding signals
    # and DIRECT TTFT are validated on a golden set; per-kind gates stay closed until
    # their phase lands. ``chat_direct_*`` are the Phase 2 knobs.
    chat_fast_paths_enabled: bool = False       # master switch for all fast paths
    chat_direct_fast_path_enabled: bool = False  # Phase 2: tool-less direct answers
    chat_viewer_fast_path_enabled: bool = False  # Phase 3: grounded over injected blocks
    # Confidence gate: the L0 signal engine only routes DIRECT when every capability
    # demand is LOW, needs_memory is False, and the message is short/plain. Longer
    # turns (or ambiguous intent) stay on the Agent path.
    chat_direct_max_chars: int = 400            # a pure user message must be <= this

    # ── Web search (agent web_search tool) ──
    # provider is free text: aggregate/keyless (no key) | duckduckgo (no key) | tavily |
    # bing | google (see web_search.py). Defaults to the keyless multi-engine aggregate so
    # the agent's web_search works out of the box with no API key; a keyed provider is
    # used only when explicitly selected.
    # These are mirrored from the generic tools namespace (cfg["tools"]["web_search"]) at
    # startup / config save, so they stay the source of truth for the flat read path.
    web_search_provider: str = "aggregate"
    web_search_api_key: str = ""
    web_search_engine_id: str = ""        # google Custom Search engine id (cx)

    # ── Reddit social search (search_social plugin) ──
    # Official OAuth creds from a free *script* app (https://www.reddit.com/prefs/apps).
    # The plugin reads these from os.environ at call time; export_secret_env() bridges the
    # .env-loaded values at agent build time. All four must be set to go live.
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_username: str = ""
    reddit_password: str = ""

    # Runtime mirror of the generic tools namespace (cfg["tools"]). Populated by apps.api at
    # startup and on /config save; tool code reads its params via get_tool_config().
    tool_configs: dict[str, dict] = {}

    # ── Agent ──
    workspace_dir: Path = Path(".")            # root for the agent's read_file/edit_file/bash
    memory_dir: Path = Path("data/memory")     # file memory directory (MEMORY.md index)
    skills_dir: Path = Path("skills")          # *.skill.md skills directory (version-controlled)
    plugins_dir: Path = Path("plugins")        # plugin directory (*/plugin.py) (version-controlled)
    research_scratch_dir: Path = Path("data/research_scratch")  # research scratch root (spike)

    # ── Research auto-run driver (T0) ──
    # The driver chains background pipeline node executions ("one-click run to PUBLISH")
    # behind a single-flight run_id. These knobs bound how far a chain may go before it
    # must stop and hand control back to a human — never a silent infinite loop.
    research_driver_max_turns: int = 14        # max chained worker turns per run_id (10-stage
    #   chain needs >=10 turns even with ZERO retries — Run 15 died at 8 with a clean path;
    #   14 = clean walk + ~4 turns of per-stage retry slack. The real runaway brake stays the
    #   no-progress fuse + the PRE-CALL cost gate below, not this absolute cap.)
    research_driver_max_no_progress_turns: int = 2  # consecutive no-progress turns → STALLED
    research_driver_max_attempts: int = 3      # transient retries per turn_index (turn_attempt cap)
    research_driver_max_cost_usd: float | None = 0.40  # cumulative auto-run cost cap. NOT a
    #   passive billing stat: enforced as a hard PRE-CALL gate — the driver refuses to start a
    #   turn once cumulative >= cap (CostLimitExceeded), and every model call inside a node
    #   transits the stage llm_gate, which checks remaining budget before transport dispatch.

    # ── Runtime logging (core.logger) ──
    log_level: str = "INFO"                    # root logger level (DEBUG/INFO/WARNING/ERROR/CRITICAL)
    log_dir: Path = Path("logs")               # rotating log directory (api.log / worker.log)
    log_file_max_bytes: int = 10 * 1024 * 1024  # single log file cap before rotation
    log_file_backups: int = 5                  # rotated files kept
    session_summary_enabled: bool = True       # generate an LLM summary on session close
    memory_recall_top_k: int = 5               # proactive recall count for the prompt memory section
    memory_note_max_chars: int = 4000          # memory_save content length cap (guardrail)

    # LLM call reliability: hard timeout + retry budget for every agent LLM call.
    llm_timeout_seconds: float = 90            # max wall time for one LLM call (first-token for streams)
    llm_max_retries: int = 2                   # retries on temporary errors (timeout / 429 / 5xx)
    llm_retry_backoff: float = 1.0             # base backoff seconds (doubles per retry)

    # Bash sandbox: "docker" runs each command in a fresh container (docker-py, hard dep);
    # "host" uses the local-process fallback (explicit opt-in, dev only — NOT a security boundary).
    bash_sandbox: str = "docker"                # "docker" | "host"
    bash_sandbox_image: str = "debian:bookworm-slim"
    bash_sandbox_network: bool = False          # container network access
    bash_sandbox_mem_limit: str = "512m"        # per-container memory cap
    bash_sandbox_cpus: float = 0.5              # per-container CPU budget (of one core)
    bash_sandbox_timeout: int = 30              # default per-command timeout (seconds)

    # Human-in-the-loop: how long an approval request waits before it is denied.
    approval_timeout_seconds: float = 120
    # Subagents: how deep child turns may nest before the loop refuses to spawn more.
    max_subagent_depth: int = 3
    # Per-turn cost cap (USD) — the loop aborts once the accumulated cost passes this.
    max_budget_per_turn_usd: float = 1.0
    # Workspace checkpoints: shadow-git snapshot dir (relative to the workspace root).
    checkpoint_dir: Path = Path(".delveta-snapshots")
    # Agent audit trail: one JSONL line per turn event (best-effort; dir created on demand).
    audit_log_path: Path = Path("data/audit.jsonl")

    # ── gRPC retrieval service auth ──
    retrieval_grpc_token: str = ""             # shared secret; empty disables auth (dev only)
    retrieval_grpc_tls_cert: str = ""          # server TLS cert path; empty = insecure port
    retrieval_grpc_tls_key: str = ""           # server TLS private-key path (with the cert)
    retrieval_grpc_tls_ca: str = ""            # client-side CA bundle; empty = insecure channel
    retrieval_grpc_rate_limit: int = 0         # max Retrieve req/s per client (0 = unlimited)

    # ── RAG operations ──
    query_cache_ttl_seconds: int = 300         # Redis query-cache TTL (0 disables the cache)
    memory_recall_min_len: int = 4             # queries at/below this length always recall (elliptical)
    memory_recall_trigger_words: list[str] = [  # lexical prefilter: these imply memory-seeking intent
        "remember", "recall", "earlier", "before", "previously", "prior",
        "last time", "we discussed", "we talked", "you told me",
        "你记得", "记得", "上次", "之前", "以前", "说过", "你说过", "我们说过",
    ]
    history_max_messages: int = 40             # chat history length that triggers compaction
    history_keep_messages: int = 20            # most-recent messages kept after compaction
    prompt_max_chars: int = 120_000            # total window char budget that triggers compaction (~30k tokens)
    prompt_message_max_chars: int = 8000       # per-message content cap when building the LLM request (snip)
    # Char cap on the 5-section structured summary produced by one fold. The fold input is
    # rebuilt from RAW messages every time (no summary-of-summary) — its token cost grows
    # linearly with the fold range; this is the deliberate trade-off for zero generational
    # memory decay, paid only at low-frequency compaction events (never in normal turns).
    compaction_summary_max_chars: int = 2500

    # Project context (DELVETA.md conventions injected into the prompt's PROJECT_CONTEXT zone).
    project_context_files: list[str] = ["DELVETA.md"]
    project_context_max_chars: int = 8000      # per-file read cap for the project convention file

    # ── Auth ──
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080
    # When True, startup fails fast if jwt_secret is left at its insecure default ("change-me").
    # Off by default so zero-config local startup (scripts/start_desktop.sh) keeps working.
    enforce_secure_secrets: bool = False
    # Fixed-window auth rate limits (per client IP, redis INCR+EXPIRE). 0 disables the limit.
    auth_login_rpm: int = 30            # /auth/login + /auth/session-login
    auth_register_rpm: int = 10         # /auth/register
    auth_recovery_rpm: int = 5          # forgot-password / reset-password / resend-verification
    auth_rate_limit_window: int = 60    # counter window in seconds

    # Anonymous guests may chat without an account, capped per day (-1 = unlimited).
    guest_daily_limit: int = 10
    # Signed guest identity token lifetime (seconds): after this the client's gt_ token
    # expires and the server mints a fresh identity on the next anonymous request.
    guest_token_ttl_seconds: int = 30 * 86400
    # Minimum wallet balance (USD) required for an overflow (beyond free quota) request.
    # 0 means "positive balance" — a drained wallet blocks the next overflow request with 402.
    wallet_gate_min_balance_usd: float = 0.0

    # ── Cache paths ──
    audio_cache_path: Path = Path("data/audio_cache")
    image_cache_path: Path = Path("data/image_cache")

    # ── Cloud drive (per-user file store + shared RAG corpus) ──
    object_store_root: Path = Path("data/objects")  # sharded physical object store
    drive_chunk_size: int = 8 * 1024 * 1024         # default upload chunk size (bytes)
    drive_max_chunks: int = 1024                    # max chunks per upload (8MB → 8GB)
    drive_max_file_size: int = 0                    # max upload bytes, 0 = unlimited
    ingest_chunk_chars: int = 1200                  # RAG chunk target length (chars)
    ingest_chunk_overlap: int = 150                 # RAG chunk overlap (chars)
    embed_batch_size: int = 16                      # embeddings per batch during ingest

    # ── Runtime config (legacy JSON file; imported into DB once on startup) ──
    config_path: Path = Path("data/config.json")


settings = Settings()

# Secret Settings fields that standalone plugins (discovered from disk, no access to
# Settings) read from os.environ at call time. export_secret_env() bridges the .env-loaded
# values into os.environ at agent build time, so a direct environment variable still wins.
_SECRET_ENV_FIELDS = {
    "reddit_client_id": "REDDIT_CLIENT_ID",
    "reddit_client_secret": "REDDIT_CLIENT_SECRET",
    "reddit_username": "REDDIT_USERNAME",
    "reddit_password": "REDDIT_PASSWORD",
}


def export_secret_env() -> None:
    """Mirror .env-loaded secret Settings into os.environ (existing env vars win)."""
    for field, env in _SECRET_ENV_FIELDS.items():
        value = getattr(settings, field)
        if value:
            os.environ.setdefault(env, value)


def get_tool_config(tool_id: str) -> dict:
    """Runtime config dict for a tool: ``tools.<tool_id>.<param>``.

    Tool code reads its params by name, e.g. ``get_tool_config("amap").get("api_key")``.
    The namespace is mirrored into ``settings.tool_configs`` at startup and on config save,
    so this is a pure in-process read (no DB round trip at call time).
    """
    return (settings.tool_configs or {}).get(tool_id, {})
