"""Action Catalog store (migration 0011, Action-Universe ruling 2026-09-25).

The Catalog is INVENTORY, not routing — nothing here writes ``capabilities``
or the live query tables. Joining to registered actions is a read-time
``tool_binding`` join in the admin API, so no second truth can drift.

Since migration 0014 the pre-Registry Draft Configuration
(action_query_drafts / action_query_draft_items) is gone: not-yet-registered
actions get a capability row created directly from the Catalog entry, and its
corpus is edited through the live query plane (:mod:`.queries`).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from core.infrastructure.db import ActionCatalogModel, SessionLocal


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
