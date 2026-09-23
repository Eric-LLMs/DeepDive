"""Intent Registry console (admin): drafts CRUD, validate/preview, publish,
version history and rollback (QIR P1 step 4).

Permission posture (P1 ruling 5, ratified): NO new RBAC framework — every route
here rides the existing console admin gate (``require_admin``), and the admin
username is recorded as the audit actor on every mutation. Fine-grained
EDIT/VALIDATE/PUBLISH/ROLLBACK separation is deferred; §8.18's hard constraint
is met: ordinary users cannot reach this router at all, and Publish/Rollback
(plus rejected attempts, which leave no version row) are audit-recorded.
"""
from __future__ import annotations

from api.auth import AuthAdmin, require_admin
from api.deps import _embedder
from api.schemas import (
    RegistryDraftCreateRequest,
    RegistryDraftUpdateRequest,
    RegistryPublishRequest,
    RegistryRollbackRequest,
)
from core.application.chat.intent_funnel.registry import (
    CapabilityEntry,
    PublishRejectedError,
    RegistryConflictError,
    RegistryError,
    RegistryNotFoundError,
    RegistryStateError,
    audit,
    create_draft,
    get_version,
    list_audit,
    list_drafts,
    list_versions,
    preview_draft,
    publish_draft,
    rollback,
    update_draft,
)
from core.infrastructure.db import SessionLocal
from core.infrastructure.request_context import (
    reset_request_execution_mode,
    set_request_execution_mode,
)
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["registry-admin"])


def _entry_json(e: CapabilityEntry) -> dict:
    return {**e.to_payload(), "row_version": e.row_version}


# ── drafts (editable source of truth) ─────────────────────────────────────────────

@router.get("/admin/registry/drafts")
async def get_drafts(_: AuthAdmin = Depends(require_admin)) -> dict:
    return {"drafts": [_entry_json(e) for e in await list_drafts(session_factory=SessionLocal)]}


@router.post("/admin/registry/drafts")
async def post_draft(
    body: RegistryDraftCreateRequest, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    entry = CapabilityEntry(
        capability_id=body.capability_id.strip(),
        tool_binding=body.tool_binding.strip(),
        description=body.description,
        patterns=tuple(body.patterns), aliases=tuple(body.aliases),
        examples=tuple(body.examples), negatives=tuple(body.negatives),
        arg_slots=dict(body.arg_slots), permissions=body.permissions,
        execution_policy=body.execution_policy, intent_kind=body.intent_kind,
        enabled=body.enabled,
        status=body.status, replacement_capability_id=body.replacement_capability_id,
    )
    try:
        created = await create_draft(entry, session_factory=SessionLocal)
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("draft_create", actor_username=admin.username,
                target=body.capability_id, session_factory=SessionLocal)
    return {"draft": _entry_json(created)}


@router.patch("/admin/registry/drafts/{capability_id}")
async def patch_draft(
    capability_id: str, body: RegistryDraftUpdateRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    try:
        updated = await update_draft(
            capability_id, body.patch, body.expected_row_version,
            session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:  # non-draft field in patch
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit("draft_update", actor_username=admin.username, target=capability_id,
                detail={"patch": body.patch, "row_version": updated.row_version},
                session_factory=SessionLocal)
    return {"draft": _entry_json(updated)}


# ── validate / preview / publish / rollback ───────────────────────────────────────

@router.get("/admin/registry/preview")
async def get_preview(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Dry-run the publish BUILD (validation + real embeddings) with zero writes.
    8.5's full-funnel query preview rides on top once step 5 marks execution_mode."""
    drafts = await list_drafts(session_factory=SessionLocal)
    # 8.14: the build's embedding calls are preview usage, not anyone's bill.
    token = set_request_execution_mode("preview")
    try:
        issues, snap = await preview_draft(drafts, _embedder())
    finally:
        reset_request_execution_mode(token)
    return {
        "issues": issues,
        "capabilities": len(drafts),
        "snapshot_version": snap.version if snap else None,
        "routable": len(snap.capabilities) if snap else 0,
    }


@router.post("/admin/registry/publish")
async def post_publish(
    body: RegistryPublishRequest, admin: AuthAdmin = Depends(require_admin)
) -> dict:
    try:
        view = await publish_draft(
            _embedder(), actor_username=admin.username, note=body.note,
            session_factory=SessionLocal,
        )
    except PublishRejectedError as exc:
        # the rejection itself is the audit-worthy event (no version row exists)
        await audit("publish_rejected", actor_username=admin.username, ok=False,
                    detail={"issues": exc.issues}, session_factory=SessionLocal)
        raise HTTPException(status_code=422, detail={"issues": exc.issues}) from exc
    except (RegistryConflictError, RegistryStateError) as exc:
        await audit("publish", actor_username=admin.username, ok=False,
                    detail={"error": str(exc)}, session_factory=SessionLocal)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("publish", actor_username=admin.username, target=f"v{view.version}",
                detail={"fingerprint": view.fingerprint, "capabilities": len(view.entries),
                        "note": body.note}, session_factory=SessionLocal)
    return {"version": view.version, "state": view.state, "fingerprint": view.fingerprint}


@router.get("/admin/registry/versions")
async def get_versions(_: AuthAdmin = Depends(require_admin)) -> dict:
    versions = await list_versions(session_factory=SessionLocal)
    return {
        "versions": [
            {
                "version": v.version, "state": v.state, "fingerprint": v.fingerprint,
                "source_version": v.source_version, "actor_username": v.actor_username,
                "note": v.note, "error": v.error,
                "capabilities": len(v.entries),
                "routable": len(v.capabilities),
            }
            for v in versions
        ]
    }


@router.get("/admin/registry/versions/{version}")
async def get_one_version(version: int, _: AuthAdmin = Depends(require_admin)) -> dict:
    try:
        v = await get_version(version, session_factory=SessionLocal)
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "version": v.version, "state": v.state, "fingerprint": v.fingerprint,
        "source_version": v.source_version, "actor_username": v.actor_username,
        "note": v.note, "error": v.error,
        "entries": [e.to_payload() for e in v.entries],
    }


@router.post("/admin/registry/versions/{version}/rollback")
async def post_rollback(
    version: int, body: RegistryRollbackRequest,
    admin: AuthAdmin = Depends(require_admin),
) -> dict:
    try:
        view = await rollback(
            version, actor_username=admin.username, session_factory=SessionLocal,
        )
    except RegistryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RegistryConflictError, RegistryStateError, RegistryError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await audit("rollback", actor_username=admin.username, target=f"v{view.version}",
                detail={"from_version": version, "fingerprint": view.fingerprint},
                session_factory=SessionLocal)
    return {"version": view.version, "state": view.state, "source_version": version}


# ── audit read (admin visibility of the trail itself) ─────────────────────────────

@router.get("/admin/registry/audit")
async def get_audit(
    limit: int = 100, _: AuthAdmin = Depends(require_admin),
) -> dict:
    return {"entries": await list_audit(limit=min(limit, 500), session_factory=SessionLocal)}
