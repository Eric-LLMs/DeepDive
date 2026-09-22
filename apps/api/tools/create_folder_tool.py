"""``create_folder``: create a folder in the caller's cloud drive (My Drive root by default).

Registers the SAME service entry the REST endpoint uses (``DriveService.create_folder`` —
validation, membership check, unique-suffix, repo write, audit log), so the LLM keeps the
capability as fallback for the chat fast path's ``create_folder`` allowlist entry. Every
``DriveError`` this call can raise happens BEFORE any write (name/parent validation,
workspace membership, collision) — the body re-raises them as ``"preflight: …"`` value
errors, the channel the ACTION executor reads as "provably not executed ⇒ safe to
escalate".
"""
from __future__ import annotations

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.application.drive_service import DriveError, DriveService
from core.infrastructure.request_context import get_request_user_id


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def create_folder(args: dict, exec: ToolExecution) -> str:
        uid = get_request_user_id()
        if uid is None:
            raise ValueError("preflight: create_folder requires an authenticated user")
        name = (args.get("name") or "").strip()
        if not name:
            raise ValueError("preflight: folder name must be a single non-empty name")
        parent_path = (args.get("parent_path") or "").strip() or None
        drive = DriveService(ctx.resolve("session_factory"))
        try:
            folder = await drive.create_folder(uid, None, parent_path, name)
        except DriveError as exc:
            # All DriveError paths of create_folder are pre-write ⇒ no side effect.
            raise ValueError(f"preflight: {exc}") from None
        return f"Created folder '{folder['path']}' in your drive."

    runtime.register(
        define_tool(
            name="create_folder",
            description="Create a folder in the user's cloud drive. Returns the created "
            "folder path. Fails with an error if the name is invalid or a write is denied; "
            "a busy name is auto-suffixed, never overwritten.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Folder name (single name, no '/')."},
                    "parent_path": {
                        "type": "string",
                        "description": "Optional existing folder path to create it under "
                        "(default: drive root).",
                    },
                },
                "required": ["name"],
            },
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=create_folder,
        )
    )
