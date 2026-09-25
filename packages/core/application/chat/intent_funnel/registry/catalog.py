"""Action Catalog store (migration 0011, Action-Universe ruling 2026-09-25).

Three facts this module enforces by construction:

* The Catalog is INVENTORY, not routing — nothing here writes ``capabilities``,
  ``registry_versions`` or ``qir_examples``. Joining to registered actions is a
  read-time ``tool_binding`` join in the admin API, so no second truth can drift.
* Query drafts are PRE-REGISTRY configuration only. A sentence stored here can
  reach the active index by exactly one path: explicit register (copy into a
  capabilities Draft row) -> Validate -> Publish. There is no API here that
  touches the runtime corpus.
* Registered actions do NOT keep editable drafts: their corpus lives in the
  capabilities Draft row (one lifecycle, one editor per sentence). The router
  rejects draft writes for registered actions before calling into this store.
"""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import delete, select

from core.infrastructure.db import (
    ActionCatalogModel,
    ActionQueryDraftModel,
    ActionQueryDraftItemModel,
    SessionLocal,
)

from .store import RegistryNotFoundError

_KINDS = ("standard", "similar", "negative")


def _factory(session_factory: Any = None):
    return session_factory or SessionLocal


def _row_to_dict(r: ActionCatalogModel) -> dict:
    return {
        "action_key": r.action_key,
        "display_name": r.display_name,
        "description": r.description,
        "tool_binding": r.tool_binding,
        "route": r.route,
        "implementation_ref": r.implementation_ref,
        "status": r.status,
    }


async def list_catalog(*, session_factory: Any = None) -> list[dict]:
    async with _factory(session_factory)() as session:
        rows: Sequence[ActionCatalogModel] = (
            await session.execute(
                select(ActionCatalogModel).order_by(ActionCatalogModel.action_key)
            )
        ).scalars().all()
        return [_row_to_dict(r) for r in rows]


async def get_catalog(action_key: str, *, session_factory: Any = None) -> dict | None:
    async with _factory(session_factory)() as session:
        r = (
            await session.execute(
                select(ActionCatalogModel).where(ActionCatalogModel.action_key == action_key)
            )
        ).scalar_one_or_none()
        return _row_to_dict(r) if r is not None else None


async def draft_counts(session_factory: Any = None) -> dict[str, dict[str, int]]:
    """{action_key: {standard, similar, negative}} for the Universe table's
    Draft-corpus column — one aggregate read, no per-row queries."""
    async with _factory(session_factory)() as session:
        rows = (
            await session.execute(
                select(
                    ActionQueryDraftItemModel.action_key,
                    ActionQueryDraftItemModel.kind,
                ).where(ActionQueryDraftItemModel.enabled.is_(True))
            )
        ).all()
    out: dict[str, dict[str, int]] = {}
    for key, kind in rows:
        d = out.setdefault(key, {"standard": 0, "similar": 0, "negative": 0})
        if kind in d:
            d[kind] += 1
    return out


async def get_query_draft(action_key: str, *, session_factory: Any = None) -> dict:
    """The full Draft Configuration for one action (empty shape when none)."""
    async with _factory(session_factory)() as session:
        head = (
            await session.execute(
                select(ActionQueryDraftModel).where(
                    ActionQueryDraftModel.action_key == action_key
                )
            )
        ).scalar_one_or_none()
        items = (
            await session.execute(
                select(ActionQueryDraftItemModel)
                .where(
                    ActionQueryDraftItemModel.action_key == action_key,
                    ActionQueryDraftItemModel.enabled.is_(True),
                )
                .order_by(
                    ActionQueryDraftItemModel.kind, ActionQueryDraftItemModel.position
                )
            )
        ).scalars().all()
    draft = {"action_key": action_key, "standard": "", "similar": [], "negatives": [],
             "parameters": dict(head.parameters) if head and head.parameters else {},
             "arg_slots": dict(head.arg_slots) if head and head.arg_slots else {},
             "updated_by": head.updated_by if head else None, "exists": head is not None}
    for it in items:
        if it.kind == "standard":
            draft["standard"] = it.text
        elif it.kind == "similar":
            draft["similar"].append(it.text)
        elif it.kind == "negative":
            draft["negatives"].append(it.text)
    return draft


