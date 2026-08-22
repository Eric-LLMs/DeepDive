"""Dependency injection: wire up singletons + per-request resources.

Singletons are lightweight clients (llm/tts/embedder) that talk to model services over
HTTP; no model is loaded into the API process itself. The agent is a :class:`AgentKernel`
composition: a :class:`Context` (capability DI), a cache-boundary :class:`SystemPrompt`
assembler, a deferred-tool :class:`ToolGateway`, dual-track memory, and a
:class:`ReactLoopAgent` step pipeline.
"""
from functools import lru_cache

from agent import (
    Context,
    FileMemoryStore,
    PluginManager,
    SkillRegistry,
    ToolRuntime,
    register_builtin_plugins,
)
from agent.fs_tools import register_fs_tools
from agent.kernel import AgentKernel, KernelConfig
from agent.memory.retrieval import RRFMemoryRetriever
from agent.memory.service import MemoryService
from agent.sandbox import Sandbox
from api.tools import register_builtin_tools
from core.application.drive_service import DriveService
from core.application.services import VocabularyService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore, TaskQueue
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.memory_retrieval import PgKeywordRecaller, PgVectorRecaller
from core.infrastructure.repositories import (
    SqlDomainRepository,
    SqlMatchRepository,
    SqlSentenceRepository,
    SqlTermRepository,
)
from core.infrastructure.retrieval_grpc import GrpcRetriever
from core.infrastructure.tts import TTSClient
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
from core.infrastructure.web_search import get_web_search_provider
from fastapi import Depends, Request
from rag import RAGPipeline, build_pipeline

# Lightweight singletons
llm = OpenAILLM()
tts = TTSClient()


@lru_cache
def _embedder() -> TEIEmbedder:
    return TEIEmbedder()


@lru_cache
def _retriever() -> RAGPipeline:
    return build_pipeline(
        embedder=TEIEmbedder(),
        vector_store=PgVectorStore(SessionLocal),
        session_factory=SessionLocal,
        llm=llm,
        settings=settings,
    )


@lru_cache
def _agent() -> AgentKernel:
    runtime = ToolRuntime()
    ctx = Context()

    # Retrieval is a capability seam: the tool calls require("retrieval"), so the provider
    # (in-process RAGPipeline or a gRPC client) is swappable via settings.retrieval_mode.
    if settings.retrieval_mode == "grpc":
        ctx.provide("retrieval", GrpcRetriever(settings.retrieval_grpc_addr))
    else:
        ctx.provide("retrieval", _retriever())

    ctx.provide("web_search", get_web_search_provider())

    # Domain tools first (the kernel registers the core meta-tools on top).
    register_builtin_tools(runtime, ctx, llm)
    register_fs_tools(runtime, settings.workspace_dir)

    skills = SkillRegistry.from_dir(settings.skills_dir)

    # Dual-track memory: session recall = RRF over pgvector + tsvector (tsvector-only when
    # the embedding service is offline — never a silent empty); the file track stays local.
    file_memory = FileMemoryStore(settings.memory_dir)
    retriever = RRFMemoryRetriever(
        keyword=PgKeywordRecaller(SessionLocal),
        vector=PgVectorRecaller(SessionLocal, _embedder()),
    )
    memory = MemoryService(
        file_store=file_memory,
        retriever=retriever,
        memory_md_path=settings.memory_dir / "MEMORY.md",
        note_max_chars=settings.memory_note_max_chars,
    )

    soul = _read_soul()
    sandbox = Sandbox()  # default session = READ-only; WRITE/NETWORK tools are gated

    kernel = AgentKernel(
        llm,
        runtime,
        soul=soul,
        memory=memory,
        skills=skills,
        sandbox=sandbox,
        config=KernelConfig(recall_top_k=settings.memory_recall_top_k),
    )

    manager = PluginManager(runtime, skills, ctx)
    register_builtin_plugins(manager)
    manager.discover(settings.plugins_dir)
    return kernel


def _read_soul() -> str:
    """Load the identity persona (``data/soul.md``), falling back to a one-line persona."""
    soul_path = settings.memory_dir.parent / "soul.md"
    try:
        return soul_path.read_text(encoding="utf-8")
    except OSError:
        return "You are DeepDive, a focused learning-workbench assistant."


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


def get_agent() -> AgentKernel:
    return _agent()


@lru_cache
def _drive_service() -> DriveService:
    return DriveService(SessionLocal)


def get_drive_service() -> DriveService:
    return _drive_service()


def get_task_queue(request: Request) -> TaskQueue:
    """Return a :class:`TaskQueue` backed by the lifespan-created arq Redis pool."""
    return TaskQueue(request.app.state.redis, JobStore(SessionLocal))
