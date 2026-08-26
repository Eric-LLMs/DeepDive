"""Tool permission vocabulary: the Read / Write / Network capability classes.

A tool's permission class (auto-classified from its definition, or declared explicitly)
is what the :class:`~agent.security.sandbox.Sandbox` checks before execution: tools that require a
permission outside the session's granted set are denied (or asked for approval) by the
PreToolUse permission gate.
"""
from __future__ import annotations

from enum import Enum


class ToolPermission(Enum):
    """A tool's capability class, used by the sandbox to gate execution."""

    READ = "read"        # read-only access (files, search, lookup) — the session default
    WRITE = "write"      # mutating access (edit_file, save, delete, side effects)
    NETWORK = "network"  # external I/O (bash network calls, web_search, HTTP)


PERMISSION_ALL = frozenset(p for p in ToolPermission)


def permission_names(permissions) -> tuple[str, ...]:
    """Sortable human-readable names for a set/iterable of permissions."""
    return tuple(sorted(p.value for p in permissions))
