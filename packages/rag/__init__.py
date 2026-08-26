"""RAG module: a config-driven, node-pluggable retrieval pipeline.

Externally the pipeline factory and its composable pieces are exposed for assembly,
testing, and replacement in deps. The pipeline topology itself lives in the admin
console / ``app_settings["rag"]`` config (see ``rag.pipeline.pipeline_config``).
"""
from rag.pipeline.executor import PipelineDeps, RAGPipeline, RetrievalUnavailable
from rag.pipeline.factory import build_pipeline
from rag.pipeline.pipeline_config import ChunkingConfig, NodeConfig, RagPipelineConfig
from rag.pipeline.registry import registry
from rag.query.query_rewrite import QueryRewriter, RewriteResult
from rag.rank.cross_encoder import CrossEncoderReranker
from rag.rank.rrf import rrf_fusion
from rag.recall.base import Recaller
from rag.recall.keyword import KeywordRecaller
from rag.recall.vector import VectorRecaller
from rag.types import SearchHit

__all__ = [
    "ChunkingConfig",
    "CrossEncoderReranker",
    "KeywordRecaller",
    "NodeConfig",
    "PipelineDeps",
    "QueryRewriter",
    "RAGPipeline",
    "RagPipelineConfig",
    "Recaller",
    "RetrievalUnavailable",
    "RewriteResult",
    "SearchHit",
    "VectorRecaller",
    "build_pipeline",
    "registry",
    "rrf_fusion",
]
