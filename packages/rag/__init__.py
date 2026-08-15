"""RAG module: query rewrite → multi-channel recall → RRF fusion → rerank.

Externally only the pipeline and its composable parts are exposed, for easy assembly/testing/replacement in deps.
"""
from rag.factory import build_pipeline
from rag.pipeline import RAGPipeline
from rag.query_rewrite import QueryRewriter, RewriteResult
from rag.rank.cross_encoder import CrossEncoderReranker
from rag.rank.rrf import rrf_fusion
from rag.recall.base import Recaller
from rag.recall.keyword import KeywordRecaller
from rag.recall.vector import VectorRecaller
from rag.types import SearchHit

__all__ = [
    "RAGPipeline",
    "build_pipeline",
    "QueryRewriter",
    "RewriteResult",
    "CrossEncoderReranker",
    "rrf_fusion",
    "Recaller",
    "KeywordRecaller",
    "VectorRecaller",
    "SearchHit",
]
