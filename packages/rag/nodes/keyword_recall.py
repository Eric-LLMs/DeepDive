"""keyword_recall node: tsvector full-text recall over the query variants.

Appends one ranking list per query variant to ``ctx["rankings"]``. Runs independently of
vector recall, so a vector-store outage degrades to keyword-only instead of failing the
whole pipeline.
"""
from __future__ import annotations

from rag.nodes.base import Node, NodeStatus


class KeywordRecallNode(Node):
    name = "keyword_recall"
    display_name = "Keyword Recall"
    stage = "ranking"
    description = "tsvector full-text search over each variant; CJK queries are jieba-segmented and matched against content_search."

    async def run(self, ctx, deps) -> NodeStatus:
        variants = ctx.get("variants") or [ctx.request.query]

        rankings = ctx.get("rankings", [])
        new_hits = []
        for q in variants:
            hits = await deps.keyword_recaller.recall(
                q, None, ctx.request.top_k * 2, ctx.request.filters
            )
            rankings.append(hits)
            new_hits.extend(hits)

        ctx.set("rankings", rankings)
        ctx.set_out(
            "keyword_recall",
            {"queries": variants, "hits": [h.id for h in new_hits]},
        )
        return NodeStatus.OK
