"""Capability seam: named providers registered/required by string key.

A minimal Definition/Provider/Consumer split. The retrieval capability
is the first real consumer: the API registers either the in-process :class:`RAGPipeline` or a
gRPC client under the ``"retrieval"`` key, and tools call :meth:`Capabilities.require` to get
whichever provider is active. Providers are swappable without touching the tool code.
"""
from __future__ import annotations

from typing import Any, Callable


class CapabilityError(RuntimeError):
    """Raised when a required capability has not been provided."""


class Capabilities:
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def provide(self, name: str, provider: Any) -> Callable[[], None]:
        """Register ``provider`` under ``name``; returns a disposer that removes it."""
        self._providers[name] = provider

        def dispose() -> None:
            if self._providers.get(name) is provider:
                del self._providers[name]

        return dispose

    def require(self, name: str) -> Any:
        """Get the provider registered under ``name``; raises :class:`CapabilityError` if missing."""
        if name not in self._providers:
            raise CapabilityError(f"capability not provided: {name!r}")
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers
