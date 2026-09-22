"""Base chat context: cheap request/session facts + lazy hydration of heavy context.

Everything between "HTTP identity resolved" (transport layer, stays in the router)
and "executor invocation" is assembled here: attachment notes, inline images,
handoff notes, viewer assembly + short-circuit, session/research binding, session
memory store and the turn history (v2 zero-read or legacy recovery).

I/O rule (design §5): this module adds NO new I/O beyond what the legacy router
already performed in the same order; heavy fetches (viewer slices, memory recall)
only happen when the ExecutionPlan asks for them in later phases.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from core.application.chat.executors.base import ChatDeps, ViewerDeps
from core.application.drive_service import DriveError, DriveService
from core.config import settings
from core.infrastructure.memory import (
    SessionMemoryStore,
    apply_compaction,
    assemble_recovery_history,
    create_session,
    needs_compaction,
    set_session_type,
    summary_block,
)
from core.infrastructure.vision_caption import model_supports_vision
from core.logger import set_log_context

logger = logging.getLogger(__name__)

# Image suffixes that the ``vision`` tool (not ``read_document``) should open. Mirrors
# ``_IMAGE_EXTS`` in apps/api/tools/read_document_tool.py.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Screenshots larger than this are not inlined (a base64 data URL past it would bloat the
# request and risk a provider size 4xx); the caller falls back to the ``vision`` tool path.
INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024


class ResearchConflict(Exception):
    """begin_run refused the turn (409 already-running / 404 task missing).

    Raised by :func:`resolve_research_context`; the router maps it to an
    HTTPException so this module stays framework-free.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ChatTurnContext:
    """Everything one turn needs after base-context resolution (before execution)."""

    body: Any                      # ChatRequest-shaped (message/attach/handoff/viewer/…)
    user: Any | None               # AuthUser or None (guest)
    user_id: Any
    guest_token: str | None
    log_user: Any | None
    tier: str
    notice: str | None
    # LLM channel pinned for this turn (already installed via set_request_llm_channel)
    model: str | None
    base_url: str | None
    api_key: str | None
    business_name: str | None
    credential_id: Any | None
    # Composed user text (attach/handoff notes prefixed; NEVER modified by viewer)
    user_text: str
    owned_asset_id: str | None
    inline_image: str | None
    # Viewer (reference context; abort short-circuits the turn pre-execution)
    viewer_assembly: dict | None = None
    viewer_abort: dict | None = None
    # Session + research binding
    session_id: Any = None
    research_service: Any | None = None
    bound_task_id: str | None = None
    effective_handoff: dict | None = None
    research_notice: str | None = None
    research_turn: bool = False
    log_tokens: Any = None
    # Memory + history (hydrated for every legacy turn today; gated by the plan later)
    session_memory: Any = None
    history: list[dict] = field(default_factory=list)
    compaction_payload: dict | None = None
    compaction_deferred: str | None = None
    # The kernel run(context=…) payload, assembled per route (see build_turn_context)
    agent_context: dict | None = None
    disable_thinking: bool = False


