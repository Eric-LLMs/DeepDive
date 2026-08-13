"""RAG module: query rewrite → multi-channel recall → RRF fusion → rerank.

Externally only the pipeline and its composable parts are exposed, for easy assembly/testing/replacement in deps.
"""
from core.rag.pipeline import RAGPipeline
from core.rag.query_rewrite import QueryRewriter, RewriteResult
from core.rag.rank.cross_encoder import CrossEncoderReranker
from core.rag.rank.rrf import rrf_fusion
from core.rag.recall.base import Recaller
from core.rag.recall.keyword import KeywordRecaller
from core.rag.recall.vector import VectorRecaller
from core.rag.types import SearchHit

__all__ = [
    "RAGPipeline",
    "QueryRewriter",
    "RewriteResult",
    "CrossEncoderReranker",
    "rrf_fusion",
    "Recaller",
    "KeywordRecaller",
    "VectorRecaller",
    "SearchHit",
]
