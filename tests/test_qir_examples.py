"""Phase 3 — the SQL Intent Query Library (migration 0009, ruling 2026-09-25).

Pins the new storage contract:

* the vectors leave the app_settings blob and become ``qir_examples`` ROWS,
  written inside the SAME Build-Then-Swap transaction as the version pointer;
* every read is pinned to ONE version (``qir_version = <active> AND enabled``)
  — cross-version recall is physically excluded, and an active version with no
  rows is a FAULT (None -> RECALL_UNAVAILABLE), never a blob-vector fallback;
* recall prefers the pgvector ANN lane and degrades HONESTLY (WARNING) to the
  in-process cosine over the same rows when the ANN query faults;
* ANN-vs-exact agreement is asserted as ORDERING, not bitwise-identity: HNSW
  is an approximate index, so the DB-gated test pins direction/monotonicity
  and >=80% top-k overlap, and says which is which.
"""
from __future__ import annotations

import types

import pytest
from core.application.chat.intent_funnel.recall import _ANN_POOL_FACTOR
from core.application.chat.intent_funnel.recall import recall as recall_fn
from core.application.chat.qir import store as qir_store
from core.application.chat.qir.types import Capability, ExampleVector, Snapshot

VERSION = "qir1-test00000000"


def _snap(vecs: tuple[ExampleVector, ...]) -> Snapshot:
    caps = (Capability(id="cap-a", tool_binding="create_folder",
                       description="d", examples=("新建文件夹", "建个目录")),)
    return Snapshot(version=VERSION, built_at=0.0, capabilities=caps,
                    example_vectors=vecs)


class _Res:
    def __init__(self, rows=(), scalar=None):
        self._rows, self._scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _Sess:
    def __init__(self, results=()):
        self.results = list(results)
        self.added: list = []
        self.merged: list = []
        self.executed: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return self.results.pop(0)

    async def merge(self, obj):
        self.merged.append(obj.key)
        return obj

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _factory(*sessions):
    pool = list(sessions)

    def _f():
        assert pool, "store opened more sessions than canned"
        return pool.pop(0)

    return _f


# ═════════════════ write side: rows land with the pointer, blob is bare ═════════

async def test_write_active_stages_rows_and_bare_blob():
    qir_store.invalidate_cache()
    vecs = (
        ExampleVector("cap-a", 0, (1.0, 0.0)),
        ExampleVector("cap-a", 1, (0.0, 1.0)),
    )
    s = _Sess(results=[_Res(rows=(), scalar=None)])  # the purge DELETE
    await qir_store.write_active(s, _snap(vecs))
    assert s.merged == ["qir_active", "qir_version"]
    # one purge of every NON-active version, then per-sentence rows
    assert len(s.executed) == 1 and "DELETE" in s.executed[0][0].upper()
    assert [r.kind for r in s.added] == ["canonical", "synonym"]  # position 0
    assert [r.text for r in s.added] == ["新建文件夹", "建个目录"]
    assert [r.language for r in s.added] == ["zh", "zh"]           # CJK build-time tag
    assert {r.qir_version for r in s.added} == {VERSION}
    assert [list(r.embedding) for r in s.added] == [[1.0, 0.0], [0.0, 1.0]]


async def test_write_active_english_rows_tag_en():
    qir_store.invalidate_cache()
    snap = Snapshot(
        version=VERSION, built_at=0.0,
        capabilities=(Capability(id="cap-a", tool_binding="create_folder",
                                 description="d",
                                 examples=("create a folder", "make a folder")),),
        example_vectors=(ExampleVector("cap-a", 0, (1.0,)),
                         ExampleVector("cap-a", 1, (0.0,))))
    s = _Sess(results=[_Res()])
    await qir_store.write_active(s, snap)
    assert [r.language for r in s.added] == ["en", "en"]


# ═══════════════════ read side: active = blob + pinned rows ════════════════════


async def test_active_reads_rows_for_the_marker_version_only():
    qir_store.invalidate_cache()
    row = types.SimpleNamespace(capability_id="cap-a", example_index=0,
                                text="新建文件夹", embedding=[1.0, 0.0])
    marker = _Res(scalar=types.SimpleNamespace(value={"version": VERSION}))
    blob = _Res(scalar=types.SimpleNamespace(value={
        "version": VERSION, "built_at": 0.0,
        "capabilities": [
            {"id": "cap-a", "tool_binding": "create_folder",
             "description": "d", "examples": ["新建文件夹"],
             "negatives": [], "enabled": True}],
        "example_vectors": []}))
    rows = _Res(rows=[row])
    s = _Sess(results=[marker, blob, rows])
    snap = await qir_store.active(session_factory=_factory(s))
    assert snap is not None and snap.version == VERSION
    assert [(v.capability_id, v.example_index, v.vector)
            for v in snap.example_vectors] == [("cap-a", 0, (1.0, 0.0))]
    # the row query carried the single-version + enabled predicates
    sql = s.executed[-1][0]
    assert "qir_examples" in sql and "enabled" in sql