async def attach_note(
    body: Any, drive: DriveService, user_id: Any, *, inline: bool
) -> str | None:
    """Build a context note for an attached cloud asset, or ``None`` when there is none.

    Attachments are read-only references: we verify the caller can read the asset, then
    prefix a ``[Attached: …]`` note to the user message so the agent knows which document
    the user is troubleshooting. The note also names the channel that can actually open it:

    - ``inline=True`` (a screenshot attached to a turn whose chat model is vision-capable):
      the pixels ride as a multimodal block right below this note, so the model must NOT call
      ``vision`` for it — the directive locks the answer to the embedded image and forbids
      falling back on prior documents/RAG/earlier images (the switch that was failing).
    - ``inline=False``: text-only chat model / non-image file → route through the right tool
      (``vision`` for images, ``read_document`` for PDF/Word/Excel/text).
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
        raise PermissionError(f"no access to the attached file: {exc}")
    name = attach.get("name") or "document"
    suffix = name[name.rfind("."):].lower() if "." in name else ""
    mime = (attach.get("mime_type") or "").lower()
    is_image = suffix in IMAGE_SUFFIXES or mime.startswith("image/")
    if inline and is_image:
        hint = (
            "The attached image is included INLINE with this message (its picture is shown "
            "as an image part below) — you can see it directly. Answer ONLY from THIS image. "
            "Any earlier image or document content in the conversation describes a DIFFERENT "
            "file and never applies here; do not answer about this image from old documents, "
            "RAG hits, or a previous screenshot. Do NOT call the `vision` tool for it."
        )
    elif is_image:
        hint = (
            "This message carries a NEW image that is NOT in the prompt as text — its "
            "pixels are only readable through the tool. Call the `vision` tool with this "
            "asset_id and answer ONLY from what it returns. This image is NEW to this "
            "message — any earlier image analysis or document content in the conversation "
            "describes a DIFFERENT file and never applies to this one; do not answer about "
            "this image from earlier documents, RAG hits, or a previous screenshot."
        )
    else:
        hint = "Call the `read_document` tool with this asset_id to extract its text."
    return f"[Attached: {name} (asset_id {asset_id})] {hint}"


async def resolve_inline_image(
    body: Any, drive: DriveService, user_id: Any, model: str | None
) -> str | None:
    """A ``data:`` URL for the attached image when it should be INLINED this turn.

    Inline conditions: the routed chat model is vision-capable (:func:`model_supports_vision`),
    the attach is a readable image asset, and its bytes fit the size cap. Returning ``None``
    leaves the turn on the existing ``vision``-tool path (text-only model / non-image / oversized).
    """
    if not model_supports_vision(model):
        return None
    attach = body.attach or {}
    if attach.get("kind") != "asset" or user_id is None:
        return None
    asset_id = attach.get("asset_id")
    if not asset_id:
        return None
    name = attach.get("name") or ""
    mime = (attach.get("mime_type") or "").lower()
    suffix = name[name.rfind("."):].lower() if "." in name else ""
    if not (suffix in IMAGE_SUFFIXES or mime.startswith("image/")):
        return None
    try:
        _name, asset_mime, data = await drive.download(user_id, UUID(asset_id))
    except DriveError as exc:
        logger.warning("inline image: download failed (%s); using vision tool", exc)
        return None
    except Exception:  # noqa: BLE001 - malformed id / storage hiccup → tool path, never fail the turn
        return None
    if not data or len(data) > INLINE_IMAGE_MAX_BYTES:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{(asset_mime or mime or 'image/png')};base64,{b64}"


def handoff_note(body: Any) -> str | None:
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
    if mode == "research_run":
        # The desktop Run control. Mid-chain this means "drive the remaining stages"; on a
        # task that already reached PUBLISH begin_run reset it to a NEW edition (stage ->
        # DISCOVER, evidence graph emptied), so the agent must re-gather rather than treat
        # the prior edition's report as current evidence.
        return (
            f"[Research handoff: run {project_id} via research_project (action resume), "
            "Run-control start. Do NOT create a new project — the project already exists. "
            "Continue the current stage and drive every remaining stage through the "
            "deep_research skill to PUBLISH. If this task had already reached PUBLISH, it was "
            "reset to a fresh edition: the stage is DISCOVER, gates are NOT_RUN and the "
            "evidence graph was emptied, so re-gather sources from scratch instead of "
            "reusing the prior edition's report as current evidence.]"
        )
    return (
        f"[Research handoff: resume project {project_id} via research_project (action resume), "
        f"mode {mode}. Do NOT create a new project — the project already exists. Continue "
        "it through the deep_research skill stages and advance to PUBLISH.]"
    )


async def asset_readable(drive: DriveService, user_id: Any, asset_id: Any) -> bool:
    """drive.ensure_asset_readable as a boolean — viewer context is auxiliary, a bad id
    drops the scope instead of failing the chat request."""
    if user_id is None or not asset_id:
        return False
    try:
        await drive.ensure_asset_readable(user_id, UUID(str(asset_id)))
        return True
    except DriveError:
        return False
    except Exception:
        logger.warning("viewer: unexpected drive check failure", exc_info=True)
        return False


async def build_viewer_assembly(body: Any, drive: DriveService, user_id: Any, deps: ViewerDeps) -> dict | None:
    """Permission-check the viewer's asset identities, then run the pure assembler."""
    if body.viewer is None:
        return None
    viewer = body.viewer
    asset_ok = await asset_readable(drive, user_id, viewer.asset_id)
    frame_readable = set()
    for sel in viewer.selections:
        # Legacy shape kept: only the awaitable check nests; the dedupe guard stays outer.
        if sel.image_asset_id and str(sel.image_asset_id) not in frame_readable:  # noqa: SIM102
            if await asset_readable(drive, user_id, sel.image_asset_id):
                frame_readable.add(str(sel.image_asset_id))
    return deps.build_blocks(
        viewer, body.message,
        asset_readable=asset_ok, frame_readable_ids=frame_readable,
    )


def viewer_abort(assembly: dict | None) -> dict | None:
    """Short-circuit payload when the turn must not reach the agent. Since documents
    stopped being intent-matched server-side, ``too_large``/``unavailable`` only arise from
    video FULL (over budget / untrusted full capture) — honest stop, no downgrade, no RAG
    fallback."""
    if assembly and assembly["status"] in ("too_large", "unavailable"):
        return {
            "mode": assembly["mode"], "status": assembly["status"],
            "rejected": assembly["rejected"],
        }
    return None


