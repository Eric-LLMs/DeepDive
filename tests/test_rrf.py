"""Tests for reciprocal-rank fusion (dedup + descending RRF score)."""
from rag.rank.rrf import rrf_fusion
from rag.types import SearchHit


def _hit(doc_id: str, text: str = "") -> SearchHit:
    return SearchHit(id=doc_id, text=text, score=0.0)


def test_rrf_fuses_and_sorts():
    r1 = [_hit("a"), _hit("b")]
    r2 = [_hit("b"), _hit("c")]

    fused = rrf_fusion([r1, r2], k=60)

    # b appears in both lists (rank 1 + rank 2) → highest score; a (rank 1) beats c (rank 2).
    assert [h.id for h in fused] == ["b", "a", "c"]


def test_rrf_dedups():
    r1 = [_hit("x"), _hit("y")]
    r2 = [_hit("x")]

    fused = rrf_fusion([r1, r2], k=60)

    assert [h.id for h in fused] == ["x", "y"]
    assert len(fused) == 2


def test_rrf_empty_rankings():
    assert rrf_fusion([]) == []
    assert rrf_fusion([[]]) == []
