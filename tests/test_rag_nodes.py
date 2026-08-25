"""Tests for individual pipeline nodes: rewrite degrade, parent dedup, CRAG verdict."""
from rag.context import PipelineContext, RagRequest
from rag.nodes.base import NodeStatus
from rag.nodes.crg_check import CrgCheckNode, judge_relevance
from rag.nodes.parent_expand import ParentExpandNode, expand_with_parents
from rag.nodes.query_rewrite import QueryRewriteNode
from rag.types import SearchHit


def _ctx(query: str = "q") -> PipelineContext:
    return PipelineContext(RagRequest(query=query, top_k=5, filters={"user_id": "u"}))


def _deps(**kwargs):
    return type("Deps", (), kwargs)()


# ── query_rewrite ──
class _Llm:
    def __init__(self, raw: str):
        self.raw = raw

    async def complete(self, prompt, system):
        return self.raw


async def test_rewrite_produces_variants():
    llm = _Llm('["variant one", "variant two"]')
    node = QueryRewriteNode(params={"n_variants": 2, "hyde": False})
    ctx = _ctx("original query")
    assert await node.run(ctx, _deps(llm=llm)) is NodeStatus.OK
    assert ctx.get("variants")[0] == "original query"
    assert "variant one" in ctx.get("variants")


async def test_rewrite_degrades_to_original_query():
    llm = _Llm("not json at all")
    node = QueryRewriteNode(params={"n_variants": 2, "hyde": False})
    ctx = _ctx("original query")
    assert await node.run(ctx, _deps(llm=llm)) is NodeStatus.OK
    assert ctx.get("variants") == ["original query"]


async def test_rewrite_without_llm_is_identity():
    node = QueryRewriteNode(params={"n_variants": 2})
    ctx = _ctx("original query")
    assert await node.run(ctx, _deps(llm=None)) is NodeStatus.OK
    assert ctx.get("variants") == ["original query"]
    assert ctx.get("hyde_doc") is None


class _BoomLlm:
    async def complete(self, prompt, system):
        raise RuntimeError("503 model_not_found")


async def test_rewrite_degrades_when_llm_raises():
    node = QueryRewriteNode(params={"n_variants": 2, "hyde": True})
    ctx = _ctx("original query")
    assert await node.run(ctx, _deps(llm=_BoomLlm())) is NodeStatus.OK
    assert ctx.get("variants") == ["original query"]
    assert ctx.get("hyde_doc") is None


# ── parent_expand ──
def test_expand_dedups_siblings_and_keeps_order():
    hits = [
        SearchHit(id="leaf2", text="b", score=0.6),
        SearchHit(id="leaf1", text="a", score=0.9),
        SearchHit(id="orphan", text="o", score=0.3),
    ]
    parents = {
        "leaf2": {"id": "p1", "text": "PARENT", "meta": {"page": 1}},
        "leaf1": {"id": "p1", "text": "PARENT", "meta": {"page": 1}},
    }
    expanded = expand_with_parents(hits, parents)
    # p1 appears once, at the position of its first leaf (leaf2), before the orphan.
    assert [h.id for h in expanded] == ["p1", "orphan"]
    assert expanded[0].text == "PARENT"
    assert expanded[0].source == "parent"


def test_expand_passes_through_non_leaf_hits():
    hits = [SearchHit(id="x", text="x", score=0.5)]
    assert expand_with_parents(hits, {}) == hits


async def test_parent_expand_skips_when_no_parents():
    node = ParentExpandNode()
    ctx = _ctx()
    ctx.set("hits", [SearchHit(id="leaf", text="t", score=0.5)])
    class _Repo:
        async def get_parents_by_child_ids(self, ids):
            return {}

    assert await node.run(ctx, _deps(chunk_repo=_Repo())) is NodeStatus.OK
    assert ctx.get_out("parent_expand")["skipped"]


# ── crg_check ──
async def test_crg_judge_relevant():
    llm = _Llm('{"verdict": "relevant"}')
    assert await judge_relevance(llm, "q", "evidence") == "relevant"


async def test_crg_judge_unknown_on_parse_failure():
    llm = _Llm("garbage")
    assert await judge_relevance(llm, "q", "evidence") == "unknown"


async def test_crg_drops_irrelevant_hits():
    node = CrgCheckNode()
    ctx = _ctx("q")
    ctx.set("hits", [SearchHit(id="h1", text="irrelevant text", score=0.5)])
    assert await node.run(ctx, _deps(llm=_Llm('{"verdict": "irrelevant"}'))) is NodeStatus.OK
    assert ctx.final_hits() == []
    assert ctx.get("quality") == "irrelevant"


async def test_crg_keeps_relevant_hits():
    node = CrgCheckNode()
    ctx = _ctx("q")
    ctx.set("hits", [SearchHit(id="h1", text="good text", score=0.5)])
    assert await node.run(ctx, _deps(llm=_Llm('{"verdict": "relevant"}'))) is NodeStatus.OK
    assert len(ctx.final_hits()) == 1
    assert ctx.get("quality") == "relevant"
