"""RAG pipeline assembly factory.

Builds the config-driven pipeline from injected implementations, so the API, the
retrieval service, and the admin test console share one assembly path. Constructor /
dependency changes stay isolated here.

``config`` may be passed explicitly (e.g. a stored config loaded from ``app_settings``);
when omitted it defaults to the env-seeded :class:`RagPipelineConfig`, keeping the
pre-refactor behaviour byte-identical.
"""
from __future__ import annotations

from core.infrastructure.drive_repositories import SqlChunkRepository

from rag.pipeline import PipelineDeps, RAGPipeline
from rag.pipeline_config import RagPipelineConfig
from rag.recall.keyword import KeywordRecaller
from rag.recall.vector import VectorRecaller


def build_pipeline(
    embedder,
    vector_store,
    session_factory,
    llm,
    settings,
    *,
    config: RagPipelineConfig | None = None,
) -> RAGPipeline:
    """Assemble a :class:`RAGPipeline`.

    - ``embedder``: implements ``EmbeddingPort`` (TEIEmbedder etc.)
    - ``vector_store``: implements ``VectorStorePort`` (PgVectorStore etc.)
    - ``session_factory``: async session factory for keyword recall + chunk repo
    - ``llm``: implements ``LLMPort`` (query rewrite / CRAG check)
    - ``settings``: config object seeding default params
    - ``config``: explicit pipeline config (stored config); defaults to env-seeded
    """
    deps = PipelineDeps(
        embedder=embedder,
        vector_recaller=VectorRecaller(vector_store),
        keyword_recaller=KeywordRecaller(session_factory),
        llm=llm,
        session_factory=session_factory,
        chunk_repo=SqlChunkRepository(session_factory),
    )
    cfg = config if config is not None else RagPipelineConfig.default(settings)
    return RAGPipeline(cfg, deps)
