"""arq WorkerSettings: build shared clients once at startup, expose them via ``ctx``.

The worker never loads models in-process; llm/tts/embedder are HTTP clients to the model
containers, images is the scraper, and session_factory/job_store talk to PostgreSQL.
"""
from arq.connections import RedisSettings

from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.tts import TTSClient
from core.infrastructure.vector import TEIEmbedder

from apps.worker import tasks


async def startup(ctx) -> None:
    ctx["llm"] = OpenAILLM()
    ctx["tts"] = TTSClient()
    ctx["images"] = ImageScraper()
    ctx["embedder"] = TEIEmbedder()
    ctx["session_factory"] = SessionLocal
    ctx["job_store"] = JobStore(SessionLocal)


async def shutdown(ctx) -> None:
    # HTTP clients (httpx.AsyncClient) are lazily constructed by llm/tts/embedder and have no
    # explicit close hook; nothing to tear down here.
    return None


class WorkerSettings:
    functions = [
        tasks.tts,
        tasks.image_fetch,
        tasks.explain,
        tasks.generate_definition,
        tasks.analyze_syntax,
        tasks.index_sentences,
        tasks.session_finalize,
        tasks.generate_media,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_concurrency
    job_timeout = settings.worker_job_timeout
