"""gRPC retrieval service entrypoint.

Builds the same RAG pipeline the API used to run in-process and exposes it over gRPC.
Start from the repo root: ``python -m apps.retrieval.main``.

The service is gated by :class:`AuthGuard` (shared-secret token, per-peer rate limit,
tenant binding) and serves TLS when ``retrieval_grpc_tls_cert`` / ``_key`` are set.
"""
import asyncio
import logging

import grpc
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.llm import OpenAILLM
from core.infrastructure.proto import retrieval_pb2_grpc
from core.infrastructure.vector import PgVectorStore, TEIEmbedder
from rag import RAGPipeline, build_pipeline

from apps.retrieval.server import AuthGuard, RetrievalService

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


def _server_credentials() -> grpc.ServerCredentials | None:
    if settings.retrieval_grpc_tls_cert and settings.retrieval_grpc_tls_key:
        with open(settings.retrieval_grpc_tls_cert, "rb") as fh:
            cert = fh.read()
        with open(settings.retrieval_grpc_tls_key, "rb") as fh:
            key = fh.read()
        return grpc.ssl_server_credentials([(key, cert)])
    return None


async def serve() -> None:
    server = grpc.aio.server()
    retrieval_pb2_grpc.add_RetrievalServiceServicer_to_server(
        RetrievalService(
            _build_pipeline(),
            auth=AuthGuard(settings.retrieval_grpc_token, settings.retrieval_grpc_rate_limit),
        ),
        server,
    )
    address = settings.retrieval_grpc_addr
    creds = _server_credentials()
    if creds is not None:
        server.add_secure_port(address, creds)
    else:
        server.add_insecure_port(address)
    await server.start()
    logger.info("retrieval service listening on %s (tls=%s)", address, creds is not None)
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
