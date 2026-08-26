"""Workspace checkpoints: shadow-git snapshots for safe rollback.

The agent's file tools mutate a shared workspace. Before each turn the kernel records a
shadow-git commit of ``settings.workspace_dir`` into an out-of-tree git-dir
(``settings.checkpoint_dir``), so ``revert_to_checkpoint`` / ``POST /checkpoints/{id}/revert``
can restore the workspace to a known-good state. The shadow repo is initialized once with
ignore rules written to ``info/exclude`` (never a ``.gitignore`` in the user's workspace);
large media and derived artifacts never enter a snapshot, keeping each commit small.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from agent.decisions import text_block
from agent.tool_permissions import ToolPermission
from agent.tools import ToolDefinition, ToolOutput, define_tool

# Large / derived artifacts never enter a checkpoint (info/exclude — no workspace .gitignore).
_IGNORED = (
    ".deepdive-snapshots/",
    ".git/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.mp4", "*.mkv", "*.avi", "*.mov", "*.webm",
    "*.mp3", "*.wav", "*.flac", "*.m4a", "*.ogg",
    "*.zip", "*.tar", "*.gz", "*.tgz", "*.7z",
    "data/media_output/",
)


class CheckpointError(RuntimeError):
    """Raised when a snapshot or revert fails (bad id, git unavailable, …)."""


class CheckpointStore:
    """Shadow-git snapshot store over a workspace (the git-dir lives out-of-tree)."""

    def __init__(self, workspace: Path, shadow_dir: Path) -> None:
        self.workspace = workspace.resolve()
        self.shadow = shadow_dir.resolve()
        self._git_dir = self.shadow / ".git"
        self._ready = False

    # ── plumbing ──
    def _git(self, *args: str) -> str:
        """Run git against the shadow git-dir with the workspace as its work-tree."""
        cmd = [
            "git",
            "-c", "user.name=deepdive",
            "-c", "user.email=deepdive@local",
            "--git-dir", str(self._git_dir),
            "--work-tree", str(self.workspace),
            *args,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            raise CheckpointError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def ensure_ready(self) -> None:
        """Init the shadow repo + baseline commit once (idempotent)."""
        if self._ready:
            return
        self.shadow.mkdir(parents=True, exist_ok=True)
        if not (self._git_dir / "HEAD").exists():
            proc = subprocess.run(
                ["git", "init", "--quiet", str(self.shadow)],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode != 0:
                raise CheckpointError(f"git init failed: {proc.stderr.strip()}")
        self._write_excludes()
        # Baseline commit so HEAD always resolves and `add -A` diffs against a real parent.
        self._git("add", "-A")
        self._git("commit", "--quiet", "--allow-empty", "-m", "baseline", "--no-verify")
        self._ready = True

    def _write_excludes(self) -> None:
        info = self._git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [p for p in _IGNORED if f"\n{p}" not in f"\n{current}"]
        if missing:
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(missing) + "\n")

    # ── public API ──
    def snapshot(self, reason: str = "checkpoint") -> str:
        """Commit the workspace's current state; returns the commit id (HEAD when unchanged)."""
        self.ensure_ready()
        self._git("add", "-A")
        if self._git("diff", "--cached", "--name-only"):
            self._git("commit", "--quiet", "-m", reason, "--no-verify")
        return self._git("rev-parse", "HEAD")

    def revert(self, checkpoint_id: str) -> str:
        """Restore tracked workspace files to ``checkpoint_id`` (reset --hard).

        Untracked files are left in place; only content that was committed into the shadow
        repo is rolled back. A full wipe of files the agent created mid-turn is out of scope.
        """
        self.ensure_ready()
        self._git("cat-file", "-e", f"{checkpoint_id}^{{commit}}")  # raises if unknown
        self._git("reset", "--hard", checkpoint_id)
        return self._git("rev-parse", "HEAD")

    def current(self) -> str:
        """The latest checkpoint id (shadow repo HEAD)."""
        self.ensure_ready()
        return self._git("rev-parse", "HEAD")


def revert_to_checkpoint_tool(store: CheckpointStore) -> ToolDefinition:
    """The ``revert_to_checkpoint`` meta-tool: restore the workspace to a prior snapshot.

    Destructive by nature (overwrites the agent's file edits), so it classifies as WRITE and
    the sandbox gates it behind session permissions / approval.
    """

    async def execute(args: dict, exec) -> dict:
        try:
            head = await asyncio.to_thread(store.revert, args["checkpoint_id"])
        except CheckpointError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "message": f"Workspace reverted to checkpoint {args['checkpoint_id']}.",
            "head": head,
        }

    return define_tool(
        name="revert_to_checkpoint",
        description=(
            "Restore the workspace files to the state recorded by a checkpoint id. Use this "
            "to undo a bad batch of file edits. Get an id from a previous turn's checkpoint."
        ),
        parameters={
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "string", "description": "The checkpoint id to restore."}
            },
            "required": ["checkpoint_id"],
        },
        output=ToolOutput(
            schema={"type": "object"},
            render=lambda args, value: [text_block(json.dumps(value, ensure_ascii=False))],
        ),
        execute=execute,
        permission={ToolPermission.WRITE},  # destructive: mutates the workspace
    )