async def test_active_without_rows_is_a_fault_not_a_blob_fallback():
    qir_store.invalidate_cache()
    marker = _Res(scalar=types.SimpleNamespace(value={"version": VERSION}))
    blob = _Res(scalar=types.SimpleNamespace(value={
        "version": VERSION, "built_at": 0.0, "capabilities": [],
        "example_vectors": [{"capability_id": "cap-a",
                             "example_index": 0,
                             "vector": [1.0, 0.0]}]}))
    rows = _Res(rows=[])   # table says this version has NO examples
    s = _Sess(results=[marker, blob, rows])
    snap = await qir_store.active(session_factory=_factory(s))
    assert snap is None    # the blob's stale vectors are NOT resurrected


# ═══════════════════════ recall: ANN lane + honest degrade ═════════════════════


def _index(*, session_factory=None):
    return types.SimpleNamespace(
        version=VERSION,
        capabilities=(Capability(id="cap-a", tool_binding="t", description="d",
                                 examples=("新建文件夹",)),),
        example_vectors=(ExampleVector("cap-a", 0, (1.0, 0.0)),),
        session_factory=session_factory,
    )


class _Embedder:
    def __init__(self, vec):
        self.vec = vec

    async def embed(self, texts):
        return [list(self.vec) for _ in texts]


async def test_ann_lane_scores_merges_and_pins_the_version():
    # ANN returns distance-scored rows; cap-merge keeps the best example only.
    rows = _Res(rows=[("cap-b", 2, "别的句", 0.91), ("cap-a", 0, "新建文件夹", 0.60),
                      ("cap-a", 1, "建个目录", 0.95)])
    s = _Sess(results=[rows])
    idx = _index(session_factory=_factory(s))
    res = await recall_fn(idx, "q", embedder=_Embedder([1.0, 0.0]),
                                  top_k=3, min_score=0.82)
    sql, params = s.executed[0]
    assert "qir_examples" in sql and "<=>" in sql          # the ANN operator
    assert params["version"] == VERSION                    # single-version predicate
    assert params["lim"] == 3 * _ANN_POOL_FACTOR
    cands = {c.capability_id: c for c in res.candidates}
    assert set(cands) == {"cap-a", "cap-b"}                # garbage below the gate dropped
    assert cands["cap-a"].score == 0.95                    # BEST example merged, not first
    assert cands["cap-a"].matched_example == "建个目录"
    assert cands["cap-a"].origin == "recall"


async def test_ann_fault_degrades_to_in_process_cosine_with_warning(caplog):
    import logging

    class _Boom:
        def __call__(self):
            raise _BoomCtx()

    class _BoomCtx:
        async def __aenter__(self):
            raise RuntimeError("hnsw index missing")

        async def __aexit__(self, *exc):
            return False

    idx = _index(session_factory=_Boom())
    with caplog.at_level(logging.WARNING,
                         logger="core.application.chat.intent_funnel.recall"):
        res = await recall_fn(idx, "q", embedder=_Embedder([1.0, 0.0]),
                                      top_k=3, min_score=0.5)
    assert res.candidates[0].capability_id == "cap-a"      # same answer, honest lane
    assert res.candidates[0].score == pytest.approx(1.0)
    assert any("ANN query failed" in r.getMessage() for r in caplog.records)


async def test_index_without_session_factory_stays_in_process():
    # doubles that patch load_index hand vector-only indexes — no ANN attempt.
    idx = _index(session_factory=None)
    res = await recall_fn(idx, "q", embedder=_Embedder([0.0, 1.0]),
                                  top_k=3, min_score=0.5)
    assert res.candidates == ()          # orthogonal -> below the gate


