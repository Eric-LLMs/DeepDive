"""Minimal versioned store for the Intent Registry (QIR P1 step 1).

Primitives only — no policy. Step 2 layers Validate/Preview/Build-Then-Swap
(embedding/index build) on top of ``stage_version`` + ``activate_version``;
step 4 wraps these in admin endpoints with audit.

Doctrine pinned here (docs/temp.md 8.2/8.3 + P1 rulings 4 and 7):

* Draft edits are optimistic-concurrency: a writer must present the ``row_version``
  it read, or get ``RegistryConflictError`` — silent overwrite is impossible.
* Publishing stages an IMMUTABLE version row, then ``activate_version`` swaps in a
  short transaction: old active -> superseded, staged -> active. A partial unique
  index (migration 0002) makes a second active a hard DB error.
* Build failure marks the staged version FAILED; the old active keeps serving.
  Cross-version active combos are structurally impossible.
* Rollback never edits history: it stages a NEW version carrying a copy of the old
  payload plus ``source_version`` provenance.
* A disabled/deprecated capability is projected OUT of the runtime view
  (``RegistryVersionView.capabilities``) — it can never be an executable candidate.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from core.infrastructure.db import (
    CapabilityModel,
    RegistryAuditModel,
    RegistryVersionModel,
    SessionLocal,
)

from .types import (
    STATE_ACTIVE,
    STATE_FAILED,
    STATE_STAGED,
    STATE_SUPERSEDED,
    CapabilityEntry,
    RegistryVersionView,
)

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """Base for Registry store failures (admin plane; the runtime read path that
    wires in at step 3 wraps these into fail-open)."""


class RegistryConflictError(RegistryError):
    """Optimistic-concurrency miss or duplicate capability_id."""


class RegistryNotFoundError(RegistryError):
    """Target row (draft capability / registry version) does not exist."""


class RegistryStateError(RegistryError):
    """Illegal state transition (e.g. activating a version that is not staged)."""


def _factory(session_factory: Any = None):
    return session_factory or SessionLocal


# ── Draft (capabilities table) ───────────────────────────────────────────────────

# Columns an editor may patch; capability_id (identity) and row_version (token)
# are not in here on purpose.
DRAFT_PATCH_FIELDS = frozenset({
    "tool_binding", "description", "patterns", "aliases", "examples", "negatives",
    "arg_slots", "permissions", "execution_policy", "enabled", "status",
    "replacement_capability_id",
})

_LIST_FIELDS = frozenset({"patterns", "aliases", "examples", "negatives"})


async def list_drafts(*, session_factory: Any = None) -> list[CapabilityEntry]:
    async with _factory(session_factory)() as session:
        rows = (
            await session.execute(
                select(CapabilityModel).order_by(CapabilityModel.capability_id)
            )
        ).scalars().all()
        return [CapabilityEntry.from_row(r) for r in rows]


async def get_draft(capability_id: str, *, session_factory: Any = None) -> CapabilityEntry | None:
    async with _factory(session_factory)() as session:
        row = (
            await session.execute(
                select(CapabilityModel).where(CapabilityModel.capability_id == capability_id)
            )
        ).scalar_one_or_none()
        return CapabilityEntry.from_row(row) if row is not None else None


async def create_draft(entry: CapabilityEntry, *, session_factory: Any = None) -> CapabilityEntry:
    """Insert a new Draft row. Duplicate capability_id -> RegistryConflictError."""
    async with _factory(session_factory)() as session:
        session.add(CapabilityModel(
            capability_id=entry.capability_id,
            tool_binding=entry.tool_binding,
            description=entry.description,
            patterns=list(entry.patterns),
            aliases=list(entry.aliases),
            examples=list(entry.examples),
            negatives=list(entry.negatives),
            arg_slots=dict(entry.arg_slots),
            permissions=entry.permissions,
            execution_policy=entry.execution_policy,
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
    return await get_draft(entry.capability_id, session_factory=session_factory)  # type: ignore[return-value]


async def update_draft(
    capability_id: str,
    patch: dict,
    expected_row_version: int,
    *,
    session_factory: Any = None,
) -> CapabilityEntry:
    """Optimistic-concurrency Draft edit: the UPDATE only lands if the stored
    row_version still equals ``expected_row_version``; a miss means somebody else
    wrote first (or the row is gone) — never a silent overwrite."""
    unknown = set(patch) - DRAFT_PATCH_FIELDS
    if unknown:
        raise ValueError(f"non-draft fields in patch: {sorted(unknown)}")
    values: dict[str, Any] = dict(patch)
    for k in _LIST_FIELDS & set(values):
        values[k] = list(values[k])
    async with _factory(session_factory)() as session:
        values.update(
            row_version=expected_row_version + 1,
            updated_at=func.now(),
        )
        res = await session.execute(
            update(CapabilityModel)
            .where(
                CapabilityModel.capability_id == capability_id,
                CapabilityModel.row_version == expected_row_version,
            )
            .values(**values)
        )
        if res.rowcount == 0:
            # Distinguish absent-row from concurrent-write for a useful error.
            exists = (
                await session.execute(
                    select(CapabilityModel.capability_id).where(
                        CapabilityModel.capability_id == capability_id
                    )
                )
            ).scalar_one_or_none()
            await session.rollback()
            if exists is None:
                raise RegistryNotFoundError(f"no draft capability {capability_id!r}")
            raise RegistryConflictError(
                f"draft capability {capability_id!r} changed under you "
                f"(expected row_version {expected_row_version})"
            )
        await session.commit()
    return await get_draft(capability_id, session_factory=session_factory)  # type: ignore[return-value]


# ── Versions (registry_versions table): stage -> activate ────────────────────────

def content_fingerprint(entries: Sequence[CapabilityEntry]) -> str:
    """Content-address the capability SET (Draft bookkeeping excluded). Mirrors the
    qir1-/wf1- doctrine: structure is hashed in, storage metadata is not."""
    canon = json.dumps(
        [e.to_payload() for e in sorted(entries, key=lambda e: e.capability_id)],
        ensure_ascii=False, sort_keys=True,
    )
    return "reg1-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def _payload_of(entries: Sequence[CapabilityEntry]) -> dict:
    return {"capabilities": [e.to_payload() for e in entries]}


async def stage_version(
    entries: Sequence[CapabilityEntry],
    *,
    actor_user_id: Any = None,
    actor_username: str | None = None,
    note: str | None = None,
    source_version: int | None = None,
    session_factory: Any = None,
) -> RegistryVersionView:
    """Freeze ``entries`` into a new IMMUTABLE staged version (state=staged).

    Version numbers are monotonically increasing; a concurrent stager racing for
    the same number loses on the integer PK and retries with the next value."""
    if not entries:
        raise ValueError("refusing to stage an empty Registry version")
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
                state=STATE_STAGED,
                payload=_payload_of(entries),
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
            logger.info(
                "registry staged v%d (%s) caps=%d actor=%s",
                next_version, fingerprint, len(entries), actor_username,
            )
            row = await session.get(RegistryVersionModel, next_version)
            return RegistryVersionView.from_row(row)
    raise RegistryConflictError("could not stage a version (concurrent writers)") from last_exc


async def mark_failed(version: int, error: str, *, session_factory: Any = None) -> None:
    """Record a failed build: staged -> failed. The old active keeps serving
    (ruling 7: a failed new version never disturbs live traffic)."""
    async with _factory(session_factory)() as session:
        res = await session.execute(
            update(RegistryVersionModel)
            .where(
                RegistryVersionModel.version == version,
                RegistryVersionModel.state == STATE_STAGED,
            )
            .values(state=STATE_FAILED, error=error)
        )
        if res.rowcount == 0:
            row = await session.get(RegistryVersionModel, version)
            await session.rollback()
            if row is None:
                raise RegistryNotFoundError(f"no registry version {version}")
            raise RegistryStateError(
                f"version {version} is {row.state!r}, only staged builds can fail"
            )
        await session.commit()


async def activate_version(version: int, *, session_factory: Any = None) -> RegistryVersionView:
    """Atomic Build-Then-Swap: old active -> superseded, staged -> active, in ONE
    transaction. The partial unique index makes a concurrent double-activate a DB
    error; we surface it as RegistryStateError and the caller retries the read."""
    factory = _factory(session_factory)
    async with factory() as session:
        try:
            await session.execute(
                update(RegistryVersionModel)
                .where(RegistryVersionModel.state == STATE_ACTIVE)
                .values(state=STATE_SUPERSEDED)
            )
            res = await session.execute(
                update(RegistryVersionModel)
                .where(
                    RegistryVersionModel.version == version,
                    RegistryVersionModel.state == STATE_STAGED,
                )
                .values(state=STATE_ACTIVE, activated_at=func.now())
            )
            if res.rowcount == 0:
                await session.rollback()
                row = await session.get(RegistryVersionModel, version)
                if row is None:
                    raise RegistryNotFoundError(f"no registry version {version}")
                raise RegistryStateError(
                    f"version {version} is {row.state!r}, only staged versions activate"
                )
            await session.commit()
        except IntegrityError as exc:
            # The single-active partial index rejected the swap (another writer
            # activated concurrently). Nothing half-published: this tx is dead.
            await session.rollback()
            raise RegistryStateError(
                "concurrent activation blocked by the single-active invariant"
            ) from exc
    invalidate_cache()
    view = await get_version(version, session_factory=factory)
    logger.info("registry activated v%d caps=%d", view.version, len(view.capabilities))
    return view


async def get_version(version: int, *, session_factory: Any = None) -> RegistryVersionView:
    async with _factory(session_factory)() as session:
        row = await session.get(RegistryVersionModel, version)
        if row is None:
            raise RegistryNotFoundError(f"no registry version {version}")
        return RegistryVersionView.from_row(row)


async def list_versions(*, limit: int = 50, session_factory: Any = None) -> list[RegistryVersionView]:
    async with _factory(session_factory)() as session:
        rows = (
            await session.execute(
                select(RegistryVersionModel)
                .order_by(RegistryVersionModel.version.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [RegistryVersionView.from_row(r) for r in rows]


async def rollback(
    to_version: int,
    *,
    actor_user_id: Any = None,
    actor_username: str | None = None,
    session_factory: Any = None,
) -> RegistryVersionView:
    """Rollback = re-PUBLISH an old payload as a NEW version (8.2: history is never
    edited). Provenance is kept via ``source_version``."""
    factory = _factory(session_factory)
    target = await get_version(to_version, session_factory=factory)
    staged = await stage_version(
        target.entries,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        note=f"rollback to v{to_version}",
        source_version=to_version,
        session_factory=factory,
    )
    return await activate_version(staged.version, session_factory=factory)


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
    write must never mask the operation it describes, so callers wrap it in their
    own try/except if they cannot afford a 500 from the log line."""
    async with _factory(session_factory)() as session:
        session.add(RegistryAuditModel(
            action=action, actor_username=actor_username, target=target,
            ok=ok, detail=detail or {},
        ))
        await session.commit()


