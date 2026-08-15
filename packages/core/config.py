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
    database_url: str = "postgresql+asyncpg://deepdive:deepdive@localhost:5432/deepdive"
    redis_url: str = "redis://localhost:16379/0"

    # ── Worker / jobs ──
    worker_concurrency: int = 10         # arq max concurrent jobs
    worker_job_timeout: int = 300        # arq per-job timeout (seconds)

    # ── LLM (via LiteLLM gateway; the gateway routes the virtual model name) ──
    llm_api_key: str = ""
    llm_base_url: str = "http://localhost:4000/v1"
    llm_model: str = "deepdive-chat"

    # ── TTS (Kokoro-FastAPI service, OpenAI-compatible /v1/audio/speech) ──
    tts_base_url: str = "http://localhost:8880/v1"
    tts_api_key: str = "not-needed"   # Kokoro-FastAPI ignores auth; the openai SDK needs a non-empty key
    tts_model: str = "kokoro"
    tts_voice: str = "am_michael"

    # ── Embedding (TEI service) ──
    embedding_base_url: str = "http://localhost:8080"   # TEI /embed
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

    # ── Agent ──
    memory_dir: Path = Path("data/memory")     # file memory directory (MEMORY.md index)
    skills_dir: Path = Path("data/skills")     # *.skill.md skills directory
    plugins_dir: Path = Path("data/plugins")   # third-party plugin directory (*/plugin.py)
    session_summary_enabled: bool = True       # generate an LLM summary on session close
    memory_recall_top_k: int = 5               # vector recall count for the prompt memory section

    # ── Auth ──
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080

    # ── Cache paths ──
    audio_cache_path: Path = Path("data/audio_cache")
    image_cache_path: Path = Path("data/image_cache")


settings = Settings()