# ════════════════════ DB-gated: ANN <-> exact-cosine agreement ═════════════════
#
# The ruling deliberately does NOT demand per-row identity between the ANN lane
# and exact cosine: HNSW is approximate. What must hold is the SEMANTICS —
# (1) score direction (closer example -> higher score), (2) high Top-K
# agreement against exact cosine on the same rows, (3) correct per-capability
# best-example merge. The whole run lives in ONE uncommitted transaction on the
# dev DB and is rolled back — no fixture rows ever become visible.

def _db_available() -> bool:
    import asyncio

    async def probe():
        try:
            from core.config import settings
            from sqlalchemy import text as sql_text
            from sqlalchemy.ext.asyncio import create_async_engine
            eng = create_async_engine(settings.database_url)
            try:
                async with eng.connect() as c:
                    r = (await c.execute(sql_text(
                        "select to_regclass('public.qir_examples')"))).scalar()
                    return r is not None
            finally:
                await eng.dispose()
        except Exception:  # noqa: BLE001 - a probe reports "no DB", whatever the cause
            return False
    try:
        return asyncio.run(probe())
    except RuntimeError:  # already-running loop etc.
        return False


requires_pgvector = pytest.mark.skipif(
    not _db_available(), reason="local Postgres/pgvector not reachable")


@requires_pgvector
async def test_ann_and_exact_cosine_agree_on_ordering():
    import random

    from core.application.chat.intent_funnel.recall import _cosine
    from core.config import settings
    from core.infrastructure.db import SessionLocal
    from sqlalchemy import text as sql_text

    rnd = random.Random(20260925)
    dim = settings.embedding_dim

    def unit(vec):
        n = sum(x * x for x in vec) ** 0.5
        return [x / n for x in vec]

    # 8 answer anchors + 192 filler rows: with 8 rows an "approximate" index IS
    # exact — the point of the fillers is that HNSW must do REAL pruning over a
    # 200-row 1024-dim pool and still agree with the full exact scan.
    anchors = [unit([rnd.gauss(0, 1) for _ in range(dim)]) for _ in range(8)]
    corpus = [(f"cap-{ci}", a) for ci, a in enumerate(anchors)]
    corpus += [(f"filler-{i}", unit([rnd.gauss(0, 1) for _ in range(dim)]))
               for i in range(192)]
    qrys = []
    for i in range(12):
        base = anchors[i % 8]
        v = unit([0.8 * base[d] + 0.2 * rnd.gauss(0, 1) for d in range(dim)])
        qrys.append((f"q{i}", v))

    version = "qir1-agreement-probe"
    session = SessionLocal()
    try:
        # rows inside the transaction, NEVER committed
        for cid, vec in corpus:
            await session.execute(sql_text(
                "INSERT INTO qir_examples (capability_id, example_index, kind,"
                " language, text, embedding, enabled, qir_version) VALUES"
                " (:cid, 0, 'canonical', 'en', :t, CAST(:emb AS vector), true, :v)"
                " ON CONFLICT DO NOTHING"),
                {"cid": cid, "t": cid,
                 "emb": "[" + ",".join(repr(x) for x in vec) + "]", "v": version})
        await session.flush()
        overlap_hits = total = 0
        for name, qv in qrys:
            lit = "[" + ",".join(repr(x) for x in qv) + "]"
            ann = (await session.execute(sql_text(
                "SELECT capability_id, 1 - (embedding <=> CAST(:q AS vector)) AS score"
                "  FROM qir_examples WHERE qir_version = :v AND enabled"
                " ORDER BY embedding <=> CAST(:q AS vector) LIMIT 16"),
                {"q": lit, "v": version})).all()
            # exact cosine over the SAME population
            exact = sorted(
                ((cid, _cosine(qv, vec)) for cid, vec in corpus),
                key=lambda t: t[1], reverse=True)
            assert ann, name
            # (1) direction: the ANN pool's best matches the exact best, and
            # ANN-reported scores DECREASE along its own ordering
            assert ann[0][0] == exact[0][0], (name, ann[0], exact[0])
            scores = [float(r[1]) for r in ann]
            assert scores == sorted(scores, reverse=True)
            # (2) top-k ordering agreement >= 80% (k = 10 of the 16-row pool)
            k = 10
            hit = len({r[0] for r in ann[:k]} & {c for c, _ in exact[:k]})
            overlap_hits += hit
            total += k
            # (3) scores agree with exact cosine to 1e-6 for shared rows
            ex = dict(exact)
            for cid, s in ann:
                assert s == pytest.approx(ex[cid], abs=1e-6)
        assert overlap_hits / total >= 0.8, overlap_hits / total
    finally:
        await session.rollback()
        await session.close()
