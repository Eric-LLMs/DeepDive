"""Project context loader: CLAUDE.md / AGENTS.md conventions for the PROJECT_CONTEXT zone.

The cache-boundary prompt reserves a ``PromptZone.PROJECT_CONTEXT`` partition for project
conventions (Claude Code's ``CLAUDE.md``, OpenClaw's ``AGENTS.md``). :func:`read_project_context`
reads the first existing convention file under the agent's workspace and returns it capped at a
character budget, so project rules reach the model while the zone stays stable per project and
bounded in size. When no convention file exists it returns ``""`` — the assembler drops the empty
zone, so the rendered prompt is byte-identical to the no-project-context case.
"""
from __future__ import annotations

from pathlib import Path


def _cap(text: str, max_chars: int) -> str:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip() + "…(truncated)"
    return text


def read_project_context(
    workspace: Path,
    *,
    files: list[str] | None = None,
    max_chars: int = 8000,
) -> str:
    """Return the first existing convention file's contents under ``workspace``.

    Files are tried in order (``CLAUDE.md`` before ``AGENTS.md``, Claude Code precedence), so a
    repo with both conventions only feeds the primary one to the model. Reads are capped at
    ``max_chars``; a missing file or an empty workspace yields ``""``.
    """
    for name in files or ["CLAUDE.md", "AGENTS.md"]:
        path = (workspace / name).resolve()
        try:
            if path.is_file():
                return _cap(path.read_text(encoding="utf-8", errors="replace"), max_chars)
        except OSError:
            continue
    return ""
