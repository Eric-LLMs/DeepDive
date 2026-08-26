"""Decision and result vocabulary for the tool runtime.

Lifecycle decisions are *return values*, not exceptions:

- a ``tools/pre-execute`` listener returns a :class:`PreToolDecision` (allow/deny/ask);
- a ``tools/post-execute`` listener returns a :class:`PostToolDecision` (accept/block);
- a monotonic guard returns a deny *reason* string (or ``None`` to pass).

These types are frozen where possible so listeners cannot accidentally mutate shared
pipeline state mid-flight.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContentBlock:
    """A model-visible content block. Only ``text`` is supported for now (kept extensible)."""

    type: str
    text: str


def text_block(text: str) -> ContentBlock:
    """Build a text content block."""
    return ContentBlock(type="text", text=text)


@dataclass(frozen=True)
class PreToolDecision:
    """Decision returned by a ``tools/pre-execute`` listener."""

    kind: str  # "allow" | "deny" | "ask"
    reason: str | None = None

    @classmethod
    def allow(cls) -> PreToolDecision:
        return cls(kind="allow")

    @classmethod
    def deny(cls, reason: str) -> PreToolDecision:
        return cls(kind="deny", reason=reason)

    @classmethod
    def ask(cls, reason: str | None = None) -> PreToolDecision:
        return cls(kind="ask", reason=reason)


@dataclass(frozen=True)
class PostToolDecision:
    """Decision returned by a ``tools/post-execute`` listener."""

    kind: str  # "accept" | "block"
    content: list[ContentBlock] | None = None
    value: Any = None
    feedback: str | None = None
    additional_contexts: list[Any] = field(default_factory=list)

    @classmethod
    def accept(cls, **kwargs) -> PostToolDecision:
        return cls(kind="accept", **kwargs)

    @classmethod
    def block(cls, feedback: str, **kwargs) -> PostToolDecision:
        return cls(kind="block", feedback=feedback, **kwargs)


@dataclass(frozen=True)
class ToolFailure:
    """A tool-level error (bad args, runtime exception, or a block decision)."""

    message: str
    info: dict | None = None


@dataclass(frozen=True)
class ToolExecutionSuccess:
    """A successful tool run: canonical ``value`` + model-visible ``content``."""

    value: Any = None
    content: list[ContentBlock] = field(default_factory=list)
    meta: dict | None = None
    is_error: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ToolExecutionFailure:
    """A failed tool run. ``error`` is the first field so callers can write
    ``ToolExecutionFailure(ToolFailure(...))``."""

    error: ToolFailure
    content: list[ContentBlock] = field(default_factory=list)
    is_error: bool = field(default=True, init=False)


ToolExecutionResult = ToolExecutionSuccess | ToolExecutionFailure


@dataclass
class ToolExecution:
    """A materialized tool call: frozen arguments + execution context.

    ``deferred_contexts`` lets a tool inject extra messages into the session after it
    runs; ``concludes_turn`` signals the loop to stop early.
    """

    call_id: str
    name: str
    arguments: dict
    agent: Any = None
    signal: Any = None
    token: Any = None
    deferred_contexts: list[Any] = field(default_factory=list)
    concludes_turn: bool = False

    def defer_context(self, *contexts: Any) -> None:
        self.deferred_contexts.extend(contexts)

    def conclude_turn(self) -> None:
        self.concludes_turn = True


# A monotonic guard: return a deny reason string, or None to pass.
Guard = Callable[[ToolExecution], Awaitable[str | None]]
