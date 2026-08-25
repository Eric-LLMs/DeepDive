"""query_rewrite node: LLM multi-query expansion + HyDE.

Wraps the existing :class:`QueryRewriter`. On LLM failure the rewriter itself degrades
to the original query (``_multi_query``/``_hyde`` swallow exceptions), so this node is
never a pipeline-stopper.
"""
from __future__ import annotations

from rag.nodes.base import Node, NodeStatus
from rag.query_rewrite import QueryRewriter


class QueryRewriteNode(Node):
    name = "query_rewrite"
    display_name = "Query Rewrite"
    params_schema = {
        "type": "object",
        "properties": {
            "n_variants": {"type": "integer", "description": "Extra query variants to generate"},
            "hyde": {"type": "boolean", "description": "Generate a hypothetical document"},
        },
        "required": [],
    }
    default_params = {"n_variants": 2, "hyde": False}
    description = "LLM expands the question into N variant queries (optionally a HyDE hypothetical doc); falls back to the original query on LLM failure."

    async def run(self, ctx, deps) -> NodeStatus:
        query = ctx.request.query
        if deps.llm is None:
            ctx.set("variants", [query])
            ctx.set("hyde_doc", None)
            ctx.set_out("query_rewrite", {"queries": [query], "hyde": False})
            return NodeStatus.OK

        n_variants = int(self.params.get("n_variants", 2))
        hyde = bool(self.params.get("hyde", False))
        result = await QueryRewriter(deps.llm, n_variants=n_variants, hyde=hyde).rewrite(query)

        ctx.set("variants", result.queries)
        ctx.set("hyde_doc", result.hyde_doc)
        ctx.set_out(
            "query_rewrite",
            {"queries": result.queries, "hyde": result.hyde_doc is not None},
        )
        return NodeStatus.OK
