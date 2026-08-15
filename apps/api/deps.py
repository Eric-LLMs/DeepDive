"""Dependency injection: wire up singletons + per-request resources.

Singletons are lightweight clients (llm/tts/embedder) that talk to model services over
HTTP; no model is loaded into the API process itself. The agent is assembled from a
:class:`Context` (capability DI), a :class:`SystemPrompt` (layered prompt), and a
:class:`ReactLoopAgent` (step pipeline).
"""
from functools import lru_cache

from fastapi import Depends, Request

from agent import (
    MEMORY_ORDER,
    PERSONA_ORDER,
    SKILLS_ORDER,
    Context,
    FileMemoryStore,
    PluginManager,
    ReactLoopAgent,
    SkillRegistry,
    SystemPrompt,
    ToolRuntime,
    register_builtin_plugins,
)
from api.tools import register_builtin_tools
from core.application.services import VocabularyService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.jobs import JobStore, TaskQueue
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.retrieval_grpc import GrpcRetriever
from core.infrastructure.repositories import (
    SqlDomainRepository,
    SqlMatchRepository,
    SqlSentenceRepository,
    SqlTermRepository,
)
from core.infrastructure.tts import TTSClient
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
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
def _agent() -> ReactLoopAgent:
    runtime = ToolRuntime()
    ctx = Context()

    # Retrieval is a capability seam: the tool calls require("retrieval"), so the provider
    # (in-process RAGPipeline or a gRPC client) is swappable via settings.retrieval_mode.
    if settings.retrieval_mode == "grpc":
        ctx.provide("retrieval", GrpcRetriever(settings.retrieval_grpc_addr))
    else:
        ctx.provide("retrieval", _retriever())

    register_builtin_tools(runtime, ctx, llm)

    skills = SkillRegistry.from_dir(settings.skills_dir)
    file_memory = FileMemoryStore(settings.memory_dir)

    system_prompt = SystemPrompt()
    system_prompt.section("persona", PERSONA_ORDER, "You are a helpful learning assistant.")

    async def memory_section(context: dict) -> str:
        user_msg = context.get("user_msg", "")
        parts: list[str] = []
        for mem in await file_memory.search(user_msg, limit=3):
            parts.append(f"## 记忆:{mem.name}\n{mem.content}")
            if mem.freshness_note:
                parts.append(mem.freshness_note)
        session_memory = context.get("session_memory")
        if session_memory is not None:
            try:
                query_embedding = (await _embedder().embed([user_msg]))[0]
                recalled = await session_memory.search(
                    query_embedding, top_k=settings.memory_recall_top_k
                )
            except Exception:  # noqa: BLE001 - memory recall must not break the prompt
                recalled = []
            for m in recalled:
                parts.append(f"[{m['role']}] {m['text']}")
        return "\n\n".join(parts)

    async def skills_section(context: dict) -> str:
        relevant = skills.relevant(context.get("user_msg", ""))
        if not relevant:
            return ""
        return "\n\n".join(f"### skill:{s.name}\n{s.instructions}" for s in relevant)

    system_prompt.section("memory", MEMORY_ORDER, memory_section)
    system_prompt.section("skills", SKILLS_ORDER, skills_section)

    manager = PluginManager(runtime, skills, ctx)
    register_builtin_plugins(manager)
    manager.discover(settings.plugins_dir)
    return ReactLoopAgent(llm, runtime, system_prompt)


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


def get_agent() -> ReactLoopAgent:
    return _agent()


def get_task_queue(request: Request) -> TaskQueue:
    """Return a :class:`TaskQueue` backed by the lifespan-created arq Redis pool."""
    return TaskQueue(request.app.state.redis, JobStore(SessionLocal))
