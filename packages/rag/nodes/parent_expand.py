"""parent_expand node: small-to-big (parent/child) expansion.

When parent-child indexing is enabled, recall only surfaces leaf chunks; this node
replaces each leaf hit with its parent chunk's full text so the model sees a bigger
context window. Sibling leaves sharing one parent collapse into a single parent hit
(dedup) while preserving the position of the first matching leaf — so the ranking the
fusion produced is kept and the context is not flooded with near-duplicate text.
"""
from __future__ import annotations

from rag.nodes.base import Node, NodeStatus
from rag.types import SearchHit


def expand_with_parents(
    hits: list[SearchHit],
    parents_by_child: dict[str, dict],
) -> list[SearchHit]:
    """Replace leaf hits with their parent text, deduping siblings, in first-leaf order.

    ``parents_by_child`` maps a leaf chunk id to a parent dict ``{id, text, meta}``.
    A leaf without a parent passes through unchanged.
    """
    expanded: list[SearchHit] = []
    seen: set[str] = set()
    for h in hits:
        parent = parents_by_child.get(h.id)
        if parent is None:
            expanded.append(h)
            continue
        pid = parent["id"]
        if pid in seen:
            continue  # sibling already folded into this parent
        seen.add(pid)
        expanded.append(
            SearchHit(
                id=pid,
                text=parent["text"],
                score=h.score,
                meta={**(h.meta or {}), **(parent.get("meta") or {})},
                source="parent",
            )
        )
    return expanded


class ParentExpandNode(Node):
    name = "parent_expand"
    display_name = "Parent Expand (small-to-big)"
    stage = "ranking"
    description = "Small-to-big: replaces each leaf hit with its parent chunk's full text; sibling leaves dedupe to one parent. No-op when no parent chunks exist (parent_child ingest flag off)."

    async def run(self, ctx, deps) -> NodeStatus:
        hits = ctx.final_hits()
        if not hits:
            ctx.set_out("parent_expand", {"skipped": True, "reason": "no hits to expand"})
            return NodeStatus.OK
        if deps.chunk_repo is None:
            return NodeStatus.SKIP

        leaf_ids = [h.id for h in hits]
        parents_by_child = await deps.chunk_repo.get_parents_by_child_ids(leaf_ids)
        if not parents_by_child:
            ctx.set_out("parent_expand", {"skipped": True, "reason": "no parents found"})
            return NodeStatus.OK

        expanded = expand_with_parents(hits, parents_by_child)
        ctx.set("hits", expanded)
        ctx.set_out(
            "parent_expand",
            {
                "leafs": len(hits),
                "expanded": len(expanded),
                "parent_ids": sorted({p["id"] for p in parents_by_child.values()}),
            },
        )
        return NodeStatus.OK
