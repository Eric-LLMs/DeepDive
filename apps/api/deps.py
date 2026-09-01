"""Dependency injection: FastAPI-facing getters + per-request resources.

The agent kernel and its shared capability singletons (llm/embedder/retriever/drive) live
in :mod:`api.agent_factory`; this module re-exports them for the routers and adds the
request-scoped getters (vocab service, task queue) that only the API process needs. The
background worker imports the kernel from ``api.agent_factory`` directly and never touches
this module, so it doesn't drag in FastAPI.
"""
from api.agent_factory import (
    _batch_embedder,
    _embedder,
    _retriever,
    get_agent_kernel,
    get_drive_service,
    llm,
)
from agent.engine.kernel import AgentKernel
from core.application.services import VocabularyService
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore, TaskQueue
from core.infrastructure.repositories import (
    SqlDomainRepository,
    SqlMatchRepository,
    SqlSentenceRepository,
    SqlTermRepository,
)
from core.infrastructure.tts import TTSClient
from fastapi import Depends, Request

# Per-process lightweight singletons
tts = TTSClient()


def get_agent() -> AgentKernel:
    return get_agent_kernel()


async def get_session():
    async with SessionLocal() as session:
        yield session


def get_vocab_service(session=Depends(get_session)) -> VocabularyService:
    return VocabularyService(
        SqlDomainRepository(session),
        SqlTermRepository(session),
        SqlSentenceRepository(session),
        SqlMatchRepository(session),
        llm,
        tts,
        ImageScraper(),
        _embedder(),
    )


def get_task_queue(request: Request) -> TaskQueue:
    """Return a :class:`TaskQueue` backed by the lifespan-created arq Redis pool."""
    return TaskQueue(request.app.state.redis, JobStore(SessionLocal))
