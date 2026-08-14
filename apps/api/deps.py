"""Dependency injection: wire up singletons + per-request resources.

Singletons are lightweight clients (llm/tts/embedder) that talk to model services over
HTTP; no model is loaded into the API process itself.
"""
import json
from functools import lru_cache

from fastapi import Depends

from core.agent import (
    Agent,
    Capabilities,
    ContextBuilder,
    FileMemoryStore,
    PluginManager,
    SkillRegistry,
    ToolExecution,
    ToolOutput,
    ToolRuntime,
    define_tool,
    register_builtin_plugins,
    text_block,
)
from core.application.services import VocabularyService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.images import ImageScraper
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
from core.rag import (
    CrossEncoderReranker,
    KeywordRecaller,
    QueryRewriter,
    RAGPipeline,
    VectorRecaller,
)

# Lightweight singletons
llm = OpenAILLM()
tts = TTSClient()


@lru_cache
def _embedder() -> TEIEmbedder:
    return TEIEmbedder()


@lru_cache
def _retriever() -> RAGPipeline:
    embedder = TEIEmbedder()
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
    runtime = ToolRuntime()
    capabilities = Capabilities()
    # Retrieval is a capability seam: the tool calls require("retrieval"), so the provider
    # (in-process RAGPipeline or a gRPC client) is swappable via settings.retrieval_mode.
    if settings.retrieval_mode == "grpc":
        capabilities.provide("retrieval", GrpcRetriever(settings.retrieval_grpc_addr))
    else:
        capabilities.provide("retrieval", _retriever())

    async def rag_search(args: dict, exec: ToolExecution) -> list[dict]:
        retriever = capabilities.require("retrieval")
        return await retriever.retrieve(args.get("query", ""), args.get("top_k", 5))

    runtime.register(
        define_tool(
            name="rag_search",
            description="Search learning material (text chunks) for information relevant "
            "to the query. Returns matching chunks.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "top_k": {"type": "integer", "description": "Number of results."},
                },
                "required": ["query"],
            },
            output=ToolOutput(
                schema={"type": "array"},
                render=lambda args, value: [
                    text_block(json.dumps(value, ensure_ascii=False, default=str))
                ],
            ),
            execute=rag_search,
        )
    )

    async def translate(args: dict, exec: ToolExecution) -> str:
        return await llm.complete(
            args["text"], "You are a translator. Translate the text into natural Chinese."
        )

    runtime.register(
        define_tool(
            name="translate",
            description="Translate English text into natural, fluent Chinese.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "English text to translate."},
                },
                "required": ["text"],
            },
            output=ToolOutput(schema={"type": "string"}, render=lambda args, value: [text_block(value)]),
            execute=translate,
        )
    )

    skills = SkillRegistry.from_dir(settings.skills_dir)
    manager = PluginManager(runtime, skills)
    register_builtin_plugins(manager)
    manager.discover(settings.plugins_dir)
    context = ContextBuilder(memory=FileMemoryStore(settings.memory_dir), skills=skills)
    return Agent(llm, runtime, context=context)


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
