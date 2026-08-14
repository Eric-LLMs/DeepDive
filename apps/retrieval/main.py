"""gRPC retrieval service entrypoint.

Builds the same RAG pipeline the API used to run in-process and exposes it over gRPC.
Start from the repo root: ``python -m apps.retrieval.main``.
"""
import asyncio
import logging

import grpc

from apps.retrieval.server import RetrievalService
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.proto import retrieval_pb2_grpc
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
from core.rag import (
    CrossEncoderReranker,
    KeywordRecaller,
    QueryRewriter,
    RAGPipeline,
    VectorRecaller,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("retrieval")


def build_pipeline() -> RAGPipeline:
    llm = OpenAILLM()
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


async def serve() -> None:
    server = grpc.aio.server()
    retrieval_pb2_grpc.add_RetrievalServiceServicer_to_server(
        RetrievalService(build_pipeline()), server
    )
    address = settings.retrieval_grpc_addr
    server.add_insecure_port(address)
    await server.start()
    logger.info("retrieval service listening on %s", address)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
