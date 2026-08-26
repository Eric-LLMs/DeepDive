"""cross_encoder node: optional BGE cross-encoder rerank of the fused candidates.

Skips when ``model_name`` is empty (matches the pre-refactor behaviour where an empty
``reranker_model`` disabled reranking). The heavy model instance is cached per model
name across runs, so a per-query node construction does not reload it.
"""
from __future__ import annotations

from typing import ClassVar

from rag.nodes.base import Node, NodeStatus
from rag.rank.cross_encoder import CrossEncoderReranker

_reranker_cache: dict[str, CrossEncoderReranker] = {}


def _get_reranker(model_name: str) -> CrossEncoderReranker:
    if model_name not in _reranker_cache:
        _reranker_cache[model_name] = CrossEncoderReranker(model_name)
    return _reranker_cache[model_name]


class CrossEncoderNode(Node):
    name = "cross_encoder"
    display_name = "Cross Encoder Rerank"
    stage = "ranking"
    params_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "model_name": {
                "type": "string",
                "description": "Reranker model name; empty string disables the stage",
            },
        },
        "required": [],
    }
    default_params: ClassVar[dict] = {"model_name": ""}
    description = "Content-based rerank of the fused candidates with a BGE cross-encoder; SKIPs (no-op) while model_name is empty."

    async def run(self, ctx, deps) -> NodeStatus:
        model_name = str(self.params.get("model_name") or "")
        if not model_name:
            ctx.set_out(
                "cross_encoder", {"skipped": True, "reason": "no reranker model configured"}
            )
            return NodeStatus.SKIP

        hits = ctx.final_hits()
        if not hits:
            ctx.set_out("cross_encoder", {"skipped": True, "reason": "no candidates"})
            return NodeStatus.SKIP

        reranked = await _get_reranker(model_name).rerank(ctx.request.query, hits)
        ctx.set("hits", reranked)
        ctx.set_out(
            "cross_encoder",
            {"scores": [round(h.score, 4) for h in reranked]},
        )
        return NodeStatus.OK
