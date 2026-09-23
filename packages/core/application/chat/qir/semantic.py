"""Semantic candidate generation — coarse retrieval ONLY, never a trigger.

Scores the raw query against the snapshot's pre-built example vectors (cosine,
computed in-process: the example corpus is platform-curated and tiny). Output is
evidence for the Decision stage; nothing here may execute. An ambiguous head
race (margin below threshold) abstains WITHOUT spending the decision call —
matching the cascade contract "Semantic -> ambiguous -> abstain -> Agent".

Tenant note: capability examples are platform-level routing metadata (no user
content), so candidate visibility is global by construction; if per-tenant
example sets are ever introduced, filter here before scoring.
"""
from __future__ import annotations

import math

from .types import SemanticCandidate, Snapshot


def _cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def candidates(
    embedder, snapshot: Snapshot, query: str,
    *, top_k: int, min_score: float, margin: float,
) -> list[SemanticCandidate]:
    """Top-K capabilities by best-example score, gated by min_score + margin."""
    vectors = await embedder.embed([query])
    if not isinstance(vectors, list) or not vectors or not vectors[0]:
        return []
    qvec = vectors[0]

    best: dict[str, tuple[float, str]] = {}  # capability_id -> (score, example text)
    examples = {(c.id, i): e for c in snapshot.capabilities for i, e in enumerate(c.examples)}
    for ev in snapshot.example_vectors:
        key = (ev.capability_id, ev.example_index)
        if key not in examples:
            continue  # vector without a matching example — refuse to score it
        score = _cosine(qvec, ev.vector)
        prev = best.get(ev.capability_id)
        if prev is None or score > prev[0]:
            best[ev.capability_id] = (score, examples[key])

    ranked = sorted(
        (
            SemanticCandidate(capability_id=cid, score=s, top_example=ex)
            for cid, (s, ex) in best.items()
            if s >= min_score
        ),
        key=lambda c: c.score, reverse=True,
    )[: max(1, top_k)]
    if len(ranked) >= 2 and ranked[0].score - ranked[1].score < margin:
        return []  # too close to call — abstain, never spend a decision on noise
    return ranked
