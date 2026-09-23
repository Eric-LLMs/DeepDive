"""Publish pipeline — Validate / Preview / Build-Then-Swap (QIR P1 step 2).

The fixed flow of docs/temp.md 8.2/8.3, as one callable:

    Draft -> validate -> [preview: build outside any tx, zero writes]
          -> stage registry version -> SHORT TRANSACTION:
               old active -> superseded | staged -> active | qir index pair
          -> activated  (or: staged -> FAILED, old active keeps serving)

Two properties the code is built to guarantee, not just document:

* NOTHING is written until every derived artifact (embeddings, snapshot) built
  successfully — an embedding failure costs zero DB rows (ruling 7: the live
  Registry vN + Index vN pair keeps serving; there is no vN+1/vN mix).
* the registry version swap and the QIR active-snapshot pair land in the SAME
  commit (``qir.store.write_active`` on the caller's session), so "Registry v43
  with Index v42" is not a state the database can ever show.

Validation is stricter than qir's snapshot gate because Registry rows carry the
extra vocabulary (patterns/aliases/arg_slots/policy): a draft that only QIR
would accept can still be rejected here (8.4).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from sqlalchemy import func, update

from core.application.chat.actions import DIRECT_TOOLS
from core.application.chat.qir import store as qir_store
from core.application.chat.qir.snapshot import build_snapshot
from core.application.chat.qir.types import Snapshot
from core.infrastructure.db import RegistryVersionModel

from .store import (
    RegistryError,
    RegistryStateError,
    STATE_ACTIVE,
    STATE_STAGED,
    STATE_SUPERSEDED,
    RegistryVersionView,
    _factory,
    get_version,
    invalidate_cache,
    mark_failed,
    stage_version,
)
from .types import (
    RE_PREFIX,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DISABLED,
    VALID_KINDS,
    CapabilityEntry,
)

logger = logging.getLogger(__name__)

# arg_slots.source minimal enum (P1 ruling 2 — no speculative additions).
# plugin:<name> is the seventh form: the table registers WHICH extractor, the
# extractor itself stays in code (8.1-b).
VALID_SOURCES = frozenset({
    "user_input", "viewer.current_page", "viewer.selection",
    "attachment", "turn_context", "fixed",
})
VALID_POLICIES = frozenset({"auto", "approval", "sandbox"})
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED, STATUS_DEPRECATED})


class PublishRejectedError(RegistryError):
    """Publish gate closed: the draft set failed validation. Carries every issue
    (not just the first) so the editor fixes one round, not one page."""

    def __init__(self, issues: Sequence[str]):
        super().__init__("; ".join(issues))
        self.issues = list(issues)


# ── Validate (8.4) ────────────────────────────────────────────────────────────────

def validate_entries(entries: Sequence[CapabilityEntry]) -> list[str]:
    """Every rule of 8.4 as a pure function; empty list == publishable."""
    issues: list[str] = []
    if not entries:
        return ["refusing to publish an empty Registry"]
    seen: set[str] = set()
    for e in entries:
        cid = e.capability_id.strip()
        if not cid:
            issues.append("capability_id must not be blank")
            continue
        if cid in seen:
            issues.append(f"duplicate capability_id {cid!r}")
            continue
        seen.add(cid)
        if not e.tool_binding.strip():
            issues.append(f"{cid}: tool_binding is required")
        elif e.tool_binding not in DIRECT_TOOLS:
            issues.append(
                f"{cid}: tool_binding {e.tool_binding!r} is not an existing DIRECT_TOOLS "
                "binding (the Registry cannot invent executables)"
            )
        if not e.description.strip():
            issues.append(f"{cid}: description is required (judge prompt source)")
        if not [x for x in e.examples if str(x).strip()]:
            issues.append(f"{cid}: recall examples must be non-empty and indexable")
        if e.status not in VALID_STATUSES:
            issues.append(f"{cid}: status {e.status!r} not in {sorted(VALID_STATUSES)}")
        if e.enabled and e.status != STATUS_ACTIVE:
            issues.append(
                f"{cid}: enabled=True contradicts status={e.status!r} "
                "(disable by flipping status/enabled consistently, ruling 4)"
            )
        if e.replacement_capability_id:
            if e.status != STATUS_DEPRECATED:
                issues.append(f"{cid}: replacement_capability_id is only for deprecated entries")
            else:
                r = e.replacement_capability_id.strip()
                if r not in seen and all(o.capability_id.strip() != r for o in entries):
                    issues.append(f"{cid}: replacement {r!r} is not a capability in this set")
        if e.execution_policy not in VALID_POLICIES:
            issues.append(f"{cid}: execution_policy {e.execution_policy!r} not in {sorted(VALID_POLICIES)}")
        if e.intent_kind not in VALID_KINDS:
            issues.append(f"{cid}: intent_kind {e.intent_kind!r} not in {sorted(VALID_KINDS)}")
        if any(ch.isspace() for ch in e.permissions):
            issues.append(f"{cid}: permissions must be a single token (or empty)")
        for lit in (*e.patterns, *e.aliases):
            s = str(lit).strip()
            if s.startswith(RE_PREFIX):
                try:
                    re.compile(s[len(RE_PREFIX):])
                except re.error as exc:
                    issues.append(f"{cid}: un-compilable regex pattern {s!r}: {exc}")
        for slot, source in e.arg_slots.items():
            issues.extend(
                f"{cid}: arg_slots[{slot!r}] {msg}" for msg in _slot_issues(source)
            )
    issues.extend(_pattern_conflicts(entries))
    return issues


def _slot_issues(source: Any) -> list[str]:
    src = source.get("source") if isinstance(source, dict) else source
    if not isinstance(src, str) or not src:
        return ["source must be a string or {source: str}"]
    if src in VALID_SOURCES:
        return []
    if src.startswith("plugin:"):
        return [] if src[len("plugin:"):].strip() else ["plugin: source needs a name"]
    return [
        f"source {src!r} outside the minimal enum "
        f"{sorted(VALID_SOURCES)} or plugin:<name>"
    ]


def _routable(e: CapabilityEntry) -> bool:
    return e.enabled and e.status == STATUS_ACTIVE


def _pattern_conflicts(entries: Sequence[CapabilityEntry]) -> list[str]:
    """Deterministic-match collision: the same literal in two ROUTABLE capabilities
    makes the Matcher ambiguous by construction — reject at the gate instead."""
    owner: dict[str, str] = {}
    issues: list[str] = []
    for e in entries:
        if not _routable(e):
            continue
        for lit in (*e.patterns, *e.aliases):
            lit = str(lit).strip()
            if not lit:
                issues.append(f"{e.capability_id}: blank pattern/alias entry")
                continue
            prev = owner.setdefault(lit, e.capability_id)
            if prev != e.capability_id:
                issues.append(
                    f"deterministic conflict: {lit!r} claimed by {prev!r} and "
                    f"{e.capability_id!r}"
                )
    return issues


def to_qir_draft(entries: Sequence[CapabilityEntry]) -> dict:
    """Project Registry rows into the qir snapshot-builder's draft shape (the
    routing subset only — 8.4 gate already ran, qir re-checks its own structure)."""
    return {
        "capabilities": [
            {
                "id": e.capability_id,
                "tool_binding": e.tool_binding,
                "description": e.description,
                "examples": list(e.examples),
                "negatives": list(e.negatives),
                "enabled": e.enabled and e.status == STATUS_ACTIVE,
            }
            for e in entries
        ]
    }


# ── Preview (8.5, primitive) ──────────────────────────────────────────────────────

async def preview_draft(
    entries: Sequence[CapabilityEntry], embedder,
) -> tuple[list[str], Snapshot | None]:
    """Dry-run the BUILD half of publishing: validation + real embedding cost,
    ZERO writes. The full-funnel query preview (Matcher->...->Route) rides on the
    returned snapshot once the step-3 Matcher reads the Registry; tool execution
    is never in reach of this function (8.8: intent nodes hold no execution)."""
    issues = validate_entries(entries)
    if issues:
        return issues, None
    snap = await build_snapshot(to_qir_draft(entries), embedder)
    return [], snap


# ── Build-Then-Swap publish (8.3) ─────────────────────────────────────────────────

async def publish_draft(
    embedder,
    *,
    drafts: Sequence[CapabilityEntry] | None = None,
    actor_user_id: Any = None,
    actor_username: str | None = None,
    note: str | None = None,
    session_factory: Any = None,
) -> RegistryVersionView:
    """The one publish entry point (Draft table is the source when ``drafts`` is None).

    Raises PublishRejectedError / SnapshotError BEFORE any write; after staging,
    any swap failure marks the new version FAILED (history shows why) and the old
    active pair keeps serving untouched."""
    from .store import list_drafts  # local to keep the import graph flat

    factory = _factory(session_factory)
    entries_list = (
        await list_drafts(session_factory=factory)
        if drafts is None else list(drafts)
    )

    issues = validate_entries(entries_list)
    if issues:
        raise PublishRejectedError(issues)

    # -- outside any transaction: the expensive derived artifact ------------------
    snap = await build_snapshot(to_qir_draft(entries_list), embedder)

    staged = await stage_version(
        entries_list, actor_user_id=actor_user_id, actor_username=actor_username,
        note=note, session_factory=factory,
    )

    # -- the single short transaction: swap BOTH stores together --------------------
    try:
        async with factory() as session:
            await session.execute(
                update(RegistryVersionModel)
                .where(RegistryVersionModel.state == STATE_ACTIVE)
                .values(state=STATE_SUPERSEDED)
            )
            res = await session.execute(
                update(RegistryVersionModel)
                .where(
                    RegistryVersionModel.version == staged.version,
                    RegistryVersionModel.state == STATE_STAGED,
                )
                .values(state=STATE_ACTIVE, activated_at=func.now())
            )
            if res.rowcount == 0:
                raise RegistryStateError(
                    f"version {staged.version} left staged state mid-publish "
                    "(concurrent publish? refusing to half-swap)"
                )
            await qir_store.write_active(session, snap)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - record, keep old active serving, re-raise
        try:
            await mark_failed(
                staged.version, f"activation swap failed: {exc!r}",
                session_factory=factory,
            )
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.exception("registry v%d: could not record the failed activation", staged.version)
        raise
    invalidate_cache()
    logger.info(
        "registry published v%d (qir %s) caps=%d actor=%s",
        staged.version, snap.version, len(entries_list), actor_username,
    )
    return await get_version(staged.version, session_factory=factory)
