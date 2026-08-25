"""rrf_fusion node: fuse the per-channel rankings into one ranked hit list.

Consumes ``ctx["rankings"]`` and becomes the current result list (``ctx["hits"]``) that
downstream ranking nodes refine. Pure rank-based, so vector and keyword scores of
different scales fuse fairly.
"""
from __future__ import annotations

from rag.nodes.base import Node, NodeStatus
from rag.rank.rrf import rrf_fusion


class RrfFusionNode(Node):
    name = "rrf_fusion"
    display_name = "RRF Fusion"
    stage = "ranking"
    params_schema = {
        "type": "object",
        "properties": {
            "k": {"type": "integer", "description": "RRF constant (default 60)"},
        },
        "required": [],
    }
    default_params = {"k": 60}
    description = "Fuses every per-channel ranking into one result list via Reciprocal Rank Fusion (rank-only, scale-free). Required: the recall channels only produce rankings."

    async def run(self, ctx, deps) -> NodeStatus:
        rankings = ctx.get("rankings", [])
        fused = rrf_fusion(rankings, k=int(self.params.get("k", 60)))
        ctx.set("fused", fused)
        ctx.set("hits", fused)
        ctx.set_out(
            "rrf_fusion",
            {"rankings_in": [len(r) for r in rankings], "hits": [h.id for h in fused]},
        )
        return NodeStatus.OK
