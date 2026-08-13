"""Application configuration.

Reads from environment variables / .env via pydantic-settings, replacing the old project's config.py + config.yaml.
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
    database_url: str = "postgresql+asyncpg://deepgloss:deepgloss@localhost:5432/deepgloss"
    redis_url: str = "redis://localhost:16379/0"

    # ── LLM ──
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # ── TTS (falls back to LLM config when unset) ──
    tts_api_key: str = ""
    tts_base_url: str = ""
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    # edge-tts fallback voice (used when the OpenAI-compatible TTS fails)
    edge_tts_voice: str = "en-US-AvaNeural"

    # ── Embedding ──
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # ── RAG ──
    rag_query_rewrite: bool = True      # whether to rewrite/expand the query with the LLM before recall
    rag_multi_query_n: int = 2          # number of additional query variants to generate
    rag_hyde: bool = False              # whether to enable HyDE (hypothetical document)
    reranker_model: str = ""            # cross-encoder rerank model name; empty string disables reranking

    # ── STT ──
    stt_model: str = "whisper-1"

    # ── Agent ──
    memory_dir: Path = Path("data/memory")     # file memory directory (MEMORY.md index)
    skills_dir: Path = Path("data/skills")     # *.skill.md skills directory
    plugins_dir: Path = Path("data/plugins")   # third-party plugin directory (*/plugin.py)

    # ── Auth ──
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080

    # ── Cache paths ──
    audio_cache_path: Path = Path("data/audio_cache")
    image_cache_path: Path = Path("data/image_cache")

    @property
    def tts_key(self) -> str:
        """Independent TTS key; falls back to the LLM key by default."""
        return self.tts_api_key or self.llm_api_key

    @property
    def tts_url(self) -> str:
        """Independent TTS base_url; falls back to the LLM base_url by default."""
        return self.tts_base_url or self.llm_base_url


settings = Settings()
