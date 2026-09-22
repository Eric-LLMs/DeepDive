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


# ── Evidence the optimization is REAL and SEMANTICS-FREE ─────────────────────────

class _Timed:
    """Records its own wall-clock window on the event loop so overlap is observable."""

    def __init__(self, hits, delay):
        self.hits = hits
        self.delay = delay
        self.window: tuple[float, float] | None = None

    async def recall(self, query, embedding, top_k, filters=None):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(self.delay)
        self.window = (t0, loop.time())
        return self.hits


async def test_pair_really_overlaps_in_time():
    # Not just "results are the same" — the two channels are CONCURRENT: their sleep
    # windows intersect and total wall time is far under the sequential sum (2*d).
    d = 0.08
    vec, kw = _Timed([_hit("v1")], d), _Timed([_hit("k1")], d)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    hits = await _pipe(vec, kw).retrieve("q")
    elapsed = loop.time() - t0
    assert [h["id"] for h in hits] == ["v1", "k1"]
    assert vec.window[0] < kw.window[1] and kw.window[0] < vec.window[1]
    assert elapsed < 1.5 * d  # sequential would be >= 2*d


@pytest.mark.parametrize("kw_down", [False, True], ids=["both-up", "keyword-down"])
async def test_ab_diff_parallel_matches_sequential(monkeypatch, kw_down):
    # Same inputs, same node config; the ONLY difference is the pairing switch. The
    # sequential run (pre-Phase-4 contract, forced by emptying _RECALL_NAMES) is the
    # oracle: hits, per-node trace (name/status/out — ms excluded as timing noise) and
    # the error list must be IDENTICAL, including when one channel is down.
    import rag.pipeline.executor as ex

    def _fresh():
        return _pipe(_SlowVec([_hit("v1"), _hit("v2")]), _FastKw([_hit("k1")], raise_=kw_down))

    par = await _fresh().trace("q", filters={"user_id": "u"})
    monkeypatch.setattr(ex, "_RECALL_NAMES", frozenset())  # pure sequential fallback
    seq = await _fresh().trace("q", filters={"user_id": "u"})

    assert par["hits"] == seq["hits"]
    assert [(t.name, t.status, t.out) for t in par["trace"]] == \
        [(t.name, t.status, t.out) for t in seq["trace"]]
    assert par["errors"] == seq["errors"]


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, object] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


async def test_cache_seam_unchanged_by_pair(monkeypatch):
    # The cache wraps ABOVE the pipeline, so the pair is invisible to it: a second
    # identical retrieve() is served from cache without re-entering either channel.
    from rag import query_cache

    vec, kw = _SlowVec([_hit("v1")]), _FastKw([_hit("k1")])
    monkeypatch.setattr(query_cache, "_client", _FakeRedis())
    cached = query_cache.CachedRetriever(_pipe(vec, kw), ttl_seconds=60)

    first = await cached.retrieve("q", 5, {"user_id": "u"})
    second = await cached.retrieve("q", 5, {"user_id": "u"})
    assert [h["id"] for h in first] == ["v1", "k1"]
    assert second == first
    assert len(vec.filters_seen) == 1 and len(kw.filters_seen) == 1  # second call: cache hit