def _turn_tail(items: Any) -> list[dict]:
    """Schema tail entries → the internal tail shape ``{message_id, role, content}``."""
    return [
        {
            "message_id": str(t.message_id) if t.message_id else None,
            "role": t.role,
            "content": t.content,
        }
        for t in items
    ]


async def assemble_turn_history(
    body: Any,
    session_memory: SessionMemoryStore,
    session_id: Any,
    user_text: str,
    *,
    deps: ChatDeps,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
) -> tuple[list[dict], dict | None, str | None]:
    """Build the turn's history under session-memory v2. Returns
    ``(history, compaction_payload, compaction_deferred)``.

    - **v2 client** (``context_state`` present): ZERO SQL reads on a normal turn —
      history = client summary + client tail, assembled in memory. Only when the
      threshold is crossed does ``apply_compaction`` read SQL (behind its dual
      persistence barrier). A deferred compaction changes nothing: the full (over
      budget) tail is used as-is for this turn and the next turn retries.
    - **Legacy client** (no ``context_state``): client-less recovery mode — bounded
      SQL load after the checkpoint watermark + the same compaction path
      (:func:`assemble_recovery_history`); correct but not zero-read, kept so P1
      ships without a client update. §8.3: a v2 request whose watermark implies
      history but whose tail is empty is a 422 at schema level — the v2 path has no
      silent SQL fallback.
    """
    if body.context_state is None:
        history, compaction, deferred, audit = await assemble_recovery_history(
            deps.session_factory, session_id, deps.llm, user_text,
            model=model, base_url=base_url, api_key=api_key,
        )
        if audit:
            session_memory.record_event("compaction", audit)
        return history, compaction, deferred

    summary = body.context_state.summary
    tail = _turn_tail(body.tail)
    history = summary_block(summary) + [
        {"role": m["role"], "content": m["content"]} for m in tail
    ]
    if not needs_compaction(summary=summary, tail=tail, new_message=user_text):
        return history, None, None
    outcome = await apply_compaction(
        session_factory=deps.session_factory,
        session_id=session_id,
        llm=deps.llm,
        tail=tail,
        has_pending_mutations=body.context_state.has_pending_mutations,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
    if outcome.status == "compacted":
        session_memory.record_event("compaction", outcome.audit or {})
        return outcome.context, outcome.compaction, None
    return history, None, outcome.deferred_reason


def resolve_research_context(
    drive: DriveService, user: Any, session_id: Any, body_handoff: dict | None
) -> tuple:
    """Durable research handoff resolution shared by both chat routes.

    Returns ``(service, bound_task_id, effective_handoff, notice)`` where ``service`` is
    ``None`` when this session is not a research session. The client sends the handoff once
    (the first turn of a research session); a session already bound to a task re-synthesizes
    it here, so later turns keep the research grant (WRITE + NETWORK, see
    :meth:`Sandbox._effective_permissions`) and the agent keeps targeting the same task
    instead of creating a duplicate project. A binding conflict is surfaced as a non-fatal
    in-stream notice — it never breaks the chat.
    """
    from plugins.research.plugin import ResearchService  # lazy: sibling plugin package

    if user is None:
        return None, None, None, None
    service: Any | None = None
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


def _begin_run(research_service: Any, user_id: Any, bound_task_id: str, session_id: Any, effective_handoff: dict | None) -> None:
    """Single active-run mutex per task (T4 invariant #2). Raises ResearchConflict so
    the router can answer 409/404 without a framework dependency here."""
    try:
        # The desktop Run control on a task that already reached PUBLISH is a NEW-edition
        # run (handoff mode "research_run"): begin_run resets the finished task so it
        # drives DISCOVER→…→PUBLISH again into temp/vN + outputs/_vN instead of stopping
        # at turn 0. A plain resume ("research_resume", e.g. a typed session message)
        # never restarts a finished task — casual chat must not burn a full re-run.
        new_edition = (effective_handoff or {}).get("mode") == "research_run"
        research_service.begin_run(
            user_id, bound_task_id, session_id=str(session_id), new_edition=new_edition
        )
    except ValueError as exc:
        msg = str(exc)
        if "already running" in msg:
            raise ResearchConflict(409, msg) from exc
        if "not found" in msg:
            raise ResearchConflict(404, msg) from exc
        raise


async def build_turn_context(
    *,
    body: Any,
    deps: ChatDeps,
    user: Any,
    user_id: Any,
    guest_token: str | None,
    log_user: Any,
    tier: str,
    notice: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    business_name: str | None,
    credential_id: Any,
    stream: bool,
    log_tag: bool = False,
) -> ChatTurnContext:
    """Run the shared pre-execution sequence (identical to the legacy router's).

    ``stream`` selects the streaming route's differences that Phase 1 preserves
    verbatim: ephemeral-session typing, no inline-image in the kernel context, and
    the effective ``disable_thinking`` flag. ``log_tag`` (legacy route only) pins the
    turn's user/session onto every log line emitted during resolution.
    """
    drive = deps.drive
    user_text = body.message
    # Only attaches the client flagged ``owned`` (a 📷 screenshot created for this message)
    # own a drive asset. Referential attaches leave the link NULL so deleting the message
    # never touches a referenced document.
    owned_asset_id = (
        body.attach.get("asset_id") if body.attach and body.attach.get("owned") else None
    )
    inline_image = await resolve_inline_image(body, drive, user_id, model)
    if body.attach:
        note = await attach_note(body, drive, user_id, inline=inline_image is not None)
        if note:
            user_text = f"{note}\n\n{body.message}"
    note = handoff_note(body)
    if note:
        user_text = f"{note}\n\n{user_text}"

    ctx = ChatTurnContext(
        body=body, user=user, user_id=user_id, guest_token=guest_token,
        log_user=log_user, tier=tier, notice=notice,
        model=model, base_url=base_url, api_key=api_key,
        business_name=business_name, credential_id=credential_id,
        user_text=user_text, owned_asset_id=owned_asset_id, inline_image=inline_image,
    )

    viewer_assembly = await build_viewer_assembly(body, drive, user_id, deps.viewer)
    ctx.viewer_assembly = viewer_assembly
    abort = viewer_abort(viewer_assembly)
    ctx.viewer_abort = abort
    if abort:
        # FULL over budget / untrusted capture: honest stop BEFORE any session write or
        # model call — the route short-circuits on this payload.
        return ctx

    ctx.session_id = body.session_id or await create_session(
        deps.session_factory, user_id, title=body.message,
        type=1 if (stream and body.ephemeral) else 0,
    )
    if log_tag:
        # Tag every log line this turn emits (research run, mirror, RAG recall) with the
        # user + session it belongs to; the route resets after building the response.
        ctx.log_tokens = set_log_context(user_id=str(user_id), session_id=str(ctx.session_id))

    # Chat-driven research: bind this session to the handoff's task (mirror + grant).
    # The single-task run mutex (T4 invariant #2) is shared with the stream route so a
    # non-streaming turn and a streaming turn for the same task can never overlap.
    (
        ctx.research_service, ctx.bound_task_id, ctx.effective_handoff, ctx.research_notice,
    ) = deps.resolve_research(drive, user, ctx.session_id, body.handoff)
    if ctx.bound_task_id:
        # Isolation marker: a session bound to a research task is stored as type=1 —
        # hidden from the chat sidebar, opened only from the Research tab, deleted with
        # the task.
        try:
            await set_session_type(deps.session_factory, UUID(str(ctx.session_id)), 1)
        except Exception:
            logger.warning("research: failed to mark the bound session as type=1", exc_info=True)
    ctx.research_turn = ctx.research_service is not None and ctx.bound_task_id is not None
    if ctx.research_turn:
        _begin_run(ctx.research_service, user_id, ctx.bound_task_id, ctx.session_id, ctx.effective_handoff)

    ctx.session_memory = SessionMemoryStore(
        deps.session_factory, deps.embedder(), deps.llm, ctx.session_id, user_id,
        attach_asset_id=owned_asset_id,
    )
    ctx.history, ctx.compaction_payload, ctx.compaction_deferred = await assemble_turn_history(
        body, ctx.session_memory, ctx.session_id, ctx.user_text,
        deps=deps, model=model, base_url=base_url or None, api_key=api_key or None,
    )

    # The kernel run(context=…) payload. Viewer rides the reference-context channel
    # (never user_text); ``inline_image`` reaches the kernel only on the legacy route —
    # the streaming route never passed it and Phase 1 keeps that byte-for-byte.
    agent_context = {
        **({"handoff": ctx.effective_handoff} if ctx.effective_handoff else {}),
        **({"viewer": viewer_assembly} if viewer_assembly and viewer_assembly["status"] in ("injected", "stub") else {}),
        **({"inline_image": inline_image} if (inline_image and not stream) else {}),
    } or None
    ctx.agent_context = agent_context
    # Video FOCUS turns are on-screen Q&A about a small subtitle window — never pay the
    # thinking prefill tax for them (voice-call precedent). Documents never reach
    # "focus" anymore (they go through the stub).
    ctx.disable_thinking = bool(
        stream and (body.disable_thinking or (viewer_assembly or {}).get("mode") == "focus")
    )
    return ctx
