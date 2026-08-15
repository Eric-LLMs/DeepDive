"""Append-only session event log.

The agent loop records each step (LLM call, tool execution, result) as an immutable event,
making a turn observable and auditable without reconstructing state from the mutable
message list. Events can be serialized to JSONL for persistence.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# Event type constants (mirror the runtime's session + tool lifecycle).
SESSION_START = "session-start"
SESSION_END = "session-end"
LLM_CALL = "llm-call"
TOOL_CALL = "tool-call"
TOOL_RESULT = "tool-result"


@dataclass(frozen=True)
class SessionEvent:
    type: str
    seq: int
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)


class SessionLog:
    """Append-only event log for one agent session/turn."""

    def __init__(self) -> None:
        self._events: list[SessionEvent] = []
        self._seq = 0

    def append(self, type_: str, **payload: Any) -> SessionEvent:
        """Record an event. Returns the event (still recorded even if the caller ignores it)."""
        event = SessionEvent(
            type=type_,
            seq=self._seq,
            timestamp=time.time(),
            payload=dict(payload),
        )
        self._seq += 1
        self._events.append(event)
        return event

    def events(self, *types: str) -> list[SessionEvent]:
        """Return events in append order, optionally filtered by type(s)."""
        if not types:
            return list(self._events)
        return [e for e in self._events if e.type in types]

    def to_jsonl(self) -> str:
        """Serialize the log as newline-delimited JSON for persistence/audit."""
        return "\n".join(json.dumps(asdict(e), ensure_ascii=False) for e in self._events)

    def __len__(self) -> int:
        return len(self._events)
