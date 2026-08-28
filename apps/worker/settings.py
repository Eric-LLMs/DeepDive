"""arq WorkerSettings: build shared clients once at startup, expose them via ``ctx``.

The worker never loads models in-process; llm/tts/embedder are HTTP clients to the model
containers, images is the scraper, and session_factory/job_store talk to PostgreSQL.
"""
import logging
from typing import ClassVar

logger = logging.getLogger(__name__)

from arq import cron
from arq.connections import RedisSettings
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.tts import TTSClient
from core.infrastructure.vector import TEIEmbedder

from apps.api.tools.toolkit.session_source import cleanup_stale_sources
from apps.worker import tasks


async def _active_llm_channel() -> tuple[str | None, str | None, str | None]:
    """Resolve the admin-configured LLM channel from the DB as ``(base_url, api_key, model)``.

    Mirrors the API chat path (``_channel_route``): pick an active credential, then the model
    it routes to (preferred active ``credential_models`` route, else the first active catalog
    model). The model is the provider's real id (``provider_model_name``). When the catalog has
    no active channel the worker falls back to the legacy settings (the litellm gateway), so a
    fresh deploy still boots.
    """
    from core.infrastructure.db import (
        CredentialModelModel,
        LLMCredentialModel,
        LLMModelModel,
    )
    from sqlalchemy import select

    async with SessionLocal() as session:
        credential = (
            await session.execute(
                select(LLMCredentialModel)
                .where(LLMCredentialModel.is_active.is_(True))
                .order_by(LLMCredentialModel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        model = None
        if credential is not None:
            model_id = (
                await session.execute(
                    select(CredentialModelModel.model_id)
                    .where(
                        CredentialModelModel.credential_id == credential.id,
                        CredentialModelModel.is_active.is_(True),
                    )
                    .order_by(CredentialModelModel.priority)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if model_id is not None:
                model = await session.get(LLMModelModel, model_id)
        if model is None:
            model = (
                await session.execute(
                    select(LLMModelModel)
                    .where(LLMModelModel.is_active.is_(True))
                    .order_by(LLMModelModel.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if credential is None:
            return None, None, None
        model_name = (model.provider_model_name or model.name) if model is not None else None
        return credential.base_url, credential.api_key, model_name


async def startup(ctx) -> None:
    # A worker killed mid-job (OOM / SIGKILL) can leave an orphaned session transcript in
    # .toolkit_session_src; sweep anything older than 24h so the temp dir never accumulates.
    removed = cleanup_stale_sources(settings.workspace_dir)
    if removed:
        logger.info("cleaned %d stale toolkit session transcripts", removed)
    base_url, api_key, model = await _active_llm_channel()
    ctx["llm"] = OpenAILLM(api_key=api_key, base_url=base_url, model=model)
    ctx["tts"] = TTSClient()
    ctx["images"] = ImageScraper()
    # Batch embed (session finalize / sentence indexing / RAG ingest) can exceed the
    # chat-path fast-fail budget (5s) when inputs are long; a leaf batch of 16 chunks runs
    # ~20-25s on TEI, so keep a generous headroom.
    ctx["embedder"] = TEIEmbedder(timeout=120.0)
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
    functions: ClassVar[list] = [
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
        tasks.toolkit_generate,
        tasks.run_agent_turn,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_concurrency
    job_timeout = settings.worker_job_timeout
    # Match arq's retry budget to PG: FAILED is only written on the final attempt.
    max_tries = settings.worker_max_tries
    # Daily audit-event retention (purges only session_events; runs once at startup too).
    cron_jobs: ClassVar[list] = [
        cron(tasks.prune_session_events, run_at_startup=True, **_cron_parts(settings.retention_cron)),
    ]
