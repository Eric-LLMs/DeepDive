"""Phase 4d: concurrent vector+keyword recall is a PURE EXECUTION optimization.

The pair (configured-consecutive vector_recall + keyword_recall) runs concurrently,
so these tests pin what must NOT change while it does:

* identical fusion result — rankings merge back in CONFIGURED order, so the documented
  RRF "vector wins the tie" semantics hold even when keyword finishes first (proven by
  making the vector channel the SLOW one);
* identical trace — same node names, same configured order, same per-node products;
* identical degrade — one channel down → the other survives; both down → the same
  RetrievalUnavailable;
* identical ACL — both recallers still receive the request's tenant filters.

No second pipeline, no registry change: the SAME configured nodes are exercised, the
difference only shows in wall-clock overlap, which the sleeps induce deterministically.
"""
from __future__ import annotations

import asyncio

import pytest
from rag.pipeline.executor import PipelineDeps, RAGPipeline, RetrievalUnavailable
from rag.pipeline.pipeline_config import NodeConfig, RagPipelineConfig
from rag.types import SearchHit


def _hit(hid: str) -> SearchHit:
    return SearchHit(id=hid, text=hid, score=0.5, meta={})


class _Embed:
    async def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class _SlowVec:
    def __init__(self, hits, delay=0.05):
        self.hits = hits
        self.delay = delay
        self.filters_seen = []

    async def recall(self, query, embedding, top_k, filters=None):
        self.filters_seen.append(filters)
        await asyncio.sleep(self.delay)  # vector is the SLOW channel on purpose
        return self.hits


class _FastKw:
    def __init__(self, hits=None, raise_=False):
        self.hits = hits or []
        self.raise_ = raise_
        self.filters_seen = []

    async def recall(self, query, embedding, top_k, filters=None):
        self.filters_seen.append(filters)
        if self.raise_:
            raise RuntimeError("keyword service down")
        return self.hits


class _NoLLM:
    pass


def _pipe(vec, kw):
    cfg = RagPipelineConfig(nodes=[
        NodeConfig("vector_recall"),
        NodeConfig("keyword_recall"),
        NodeConfig("rrf_fusion"),
    ])
    return RAGPipeline(cfg, PipelineDeps(
        embedder=_Embed(), vector_recaller=vec, keyword_recaller=kw,
        llm=_NoLLM(), session_factory=None, chunk_repo=None,
    ))


async def test_pair_concurrency_keeps_configured_ranking_order():
    # Vector sleeps; keyword resolves first. The merged rankings must STILL be
    # [vector…, keyword…] (configured order) → the vector hit wins the RRF tie.
    pipe = _pipe(_SlowVec([_hit("v1")]), _FastKw([_hit("k1")]))
    hits = await pipe.retrieve("q", top_k=5, filters={"user_id": "u"})
    assert [h["id"] for h in hits] == ["v1", "k1"]


async def test_pair_trace_is_configured_order_and_complete():
    pipe = _pipe(_SlowVec([_hit("v1")]), _FastKw([_hit("k1")]))
    res = await pipe.trace("q")
    names = [t.name for t in res["trace"]]
    assert names == ["vector_recall", "keyword_recall", "rrf_fusion"]
    by = {t.name: t for t in res["trace"]}
    assert by["vector_recall"].status == "OK"
    assert by["vector_recall"].out["hits"] == ["v1"]
    assert by["keyword_recall"].out["hits"] == ["k1"]
    # rrf_fusion still sees BOTH channels' rankings (2 lists in).
    assert by["rrf_fusion"].out["rankings_in"] == [1, 1]


async def test_pair_degrades_exactly_like_sequential():
    pipe = _pipe(_SlowVec([_hit("v1")]), _FastKw(raise_=True))
    hits = await pipe.retrieve("q")  # one channel down → other survives
    assert [h["id"] for h in hits] == ["v1"]

    # Empty-but-successful vector + failing keyword: rankings exist (one empty list)
    # so RetrievalUnavailable must NOT fire — it needs NO rankings at all plus errors.
    # This mirrors the sequential contract exactly.
    pipe2 = _pipe(_SlowVec([]), _FastKw(raise_=True))
    res = await pipe2.trace("q")
    assert res["hits"] == []
    assert any("keyword_recall" in e for e in res["errors"])


async def test_pair_total_failure_raises_unavailable():
    class _DeadVec(_SlowVec):
        async def recall(self, *a, **k):
            raise RuntimeError("vector store down")

    pipe = _pipe(_DeadVec([]), _FastKw(raise_=True))
    with pytest.raises(RetrievalUnavailable):
        await pipe.retrieve("q")


async def test_pair_propagates_tenant_filters_to_both_channels():
    vec, kw = _SlowVec([_hit("v1")]), _FastKw([_hit("k1")])
    await _pipe(vec, kw).retrieve("q", filters={"user_id": "tenant-9"})
    assert vec.filters_seen and kw.filters_seen
    assert all(f == {"user_id": "tenant-9"} for f in vec.filters_seen + kw.filters_seen)


async def test_non_consecutive_recall_nodes_stay_sequential():
    # An operator config with another node BETWEEN the channels must not pair them:
    # parent_expand consumes hits, not rankings — but ordering-wise this only checks
    # the pipeline still runs every node exactly once in configured order.
    cfg = RagPipelineConfig(nodes=[
        NodeConfig("vector_recall"),
        NodeConfig("rrf_fusion"),
        NodeConfig("keyword_recall"),  # not consecutive with vector_recall
    ])
    vec, kw = _SlowVec([_hit("v1")]), _FastKw([_hit("k1")])
    pipe = RAGPipeline(cfg, PipelineDeps(
        embedder=_Embed(), vector_recaller=vec, keyword_recaller=kw,
        llm=_NoLLM(), session_factory=None, chunk_repo=None,
    ))
    res = await pipe.trace("q")
    assert [t.name for t in res["trace"]] == ["vector_recall", "rrf_fusion", "keyword_recall"]
