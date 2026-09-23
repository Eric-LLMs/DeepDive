"""Snapshot persistence + atomic publish (app_settings JSONB, no migration).

Storage shape (reuses the ``rag/config_store.py`` precedent: DB truth, tiny
in-process cache):

* ``qir_version``  — {"version": str}: the cheap freshness marker read per turn.
* ``qir_active``   — the full immutable Snapshot JSON (capabilities + derived
  example vectors).

Publish writes BOTH rows inside a single session/commit — one transaction, so a
reader either sees the old pair or the new pair, never a mix (Atomic Swap). Any
build failure raises before a session opens, so the previous snapshot keeps
serving. ``active()`` never raises: a DB/store fault degrades to ``None``
(fail-open — routing abstains, the Agent path is unaffected).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from core.infrastructure.db import AppSettingModel, SessionLocal

from .types import Snapshot

logger = logging.getLogger(__name__)

_VERSION_KEY = "qir_version"
_ACTIVE_KEY = "qir_active"

# process cache: (version, snapshot) — coherence enforced by the version marker;
# the executor re-validates the stamped version right before dispatch, so a
# multi-worker cache window can only ever DENY a stale route, never execute one.
_cache: tuple[str, Snapshot] | None = None


async def active(session_factory=None) -> Snapshot | None:
    """The currently published snapshot, or None (never published / store down)."""
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
        await _upsert(session, _ACTIVE_KEY, snap.to_json())
        await _upsert(session, _VERSION_KEY, {"version": snap.version})
        await session.commit()
    global _cache
    _cache = (snap.version, snap)
    logger.info("qir.store published version=%s capabilities=%d", snap.version, len(snap.capabilities))
    return snap


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
