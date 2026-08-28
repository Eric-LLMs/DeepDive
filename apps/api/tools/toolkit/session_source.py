"""Session-to-source helpers for the toolkit pipeline.

A session-scoped generation feeds the *conversation transcript* into the same pipeline the
workspace-file tools use. These pure helpers build that transcript and map each produced
artifact extension to a Cloud-Drive asset name + mime, so the worker stays thin and the
naming logic is unit-testable.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

_PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_MD_MIME = "text/markdown"
_MMD_MIME = "text/plain"

# Directory (under the workspace) that holds one transcript per in-flight session job.
SESSION_SRC_DIR = ".toolkit_session_src"

# Characters that are hostile in a file name (Windows + separators).
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_name(title: str) -> str:
    """Return a filesystem-safe stem for ``title`` (used for temp file + drive name)."""
    clean = _ILLEGAL.sub("_", (title or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:80] or "session"


def build_transcript(title: str | None, messages: list[dict]) -> str:
    """Render a session's messages as a grounded Markdown transcript.

    Only ``user`` / ``assistant`` messages are included — tool/system messages (large raw
    tool outputs, error noise) are filtered out so the token budget is spent on the Q&A.
    Every kept message becomes ``**<Role>:** <content>`` under a ``# <title>`` heading, which
    is what the pipeline extracts, token-counts, and maps over. Raises ``ValueError`` when
    the session carries no usable message content (a title alone is not source material).
    """
    body: list[str] = []
    for msg in messages:
        role = (msg.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue  # tool / system noise is not source material
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        body.append(f"**{role.capitalize()}:** {content}")
    if not body:
        raise ValueError("session has no messages to generate from")
    if title and title.strip():
        return f"# {title.strip()}\n\n" + "\n\n".join(body) + "\n"
    return "\n\n".join(body) + "\n"


def cleanup_stale_sources(workspace: Path, *, max_age_s: int = 24 * 3600) -> int:
    """Delete orphaned session transcript files older than ``max_age_s``.

    A worker killed mid-job (OOM / SIGKILL) can leave a transcript behind; the next startup
    sweeps them so the temp dir never accumulates. Returns the number of files removed.
    """
    root = Path(workspace) / SESSION_SRC_DIR
    if not root.is_dir():
        return 0
    now = datetime.now(UTC)
    removed = 0
    for path in root.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if (now - mtime).total_seconds() > max_age_s:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def artifact_plan(tool: str, title: str) -> dict[str, tuple[str, str]]:
    """Map an output file extension -> ``(drive asset name, mime)`` for ``tool``.

    Slides produce two artifacts (Marp Markdown + .pptx); mindmap and summary produce one.
    The names follow the user's choice: ``<session title>_<tool>.<ext>``.
    """
    safe = sanitize_name(title)
    if tool == "mindmap":
        return {".mmd": (f"{safe}_mindmap.mmd", _MMD_MIME)}
    if tool == "summary":
        return {".md": (f"{safe}_summary.md", _MD_MIME)}
    return {
        ".md": (f"{safe}_slides.md", _MD_MIME),
        ".pptx": (f"{safe}_slides.pptx", _PPTX_MIME),
    }
