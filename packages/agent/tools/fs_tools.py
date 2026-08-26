"""Core resident filesystem / shell tools for the agent kernel.

``read_file`` (READ), ``edit_file`` (WRITE), and ``bash`` (WRITE + NETWORK) are the
microkernel's resident file-system tools. All file access is rooted at a workspace
directory (path traversal is rejected), and the permission class is declared explicitly
so the :class:`~agent.security.sandbox.Sandbox` gates them: the default READ-only session denies
``edit_file`` / ``bash`` unless the host grants WRITE / NETWORK.

``bash`` delegates to a :class:`~agent.tools.bash_sandbox.BashSandbox` (host or docker) and
applies a best-effort workspace-escape guard; the sandbox is the real isolation boundary.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from agent.engine.decisions import ToolExecution, text_block
from agent.tools.bash_sandbox import BashSandbox, assert_no_escape, get_bash_sandbox
from agent.tools.definition import ToolDefinition, ToolOutput, define_tool
from agent.tools.tool_permissions import ToolPermission


def _resolve(workspace: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` inside ``workspace``; raise ValueError on escape."""
    root = workspace.resolve()
    candidate = (root / raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return candidate


def _read(path: Path, max_chars: int) -> str:
    """Sync body of ``read_file`` (runs in a worker thread to keep the loop free)."""
    if not path.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars and path.stat().st_size > max_chars:
        return text[:max_chars] + "\n…(truncated)"
    return text


def read_file_tool(workspace: Path) -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        path = _resolve(workspace, args["path"])
        return await asyncio.to_thread(_read, path, args.get("max_chars", 0))

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


def _apply_edit(path: Path, old: str | None, new: str) -> tuple[str, bool]:
    """Compute the edited content in a worker thread; returns ``(content, replaced)``."""
    if old is not None and path.is_file():
        current = path.read_text(encoding="utf-8", errors="replace")
        if old not in current:
            raise ValueError("old_text not found in the file — nothing replaced")
        return current.replace(old, new, 1), True
    return new, False


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically: temp file in the same dir + ``os.replace``.

    Never leaves a truncated file if the process dies mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def edit_file_tool(workspace: Path) -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        path = _resolve(workspace, args["path"])
        old = args.get("old_text")
        new = args.get("new_text", "")
        content, replaced = await asyncio.to_thread(_apply_edit, path, old, new)
        await asyncio.to_thread(_atomic_write, path, content)
        if replaced:
            return f"replaced 1 occurrence in {args['path']}"
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


def bash_tool(workspace: Path, sandbox: BashSandbox) -> ToolDefinition:
    async def execute(args: dict, exec: ToolExecution) -> str:
        command = args["command"]
        timeout = int(args.get("timeout", 30))
        assert_no_escape(workspace, command)
        try:
            return await sandbox.run(command, timeout)
        except TimeoutError:
            raise TimeoutError(f"command timed out after {timeout}s")

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


def register_fs_tools(
    runtime, workspace: Path, sandbox: BashSandbox | None = None
) -> list[ToolDefinition]:
    """Register the resident fs/shell tools onto ``runtime``; returns the definitions."""
    sandbox = sandbox or get_bash_sandbox()
    tools = [
        read_file_tool(workspace),
        edit_file_tool(workspace),
        bash_tool(workspace, sandbox),
    ]
    for tool in tools:
        runtime.register(tool)
    return tools
