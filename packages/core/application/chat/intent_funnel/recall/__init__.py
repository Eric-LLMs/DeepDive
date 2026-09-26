"""Node 2 — Recall: query -> scored hits, evidence only, never adjudicates.

Discipline (chain ruling 2026-09-24 + live-table ruling 2026-09-26): cosine
here is a QUALITY GATE (filter obvious garbage), the "which one" decision
belongs to the ToolIntentModel. The index is the LIVE corpus itself —
``capability_standard_queries`` / ``capability_similar_queries`` are the
runtime truth, so there is no version predicate and no snapshot pair any more.

Two INDEPENDENT vector searches per turn (Standard path + Similar path) over
ONE user-query embedding; the hit sets are simply concatenated. The retired
per-capability MAX/AVG merge is GONE by construction: every hit at or above
``min_score`` is kept as its own candidate with full provenance (capability,
sentence, kind, language, row ids).

Predicates: row enabled AND capability enabled+active AND embedding IS NOT
NULL — so until the out-of-band backfill (scripts/embed_corpus.py) has run,
the corpus is empty for scoring purposes and this node honestly reports
RECALL_UNAVAILABLE (8.10), never a half-index.
"""
from __future__ import annotations

import hashlib
import logging
import math
import types

from sqlalchemy import text as sql_text

from ..contract import Candidate, RecallResult

logger = logging.getLogger(__name__)

# ANN reads a wide neighborhood; it is an ANN WIDTH, not a candidate cap —
# everything >= min_score inside the pool is kept.
_ANN_POOL = 64

_MARKER_SQL = sql_text(
    "SELECT"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capabilities),"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capability_standard_queries),"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capability_similar_queries)"
)

# marker -> index (fingerprint + vector rows); swapped by construction when any
# live corpus row changes (the store bumps updated_at on every write).
_INDEX_CACHE: dict[str, types.SimpleNamespace] = {}


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


_LOAD_ROWS_SQL = sql_text(
    "SELECT 'standard' AS kind, s.id, s.capability_id, s.query, s.language,"
    "       NULL AS standard_query_id, s.embedding"
    "  FROM capability_standard_queries s"
    "  JOIN capabilities c ON c.capability_id = s.capability_id"
    " WHERE s.enabled AND c.enabled AND c.status = 'active' AND s.embedding IS NOT NULL"
    " UNION ALL"
    "SELECT 'similar', q.id, s.capability_id, q.query, q.language,"
    "       q.standard_query_id, q.embedding"
    "  FROM capability_similar_queries q"
    "  JOIN capability_standard_queries s ON s.id = q.standard_query_id"
    "  JOIN capabilities c ON c.capability_id = s.capability_id"
    " WHERE q.enabled AND s.enabled AND c.enabled AND c.status = 'active'"
    "   AND q.embedding IS NOT NULL"
)


async def load_index(session_factory):
    """Load the LIVE Recall index (fingerprint + embedded rows for the
    degraded in-process lane), or None when NO embedded row is routable —
    the funnel reports that as RECALL_UNAVAILABLE until the backfill runs.
    Test doubles that patch this seam hand plain duck-typed indexes and stay
    on the in-process lane (8.17 node isolation)."""
    async with session_factory() as session:
        marker_row = (await session.execute(_MARKER_SQL)).first()
    marker = "|".join(str(x) for x in (marker_row or ()))
    cached = _INDEX_CACHE.get(marker)
    if cached is not None:
        return cached
    async with session_factory() as session:
        rows = (await session.execute(_LOAD_ROWS_SQL)).all()
    corpus = tuple(
        types.SimpleNamespace(
            kind=str(r[0]), query_id=str(r[1]), capability_id=str(r[2]),
            query=str(r[3]), language=str(r[4]),
            standard_query_id=str(r[5]) if r[5] is not None else None,
            vector=list(r[6]),
        )
        for r in rows
    )
    if not corpus:
        _INDEX_CACHE.clear()
        return None
    digest = hashlib.sha256(
        "|".join(f"{c.kind}:{c.capability_id}:{c.language}:{c.query}"
                 for c in sorted(corpus, key=lambda c: c.query_id))
        .encode("utf-8")
    ).hexdigest()[:12]
    index = types.SimpleNamespace(
        version="corpus1-" + digest,
        corpus=corpus,
        session_factory=session_factory,
    )
    if len(_INDEX_CACHE) >= 8:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[marker] = index
    return index


