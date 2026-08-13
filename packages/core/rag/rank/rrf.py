"""RRF (Reciprocal Rank Fusion): fuse the ranked results from multiple recall channels."""
from core.rag.types import SearchHit


def rrf_fusion(rankings: list[list[SearchHit]], k: int = 60) -> list[SearchHit]:
    """Fuse multiple ranked lists into one (dedup + sort by RRF score descending).

    RRF only cares about "rank" rather than "absolute score", so it can fairly fuse
    vector scores (cosine similarity) and full-text scores (ts_rank) that have different scales.
    """
    scores: dict[str, float] = {}
    hits: dict[str, SearchHit] = {}

    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank + 1)
            if hit.id not in hits:
                hits[hit.id] = hit

    fused = []
    for doc_id in sorted(scores, key=lambda d: -scores[d]):
        hit = hits[doc_id]
        fused.append(
            SearchHit(id=hit.id, text=hit.text, score=scores[doc_id], meta=hit.meta, source=hit.source)
        )
    return fused
