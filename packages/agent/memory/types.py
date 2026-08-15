"""Memory taxonomy + structured memory record (Claude Code memdir style).

Four closed types capture context NOT derivable from the current project state:
user / feedback / project / reference.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

MEMORY_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class Memory:
    """A single memory record: frontmatter metadata + body content."""

    name: str
    content: str
    description: str = ""
    type: str = ""
    path: str | None = None
    mtime_ms: float = 0.0

    @property
    def age_days(self) -> int:
        """Whole days since last write (0 = today, 1 = yesterday, ...)."""
        return max(0, int((time.time() * 1000 - self.mtime_ms) / 86_400_000))

    @property
    def freshness_note(self) -> str:
        """A staleness caveat for memories older than a day, else empty string."""
        if self.age_days <= 1:
            return ""
        return (
            f"This memory is {self.age_days} days old. Memories are point-in-time "
            "observations, not live state — verify against current code before asserting as fact."
        )
