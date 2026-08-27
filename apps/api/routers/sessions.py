"""Chat-session management + agent-operations routes: list/read/rename/delete sessions,
delete a single message, resolve a human-in-the-loop approval, revert a workspace checkpoint.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from agent.security.approvals import get_approval_bridge
from agent.tools.checkpoints import CheckpointError
from api.auth import AuthUser, require_user
from api.deps import get_agent
from api.schemas import ApprovalResolveRequest, SessionRenameRequest
from core.infrastructure.db import ChunkModel, MessageModel, SessionLocal, SessionModel
from core.infrastructure.memory import list_sessions, load_session_detail
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, update

router = APIRouter(tags=["sessions"])


async def _backfill_imported_rag(session_factory, user_id: UUID, session_id: UUID) -> None:
    """One-time on-read conversion: derive ``imported_rag`` flags from pre-flag chat chunks.

    Before the per-message flag existed, imports were tracked only in chunk meta: single-pair
    chunks keyed by the user-message id (``kind='qa'`` without ``covered``), whole-session
    chunks carrying ``meta['covered']``, and the even-older ``kind='session-qa'`` blanket.
    Reconstructing the flags here lets an already-imported session keep its persistent
    "✓ Imported" state right after the upgrade, without forcing a re-import. If every current
    user message is covered (or a legacy blanket chunk exists) the whole transcript is in the
    repo, so every message gets flagged; otherwise only the covered user messages do (a
    regenerated assistant reply has no chunk identity to recover, and clicking its button is
    an idempotent re-import). Idempotent — a message already flagged stays flagged.
    """
    async with session_factory() as session:
        chunk_rows = (
            await session.execute(
                select(ChunkModel.source_id, ChunkModel.meta).where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user_id,
                    ChunkModel.meta["session_id"].astext == str(session_id),
                )
            )
        ).all()
        msg_rows = (
            await session.execute(
                select(MessageModel.id, MessageModel.role, MessageModel.imported_rag)
                .where(MessageModel.session_id == session_id)
            )
        ).all()
    covered: set[str] = set()
    legacy_blanket = False
    for source_id, meta in chunk_rows:
        meta = meta or {}
        kind = meta.get("kind")
        if kind == "session-qa":
            legacy_blanket = True
        elif kind == "qa":
            cov = meta.get("covered")
            if isinstance(cov, list) and cov:
                covered.update(str(c) for c in cov)
            else:
                covered.add(source_id)  # legacy single-pair: source_id is the user-message id
    if not covered and not legacy_blanket:
        return
    already = {str(mid) for mid, _, flag in msg_rows if flag}
    user_ids = [str(mid) for mid, role, _ in msg_rows if role == "user"]
    if legacy_blanket or covered.issuperset(user_ids):
        candidates = [str(mid) for mid, _, _ in msg_rows]  # every message is in the repo
    else:
        candidates = [mid for mid in covered if mid in user_ids]  # covered user messages only
    flag_ids = [mid for mid in candidates if mid not in already]
    if not flag_ids:
        return
    async with session_factory() as session:
        await session.execute(
            update(MessageModel).where(MessageModel.id.in_(flag_ids)).values(imported_rag=True)
        )
        await session.commit()


@router.get("/sessions")
async def get_sessions(user: AuthUser = Depends(require_user), q: str | None = None) -> dict:
    """List the authenticated user's chat sessions (newest first).

    ``?q=`` filters by title / summary / message content (case-insensitive substring).
    """
    q = (q or "").strip()
    return {"sessions": await list_sessions(SessionLocal, user.user_id, q or None)}


@router.get("/sessions/{session_id}")
async def get_session_messages(session_id: UUID, user: AuthUser = Depends(require_user)):
    """Return a session's title + messages (with ids) if it belongs to the current user."""
    async with SessionLocal() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
    if sess is None or sess.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="session not found")
    # Recover pre-flag imported state onto the message rows so the response below carries it.
    await _backfill_imported_rag(SessionLocal, user.user_id, session_id)
    detail = await load_session_detail(SessionLocal, session_id)
    return {"session_id": str(session_id), "title": detail["title"], "messages": detail["messages"]}


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: UUID, body: SessionRenameRequest, user: AuthUser = Depends(require_user)
):
    """Rename a session. An empty title resets it to ``NULL`` so auto-naming kicks in again."""
    async with SessionLocal() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is None or sess.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="session not found")
        title = (body.title or "").strip()
        sess.title = title or None
        await session.commit()
        return {"title": sess.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: UUID, user: AuthUser = Depends(require_user)):
    """Delete a session (cascade removes its messages + events) if it belongs to the user."""
    async with SessionLocal() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is None or sess.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="session not found")
        await session.delete(sess)
        await session.commit()
    return Response(status_code=204)


@router.delete("/sessions/{session_id}/messages/{message_id}")
async def delete_session_message(
    session_id: UUID, message_id: UUID, user: AuthUser = Depends(require_user)
):
    """Delete a single message (no truncation) if its session belongs to the user."""
    async with SessionLocal() as session:
        sess = (
            await session.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one_or_none()
        if sess is None or sess.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="session not found")
        message = (
            await session.execute(select(MessageModel).where(MessageModel.id == message_id))
        ).scalar_one_or_none()
        if message is None or message.session_id != session_id:
            raise HTTPException(status_code=404, detail="message not found")
        await session.delete(message)
        await session.commit()
    return Response(status_code=204)


@router.post("/approvals/{approval_id}")
async def resolve_approval(
    approval_id: str,
    body: ApprovalResolveRequest,
    user: AuthUser = Depends(require_user),
):
    """Resolve a pending human-in-the-loop approval (allow / deny).

    Approvals are registered under the requesting user's id when their chat store is built, so
    ownership is checked before resolving — one user can't approve another's tool call. The
    shared broker resolves the store's pending future (cross-node via Redis pub/sub when
    configured), which unblocks the agent turn awaiting it.
    """
    bridge = get_approval_bridge()
    owner = await bridge.owner(approval_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="approval not found or already resolved")
    if str(user.user_id) != owner:
        raise HTTPException(status_code=403, detail="approval belongs to another user")
    await bridge.resolve(approval_id, body.allow)
    return {"ok": True, "allow": body.allow}


@router.post("/checkpoints/{checkpoint_id}/revert")
async def revert_checkpoint(checkpoint_id: str, user: AuthUser = Depends(require_user)):
    """Restore the agent workspace to a prior shadow-git checkpoint.

    Checkpoint ids are recorded before each chat turn (see the agent kernel); this endpoint
    rolls the workspace back to a known-good state after a bad batch of agent file edits.
    """
    agent = get_agent()
    if agent.checkpoints is None:
        raise HTTPException(status_code=503, detail="workspace checkpoints are disabled")
    try:
        head = await asyncio.to_thread(agent.checkpoints.revert, checkpoint_id)
    except CheckpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "head": head}
