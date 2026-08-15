"""RAG retrieval pipeline: rewrite → multi-channel recall → RRF fusion → rerank.

A composable pipeline where each stage (rewrite / recall / rank) is an independent module
that can be replaced or toggled separately.
"""
from core.ports.vector import EmbeddingPort
from rag.query_rewrite import QueryRewriter, RewriteResult
from rag.rank.cross_encoder import CrossEncoderReranker
from rag.rank.rrf import rrf_fusion
from rag.recall.base import Recaller
from rag.types import SearchHit


class RAGPipeline:
    def __init__(
        self,
        embedder: EmbeddingPort,
        vector_recaller: Recaller,
        keyword_recaller: Recaller,
        rewriter: QueryRewriter | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self.embedder = embedder
        self.vector_recaller = vector_recaller
        self.keyword_recaller = keyword_recaller
        self.rewriter = rewriter
        self.reranker = reranker

    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        """Retrieval entry point: returns the top_k items as {id, text, score, meta}."""
        rw = await self.rewriter.rewrite(query) if self.rewriter else RewriteResult(queries=[query])

        rankings: list[list[SearchHit]] = []

        # ① Semantic recall: HyDE vectorizes the hypothetical document; otherwise recall once per query variant
        embed_queries = [rw.hyde_doc] if rw.hyde_doc else rw.queries
        for q in embed_queries:
            q_embed = (await self.embedder.embed([q]))[0]
            rankings.append(await self.vector_recaller.recall(q, q_embed, top_k * 2, filters))

        # ② Keyword recall: recall once per query variant
        for q in rw.queries:
            rankings.append(await self.keyword_recaller.recall(q, None, top_k * 2, filters))

        # ③ RRF fusion
        fused = rrf_fusion(rankings)

        # ④ Rerank (optional)
        if self.reranker:
            fused = await self.reranker.rerank(query, fused)

        return [h.to_dict() for h in fused[:top_k]]
