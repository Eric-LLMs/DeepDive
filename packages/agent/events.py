"""Minimal event bus: middleware (waterfall) + observer (serial/emit) flows.

Two primitive flows:

- ``waterfall``: a middleware chain where each handler either returns a decision directly
  (short-circuit) or delegates to ``next()``. This is the decision-carrying flow used by the
  tool lifecycle (``tools/pre-execute`` → ``tools/execute`` → ``tools/post-execute``).
- ``serial`` / ``emit``: read-only observers used by ``tools/result``, ``tools/change`` and
  the session hooks. ``serial`` awaits each observer in order; ``emit`` fires them without
  blocking the caller.

All registrations are reversible: ``on``/``observe`` return a disposer callable.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

WaterfallHandler = Callable[[Any, Callable[[], Awaitable[Any]]], Awaitable[Any]]
Observer = Callable[[Any], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._waterfalls: dict[str, list[WaterfallHandler]] = {}
        self._observers: dict[str, list[Observer]] = {}

    def on(self, name: str, handler: WaterfallHandler) -> Callable[[], None]:
        """Register a waterfall middleware; returns a disposer."""
        handlers = self._waterfalls.setdefault(name, [])
        handlers.append(handler)

        def dispose() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return dispose

    def observe(self, name: str, handler: Observer) -> Callable[[], None]:
        """Register a read-only observer; returns a disposer."""
        observers = self._observers.setdefault(name, [])
        observers.append(handler)

        def dispose() -> None:
            if handler in observers:
                observers.remove(handler)

        return dispose

    async def waterfall(self, name: str, *payload: Any, base: Any = None) -> Any:
        """Run the middleware chain; ``base`` is the terminal handler/value reached by the
        final ``next()``. ``payload`` is spread into each handler as positional args (so a
        handler may take ``(exec, next)`` or ``(exec, result, next)``). Returns whatever the
        chain produces (a decision, a result, ...)."""
        handlers = list(self._waterfalls.get(name, []))

        async def run(index: int) -> Any:
            if index >= len(handlers):
                return await base() if callable(base) else base
            handler = handlers[index]

            async def next_() -> Any:
                return await run(index + 1)

            return await handler(*payload, next_)

        return await run(0)

    async def serial(self, name: str, payload: Any) -> None:
        """Run observers in registration order, awaiting each. Exceptions are logged, not raised."""
        for handler in list(self._observers.get(name, [])):
            await self._safe(handler, payload, name)

    def emit(self, name: str, payload: Any) -> None:
        """Fire observers without blocking the caller (each runs as its own task)."""
        for handler in list(self._observers.get(name, [])):
            asyncio.get_running_loop().create_task(self._safe(handler, payload, name))

    async def _safe(self, handler: Observer, payload: Any, name: str) -> None:
        try:
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - observers must not break the pipeline
            logger.exception("observer %s failed", name)