async def list_audit(*, limit: int = 100, session_factory: Any = None) -> list[dict]:
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


# ── Runtime read: the active version, cheaply ────────────────────────────────────

# Process cache keyed on the active VERSION NUMBER (tiny indexed read per call);
# the full payload is loaded only when the number changes. Same coherence doctrine
# as qir/store: a stale cache can only DENY a route, and the executor re-validates
# right before dispatch.
_active_cache: RegistryVersionView | None = None


def invalidate_cache() -> None:
    global _active_cache
    _active_cache = None


async def active_view(*, session_factory: Any = None) -> RegistryVersionView | None:
    """The active published version, or None (nothing published yet). Raises on a
    DB fault — the runtime fail-open wrapper arrives with the step-3 Matcher read."""
    global _active_cache
    factory = _factory(session_factory)
    async with factory() as session:
        marker = (
            await session.execute(
                select(RegistryVersionModel.version, RegistryVersionModel.fingerprint)
                .where(RegistryVersionModel.state == STATE_ACTIVE)
            )
        ).first()
        if marker is None:
            _active_cache = None
            return None
        version, fingerprint = int(marker[0]), str(marker[1])
        if _active_cache is not None and _active_cache.version == version \
                and _active_cache.fingerprint == fingerprint:
            return _active_cache
        row = await session.get(RegistryVersionModel, version)
        view = RegistryVersionView.from_row(row)
        _active_cache = view
        return view
