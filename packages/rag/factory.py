"""RAG pipeline assembly factory.

Builds the retrieval DAG (rewrite → multi-recall → RRF → rerank) from injected
implementations, so the API and the retrieval service share one assembly path
instead of each duplicating the conditional rewriter/reranker wiring.
"""
from rag.pipeline import RAGPipeline
from rag.query_rewrite import QueryRewriter
from rag.rank.cross_encoder import CrossEncoderReranker
from rag.recall.keyword import KeywordRecaller
from rag.recall.vector import VectorRecaller


def build_pipeline(embedder, vector_store, session_factory, llm, settings) -> RAGPipeline:
    """Assemble a :class:`RAGPipeline` from injected implementations.

    - ``embedder``: implements ``EmbeddingPort`` (TEIEmbedder etc.)
    - ``vector_store``: implements ``VectorStorePort`` (PgVectorStore etc.)
    - ``session_factory``: async session factory for keyword recall
    - ``llm``: implements ``LLMPort`` (query rewrite)
    - ``settings``: config object with ``rag_query_rewrite`` / ``rag_multi_query_n`` /
      ``rag_hyde`` / ``reranker_model`` attributes
    """
    vector_recaller = VectorRecaller(vector_store)
    keyword_recaller = KeywordRecaller(session_factory)
    rewriter = (
        QueryRewriter(llm, n_variants=settings.rag_multi_query_n, hyde=settings.rag_hyde)
        if settings.rag_query_rewrite
        else None
    )
    reranker = CrossEncoderReranker(settings.reranker_model) if settings.reranker_model else None
    return RAGPipeline(embedder, vector_recaller, keyword_recaller, rewriter, reranker)
