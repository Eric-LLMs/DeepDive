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
from rag import RAGPipeline, build_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("retrieval")


def _build_pipeline() -> RAGPipeline:
    return build_pipeline(
        embedder=TEIEmbedder(),
        vector_store=PgVectorStore(SessionLocal),
        session_factory=SessionLocal,
        llm=OpenAILLM(),
        settings=settings,
    )


async def serve() -> None:
    server = grpc.aio.server()
    retrieval_pb2_grpc.add_RetrievalServiceServicer_to_server(
        RetrievalService(_build_pipeline()), server
    )
    address = settings.retrieval_grpc_addr
    server.add_insecure_port(address)
    await server.start()
    logger.info("retrieval service listening on %s", address)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
