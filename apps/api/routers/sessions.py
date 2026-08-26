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
from core.infrastructure.db import MessageModel, SessionLocal, SessionModel
from core.infrastructure.memory import list_sessions, load_session_detail
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

router = APIRouter(tags=["sessions"])


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
