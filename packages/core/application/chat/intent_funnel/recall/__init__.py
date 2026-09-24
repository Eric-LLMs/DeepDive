"""Node 2 — Recall: query -> top-k candidates, evidence only, never adjudicates.

Discipline (design §3 Node 2 + §8.17): cosine here is a QUALITY GATE (filter
obvious garbage), the "which one" decision belongs to the ToolIntentModel. The legacy
QIR semantic layer mixed both roles — that entanglement is exactly what this
node unbundles. The index is the Recall side of the Registry's Build-Then-Swap
(§8.3): snapshot vectors are published in the SAME transaction as the registry
version, so (view.version, index.version) are observed together or not at all.

Provider ladder note: today's index is in-process cosine over a tiny
platform-curated example corpus; swapping in a vector DB / reranker later only
replaces THIS module (8.17 node isolation).
"""
from __future__ import annotations

import logging
import math

from ..contract import Candidate, RecallResult

logger = logging.getLogger(__name__)


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def recall(index, query: str, *, embedder, top_k: int,
                 min_score: float) -> RecallResult:
    """Top-k capabilities by best-example score. Raises on embedder failure —
    the funnel maps that to RECALL_UNAVAILABLE and falls open to the Agent."""
    if not (query or "").strip():
        return RecallResult(candidates=())
    vectors = await embedder.embed([query])
    if not isinstance(vectors, list) or not vectors or not vectors[0]:
        # empty embedder result is a fault, not an answer (8.10: no silent MISS)
        raise RuntimeError("recall: embedder returned no vector")
    qvec = vectors[0]

    best: dict[str, tuple[float, str]] = {}
    examples = {
        (c.id, i): e for c in index.capabilities for i, e in enumerate(c.examples)
    }
    for ev in index.example_vectors:
        key = (ev.capability_id, ev.example_index)
        if key not in examples:
            continue  # vector without a matching example — refuse to score it
        score = _cosine(qvec, ev.vector)
        prev = best.get(ev.capability_id)
        if prev is None or score > prev[0]:
            best[ev.capability_id] = (score, examples[key])

    ranked = sorted(
        (
            Candidate(capability_id=cid, score=round(s, 6), matched_example=ex)
            for cid, (s, ex) in best.items()
            if s >= min_score
        ),
        key=lambda c: c.score, reverse=True,
    )[: max(1, top_k)]
    return RecallResult(candidates=tuple(ranked))


async def load_index(session_factory):
    """Load the ACTIVE Recall index.

    The publish pipeline (Registry Build-Then-Swap) writes the index under the
    qir-store mechanism inherited from Batch 1 — the storage seam, not the old
    routing behavior. Replacing the storage format is a later-phase swap that
    must not touch this node's callers (8.17)."""
    from core.application.chat.qir import store as qir_store

    return await qir_store.active(session_factory)
