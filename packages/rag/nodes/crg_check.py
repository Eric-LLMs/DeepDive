"""crg_check node: simplified CRAG — LLM judges the fused evidence before it reaches the model.

The judge classifies the top evidence against the query as ``relevant`` /
``ambiguous`` / ``irrelevant``. ``irrelevant`` means the recalled evidence does not
answer the query at all, so the node drops the hits (the tool then returns nothing and
the model answers from knowledge rather than hallucinating over wrong context).

Like query rewrite, the judge degrades: a parse failure keeps the hits and records
``quality="unknown"`` instead of raising, so a flaky LLM never empties the result set
silently.
"""
from __future__ import annotations

import json

from rag.nodes.base import Node, NodeStatus


async def judge_relevance(llm, query: str, evidence: str) -> str:
    """Classify evidence relevance; returns ``relevant`` / ``ambiguous`` / ``irrelevant`` / ``unknown``."""
    system = (
        "You judge whether retrieved evidence can answer a user query. "
        'Reply with exactly one JSON object: {"verdict": "relevant" | "ambiguous" | "irrelevant"}.'
    )
    prompt = (
        f"Query: {query}\n\nEvidence:\n{evidence[:800]}\n\n"
        "Does the evidence answer the query? Reply with only the JSON verdict."
    )
    try:
        raw = await llm.complete(prompt, system)
        verdict = json.loads(_strip_code_fence(raw)).get("verdict", "ambiguous")
        return verdict if verdict in ("relevant", "ambiguous", "irrelevant") else "unknown"
    except Exception:  # noqa: BLE001 - judge must degrade, never empty the results
        return "unknown"


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


class CrgCheckNode(Node):
    name = "crg_check"
    display_name = "CRAG Relevance Check"
    stage = "ranking"
    params_schema = {
        "type": "object",
        "properties": {
            "max_evidence_chars": {
                "type": "integer",
                "description": "Evidence prefix length sent to the judge",
            },
        },
        "required": [],
    }
    default_params = {"max_evidence_chars": 800}
    description = "Simplified CRAG: LLM judges the top evidence relevant / ambiguous / irrelevant; drops hits judged irrelevant. A parse failure keeps the hits (never empties the result)."

    async def run(self, ctx, deps) -> NodeStatus:
        hits = ctx.final_hits()
        if not hits:
            return NodeStatus.SKIP
        if deps.llm is None:
            return NodeStatus.SKIP

        max_chars = int(self.params.get("max_evidence_chars", 800))
        evidence = "\n".join(h.text[:max_chars] for h in hits[:3])
        quality = await judge_relevance(deps.llm, ctx.request.query, evidence)

        ctx.set("quality", quality)
        if quality == "irrelevant":
            ctx.set("hits", [])
            ctx.set_out("crg_check", {"verdict": quality, "dropped": len(hits)})
        else:
            ctx.set_out("crg_check", {"verdict": quality, "kept": len(hits)})
        return NodeStatus.OK
