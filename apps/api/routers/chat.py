"""Chat routes: the agent-driven ``/chat`` turn, SSE streaming (``/chat/stream``), and
query-repository import (single Q&A pair / whole session / imported status).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from uuid import UUID

from agent.security.approvals import (
    ApprovalStore,
    get_approval_bridge,
    set_request_approval,
)
from api.auth import AuthUser, require_user, require_user_optional
from api.deps import (
    _batch_embedder,
    _embedder,
    get_agent,
    get_drive_service,
    get_task_queue,
    llm,
)
from api.routers._shared import (
    _guest_quota,
    _log_usage,
    _resolve_chat_route,
    resolve_guest_identity,
)
from api.schemas import ChatImportRequest, ChatRequest, ChatSessionImportRequest
from core.application.drive_service import DriveError, DriveService
from core.config import settings
from core.infrastructure.db import (
    ChunkModel,
    LoginTokenModel,
    MessageModel,
    SessionLocal,
    SessionModel,
)
from core.infrastructure.drive_repositories import SqlChunkRepository
from core.infrastructure.ingest import build_chunks, write_query_repo_chunks
from core.infrastructure.jobs import (
    CHAT_SESSION_IMPORT,
    RESEARCH_DRIVE,
    SESSION_FINALIZE,
    TaskQueue,
)
from core.infrastructure.memory import (
    SessionMemoryStore,
    compact_history,
    create_session,
)
from core.infrastructure.request_context import set_request_user
from core.infrastructure.security import authorize_usage, get_role
from core.logger import reset_log_context, set_log_context
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from plugins.research.plugin import ResearchService

router = APIRouter(tags=["chat"])

logger = logging.getLogger(__name__)


async def _attach_note(body: ChatRequest, drive: DriveService, user_id) -> str | None:
    """Build a context note for an attached cloud asset, or ``None`` when there is none.

    Attachments are read-only references: we verify the caller can read the asset, then
    prefix a ``[Attached: …]`` note to the user message so the agent knows which document
    the user is troubleshooting. The agent's existing drive tools (pdf_extract_text,
    doc_outline, …) can then fetch the bytes by ``asset_id``.
    """
    attach = body.attach or {}
    if attach.get("kind") != "asset" or user_id is None:
        return None
    asset_id = attach.get("asset_id")
    if not asset_id:
        return None
    try:
        await drive.ensure_asset_readable(user_id, UUID(asset_id))
    except DriveError as exc:
        raise HTTPException(status_code=403, detail=f"no access to the attached file: {exc}")
    name = attach.get("name") or "document"
    return f"[Attached: {name} (asset_id {asset_id})]"


def _handoff_note(body: ChatRequest) -> str | None:
    """Build a structured instruction prefix for a handoff payload, or ``None``.

    Handoffs are machine-readable context attached to the first message of a turn (e.g. the
    desktop "Resume Research in Chat" button resuming a Research OS project). The note is
    prefixed to the user text so the agent reliably receives the project id and the resume
    directive instead of having to infer them from prose; the same payload is sunk into the
    turn context (``current_turn().context["handoff"]``) so tools can act on it directly.
    """
    handoff = body.handoff
    if not handoff or not isinstance(handoff, dict):
        return None
    kind = handoff.get("kind")
    if kind != "research":
        return None
    project_id = handoff.get("project_id")
    if not project_id:
        return None
    mode = handoff.get("mode") or "research_resume"
    return (
        f"[Research handoff: resume project {project_id} via research_project (action resume), "
        f"mode {mode}. Do NOT create a new project — the project already exists. Continue "
        "it through the deep_research skill stages and advance to PUBLISH.]"
    )


def _resolve_research_context(drive, user, session_id, body_handoff):
    """Durable research handoff resolution shared by ``/chat`` and ``/chat/stream``.

    Returns ``(service, bound_task_id, effective_handoff, notice)`` where ``service`` is
    ``None`` when this session is not a research session. The client sends the handoff once
    (the first turn of a research session); a session already bound to a task re-synthesizes
    it here, so later turns keep the research grant (WRITE + NETWORK, see
    :meth:`Sandbox._effective_permissions`) and the agent keeps targeting the same task
    instead of creating a duplicate project. A binding conflict is surfaced as a non-fatal
    in-stream notice — it never breaks the chat.
    """
    if user is None:
        return None, None, None, None
    service: ResearchService | None = None
    effective_handoff: dict | None = None
    if body_handoff and body_handoff.get("kind") == "research":
        effective_handoff = body_handoff
    else:
        candidate = ResearchService(drive, settings.research_scratch_dir)
        known_task = candidate.task_id_for_session(user.user_id, session_id)
        if known_task:
            service = candidate
            effective_handoff = {
                "kind": "research",
                "project_id": known_task,
                "mode": "research_resume",
            }
    bound_task_id: str | None = None
    notice: str | None = None
    if effective_handoff and effective_handoff.get("project_id"):
        service = service or ResearchService(drive, settings.research_scratch_dir)
        try:
            bound_task_id = service.bind_session(
                user.user_id, effective_handoff["project_id"], session_id
            )["task_id"]
        except Exception as exc:  # noqa: BLE001 — a binding hiccup never breaks the chat
            logger.warning("research bind_session failed: %s", exc)
            notice = f"⚠️ Research: session/task binding failed — {exc}"
    return service, bound_task_id, effective_handoff, notice


async def _maybe_continue_research(
    service: ResearchService,
    queue: TaskQueue,
    *,
    user_id: UUID,
    task_id: str,
    run_id: str,
    session_id: str | None,
    channel: tuple[str | None, str | None, str | None],
) -> bool:
    """Hand an interactive research turn's run to the worker chain, or release it.

    Called right after the first (interactive) turn of a run completes, *before* ``end_run``.
    Returns ``True`` when the run was handed to ``RESEARCH_DRIVE`` (the slot stays live and
    the worker keeps driving until PUBLISH / a gate / a stop); ``False`` when the run must be
    released here (reached PUBLISH, a human gate override is pending, Stop was requested, or
    the continuation could not be scheduled — the slot is never stranded).

    The interactive turn is the "free" turn 0: the driver's no-progress / caps / cost grading
    starts with auto-turn 1. ``channel`` pins the same LLM channel the interactive turn used.
    """
    from plugins.research.driver import ResearchRunDriver, iso_now

    async def _publish_async(kind: str) -> None:
        with contextlib.suppress(Exception):
            await service.publish_change(user_id, task_id, kind=kind)

    ledger = service.get_driver_checkpoint(user_id, task_id)
    if ledger.get("cancel_requested"):
        await _publish_async("run.cancelled")
        return False
    project = service.read_project(user_id, task_id)
    if project.get("stage") == "PUBLISH":
        await _publish_async("run.finished")
        return False
    if service.pending_overrides(user_id, task_id):
        await _publish_async("run.blocked")
        return False

    # Persist the interactive turn (turn 0) as the chain's starting ledger, then schedule
    # auto-turn 1. The driver CAS-checks on arrival; a duplicate run of turn 0 is impossible
    # (this is the only site that schedules turn_index 1).
    try:
        service.set_driver_checkpoint(
            user_id, task_id,
            patch={
                "run_id": run_id,
                "turn_index": 0,
                "turn_attempt": 1,
                "turn_state": "done",
                "execution_id": f"{run_id}:0:1",
                "updated_at": iso_now(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - treat as a schedule failure below
        logger.warning("research continuation ledger failed: %s", exc)
        ResearchRunDriver().abort_run(
            service, user_id, task_id,
            run_id=run_id, execution_id=f"{run_id}:0:1",
            reason=f"could not record the run ledger: {exc}",
        )
        return False

    model, base_url, api_key = channel
    try:
        await queue.enqueue(
            RESEARCH_DRIVE,
            {
                "user_id": str(user_id),
                "task_id": task_id,
                "run_id": run_id,
                "session_id": session_id,
                "turn_index": 1,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
            },
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - never strand a RUNNING slot
        logger.warning("research continuation enqueue failed: %s", exc)
        ResearchRunDriver().abort_run(
            service, user_id, task_id,
            run_id=run_id, execution_id=f"{run_id}:0:1",
            reason=f"could not schedule the first auto turn: {exc}",
        )
        return False

    await _publish_async("run.turn")
    return True


@router.post("/chat/import")
async def chat_import_pair(
    body: ChatImportRequest,
    user: AuthUser = Depends(require_user),
    drive: DriveService = Depends(get_drive_service),
):
    """Import one chat Q&A pair (user message + assistant reply) as a query-repo chunk.

    Stored with ``source_type='chat'`` + ``source_id=<user_message_id>`` so re-importing
    the same pair is idempotent.
    """
    async with SessionLocal() as session:
        user_msg = await session.get(MessageModel, UUID(body.user_message_id))
        asst_msg = await session.get(MessageModel, UUID(body.assistant_message_id))
        session_row = await session.get(SessionModel, UUID(body.session_id))
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Ownership gate: a user may only import pairs from their own sessions, so a forged
    # session_id cannot pull another user's messages into the caller's query repo.
    if session_row.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="session does not belong to you")
    if user_msg is None or asst_msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    if user_msg.session_id != UUID(body.session_id) or asst_msg.session_id != user_msg.session_id:
        raise HTTPException(status_code=400, detail="messages do not belong to the given session")
    if user_msg.role != "user" or asst_msg.role != "assistant":
        raise HTTPException(status_code=400, detail="expected a user/assistant message pair")

    from rag.config_store import load_config  # lazy: rag is a sibling package

    # A chat Q&A that joins RAG keeps its screenshot in BOTH folders: the temporary chat/temp
    # copy stays (it dies with its chat when the session/message is deleted), and a stable
    # RAG/images copy is created for the corpus sharing the same object bytes — so emptying
    # chat/temp never removes an image the repo still references. The chunk meta references
    # the stable copy; best-effort, falling back to the owned chat/temp asset if copying fails.
    rag_image_id: str | None = None
    if user_msg.attach_asset_id is not None:
        try:
            rag_image_id = str(
                (await drive.copy_to_folder(user.user_id, user_msg.attach_asset_id, "RAG/images")).id
            )
        except DriveError:
            rag_image_id = str(user_msg.attach_asset_id)

    content = f"{user_msg.text}\n\n{asst_msg.text}"
    cfg = await load_config(SessionLocal)
    title = user_msg.text.strip()[:60] or "Chat Q&A"
    chunks = await build_chunks(content, cfg, doc_title=title, llm=llm)
    for c in chunks:
        c.meta = {**c.meta, "title": title, "kind": "qa", "session_id": str(user_msg.session_id)}
        if rag_image_id is not None:
            c.meta["image_ids"] = [rag_image_id]
    chunks_repo = SqlChunkRepository(SessionLocal)
    await chunks_repo.delete_by_source("chat", [str(user_msg.id)])
    res = await write_query_repo_chunks(
        SessionLocal,
        _batch_embedder(),
        chunks=chunks,
        user_id=user.user_id,
        source_type="chat",
        source_id=str(user_msg.id),
    )
    # Flip the per-message flag on both halves of the pair: the client renders "✓ Imported"
    # from these rows, and the flag is what stops a later duplicate re-import (the assistant
    # reply keeps its state even if the bound question is deleted or re-grouped).
    async with SessionLocal() as session:
        for mid in (user_msg.id, asst_msg.id):
            row = await session.get(MessageModel, mid)
            if row is not None:
                row.imported_rag = True
        await session.commit()
    return {"chunks": res["chunks"]}


@router.post("/chat/import-session")
async def chat_import_session(
    body: ChatSessionImportRequest,
    user: AuthUser = Depends(require_user),
    queue: TaskQueue = Depends(get_task_queue),
):
    """Enqueue a whole chat session: the LLM groups its Q&A turns into repo chunks."""
    async with SessionLocal() as session:
        session_row = await session.get(SessionModel, UUID(body.session_id))
    if session_row is None or session_row.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="session does not belong to you")
    job_id = await queue.enqueue(
        CHAT_SESSION_IMPORT,
        {"session_id": body.session_id, "user_id": str(user.user_id)},
        user_id=user.user_id,
    )
    return {"job_id": str(job_id)}


@router.get("/chat/imported")
async def chat_imported_status(
    session_id: UUID, user: AuthUser = Depends(require_user)
) -> dict:
    """Which Q&A pairs of a session are already in the query repo.

    Drives the persistent "✓ Imported" state on the desktop chat buttons: on session
    load the client fetches this so an already-imported pair stays disabled across
    session switches and app restarts.

    Coverage comes from the per-message ``imported_rag`` flags set on import (stable across
    message deletes / regroupings, so the state never spreads to sibling pairs). ``qa_source_ids``
    lists every flagged user message; ``session_imported`` is true when every current user
    message is flagged. ``legacy_session_imported`` marks a pre-flag whole-session import (old
    ``kind='session-qa'``) that has no per-message data — the client treats it as fully imported
    but allows a re-import to convert it to the flag model.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(MessageModel.id, MessageModel.role, MessageModel.imported_rag)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).all()
        legacy = (
            await session.execute(
                select(ChunkModel.id)
                .where(
                    ChunkModel.source_type == "chat",
                    ChunkModel.user_id == user.user_id,
                    ChunkModel.meta["session_id"].astext == str(session_id),
                    ChunkModel.meta["kind"].astext == "session-qa",
                )
                .limit(1)
            )
        ).first()
    user_msg_ids = [str(mid) for mid, role, _ in rows if role == "user"]
    flagged_ids = [str(mid) for mid, role, flag in rows if role == "user" and flag]
    legacy_session_imported = legacy is not None
    fully_covered = bool(user_msg_ids) and len(flagged_ids) == len(user_msg_ids)
    return {
        "qa_source_ids": sorted(flagged_ids),
        "session_imported": legacy_session_imported or fully_covered,
        "legacy_session_imported": legacy_session_imported,
    }


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    # Scope RAG / memory recall to this request's user (guest → public-link assets only).
    set_request_user(user.user_id if user is not None else None)
    # Resolve the LLM channel for this request: a logged-in user uses the channel pinned on
    # their token at login (failing over to another active channel of the same role); a guest
    # uses the ``anonymous`` role's channels. A role with no usable channel falls back to the
    # legacy /config route (empty base_url/api_key → the configured global client).
    #
    # A logged-in user whose every LLM key is disabled on the Tokens page has *no* usable
    # channel — they still log in fine, but degrade to the anonymous tier for this request:
    # guest daily quota + anonymous routing (that's the "equivalent to an anonymous user"
    # behavior; full access returns when the admin re-enables a key).
    notice = None
    guest_token = None
    tier = "free"
    async with SessionLocal() as session:
        if user is None:
            user_id, guest_token = await resolve_guest_identity(SessionLocal, body.guest_token)
            await _guest_quota(request.app.state.redis, user_id)
            token = None
            role_id = "anonymous"
            log_user = None
        else:
            user_id = user.user_id
            token = await session.get(LoginTokenModel, user.token_id)
            role_id = user.role.role_id
            log_user = user
        base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, role_id)
        if user is not None and not base_url and not api_key:
            anon = await get_role(session, "anonymous")
            anon_limit = anon.daily_request_limit if anon is not None else settings.guest_daily_limit
            limit_txt = f"每天限 {anon_limit} 次" if anon_limit >= 0 else "按匿名用户限额"
            await _guest_quota(
                request.app.state.redis, user_id,
                detail="你的额度已用完,且匿名额度也已用完。请充值或升级套餐后继续使用。",
            )
            role_id = "anonymous"
            log_user = None
            notice = (
                f"你的渠道额度已用完,已按匿名用户身份继续使用({limit_txt})。"
                "如需更多额度,请充值或升级套餐。"
            )
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, role_id)
        elif user is not None:
            tier = await authorize_usage(session, user.user_id, user.role)
        # The anonymous tier has no channel either: do NOT fall back to the legacy global
        # connection — tell the user instead (the admin must bind a channel to the role).
        if not base_url and not api_key:
            raise HTTPException(
                status_code=503,
                detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
            )

    user_text = body.message
    # Only attaches the client flagged ``owned`` (a 📷 screenshot created for this message)
    # own a drive asset. Referential attaches (🔗 drive file / local file) leave the link
    # NULL so deleting the message never touches a referenced document — the chat/temp
    # folder is UI-only; the delete decision keys off this owned link.
    owned_asset_id = (
        body.attach.get("asset_id") if body.attach and body.attach.get("owned") else None
    )
    if body.attach:
        note = await _attach_note(body, drive, user_id)
        if note:
            user_text = f"{note}\n\n{body.message}"
    handoff_note = _handoff_note(body)
    if handoff_note:
        user_text = f"{handoff_note}\n\n{user_text}"
    session_id = body.session_id or await create_session(SessionLocal, user_id, title=body.message)
    # Tag every log line this turn emits (research run, mirror, RAG recall) with the user +
    # session it belongs to; reset once the response is built.
    log_tokens = set_log_context(user_id=str(user_id), session_id=str(session_id))
    # Chat-driven research: bind this session to the handoff's task (mirror + grant), same
    # durable resolution as /chat/stream. The single-task run mutex (T4 invariant #2) is
    # shared with /chat/stream so a non-streaming turn and a streaming turn for the same task
    # can never overlap.
    research_service, bound_task_id, effective_handoff, research_notice = _resolve_research_context(
        drive, user, session_id, body.handoff
    )
    research_turn = research_service is not None and bound_task_id is not None
    if research_turn:
        try:
            research_service.begin_run(user_id, bound_task_id, session_id=str(session_id))
        except ValueError as exc:
            msg = str(exc)
            if "already running" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            if "not found" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise
    session_memory = SessionMemoryStore(
        SessionLocal, _embedder(), llm, session_id, user_id,
        attach_asset_id=owned_asset_id,
    )
    history = body.history or await session_memory.load_messages()
    history = await compact_history(
        history,
        session_factory=SessionLocal,
        session_id=session_id,
        session_memory=session_memory,
        llm=llm,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )
    research_continuing = False
    try:
        result = await get_agent().run(
            user_text,
            history,
            session_memory=session_memory,
            model=model,
            base_url=base_url or None,
            api_key=api_key or None,
            context={"handoff": effective_handoff} if effective_handoff else None,
        )
    finally:
        # /chat owns the run for the lifetime of this request. Hand the finished interactive
        # turn to the worker chain unless it hit a stop condition (PUBLISH / pending gate
        # override / cancel / enqueue failure); the single-task run slot is released only when
        # the run is NOT handed off, so the driver chain keeps owning it across turns.
        if research_turn and research_service is not None and bound_task_id is not None:
            try:
                project = research_service.read_project(user_id, bound_task_id)
                active_run = project.get("active_run") or {}
                run_id = active_run.get("run_id")
                if run_id:
                    research_continuing = await _maybe_continue_research(
                        research_service,
                        queue,
                        user_id=user_id,
                        task_id=bound_task_id,
                        run_id=run_id,
                        session_id=str(session_id),
                        channel=(model, base_url or None, api_key or None),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("research continuation decision failed: %s", exc)
            if not research_continuing:
                try:
                    research_service.end_run(user_id, bound_task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("research end_run failed: %s", exc)
    # close() (inside run) already flushed events; defer the expensive embed+summary work.
    await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
    if log_user is not None:
        await _log_usage(
            log_user, business_name, "chat", result.usage,
            credential_id=credential_id, paid=(tier == "paid"),
        )
    # Resolve this turn's user-message / assistant-answer ids so the client can delete a
    # single message. Scan ascending for the last row matching each text (texts may repeat,
    # but this turn's rows are always the newest).
    user_message_id = assistant_message_id = None
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(MessageModel.id, MessageModel.role, MessageModel.text)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
            )
        ).all()
        for m_id, role, text in rows:
            if role == "user" and text == user_text:
                user_message_id = str(m_id)
            elif role == "assistant" and text == result.final_answer:
                assistant_message_id = str(m_id)
    # Mirror this turn into the bound task's session_history.json (best-effort; the DB
    # SessionModel is the authority — a write failure only logs, it never fails the turn).
    if research_service is not None and bound_task_id is not None:
        try:
            await research_service.append_session_turn(user_id, session_id, "user", body.message)
            if result.final_answer:
                await research_service.append_session_turn(
                    user_id, session_id, "assistant", result.final_answer
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("research session_history mirror failed: %s", exc)
    reset_log_context(log_tokens)
    resp = {
        "answer": result.final_answer,
        "messages": result.messages,
        "session_id": str(session_id),
        "user_id": str(user_id),
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }
    if guest_token:
        resp["guest_token"] = guest_token
    if research_continuing:
        resp["research_continuing"] = True
    if research_notice:
        notice = f"{notice}\n{research_notice}" if notice else research_notice
    if notice:
        resp["notice"] = notice
    return resp


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    user: AuthUser | None = Depends(require_user_optional),
    queue: TaskQueue = Depends(get_task_queue),
    drive: DriveService = Depends(get_drive_service),
):
    """SSE streaming chat over the full agent path (tools + session persistence + quota).

    Emits ``{"type": "thinking"|"content"|"tool", "data": ...}`` deltas as the model reasons
    and answers, then a final ``{"type": "done", "data": {answer, session_id, user_id,
    user_message_id, assistant_message_id, notice}}`` event. A user with no usable LLM key
    degrades to the anonymous tier (guest quota), matching ``/chat``.
    """
    set_request_user(user.user_id if user is not None else None)
    notice = None
    guest_token = None
    tier = "free"
    async with SessionLocal() as session:
        if user is None:
            user_id, guest_token = await resolve_guest_identity(SessionLocal, body.guest_token)
            await _guest_quota(request.app.state.redis, user_id)
            token = None
            role_id = "anonymous"
            log_user = None
        else:
            user_id = user.user_id
            token = await session.get(LoginTokenModel, user.token_id)
            role_id = user.role.role_id
            log_user = user
        base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, token, role_id)
        if user is not None and not base_url and not api_key:
            anon = await get_role(session, "anonymous")
            anon_limit = anon.daily_request_limit if anon is not None else settings.guest_daily_limit
            limit_txt = f"每天限 {anon_limit} 次" if anon_limit >= 0 else "按匿名用户限额"
            await _guest_quota(
                request.app.state.redis, user_id,
                detail="你的额度已用完,且匿名额度也已用完。请充值或升级套餐后继续使用。",
            )
            role_id = "anonymous"
            log_user = None
            notice = (
                f"你的渠道额度已用完,已按匿名用户身份继续使用({limit_txt})。"
                "如需更多额度,请充值或升级套餐。"
            )
            base_url, api_key, model, business_name, credential_id = await _resolve_chat_route(session, None, role_id)
        elif user is not None:
            tier = await authorize_usage(session, user.user_id, user.role)
    # All keys + the anonymous tier are exhausted too — block, don't fall back to the
    # legacy global connection; the user must top up / upgrade.
    if not base_url and not api_key:
        raise HTTPException(
            status_code=503,
            detail="当前没有可用的 LLM 渠道,无法使用聊天。请联系管理员配置渠道,或充值/升级套餐后重试。",
        )

    user_text = body.message
    # Only attaches the client flagged ``owned`` (a 📷 screenshot created for this message)
    # own a drive asset. Referential attaches (🔗 drive file / local file) leave the link
    # NULL so deleting the message never touches a referenced document — the chat/temp
    # folder is UI-only; the delete decision keys off this owned link.
    owned_asset_id = (
        body.attach.get("asset_id") if body.attach and body.attach.get("owned") else None
    )
    if body.attach:
        note = await _attach_note(body, drive, user_id)
        if note:
            user_text = f"{note}\n\n{body.message}"
    handoff_note = _handoff_note(body)
    if handoff_note:
        user_text = f"{handoff_note}\n\n{user_text}"
    session_id = body.session_id or await create_session(SessionLocal, user_id, title=body.message)
    # Chat-driven research: bind this session to the handoff's task so every subsequent turn
    # mirrors into the task's ``session_history.json`` (a task-local projection — the DB
    # SessionModel stays the authoritative conversation record). A binding conflict (one
    # session may drive one task only) is surfaced as a non-fatal in-stream notice.
    #
    # ``effective_handoff`` is the research context for THIS turn. The client only sends the
    # handoff once (the first message of a research session), so for a session that is already
    # bound to a task we re-synthesize it here — the turn keeps its research grant (WRITE +
    # NETWORK, see Sandbox._effective_permissions) and the agent keeps targeting the same
    # task instead of silently creating a duplicate project.
    research_service, bound_task_id, effective_handoff, research_notice = _resolve_research_context(
        drive, user, session_id, body.handoff
    )
    # Single active-run mutex per task (T4 invariant #2): a second concurrent trigger for a
    # task that is already running is a 409 conflict. The slot is released by ``end_run`` in
    # ``gen()``'s finally, so a client disconnect cannot strand it; a crashed process's slot
    # is adopted by ``begin_run`` after the stale window. A bound session whose task vanished
    # degrades to a 404 rather than silently running against a dead task.
    research_turn = research_service is not None and bound_task_id is not None
    if research_turn:
        try:
            research_service.begin_run(user_id, bound_task_id, session_id=str(session_id))
        except ValueError as exc:
            msg = str(exc)
            if "already running" in msg:
                raise HTTPException(status_code=409, detail=msg) from exc
            if "not found" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise
    session_memory = SessionMemoryStore(
        SessionLocal, _embedder(), llm, session_id, user_id,
        attach_asset_id=owned_asset_id,
    )
    history = body.history or await session_memory.load_messages()
    history = await compact_history(
        history,
        session_factory=SessionLocal,
        session_id=session_id,
        session_memory=session_memory,
        llm=llm,
        model=model,
        base_url=base_url or None,
        api_key=api_key or None,
    )

    async def gen():
        # ``notice`` lives in the enclosing ``chat_stream`` scope (set to None above); the
        # research handoff may append to it below, so bind it nonlocal to avoid shadowing it
        # with an unbound local (UnboundLocalError killed the stream when no notice fired).
        nonlocal notice
        # Tag every log line the stream emits (agent turns, research tools, finalize) with the
        # user + session it belongs to. Set inside gen() (not chat_stream) because the SSE
        # generator runs after chat_stream returns — the sibling pump task inherits it.
        log_tokens = set_log_context(user_id=str(user_id), session_id=str(session_id))
        # The agent may block on a human-in-the-loop approval (awaiting POST /approvals/{id}),
        # so a plain `async for` over run_stream would deadlock — the stream can't advance while
        # the approval-request frame sits unyielded. Pump the stream into a queue in a sibling
        # task, and let the ApprovalStore's sink push approval frames into the same queue; this
        # generator only ever reads from the queue.
        frames: asyncio.Queue = asyncio.Queue()
        store = ApprovalStore(
            get_approval_bridge().broker,
            user_id=str(user_id),
            sink=lambda evt: frames.put_nowait(("approval", evt)),
        )
        set_request_approval(store)

        async def pump():
            # NOTE: only ever await INSIDE run_stream (never on frames). If the pump task
            # suspended on `await frames.put` were cancelled there, the CancelledError would
            # be consumed by this `finally` and the run_stream generator abandoned — its
            # cleanup would only run on a later GC aclose (GeneratorExit, not CancelledError),
            # so `turn-cancelled` would never be logged. The queue is unbounded, so put_nowait
            # never blocks; a client disconnect then lands the CancelledError in the loop's
            # `except asyncio.CancelledError`, which logs turn-cancelled and closes memory.
            try:
                async for evt in get_agent().run_stream(
                    user_text,
                    history,
                    session_memory=session_memory,
                    model=model,
                    base_url=base_url or None,
                    api_key=api_key or None,
                    context={"handoff": effective_handoff} if effective_handoff else None,
                    progress_sink=lambda evt: frames.put_nowait(("agent", evt)),
                ):
                    frames.put_nowait(("agent", evt))
            finally:
                # Sentinel so the consumer below always terminates after the stream ends,
                # including on cancellation (the loop already logs turn-cancelled).
                frames.put_nowait(("agent", {"type": "done", "data": None}))

        pump_task = asyncio.create_task(pump())
        final = None
        # Set when the interactive research turn hands the run to the worker chain (the slot
        # stays live); the done frame then tells the client the run is still active.
        research_continuing = False

        def collect_done_payload() -> dict | None:
            """Disconnect path: pull the done payload the drained pump left in the queue (the
            ``run_stream`` event, not the None sentinel) when the loop never saw it."""
            try:
                while True:
                    kind, data = frames.get_nowait()
                    if (
                        kind == "agent"
                        and isinstance(data, dict)
                        and data.get("type") == "done"
                        and data.get("data") is not None
                    ):
                        return data["data"]
            except asyncio.QueueEmpty:
                return None

        async def finalize_turn(final_payload: dict | None) -> dict:
            """Post-run bookkeeping shared by the normal path and the research disconnect path:
            session-finalize enqueue, usage logging, message-id resolution, and the task's
            ``session_history.json`` mirror. The DB SessionModel stays the authoritative chat
            record; the mirror is a task-local projection and failures only log."""
            nonlocal notice
            await queue.enqueue(SESSION_FINALIZE, {"session_id": str(session_id)})
            if log_user is not None:
                await _log_usage(
                    log_user, business_name, "chat_stream",
                    final_payload["usage"] if final_payload else None,
                    credential_id=credential_id, paid=(tier == "paid"),
                )
            # Resolve this turn's user/assistant message ids (same scan as /chat: ascending,
            # last row matching each text) so the client can delete a single message.
            answer = (final_payload or {}).get("answer", "")
            user_message_id = assistant_message_id = None
            async with SessionLocal() as session:
                rows = (
                    await session.execute(
                        select(MessageModel.id, MessageModel.role, MessageModel.text)
                        .where(MessageModel.session_id == session_id)
                        .order_by(MessageModel.created_at)
                    )
                ).all()
                for m_id, role, text in rows:
                    if role == "user" and text == user_text:
                        user_message_id = str(m_id)
                    elif role == "assistant" and answer and text == answer:
                        assistant_message_id = str(m_id)
            if research_service is not None and bound_task_id is not None:
                try:
                    await research_service.append_session_turn(user_id, session_id, "user", body.message)
                    if answer:
                        await research_service.append_session_turn(user_id, session_id, "assistant", answer)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("research session_history mirror failed: %s", exc)
            done = {
                "answer": answer,
                "session_id": str(session_id),
                "user_id": str(user_id),
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
            }
            if guest_token:
                done["guest_token"] = guest_token
            if research_notice:
                notice = f"{notice}\n{research_notice}" if notice else research_notice
            if notice:
                done["notice"] = notice
            return done

        try:
            while True:
                kind, data = await frames.get()
                if kind == "approval":
                    # Approval-request frame: forward verbatim (client POSTs /approvals/{id}).
                    yield {"data": json.dumps(data, ensure_ascii=False, default=str)}
                    continue
                if data["type"] == "done":
                    final = data["data"]
                    break
                yield {"data": json.dumps(data, ensure_ascii=False)}
        finally:
            set_request_approval(None)
            if research_turn:
                # Server-owned research run (T4 invariant #3): a client disconnect must never
                # cancel an active run. Let the pump drain to completion so the agent's turn
                # and its tool executions finish server-side, then still finalize the turn and
                # release the single-task run slot.
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
                if final is None:
                    # The loop was abandoned (client disconnect) before the done frame; run the
                    # same post-turn bookkeeping even though no client is left to stream to.
                    with contextlib.suppress(Exception):
                        await finalize_turn(collect_done_payload())
                if research_service is not None and bound_task_id is not None:
                    # Hand the finished interactive turn to the worker chain unless it hit a
                    # stop condition (PUBLISH / pending gate override / cancel / enqueue
                    # failure). ``research_continuing`` is set here and read on the normal path
                    # to tag the done frame; the single-task run slot is released only when the
                    # run is NOT handed off, so the driver chain keeps owning it across turns.
                    try:
                        project = research_service.read_project(user_id, bound_task_id)
                        active_run = project.get("active_run") or {}
                        run_id = active_run.get("run_id")
                        if run_id:
                            research_continuing = await _maybe_continue_research(
                                research_service,
                                queue,
                                user_id=user_id,
                                task_id=bound_task_id,
                                run_id=run_id,
                                session_id=str(session_id),
                                channel=(model, base_url or None, api_key or None),
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("research continuation decision failed: %s", exc)
                    if not research_continuing:
                        try:
                            research_service.end_run(user_id, bound_task_id)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("research end_run failed: %s", exc)
            else:
                # Non-research turn: the run is owned by the SSE pipe. Client disconnect stops
                # it so the turn's awaits (wait_for/tenacity/gather) re-raise CancelledError
                # and unwind cleanly.
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
            # Release the request-scoped log context: this generator may be closed (client
            # disconnect) at any yield point, so the user/session tags set at gen() start must
            # not leak into the next unit of work handled by this worker.
            reset_log_context(log_tokens)

        # Normal completion path: the loop broke on the run's done frame.
        done = await finalize_turn(final)
        if research_continuing:
            done["research_continuing"] = True
        yield {"data": json.dumps({"type": "done", "data": done}, ensure_ascii=False)}

    return EventSourceResponse(gen())
