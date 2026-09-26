"""Live-table Registry store (migration 0014, final ruling 2026-09-26).

Doctrine pinned here:

* The live tables are the RUNTIME TRUTH. Admin writes go live directly;
  ``invalidate_cache()`` (same-process) plus the per-table content marker
  (cross-process) make a stale corpus after a save impossible.
* Capability-row edits are optimistic-concurrency: a writer must present the
  ``row_version`` it read, or get ``RegistryConflictError`` — silent overwrite
  is impossible. Query-row edits live in :mod:`.queries` (embed-then-write
  atomicity).
* ``registry_versions`` is HISTORY ONLY: each admin write snapshots the
  pre-change content as a new row (never edited in place). Nothing in the
  runtime reads it; rollback = restore a snapshot + re-embed.
* A disabled/deprecated capability is excluded from routing downstream
  (Matcher/Recall predicates + executor re-validation) — never a candidate.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from core.infrastructure.db import (
    CapabilityModel,
    CapabilityNegativeModel,
    CapabilitySimilarQueryModel,
    CapabilityStandardQueryModel,
    RegistryAuditModel,
    RegistryVersionModel,
    SessionLocal,
)
from core.infrastructure.security import get_setting

from .entry import CapabilityEntry, RegistryLiveView

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """Base for Registry store failures (admin plane; the runtime read path
    wraps these into fail-open)."""


class RegistryConflictError(RegistryError):
    """Optimistic-concurrency miss or duplicate capability_id."""


class RegistryNotFoundError(RegistryError):
    """Target row (capability / history version) does not exist."""


def _factory(session_factory: Any = None):
    return session_factory or SessionLocal


# ── Capability rows (intent information) ─────────────────────────────────────────

# Columns an editor may patch on the capability row; capability_id (identity)
# and row_version (token) are not in here on purpose. The query corpus is NOT
# patchable through this path — it lives in the live query tables (:mod:`.queries`).
CAPABILITY_PATCH_FIELDS = frozenset({
    "tool_binding", "description", "patterns", "aliases",
    "request_query_examples", "parameters", "arg_slots", "permissions",
    "execution_policy", "intent_kind", "enabled", "status",
    "replacement_capability_id",
})

_LIST_FIELDS = frozenset({"patterns", "aliases", "request_query_examples"})


async def list_capabilities(*, session_factory: Any = None) -> list[CapabilityEntry]:
    """The full live entry set (admin view: every capability, even disabled)."""
    factory = _factory(session_factory)
    async with factory() as session:
        caps = (
            await session.execute(
                select(CapabilityModel).order_by(CapabilityModel.capability_id)
            )
        ).scalars().all()
        std = (await session.execute(
            select(CapabilityStandardQueryModel)
            .order_by(CapabilityStandardQueryModel.capability_id,
                      CapabilityStandardQueryModel.position)
        )).scalars().all()
        sim = (await session.execute(
            select(CapabilitySimilarQueryModel)
            .order_by(CapabilitySimilarQueryModel.position,
                      CapabilitySimilarQueryModel.id)
        )).scalars().all()
        neg = (await session.execute(
            select(CapabilityNegativeModel)
            .order_by(CapabilityNegativeModel.capability_id,
                      CapabilityNegativeModel.position)
        )).scalars().all()
    return _hydrate(caps, std, sim, neg)


async def get_capability(capability_id: str, *,
                         session_factory: Any = None) -> CapabilityEntry | None:
    factory = _factory(session_factory)
    async with factory() as session:
        row = (
            await session.execute(
                select(CapabilityModel).where(
                    CapabilityModel.capability_id == capability_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        std = (await session.execute(
            select(CapabilityStandardQueryModel)
            .where(CapabilityStandardQueryModel.capability_id == capability_id)
            .order_by(CapabilityStandardQueryModel.position)
        )).scalars().all()
        std_ids = [r.id for r in std]
        sim = (await session.execute(
            select(CapabilitySimilarQueryModel)
            .where(CapabilitySimilarQueryModel.standard_query_id.in_(std_ids))
            .order_by(CapabilitySimilarQueryModel.position)
        )).scalars().all() if std_ids else []
        neg = (await session.execute(
            select(CapabilityNegativeModel)
            .where(CapabilityNegativeModel.capability_id == capability_id)
            .order_by(CapabilityNegativeModel.position)
        )).scalars().all()
    return _hydrate([row], std, sim, neg)[0]


def _hydrate(caps, std, sim, neg) -> list[CapabilityEntry]:
    std_by_cap: dict[str, list] = {}
    sim_by_std: dict[str, list] = {}
    neg_by_cap: dict[str, list] = {}
    std_id_to_cap: dict[str, str] = {}
    for r in std:
        std_by_cap.setdefault(r.capability_id, []).append(r)
        std_id_to_cap[str(r.id)] = r.capability_id
    for r in sim:
        sim_by_std.setdefault(str(r.standard_query_id), []).append(r)
    for r in neg:
        neg_by_cap.setdefault(r.capability_id, []).append(r)
    out: list[CapabilityEntry] = []
    for c in caps:
        cap_std = std_by_cap.get(c.capability_id, [])
        cap_sim = [s for r in cap_std for s in sim_by_std.get(str(r.id), [])]
        out.append(CapabilityEntry.from_rows(
            c, cap_std, cap_sim, neg_by_cap.get(c.capability_id, [])))
    return out


async def create_capability(entry: CapabilityEntry, *,
                            session_factory: Any = None) -> CapabilityEntry:
    """Insert a new capability row (intent information; the corpus is added
    afterwards through the query plane). Duplicate capability_id ->
    RegistryConflictError."""
    async with _factory(session_factory)() as session:
        session.add(CapabilityModel(
            capability_id=entry.capability_id,
            tool_binding=entry.tool_binding,
            description=entry.description,
            patterns=list(entry.patterns),
            aliases=list(entry.aliases),
            request_query_examples=list(entry.request_query_examples),
            parameters=dict(entry.parameters),
            arg_slots=dict(entry.arg_slots),
            permissions=entry.permissions,
            execution_policy=entry.execution_policy,
            intent_kind=entry.intent_kind,
            enabled=entry.enabled,
            status=entry.status,
            replacement_capability_id=entry.replacement_capability_id,
            row_version=0,
        ))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise RegistryConflictError(
                f"capability {entry.capability_id!r} already exists"
            ) from exc
    invalidate_cache()
    return await get_capability(entry.capability_id, session_factory=session_factory)  # type: ignore[return-value]


async def update_capability(
    capability_id: str,
    patch: dict,
    expected_row_version: int,
    *,
    session_factory: Any = None,
) -> CapabilityEntry:
    """Optimistic-concurrency capability edit: the UPDATE only lands if the
    stored row_version still equals ``expected_row_version``; a miss means
    somebody else wrote first (or the row is gone) — never a silent overwrite."""
    unknown = set(patch) - CAPABILITY_PATCH_FIELDS
    if unknown:
        raise ValueError(f"non-capability fields in patch: {sorted(unknown)}")
    values: dict[str, Any] = dict(patch)
    for k in _LIST_FIELDS & set(values):
        values[k] = list(values[k])
    async with _factory(session_factory)() as session:
        values.update(row_version=expected_row_version + 1, updated_at=func.now())
        res = await session.execute(
            update(CapabilityModel)
            .where(
                CapabilityModel.capability_id == capability_id,
                CapabilityModel.row_version == expected_row_version,
            )
            .values(**values)
        )
        if res.rowcount == 0:
            exists = (
                await session.execute(
                    select(CapabilityModel.capability_id).where(
                        CapabilityModel.capability_id == capability_id
                    )
                )
            ).scalar_one_or_none()
            await session.rollback()
            if exists is None:
                raise RegistryNotFoundError(f"no capability {capability_id!r}")
            raise RegistryConflictError(
                f"capability {capability_id!r} changed under you "
                f"(expected row_version {expected_row_version})"
            )
        await session.commit()
    invalidate_cache()
    return await get_capability(capability_id, session_factory=session_factory)  # type: ignore[return-value]


# ── Content fingerprint + history snapshots ──────────────────────────────────────

def content_fingerprint(entries: Sequence[CapabilityEntry]) -> str:
    """Content-address the live corpus (row ids and row_version excluded, so
    the fingerprint says WHAT, not WHICH row). Structure is hashed in."""
    canon = json.dumps(
        [e.to_payload() for e in sorted(entries, key=lambda e: e.capability_id)],
        ensure_ascii=False, sort_keys=True,
    )
    return "live1-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


async def snapshot_history(
    entries: Sequence[CapabilityEntry],
    *,
    actor_user_id: Any = None,
    actor_username: str | None = None,
    note: str | None = None,
    source_version: int | None = None,
    session_factory: Any = None,
) -> int:
    """Append the CURRENT content as an immutable history row BEFORE an admin
    write (registry_versions = audit history only; nothing reads it at runtime).
    Returns the new snapshot number."""
    payload = {"capabilities": [e.to_payload() for e in entries]}
    fingerprint = content_fingerprint(entries)
    factory = _factory(session_factory)
    last_exc: Exception | None = None
    for _attempt in range(3):
        async with factory() as session:
            next_version = (
                await session.execute(select(func.max(RegistryVersionModel.version)))
            ).scalar_one()
            next_version = int(next_version or 0) + 1
            session.add(RegistryVersionModel(
                version=next_version,
                state="historical",
                payload=payload,
                fingerprint=fingerprint,
                source_version=source_version,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                note=note,
            ))
            try:
                await session.commit()
            except IntegrityError as exc:  # PK race — retry with the recomputed max
                await session.rollback()
                last_exc = exc
                continue
            return next_version
    raise RegistryConflictError("could not write a history snapshot") from last_exc


async def get_version(version: int, *, session_factory: Any = None) -> dict:
    """One history row, read-only (admin listing)."""
    async with _factory(session_factory)() as session:
        row = await session.get(RegistryVersionModel, version)
        if row is None:
            raise RegistryNotFoundError(f"no registry history version {version}")
        return _version_dict(row)


async def version_entries(version: int, *,
                          session_factory: Any = None) -> list[CapabilityEntry]:
    """The snapshot's full entry set — the feed for rollback (restore +
    re-embed). Rebuilt via ``from_payload``: row ids/row_version are live-table
    bookkeeping and are re-created, not restored."""
    async with _factory(session_factory)() as session:
        row = await session.get(RegistryVersionModel, version)
        if row is None:
            raise RegistryNotFoundError(f"no registry history version {version}")
        caps = (row.payload or {}).get("capabilities") or ()
    return [CapabilityEntry.from_payload(c) for c in caps]


async def list_versions(*, limit: int = 50,
                        session_factory: Any = None) -> list[dict]:
    async with _factory(session_factory)() as session:
        rows = (
            await session.execute(
                select(RegistryVersionModel)
                .order_by(RegistryVersionModel.version.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [_version_dict(r) for r in rows]


def _version_dict(row: RegistryVersionModel) -> dict:
    caps = (row.payload or {}).get("capabilities") or ()
    return {
        "version": int(row.version),
        "state": row.state,
        "fingerprint": row.fingerprint,
        "source_version": row.source_version,
        "actor_username": row.actor_username,
        "note": row.note,
        "error": row.error,
        "capabilities": len(caps),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
    }


# ── Audit trail (registry_audit) ─────────────────────────────────────────────────

async def audit(
    action: str,
    *,
    actor_username: str | None = None,
    target: str | None = None,
    ok: bool = True,
    detail: dict | None = None,
    session_factory: Any = None,
) -> None:
    """Append one admin-plane record. Fire-and-forget at call sites: the audit
    write must never mask the operation it describes."""
    async with _factory(session_factory)() as session:
        session.add(RegistryAuditModel(
            action=action, actor_username=actor_username, target=target,
            ok=ok, detail=detail or {},
        ))
        await session.commit()


async def list_audit(*, limit: int = 100,
                     session_factory: Any = None) -> list[dict]:
    async with _factory(session_factory)() as session:
        rows = (
            await session.execute(
                select(RegistryAuditModel).order_by(
                    RegistryAuditModel.created_at.desc()
                ).limit(limit)
            )
        ).scalars().all()
        return [
            {
                "action": r.action,
                "actor_username": r.actor_username,
                "target": r.target,
                "ok": r.ok,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


# ── Runtime read: the live view, cheaply ─────────────────────────────────────────

# Coherence doctrine: a tiny per-call marker (row counts + max(updated_at) over
# the four live tables) detects any change — including writes from ANOTHER
# process — and the full hydrate + fingerprint run only when the marker moves.
# invalidate_cache() additionally covers the same process without even the
# marker round-trip after an admin write.
_VIEW_MARKER_SQL = sql_text(
    "SELECT"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capabilities),"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capability_standard_queries),"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capability_similar_queries),"
    " (SELECT count(*) || ':' || coalesce(max(updated_at)::text, '-') FROM capability_negatives)"
)

_live_cache: tuple[str, RegistryLiveView] | None = None  # (marker, view)


def invalidate_cache() -> None:
    global _live_cache
    _live_cache = None


async def load_live_view(*, session_factory: Any = None) -> RegistryLiveView:
    """Full hydrate + content fingerprint (no cache): the admin write paths
    use this to snapshot history before mutating."""
    factory = _factory(session_factory)
    async with factory() as session:
        caps = (
            await session.execute(
                select(CapabilityModel).order_by(CapabilityModel.capability_id)
            )
        ).scalars().all()
        std = (await session.execute(select(CapabilityStandardQueryModel))).scalars().all()
        sim = (await session.execute(select(CapabilitySimilarQueryModel))).scalars().all()
        neg = (await session.execute(select(CapabilityNegativeModel))).scalars().all()
    entries = _hydrate(caps,
                       sorted(std, key=lambda r: (r.capability_id, r.position)),
                       sorted(sim, key=lambda r: (str(r.standard_query_id), r.position)),
                       sorted(neg, key=lambda r: (r.capability_id, r.position)))
    return RegistryLiveView(fingerprint=content_fingerprint(entries), entries=entries)


async def active_view(*, session_factory: Any = None) -> RegistryLiveView | None:
    """The runtime read model over the live tables, or None when no capability
    exists at all (nothing to consult). Raises on a DB fault — the funnel's
    fail-open wrapper turns that into REGISTRY_UNAVAILABLE."""
    global _live_cache
    factory = _factory(session_factory)
    async with factory() as session:
        marker_row = (await session.execute(_VIEW_MARKER_SQL)).first()
    marker = "|".join(str(x) for x in (marker_row or ()))
    if _live_cache is not None and _live_cache[0] == marker:
        return _live_cache[1]
    view = await load_live_view(session_factory=factory)
    if not view.entries:
        _live_cache = None
        return None
    _live_cache = (marker, view)
    return view


# ── Embedding corpus status (honest reporting for Recall / backfill) ─────────────

async def embedding_status(*, session_factory: Any = None) -> dict:
    """Per-table embedding census + the recorded embedding-profile fingerprint
    (model/provider/dim) written by the backfill script."""
    factory = _factory(session_factory)
    async with factory() as session:
        std_total, std_done = (await session.execute(sql_text(
            "SELECT count(*), count(embedding) FROM capability_standard_queries"
        ))).first()
        sim_total, sim_done = (await session.execute(sql_text(
            "SELECT count(*), count(embedding) FROM capability_similar_queries"
        ))).first()
        profile = await get_setting(session, "embedding_profile")
    return {
        "standard": {"total": int(std_total), "embedded": int(std_done)},
        "similar": {"total": int(sim_total), "embedded": int(sim_done)},
        "embedding_profile": profile,
    }
