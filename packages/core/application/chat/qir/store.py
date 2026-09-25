"""Snapshot persistence + atomic publish — version pointer in app_settings,
example vectors in their own SQL table (migration 0009).

Storage shape (DB truth, tiny in-process cache):

* ``qir_version``  — {"version": str}: the cheap freshness marker read per turn.
* ``qir_active``   — the immutable Snapshot JSON **minus** example vectors: the
  routing metadata (capabilities, descriptions, negatives) the runtime reads.
* ``qir_examples`` — one row per curated sentence per version, with its
  pgvector embedding (Phase 3, ruling 2026-09-25). The vectors are DERIVED data
  and live as rows, not as blob bytes: an active version with no rows is a
  fault to report, not a state to paper over.

Publish writes rows AND both settings rows inside a single session/commit — one
transaction, so a reader either sees the old pair or the new pair, never a mix
(Atomic Swap). Any build failure raises before a session opens, so the previous
snapshot keeps serving. ``active()`` never raises: a DB/store fault degrades to
``None`` (fail-open — routing abstains, the Agent path is unaffected).
"""
from __future__ import annotations

import dataclasses
import logging

from sqlalchemy import delete, select

from core.infrastructure.db import AppSettingModel, QirExampleModel, SessionLocal

from .types import ExampleVector, Snapshot

logger = logging.getLogger(__name__)

_VERSION_KEY = "qir_version"
_ACTIVE_KEY = "qir_active"

# process cache: (version, snapshot) — coherence enforced by the version marker;
# the executor re-validates the stamped version right before dispatch, so a
# multi-worker cache window can only ever DENY a stale route, never execute one.
_cache: tuple[str, Snapshot] | None = None


def _is_cjk(text: str) -> bool:
    """Build-time language tag (observability only — never a query predicate):
    any Han character marks the sentence 'zh', otherwise 'en'."""
    return any("一" <= ch <= "鿿" for ch in text)


async def active(session_factory=None) -> Snapshot | None:
    """The currently published snapshot (blob metadata + SQL example rows), or
    None (not published / store down / active version with no rows — a fault)."""
    global _cache
    factory = session_factory or SessionLocal
    try:
        async with factory() as session:
            marker = await _read(session, _VERSION_KEY)
            if marker is None:
                _cache = None
                return None
            version = str(marker.get("version") or "")
            if _cache is not None and _cache[0] == version:
                return _cache[1]
            raw = await _read(session, _ACTIVE_KEY)
            if not raw:
                return None
            snap = Snapshot.from_json(raw)
            if snap.version != version:
                # pair mismatch — should be impossible under one-transaction
                # publish; refuse rather than serve a half-published state.
                logger.error("qir.store version/payload mismatch: %s != %s", version, snap.version)
                return None
            rows = (await session.execute(
                select(QirExampleModel)
                .where(QirExampleModel.qir_version == version)
                .where(QirExampleModel.enabled.is_(True))
                .order_by(QirExampleModel.capability_id, QirExampleModel.example_index)
            )).scalars().all()
            if not rows:
                # The Build-Then-Swap transaction wrote blob AND rows together,
                # so an active version without rows means the table lost the
                # derived half. No blob-vector fallback is offered by ruling:
                # that would resurrect the retired recall_corpus lane. Abstain.
                logger.error("qir.store active version %s has no example rows — abstaining", version)
                return None
            snap = dataclasses.replace(snap, example_vectors=tuple(
                ExampleVector(
                    capability_id=r.capability_id, example_index=r.example_index,
                    vector=tuple(float(x) for x in r.embedding),
                ) for r in rows
            ))
            _cache = (version, snap)
            return snap
    except Exception as exc:  # noqa: BLE001 - fail-open: routing simply abstains
        logger.warning("qir.store active() failed (fail-open): %r", exc)
        return None


async def publish(draft: dict, embedder, session_factory=None) -> Snapshot:
    """Validate -> build all derived indexes -> single-transaction release.

    Raises SnapshotError (or the embedder's error) WITHOUT touching the DB when
    anything fails; on success the new version is Active."""
    from .snapshot import build_snapshot  # local: avoid an import cycle in tests

    snap = await build_snapshot(draft, embedder)  # raises before any write
    factory = session_factory or SessionLocal
    async with factory() as session:
        await write_active(session, snap)
        await session.commit()
    global _cache
    _cache = (snap.version, snap)
    logger.info("qir.store published version=%s capabilities=%d", snap.version, len(snap.capabilities))
    return snap


async def write_active(session, snap: Snapshot) -> None:
    """Stage the active pair onto the CALLER's session/transaction (no commit).

    Lets the Registry publish pipeline (§8.3 Build-Then-Swap) swap the QIR active
    snapshot inside the SAME short transaction that activates the registry version
    row — one commit, so Registry vN and Index vN can never be observed apart.

    Phase 3 split: the blob carries the ROUTING METADATA only (vectors stripped);
    the example sentences land as ``qir_examples`` rows under the same version
    key, and rows of every other version are purged — the table always holds
    exactly what is (or is about to be) active."""
    await _upsert(session, _ACTIVE_KEY, dataclasses.replace(
        snap, example_vectors=()).to_json())
    await _upsert(session, _VERSION_KEY, {"version": snap.version})
    await session.execute(
        delete(QirExampleModel).where(QirExampleModel.qir_version != snap.version)
    )
    texts = {
        (c.id, i): t for c in snap.capabilities for i, t in enumerate(c.examples)
    }
    for ev in snap.example_vectors:
        text = texts.get((ev.capability_id, ev.example_index), "")
        session.add(QirExampleModel(
            capability_id=ev.capability_id, example_index=ev.example_index,
            kind="canonical" if ev.example_index == 0 else "synonym",
            language="zh" if _is_cjk(text) else "en",
            text=text, embedding=list(ev.vector),
            enabled=True, qir_version=snap.version,
        ))


async def unpublish(session_factory=None) -> None:
    """Emergency stop: remove the active pair so routing abstains everywhere."""
    factory = session_factory or SessionLocal
    async with factory() as session:
        for key in (_VERSION_KEY, _ACTIVE_KEY):
            row = (
                await session.execute(select(AppSettingModel).where(AppSettingModel.key == key))
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
        await session.commit()
    global _cache
    _cache = None


def invalidate_cache() -> None:
    """Drop the process cache (tests / manual refresh)."""
    global _cache
    _cache = None


async def _read(session, key: str) -> dict | None:
    row = (
        await session.execute(select(AppSettingModel).where(AppSettingModel.key == key))
    ).scalar_one_or_none()
    return row.value if row is not None else None


async def _upsert(session, key: str, value: dict) -> None:
    # AsyncSession.merge is a coroutine: calling it without await stages
    # NOTHING — commit() would then publish an empty transaction and the
    # snapshot would silently never persist (caught by the real-DB smoke).
    await session.merge(AppSettingModel(key=key, value=value))
