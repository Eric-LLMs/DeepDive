"""Tests for the config-driven pipeline executor: topology, degrade, and trace.

Uses scripted fakes (no mock library) matching the repo's existing fake style. The
default config must reproduce the pre-refactor behaviour: rewrite → multi-recall → RRF,
with an optional rerank stage that skips when no model is configured.
"""
import pytest

from rag import build_pipeline
from rag.pipeline import RetrievalUnavailable
from rag.pipeline_config import NodeConfig, RagPipelineConfig
from rag.types import SearchHit


def _hit(hid: str, text: str = "") -> SearchHit:
    return SearchHit(id=hid, text=text or hid, score=0.5, meta={})


class _Embed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _VecRecall:
    def __init__(self, hits=None, raise_=False):
        self.hits = hits or []
        self.raise_ = raise_
        self.calls = 0

    async def recall(self, query, embedding, top_k, filters=None):
        self.calls += 1
        if self.raise_:
            raise RuntimeError("embedding service down")
        return self.hits


class _KwRecall:
    def __init__(self, hits=None, raise_=False):
        self.hits = hits or []
        self.raise_ = raise_
        self.calls = 0

    async def recall(self, query, embedding, top_k, filters=None):
        self.calls += 1
        if self.raise_:
            raise RuntimeError("keyword service down")
        return self.hits


class _NoLLM:
    pass


class _Sess:
    def __call__(self):
        raise RuntimeError("no db expected")


class _Settings:
    rag_query_rewrite = True
    rag_multi_query_n = 2
    rag_hyde = False
    reranker_model = ""
    ingest_chunk_chars = 1200
    ingest_chunk_overlap = 150


def _pipe(vec_hits=None, kw_hits=None, vec_raise=False, kw_raise=False, config=None):
    cfg = config or RagPipelineConfig.default(_Settings)
    deps = dict(
        embedder=_Embed(),
        vector_recaller=_VecRecall(vec_hits, vec_raise),
        keyword_recaller=_KwRecall(kw_hits, kw_raise),
        llm=None,
        session_factory=_Sess(),
        chunk_repo=None,
    )
    # build deps manually so we control the recallers (build_pipeline would build real ones)
    from rag.pipeline import PipelineDeps, RAGPipeline

    return RAGPipeline(cfg, PipelineDeps(**deps))


async def test_default_topology_rewrites_and_fuses():
    pipe = _pipe(vec_hits=[_hit("v1")], kw_hits=[_hit("k1")])
    hits = await pipe.retrieve("what is attention?", top_k=5, filters={"user_id": "u"})

    ids = [h["id"] for h in hits]
    # RRF over two channels, both rank 0 → tie; the vector channel runs first in the
    # rankings list, so its hit wins the stable tie-break.
    assert ids == ["v1", "k1"]


async def test_degrade_when_one_channel_down():
    pipe = _pipe(vec_hits=[_hit("v1")], kw_raise=True)
    hits = await pipe.retrieve("what is attention?")
    assert [h["id"] for h in hits] == ["v1"]  # keyword failed, vector survived


async def test_total_recall_failure_raises_unavailable():
    pipe = _pipe(vec_raise=True, kw_raise=True)
    with pytest.raises(RetrievalUnavailable):
        await pipe.retrieve("what is attention?")


async def test_trace_records_per_node_products():
    pipe = _pipe(vec_hits=[_hit("v1")], kw_hits=[_hit("k1")])
    res = await pipe.trace("what is attention?")
    names = [t.name for t in res["trace"]]
    assert names == ["query_rewrite", "vector_recall", "keyword_recall", "rrf_fusion", "cross_encoder"]
    by_name = {t.name: t for t in res["trace"]}
    assert by_name["query_rewrite"].status == "OK"
    assert by_name["query_rewrite"].out["queries"] == ["what is attention?"]
    assert by_name["rrf_fusion"].status == "OK"
    assert by_name["cross_encoder"].status == "SKIP"


async def test_disabled_node_does_not_run():
    cfg = RagPipelineConfig.default(_Settings)
    cfg.nodes = [n for n in cfg.nodes if n.name != "keyword_recall"]
    pipe = _pipe(vec_hits=[_hit("v1")], kw_hits=[_hit("k1")], config=cfg)
    hits = await pipe.retrieve("what is attention?")
    assert [h["id"] for h in hits] == ["v1"]


async def test_reordering_nodes_changes_nothing_for_result_contract():
    # query_rewrite absent → recall uses the raw query; pipeline still returns hits.
    cfg = RagPipelineConfig.default(_Settings)
    cfg.nodes = [
        NodeConfig("vector_recall"),
        NodeConfig("keyword_recall"),
        NodeConfig("rrf_fusion"),
    ]
    pipe = _pipe(vec_hits=[_hit("v1")], kw_hits=[_hit("k1")], config=cfg)
    hits = await pipe.retrieve("what is attention?")
    assert {h["id"] for h in hits} == {"v1", "k1"}


async def test_unknown_node_name_fails_config_not_retrieve():
    cfg = RagPipelineConfig.default(_Settings)
    cfg.nodes.append(NodeConfig("not_a_real_node"))
    pipe = _pipe(vec_hits=[_hit("v1")], kw_hits=[_hit("k1")], config=cfg)
    res = await pipe.trace("what is attention?")
    assert any("not_a_real_node" in e for e in res["errors"])
