"""Node 2 — Recall: query -> top-k candidates, evidence only, never adjudicates.

Discipline (design §3 Node 2 + §8.17): cosine here is a QUALITY GATE (filter
obvious garbage), the "which one" decision belongs to the ToolIntentModel. The legacy
QIR semantic layer mixed both roles — that entanglement is exactly what this
node unbundles. The index is the Recall side of the Registry's Build-Then-Swap
(§8.3): snapshot vectors are published in the SAME transaction as the registry
version, so (view.version, index.version) are observed together or not at all.

Phase 3 (ruling 2026-09-25): the sentence embeddings live in the SQL
``qir_examples`` table (the Intent Query Library). This node PREFERS the pgvector
ANN path — one query pinned to the ACTIVE version (``qir_version = <active> AND
enabled``; cross-version recall is physically excluded). If the ANN query faults
(missing extension, connection trouble), recall degrades HONESTLY to in-process
cosine over the same version's rows loaded by ``load_index``, with a WARNING —
never to the retired blob lane.
"""
from __future__ import annotations

import logging
import math
import types

from ..contract import Candidate, RecallResult

logger = logging.getLogger(__name__)

# ANN reads a wide neighborhood so the per-capability best-example merge is
# taken from a real pool, not a candidate-starved top slice (semantics identical
# to the exact cosine scan, which scores every example).
_ANN_POOL_FACTOR = 8


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _rank(best: dict[str, tuple[float, str]], *, top_k: int,
          min_score: float) -> RecallResult:
    """Cap-merge outcome -> the node's contract: quality gate, score order,
    top_k cut. Shared by the ANN and the in-process lanes so the two can never
    drift in WHAT they return, only in HOW they scored."""
    ranked = sorted(
        (
            Candidate(capability_id=cid, score=round(s, 6), matched_example=ex)
            for cid, (s, ex) in best.items()
            if s >= min_score
        ),
        key=lambda c: c.score, reverse=True,
    )[: max(1, top_k)]
    return RecallResult(candidates=tuple(ranked))


async def _ann_recall(index, qvec, *, top_k: int, min_score: float):
    """pgvector lane: distance-ordered neighbours, one version predicate,
    best-example merge per capability. Returns None to signal 'degrade'."""
    from sqlalchemy import text as sql_text

    q_literal = "[" + ",".join(repr(float(x)) for x in qvec) + "]"
    sql = sql_text(
        "SELECT capability_id, example_index, text,"
        "       1 - (embedding <=> CAST(:q AS vector)) AS score"
        "  FROM qir_examples"
        " WHERE qir_version = :version AND enabled"
        " ORDER BY embedding <=> CAST(:q AS vector)"
        " LIMIT :lim"
    )
    async with index.session_factory() as session:
        rows = (await session.execute(
            sql, {"q": q_literal, "version": index.version,
                  "lim": max(1, top_k) * _ANN_POOL_FACTOR},
        )).all()
    best: dict[str, tuple[float, str]] = {}
    for cid, _idx, ex, score in rows:
        s = float(score)
        prev = best.get(cid)
        if prev is None or s > prev[0]:
            best[cid] = (s, str(ex))
    return _rank(best, top_k=top_k, min_score=min_score)


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

    if getattr(index, "session_factory", None) is not None:
        try:
            return await _ann_recall(index, qvec, top_k=top_k, min_score=min_score)
        except Exception as exc:  # noqa: BLE001 - honest degrade, loud WARNING
            logger.warning("recall: ANN query failed (%r); falling back to "
                           "in-process cosine over this version's rows", exc)

    # In-process lane: exact cosine over the ACTIVE version's example vectors
    # (loaded from qir_examples by load_index — the same rows, scanned here).
    # Test doubles and blob-less fakes hand the vectors in directly.
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
    return _rank(best, top_k=top_k, min_score=min_score)


async def load_index(session_factory):
    """Load the ACTIVE Recall index.

    The publish pipeline (Registry Build-Then-Swap) writes the qir_examples
    rows and the version pointer in the SAME transaction; this seam reads the
    pair back: metadata from the (vector-free) blob, embeddings from the
    active version's rows. An active version without rows is a fault — None,
    which the funnel reports as RECALL_UNAVAILABLE (8.10: no silent half-index,
    and no blob-vector fallback since the 2026-09-25 ruling). The returned
    index carries session_factory so recall() can take the ANN lane; doubles
    that patch this seam hand plain duck-typed indexes and stay on the
    in-process lane (8.17 node isolation)."""
    from core.application.chat.qir import store as qir_store

    snap = await qir_store.active(session_factory)
    if snap is None:
        return None
    return types.SimpleNamespace(
        version=snap.version,
        capabilities=snap.capabilities,
        example_vectors=snap.example_vectors,
        session_factory=session_factory,
    )
