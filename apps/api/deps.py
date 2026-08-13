"""Dependency injection: wire up singletons + per-request resources.

Lightweight singletons (llm/tts) are created directly; the heavyweight embedder (downloads the BGE-M3 model) is lazy-loaded.
"""
from functools import lru_cache

from fastapi import Depends

from core.agent import (
    Agent,
    ContextBuilder,
    FileMemoryStore,
    PluginManager,
    SkillRegistry,
    build_default_tools,
    register_builtin_plugins,
)
from core.application.services import VocabularyService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.repositories import (
    SqlDomainRepository,
    SqlMatchRepository,
    SqlSentenceRepository,
    SqlTermRepository,
)
from core.infrastructure.tts import OpenAITTS
from core.infrastructure.vector import PgVectorStore, SentenceTransformerEmbedder
from core.rag import (
    CrossEncoderReranker,
    KeywordRecaller,
    QueryRewriter,
    RAGPipeline,
    VectorRecaller,
)

# Lightweight singletons
llm = OpenAILLM()
tts = OpenAITTS()


@lru_cache
def _embedder() -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder()  # model loads lazily on first embed()


@lru_cache
def _retriever() -> RAGPipeline:
    embedder = SentenceTransformerEmbedder()  # downloads/loads BGE-M3 on first call
    vector_recaller = VectorRecaller(PgVectorStore(SessionLocal))
    keyword_recaller = KeywordRecaller(SessionLocal)
    rewriter = (
        QueryRewriter(llm, n_variants=settings.rag_multi_query_n, hyde=settings.rag_hyde)
        if settings.rag_query_rewrite
        else None
    )
    reranker = CrossEncoderReranker(settings.reranker_model) if settings.reranker_model else None
    return RAGPipeline(embedder, vector_recaller, keyword_recaller, rewriter, reranker)


@lru_cache
def _agent() -> Agent:
    registry = build_default_tools(_retriever(), llm)
    skills = SkillRegistry.from_dir(settings.skills_dir)
    manager = PluginManager(registry, skills)
    register_builtin_plugins(manager)
    manager.discover(settings.plugins_dir)
    context = ContextBuilder(memory=FileMemoryStore(settings.memory_dir), skills=skills)
    return Agent(llm, registry, plugins=manager, context=context)


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


def get_agent() -> Agent:
    return _agent()
