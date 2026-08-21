"""Shared visibility predicate for tenant isolation across drive assets.

Three channels grant a user access to an asset:
1. ownership (``assets.user_id`` = me)
2. workspace membership (my workspace_id is in my workspace list)
3. asset-level ACL (granted to me, or public link with grantee NULL)

The predicate is expressed twice: as a SQLAlchemy expression (for ORM selects) and as a raw
SQL fragment (for the tsvector / pgvector recall queries that run raw SQL).
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select

from core.infrastructure.db import (
    AssetAclModel,
    AssetModel,
    FolderModel,
    WorkspaceMemberModel,
    WorkspaceModel,
)


def _visible_workspaces(user_id: UUID):
    """Workspace ids the user may access: those they own OR are a member of.

    The owner is NOT a row in ``workspace_members`` (ownership lives on
    ``workspaces.owner_id``), so membership alone would hide the owner's own
    workspaces from them — and with them every file a member uploads.
    """
    return select(WorkspaceModel.id).where(WorkspaceModel.owner_id == user_id).union(
        select(WorkspaceMemberModel.workspace_id).where(
            WorkspaceMemberModel.user_id == user_id
        )
    )


def folder_visible_expr(user_id: UUID):
    """SQLAlchemy boolean expression selecting ``folders`` rows the user may see.

    A folder is visible if the user created it (personal drive) or the workspace that
    scopes it is visible to them (owned or membership).
    """
    return or_(
        FolderModel.user_id == user_id,
        FolderModel.workspace_id.in_(_visible_workspaces(user_id)),
    )


def asset_visible_expr(user_id: UUID):
    """SQLAlchemy boolean expression selecting ``assets`` rows the user may see."""
    acl_ok = select(AssetAclModel.asset_id).where(
        or_(AssetAclModel.grantee_user_id == user_id, AssetAclModel.grantee_user_id.is_(None))
    )
    return or_(
        AssetModel.user_id == user_id,
        AssetModel.workspace_id.in_(_visible_workspaces(user_id)),
        AssetModel.id.in_(acl_ok),
    )


def asset_visibility_sql(user_id: UUID, chunk_alias: str = "c") -> str:
    """Raw SQL predicate over a ``chunks`` row aliased ``chunk_alias`` (bound param ``:uid``).

    Used by the keyword/vector recallers that run raw SQL against the ``chunks`` table.
    """
    return (
        f"{chunk_alias}.user_id = :uid "
        f"OR {chunk_alias}.workspace_id IN "
        f"(SELECT workspace_id FROM workspace_members WHERE user_id = :uid) "
        f"OR {chunk_alias}.workspace_id IN (SELECT id FROM workspaces WHERE owner_id = :uid) "
        f"OR EXISTS (SELECT 1 FROM asset_acl x WHERE x.asset_id = {chunk_alias}.asset_id "
        f"AND (x.grantee_user_id = :uid OR x.grantee_user_id IS NULL))"
    )