async def upsert_query_draft(
    action_key: str,
    *,
    standard: str,
    similar: Sequence[str],
    negatives: Sequence[str],
    updated_by: str | None = None,
    session_factory: Any = None,
) -> dict:
    """Replace the whole Draft Configuration atomically (head upsert + item
    rewrite). Validation only — content curation stays a human decision."""
    standard = str(standard or "").strip()
    sim = [str(s).strip() for s in (similar or ()) if str(s).strip()]
    neg = [str(n).strip() for n in (negatives or ()) if str(n).strip()]
    if len(standard) > 500:
        raise ValueError("standard query too long (max 500 chars)")
    if len(sim) > 64 or len(neg) > 20:
        raise ValueError("at most 64 similar / 20 negative queries per draft")
    async with _factory(session_factory)() as session:
        cat = (
            await session.execute(
                select(ActionCatalogModel).where(
                    ActionCatalogModel.action_key == action_key
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            raise RegistryNotFoundError(f"action {action_key!r} is not in the Action Catalog")
        head = (
            await session.execute(
                select(ActionQueryDraftModel).where(
                    ActionQueryDraftModel.action_key == action_key
                )
            )
        ).scalar_one_or_none()
        if head is None:
            session.add(ActionQueryDraftModel(action_key=action_key, updated_by=updated_by))
        else:
            head.updated_by = updated_by
        await session.execute(
            delete(ActionQueryDraftItemModel).where(
                ActionQueryDraftItemModel.action_key == action_key
            )
        )
        position = 0
        if standard:
            session.add(ActionQueryDraftItemModel(
                action_key=action_key, kind="standard", text=standard, position=position))
            position += 1
        for i, s in enumerate(sim):
            session.add(ActionQueryDraftItemModel(
                action_key=action_key, kind="similar", text=s, position=i))
        for i, n in enumerate(neg):
            session.add(ActionQueryDraftItemModel(
                action_key=action_key, kind="negative", text=n, position=i))
        await session.commit()
    return await get_query_draft(action_key, session_factory=session_factory)


async def upsert_draft_parameters(
    action_key: str,
    *,
    parameters: dict[str, Any],
    arg_slots: dict[str, Any],
    updated_by: str | None = None,
    session_factory: Any = None,
) -> dict:
    """Replace the pre-Registry parameter configuration (migration 0012).
    Same shapes as CapabilityEntry.parameters / .arg_slots — this stores the
    ADMIN'S configuration, never a copy of the runtime tool schema (that is
    read live via GET /admin/registry/tool-schemas)."""
    if not isinstance(parameters, dict) or not isinstance(arg_slots, dict):
        raise ValueError("parameters and arg_slots must be objects")
    for slot, spec in parameters.items():
        if not isinstance(spec, dict):
            raise ValueError(f"parameters[{slot!r}] must be an object")
    async with _factory(session_factory)() as session:
        cat = (
            await session.execute(
                select(ActionCatalogModel).where(
                    ActionCatalogModel.action_key == action_key
                )
            )
        ).scalar_one_or_none()
        if cat is None:
            raise RegistryNotFoundError(f"action {action_key!r} is not in the Action Catalog")
        head = (
            await session.execute(
                select(ActionQueryDraftModel).where(
                    ActionQueryDraftModel.action_key == action_key
                )
            )
        ).scalar_one_or_none()
        if head is None:
            session.add(ActionQueryDraftModel(
                action_key=action_key, updated_by=updated_by,
                parameters=parameters, arg_slots=arg_slots))
        else:
            head.updated_by = updated_by
            head.parameters = parameters
            head.arg_slots = arg_slots
        await session.commit()
    return await get_query_draft(action_key, session_factory=session_factory)