_STANDARD_SQL = sql_text(
    "SELECT s.capability_id, s.id, s.query, s.language,"
    "       1 - (s.embedding <=> CAST(:q AS vector)) AS score"
    "  FROM capability_standard_queries s"
    "  JOIN capabilities c ON c.capability_id = s.capability_id"
    " WHERE s.enabled AND c.enabled AND c.status = 'active'"
    "   AND s.embedding IS NOT NULL"
    " ORDER BY s.embedding <=> CAST(:q AS vector)"
    " LIMIT :pool"
)
_SIMILAR_SQL = sql_text(
    "SELECT s.capability_id, q.id, q.query, q.language, q.standard_query_id,"
    "       1 - (q.embedding <=> CAST(:q AS vector)) AS score"
    "  FROM capability_similar_queries q"
    "  JOIN capability_standard_queries s ON s.id = q.standard_query_id"
    "  JOIN capabilities c ON c.capability_id = s.capability_id"
    " WHERE q.enabled AND s.enabled AND c.enabled AND c.status = 'active'"
    "   AND q.embedding IS NOT NULL"
    " ORDER BY q.embedding <=> CAST(:q AS vector)"
    " LIMIT :pool"
)


def _q_literal(qvec) -> str:
    return "[" + ",".join(repr(float(x)) for x in qvec) + "]"


def _hits_from_rows(rows, kind: str) -> list[Candidate]:
    out: list[Candidate] = []
    for r in rows:
        out.append(Candidate(
            capability_id=str(r[0]), score=round(float(r[-1]), 6),
            matched_example=str(r[2]), origin="recall",
            query_kind=kind, language=str(r[3]), query_id=str(r[1]),
            standard_query_id=(str(r[4]) if kind == "similar" and r[4] is not None
                               else None),
        ))
    return out


async def _ann_recall(index, qvec, *, min_score: float) -> RecallResult:
    """pgvector lane: TWO independent distance-ordered searches (Standard
    path, Similar path). No per-capability merge — hits are concatenated as
    they came back, every one >= min_score kept."""
    q_literal = _q_literal(qvec)
    async with index.session_factory() as session:
        std_rows = (await session.execute(
            _STANDARD_SQL, {"q": q_literal, "pool": _ANN_POOL})).all()
        sim_rows = (await session.execute(
            _SIMILAR_SQL, {"q": q_literal, "pool": _ANN_POOL})).all()
    hits = _hits_from_rows(std_rows, "standard") + _hits_from_rows(sim_rows, "similar")
    return _gate(hits, min_score=min_score)


def _gate(hits: list[Candidate], *, min_score: float) -> RecallResult:
    """The quality gate + score order. EVERY hit >= min_score is a candidate
    (a capability may appear several times, through different sentences)."""
    kept = sorted((c for c in hits if c.score >= min_score),
                  key=lambda c: c.score, reverse=True)
    return RecallResult(candidates=tuple(kept))


async def recall(index, query: str, *, embedder,
                 min_score: float) -> RecallResult:
    """All live-corpus hits at or above the quality gate, score-ordered.
    The user query is embedded EXACTLY ONCE for both paths. Raises on
    embedder failure — the funnel maps that to RECALL_UNAVAILABLE and falls
    open to the Agent."""
    if not (query or "").strip():
        return RecallResult(candidates=())
    vectors = await embedder.embed([query])
    if not isinstance(vectors, list) or not vectors or not vectors[0]:
        # empty embedder result is a fault, not an answer (8.10: no silent MISS)
        raise RuntimeError("recall: embedder returned no vector")
    qvec = vectors[0]

    if getattr(index, "session_factory", None) is not None:
        try:
            return await _ann_recall(index, qvec, min_score=min_score)
        except Exception as exc:  # noqa: BLE001 - honest degrade, loud WARNING
            logger.warning("recall: ANN query failed (%r); falling back to "
                           "in-process cosine over the live corpus rows", exc)

    # In-process lane: exact cosine over the SAME rows load_index read
    # (test doubles and ANN faults land here; two paths, no merge). A dim
    # mismatch between the query vector and any corpus row is a PROFILE/DIM
    # CONFIG FAULT, not a business result: silently truncating (zip) would
    # turn it into garbage scores or a fake-empty set that exits as
    # NO_CANDIDATE. Raise instead — the funnel maps it to RECALL_UNAVAILABLE.
    hits: list[Candidate] = []
    for c in getattr(index, "corpus", ()):
        if len(c.vector) != len(qvec):
            raise RuntimeError(
                f"recall: dim mismatch — query {len(qvec)} vs corpus row "
                f"{c.query_id} ({c.capability_id}) {len(c.vector)}; embedder "
                "profile and stored vectors must share one dimension")
        hits.append(Candidate(
            capability_id=c.capability_id, score=round(_cosine(qvec, c.vector), 6),
            matched_example=c.query, origin="recall", query_kind=c.kind,
            language=c.language, query_id=c.query_id,
            standard_query_id=c.standard_query_id,
        ))
    return _gate(hits, min_score=min_score)
