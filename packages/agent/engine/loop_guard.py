"""ToolLoopTracker: detect tool-oscillation within one turn (the loop breaker).

An agent that repeatedly calls the same tool with the same arguments — or keeps hitting
the same error — is looping: burning tokens without progress. The tracker records every
dispatch and, after ``threshold`` consecutive identical ``(name, args-hash)`` calls *or*
``threshold`` identical repeated errors, reports :meth:`should_break` so the loop stops
dispatching and injects forced guidance instead of running the turn to ``max_steps``.

The tracker lives on the per-turn :class:`~agent.engine.context.AgentTurn` (``turn.loop_tracker``),
so concurrent turns never share counters. The loop resets it after injecting guidance so a
genuine new approach starts from a clean slate.
"""
from __future__ import annotations

import hashlib
import json


class ToolLoopTracker:
    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self._last_call: tuple[str, str] | None = None
        self._call_streak = 0
        self._last_error: str | None = None
        self._error_streak = 0

    @staticmethod
    def _hash_args(args: dict) -> str:
        """Stable hash of the tool arguments (order-independent, non-string values stringified)."""
        return hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def record(self, name: str, args: dict) -> None:
        """Record one dispatch (called before execution)."""
        key = (name, self._hash_args(args))
        self._call_streak = self._call_streak + 1 if key == self._last_call else 1
        self._last_call = key

    def record_error(self, tool_name: str, message: str) -> None:
        """Record a tool failure; identical errors in a row count toward the breaker."""
        key = f"{tool_name}:{message}"
        self._error_streak = self._error_streak + 1 if key == self._last_error else 1
        self._last_error = key

    def should_break(self) -> bool:
        """True when the turn is stuck on an identical call/error at or past the threshold."""
        return self._call_streak >= self.threshold or self._error_streak >= self.threshold

    def reset(self) -> None:
        """Clear the streaks (called after the loop injects forced guidance)."""
        self._last_call = None
        self._call_streak = 0
        self._last_error = None
        self._error_streak = 0
