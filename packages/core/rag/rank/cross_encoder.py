"""Cross-encoder rerank: score the fused candidates more accurately.

Lazy loading: the BGE-Reranker model is large, so it is downloaded/loaded on first call.
The model name is configured in config.reranker_model; when empty this stage is disabled.
"""
import asyncio

from core.rag.types import SearchHit


class CrossEncoderReranker:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        return self._model

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """Score each (query, hit.text) pair for relevance, returns hits sorted by the new score in descending order."""
        if not hits:
            return hits
        model = self._load()
        pairs = [(query, hit.text) for hit in hits]
        # sentence-transformers is synchronous; run it in a thread pool to avoid blocking the event loop
        scores = await asyncio.to_thread(model.predict, pairs)

        scored = [
            SearchHit(id=h.id, text=h.text, score=float(s), meta=h.meta, source=h.source)
            for h, s in zip(hits, scores)
        ]
        return sorted(scored, key=lambda h: -h.score)
