"""arq WorkerSettings: build shared clients once at startup, expose them via ``ctx``.

The worker never loads models in-process; llm/tts/embedder are HTTP clients to the model
containers, images is the scraper, and session_factory/job_store talk to PostgreSQL.
"""
from arq import cron
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
    # Batch embed (session finalize / sentence indexing) can exceed the chat-path fast-fail
    # budget (5s) when messages are long; give the worker more headroom.
    ctx["embedder"] = TEIEmbedder(timeout=60.0)
    ctx["session_factory"] = SessionLocal
    ctx["job_store"] = JobStore(SessionLocal)


async def shutdown(ctx) -> None:
    # HTTP clients (httpx.AsyncClient) are lazily constructed by llm/tts/embedder and have no
    # explicit close hook; nothing to tear down here.
    return None


def _cron_field(value: str) -> int | set[int] | None:
    """Parse one standard 5-field cron component into an arq option (``*`` → ``None``).

    Supports a bare value, a comma list, and a ``a-b`` range; the cron string is
    ``minute hour day month weekday`` and only a subset (``*``/``n``) is needed in practice.
    """
    if value.strip() in ("*", "?"):
        return None
    vals: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            vals.update(range(int(start), int(end) + 1))
        else:
            vals.add(int(part))
    return vals


def _cron_parts(schedule: str) -> dict:
    minute, hour, day, month, weekday = (p.strip() for p in schedule.split())
    return {
        "minute": _cron_field(minute),
        "hour": _cron_field(hour),
        "day": _cron_field(day),
        "month": _cron_field(month),
        "weekday": _cron_field(weekday),
    }


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
        tasks.asset_ingest,
        tasks.learning_import,
        tasks.chat_session_import,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_concurrency
    job_timeout = settings.worker_job_timeout
    # Daily audit-event retention (purges only session_events; runs once at startup too).
    cron_jobs = [
        cron(tasks.prune_session_events, run_at_startup=True, **_cron_parts(settings.retention_cron)),
    ]
