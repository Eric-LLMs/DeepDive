"""Core resident filesystem / shell tools for the agent kernel.

``read_file`` (READ), ``edit_file`` (WRITE), and ``bash`` (WRITE + NETWORK) are the
microkernel's resident file-system tools. All file access is rooted at a workspace
directory (path traversal is rejected), and the permission class is declared explicitly
so the :class:`~agent.sandbox.Sandbox` gates them: the default READ-only session denies
``edit_file`` / ``bash`` unless the host grants WRITE / NETWORK.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.decisions import ToolExecution, text_block
from agent.tool_permissions import ToolPermission
from agent.tools import ToolDefinition, ToolOutput, define_tool


def _resolve(workspace: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` inside ``workspace``; raise ValueError on escape."""
    root = workspace.resolve()
    candidate = (root / raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return candidate


def read_file_tool(workspace: Path) -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        path = _resolve(workspace, args["path"])
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        if path.stat().st_size > args.get("max_chars", 0) and args.get("max_chars"):
            return (
                path.read_text(encoding="utf-8", errors="replace")[: args["max_chars"]]
                + "\n…(truncated)"
            )
        return path.read_text(encoding="utf-8", errors="replace")

    return define_tool(
        name="read_file",
        description="Read a text file inside the workspace and return its contents.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "max_chars": {"type": "integer", "description": "Optional read cap."},
            },
            "required": ["path"],
        },
        output=ToolOutput(
            schema={"type": "string"}, render=lambda args, value: [text_block(value)]
        ),
        execute=execute,
        permission={ToolPermission.READ},
        is_concurrency_safe=True,
    )


def edit_file_tool(workspace: Path) -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        path = _resolve(workspace, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        old = args.get("old_text")
        new = args.get("new_text", "")
        if old is not None and path.is_file():
            current = path.read_text(encoding="utf-8", errors="replace")
            if old not in current:
                raise ValueError("old_text not found in the file — nothing replaced")
            path.write_text(current.replace(old, new, 1), encoding="utf-8")
            return f"replaced 1 occurrence in {args['path']}"
        path.write_text(new, encoding="utf-8")
        return f"wrote {args['path']}"

    return define_tool(
        name="edit_file",
        description=(
            "Create or edit a file inside the workspace. Provide old_text to replace a "
            "specific snippet, or new_text only to (over)write the whole file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative file path."},
                "old_text": {"type": "string", "description": "Exact text to replace (optional)."},
                "new_text": {"type": "string", "description": "Replacement / new file content."},
            },
            "required": ["path"],
        },
        output=ToolOutput(
            schema={"type": "string"}, render=lambda args, value: [text_block(value)]
        ),
        execute=execute,
        destructive=True,
        permission={ToolPermission.WRITE},
    )


def bash_tool() -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        proc = await asyncio.create_subprocess_shell(
            args["command"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=args.get("timeout", 30)
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"command timed out after {args.get('timeout', 30)}s")
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and not output:
            output = f"(exit {proc.returncode})"
        return output or f"(exit {proc.returncode})"

    return define_tool(
        name="bash",
        description=(
            "Run a shell command in the host environment. High-risk: requires the session "
            "to have granted WRITE + NETWORK permissions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {"type": "integer", "description": "Timeout in seconds."},
            },
            "required": ["command"],
        },
        output=ToolOutput(
            schema={"type": "string"}, render=lambda args, value: [text_block(value)]
        ),
        execute=execute,
        destructive=True,
        permission={ToolPermission.WRITE, ToolPermission.NETWORK},
    )


def register_fs_tools(runtime, workspace: Path) -> list[ToolDefinition]:
    """Register the resident fs/shell tools onto ``runtime``; returns the definitions."""
    tools = [read_file_tool(workspace), edit_file_tool(workspace), bash_tool()]
    for tool in tools:
        runtime.register(tool)
    return tools
