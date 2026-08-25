"""Application configuration.

Reads from environment variables / .env via pydantic-settings.
Field names map one-to-one to the environment variables in .env (case-insensitive).
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database / cache ──
    database_url: str = "postgresql+asyncpg://deepdive:deepdive@localhost:15432/deepdive"
    redis_url: str = "redis://localhost:16379/0"

    # ── Worker / jobs ──
    worker_concurrency: int = 10         # arq max concurrent jobs
    worker_job_timeout: int = 3600       # arq per-job timeout (seconds) — large-file ingest (PDF parse + embed) can exceed a few minutes
    # Audit-event retention: a daily cron purges session_events older than this many days.
    # Only the audit log is swept; messages (the recall corpus) and sessions (summaries) stay.
    session_events_retention_days: int = 30
    retention_cron: str = "17 4 * * *"   # arq cron schedule for the purge (daily 04:17 local)

    # ── LLM (via LiteLLM gateway; the gateway routes the virtual model name) ──
    llm_api_key: str = ""
    llm_base_url: str = "http://localhost:4000/v1"
    llm_model: str = "deepdive-chat"

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

    # ── STT ──
    stt_model: str = "whisper-1"

    # ── Media (desktop workbench: keyframes → PPT / PDF book) ──
    media_output_dir: Path = Path("data/media_output")

    # ── Web search (agent web_search tool) ──
    # provider is free text: duckduckgo (no key) | tavily | bing | google (see web_search.py).
    # These are mirrored from the generic tools namespace (cfg["tools"]["web_search"]) at
    # startup / config save, so they stay the source of truth for the flat read path.
    web_search_provider: str = "tavily"
    web_search_api_key: str = ""
    web_search_engine_id: str = ""        # google Custom Search engine id (cx)

    # Runtime mirror of the generic tools namespace (cfg["tools"]). Populated by apps.api at
    # startup and on /config save; tool code reads its params via get_tool_config().
    tool_configs: dict[str, dict] = {}

    # ── Agent ──
    workspace_dir: Path = Path(".")            # root for the agent's read_file/edit_file/bash
    memory_dir: Path = Path("data/memory")     # file memory directory (MEMORY.md index)
    skills_dir: Path = Path("data/skills")     # *.skill.md skills directory
    plugins_dir: Path = Path("data/plugins")   # third-party plugin directory (*/plugin.py)
    session_summary_enabled: bool = True       # generate an LLM summary on session close
    memory_recall_top_k: int = 5               # proactive recall count for the prompt memory section
    memory_note_max_chars: int = 4000          # memory_save content length cap (guardrail)
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

    # Project context (DEEPDIVE.md conventions injected into the prompt's PROJECT_CONTEXT zone).
    project_context_files: list[str] = ["DEEPDIVE.md"]
    project_context_max_chars: int = 8000      # per-file read cap for the project convention file

    # ── Auth ──
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080

    # Anonymous guests may chat without an account, capped per day (-1 = unlimited).
    guest_daily_limit: int = 10

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


def get_tool_config(tool_id: str) -> dict:
    """Runtime config dict for a tool: ``tools.<tool_id>.<param>``.

    Tool code reads its params by name, e.g. ``get_tool_config("amap").get("api_key")``.
    The namespace is mirrored into ``settings.tool_configs`` at startup and on config save,
    so this is a pure in-process read (no DB round trip at call time).
    """
    return (settings.tool_configs or {}).get(tool_id, {})
