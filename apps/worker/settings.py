"""arq WorkerSettings: build shared clients once at startup, expose them via ``ctx``.

The worker never loads models in-process; llm/tts/embedder are HTTP clients to the model
containers, images is the scraper, and session_factory/job_store talk to PostgreSQL.

**LLM channel doctrine (dispatch gateway):** the worker's ``ctx["llm"]`` is a *shell*
(``require_channel=True``) — it holds NO commercial credential in memory. Every job's
channel is resolved live from the DB by the gateway at job start (see
:func:`core.infrastructure.llm_routing.resolve_channel_for_owner` pinned in
``tasks._run``); a job whose owner has no active channel fails fast instead of silently
riding a stale or global key.
"""
import logging
from typing import ClassVar

logger = logging.getLogger(__name__)

from agent.security.approvals import configure_approval_broker
from arq import cron
from arq.connections import RedisSettings
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.redis_bus import set_bus
from core.infrastructure.tts import TTSClient
from core.infrastructure.vector import TEIEmbedder
from core.logger import configure_logging

from apps.api.tools.toolkit.session_source import cleanup_stale_sources
from apps.worker import tasks


async def startup(ctx) -> None:
    # Route worker loggers to logs/worker.log (rotating) before any job logs. arq runs jobs in
    # a fresh task per invocation, so set_log_context inside each job is concurrency-safe.
    configure_logging(settings, app="worker")
    # A worker killed mid-job (OOM / SIGKILL) can leave an orphaned session transcript in
    # .toolkit_session_src; sweep anything older than 24h so the temp dir never accumulates.
    removed = cleanup_stale_sources(settings.workspace_dir)
    if removed:
        logger.info("cleaned %d stale toolkit session transcripts", removed)
    # Distributed approval resolutions (research auto-run ASK tools resolve via the API) and
    # the research monitor's wake-up bus both publish on the shared Redis client arq hands us.
    configure_approval_broker(ctx["redis"])
    set_bus(ctx["redis"])
    # Shell client: no key, no endpoint baked in. ``tasks._run`` pins the gateway-resolved
    # owner channel into the request context at every job start; without it any LLM call
    # raises NoActiveChannelError (loud fail-fast) — the old one-off ``_active_llm_channel``
    # startup snapshot (role-blind, freeze-after-boot) is gone by design.
    ctx["llm"] = OpenAILLM(require_channel=True)
    # The shared agent-kernel LLM (api.agent_factory's module singleton — what context-free
    # sub-calls construct against) still gets the API-host default via the SAME bootstrap so
    # host and worker pricing/catalog defaults agree. NOTE: since the gateway work this is
    # NOT an LLM channel source anymore — product calls ride the per-job pinned channel;
    # ``_bootstrap_config`` only seeds settings/catalog (pricing) concerns here.
    try:
        from apps.api.routers.config import _bootstrap_config

        async with SessionLocal() as session:
            await _bootstrap_config(session)
    except Exception:
        # Best-effort catalog/pricing mirror; a failure here must never take the worker down
        # (per-owner channels resolve from the DB at job time via the gateway).
        logger.warning("worker config mirror failed; per-owner channel resolution still applies", exc_info=True)
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
        tasks.research_drive,
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
