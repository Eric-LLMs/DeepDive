"""Shared frontmatter parsing: the leading ``---`` key/value block used by skills and memory.

Both :mod:`agent.skills.registry` and :mod:`agent.memory.file` parsed this identically; the single
helper keeps the format consistent and the duplication gone.
"""
from __future__ import annotations


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` frontmatter block (key: value lines) from the body.

    Returns ``(meta, body)``; when ``text`` has no leading ``---`` marker, returns
    ``({}, text)`` unchanged.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = len(lines)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body
