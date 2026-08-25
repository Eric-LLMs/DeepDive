"""vector_recall node: semantic recall over the embedded query variants (or HyDE doc).

Appends one ranking list per embedded query to ``ctx["rankings"]``. A failure (e.g. the
embedding service is down) fails this node; the executor still runs keyword recall, and
only a total recall failure surfaces as an unavailable result.
"""
from __future__ import annotations

from rag.nodes.base import Node, NodeStatus


class VectorRecallNode(Node):
    name = "vector_recall"
    display_name = "Vector Recall"
    stage = "ranking"
    description = "Semantic recall: embeds each variant / HyDE doc and does pgvector cosine search over leaf chunks (tenant- + domain-filtered)."

    async def run(self, ctx, deps) -> NodeStatus:
        variants = ctx.get("variants") or [ctx.request.query]
        hyde = ctx.get("hyde_doc")
        embed_queries = [hyde] if hyde else variants

        rankings = ctx.get("rankings", [])
        new_hits = []
        for q in embed_queries:
            q_embed = (await deps.embedder.embed([q]))[0]
            hits = await deps.vector_recaller.recall(
                q, q_embed, ctx.request.top_k * 2, ctx.request.filters
            )
            rankings.append(hits)
            new_hits.extend(hits)

        ctx.set("rankings", rankings)
        ctx.set_out(
            "vector_recall",
            {"queries": embed_queries, "hits": [h.id for h in new_hits]},
        )
        return NodeStatus.OK
