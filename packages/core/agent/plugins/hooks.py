"""Event names + listener helpers for the tool runtime.

Hooks are expressed as named events on the :class:`EventBus`:

- session: ``agent/session-start``, ``agent/session-end`` (observed by the loop);
- tool lifecycle: ``tools/pre-execute``, ``tools/execute``, ``tools/post-execute`` (waterfall),
  ``tools/result`` (observer).

A plugin listener is a ``(kind, event, handler)`` tuple; the manager mounts it onto the bus.
"""
from typing import Any, Awaitable, Callable

# Event name constants
SESSION_START = "agent/session-start"
SESSION_END = "agent/session-end"
PRE_TOOL_USE = "tools/pre-execute"
TOOL_EXECUTE = "tools/execute"
POST_TOOL_USE = "tools/post-execute"
TOOL_RESULT = "tools/result"

WaterfallListener = tuple[str, str, Callable[[Any, Callable[[], Awaitable[Any]]], Awaitable[Any]]]
ObserverListener = tuple[str, str, Callable[[Any], Awaitable[None]]]


def waterfall(event: str, handler: Callable[[Any, Callable[[], Awaitable[Any]]], Awaitable[Any]]) -> WaterfallListener:
    """Build a waterfall listener tuple ``(kind, event, handler)``."""
    return ("waterfall", event, handler)


def observe(event: str, handler: Callable[[Any], Awaitable[None]]) -> ObserverListener:
    """Build an observer listener tuple ``(kind, event, handler)``."""
    return ("observe", event, handler)
