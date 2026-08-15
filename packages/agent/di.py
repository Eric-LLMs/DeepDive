"""Cordis-style dependency injection: Context + Fiber state machine (Python-idiomatic port).

A :class:`Context` lazily resolves named capabilities via attribute access (``ctx.retrieval``
→ ``ctx.resolve("retrieval")``). Each plugin is a :class:`Fiber`: it stays PENDING until every
``inject`` dependency is ACTIVE, then mounts (registering its tools/skills/listeners/guards),
becoming ACTIVE. A mount error moves it to FAILED instead of silently stalling — the Cordis
FAILED stage. Dependency order therefore falls out of the state machine, replacing the old
``_drain_pending`` fixpoint.

External capabilities (e.g. ``retrieval``) are registered with :meth:`Context.provide` and are
immediately resolvable, so plugin ``inject`` names may be satisfied either by another plugin's
``provides`` or by an externally provided capability.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable


class CapabilityError(RuntimeError):
    """Raised when a required capability has not been provided."""


class FiberState(Enum):
    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


class Fiber:
    """One mountable unit: activates when all ``inject`` deps are ACTIVE."""

    def __init__(
        self,
        name: str,
        inject: list[str],
        values: dict[str, Any],
        mount: Callable[[], None],
        unmount: Callable[[], None],
    ) -> None:
        self.name = name
        self.inject = list(inject)
        self.values = values  # capability name -> value, exposed once ACTIVE
        self.mount = mount
        self.unmount = unmount
        self.state = FiberState.PENDING
        self.error: Exception | None = None

    @property
    def provides(self) -> list[str]:
        return list(self.values)


class Context:
    def __init__(self, parent: "Context | None" = None) -> None:
        self._parent = parent
        self._externals: dict[str, Any] = {}  # capability name -> external value (immediate)
        self._fibers: dict[str, Fiber] = {}  # provider name -> Fiber (plugins/services)
        self._by_name: dict[str, Fiber] = {}  # plugin-provided capability name -> Fiber

    # ── lazy resolution ──
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.resolve(name)

    def resolve(self, name: str) -> Any:
        if name in self._externals:
            return self._externals[name]
        fiber = self._by_name.get(name)
        if fiber is not None and fiber.state is FiberState.ACTIVE:
            return fiber.values[name]
        if self._parent is not None:
            return self._parent.resolve(name)
        raise CapabilityError(f"capability not provided: {name!r}")

    def has(self, name: str) -> bool:
        if name in self._externals:
            return True
        fiber = self._by_name.get(name)
        if fiber is not None and fiber.state is FiberState.ACTIVE:
            return True
        return self._parent.has(name) if self._parent is not None else False

    def extend(self, **meta: Any) -> "Context":
        """Create a child scope (fallback to parent on miss)."""
        return Context(parent=self)

    # ── registration ──
    def provide(self, name: str, value: Any) -> Callable[[], None]:
        """Register an external capability (no deps); immediately resolvable."""
        self._externals[name] = value
        self._settle()

        def dispose() -> None:
            if self._externals.get(name) is value:
                del self._externals[name]

        return dispose

    def plugin(
        self,
        plugin: Any,
        mount: Callable[[], None],
        unmount: Callable[[], None],
    ) -> None:
        """Register a plugin (a packaging unit with ``inject``/``provides``)."""
        self._add_fiber(plugin.name, list(plugin.inject), dict(plugin.provides), mount, unmount)

    def service(self, service: "Service") -> None:
        """Register a class-based :class:`Service` provider."""
        self._add_fiber(
            service._name,
            list(service.inject),
            {service._name: service},
            service.start,
            service.stop,
        )

    def _add_fiber(
        self,
        name: str,
        inject: list[str],
        values: dict[str, Any],
        mount: Callable[[], None],
        unmount: Callable[[], None],
    ) -> None:
        if name in self._fibers:
            raise ValueError(f"provider already registered: {name}")
        for cap in values:
            if cap in self._by_name:
                raise ValueError(
                    f"capability {cap!r} provided by both {self._by_name[cap].name!r} and {name!r}"
                )
        fiber = Fiber(name, inject, values, mount, unmount)
        for cap in values:
            self._by_name[cap] = fiber
        self._fibers[name] = fiber
        self._settle()

    def unregister(self, name: str) -> None:
        """Roll back a provider and re-settle any PENDING dependents."""
        fiber = self._fibers.pop(name, None)
        if fiber is None:
            return
        if fiber.state is FiberState.ACTIVE:
            fiber.state = FiberState.UNLOADING
            fiber.unmount()
        fiber.state = FiberState.DISPOSED
        for cap in fiber.provides:
            if self._by_name.get(cap) is fiber:
                del self._by_name[cap]
        self._settle()

    # ── state machine ──
    def _settle(self) -> None:
        """Topological fixpoint: activate PENDING fibers whose deps are all ACTIVE."""
        progressed = True
        while progressed:
            progressed = False
            for fiber in self._fibers.values():
                if fiber.state is FiberState.PENDING and self._deps_satisfied(fiber):
                    self._activate(fiber)
                    progressed = True

    def _deps_satisfied(self, fiber: Fiber) -> bool:
        return all(self.has(name) for name in fiber.inject)

    def _activate(self, fiber: Fiber) -> None:
        fiber.state = FiberState.LOADING
        try:
            fiber.mount()
        except Exception as exc:  # noqa: BLE001 - a failing plugin must not stall others
            fiber.state = FiberState.FAILED
            fiber.error = exc
            return
        fiber.state = FiberState.ACTIVE

    # ── introspection (used by PluginManager.validate) ──
    def fibers(self) -> list[Fiber]:
        return list(self._fibers.values())

    def provider_of(self, name: str) -> Fiber | None:
        return self._by_name.get(name)

    def provided_names(self) -> set[str]:
        return set(self._externals) | set(self._by_name)

    def state_of(self, name: str) -> FiberState | None:
        fiber = self._fibers.get(name)
        return fiber.state if fiber is not None else None


class Service:
    """Optional base for class-based providers.

    A subclass declares ``provide`` (the capability name) and ``inject`` (dependency names);
    passing a :class:`Context` registers it immediately. ``start``/``stop`` are optional
    lifecycle hooks run on ACTIVE / DISPOSED.
    """

    provide: str = ""
    inject: list[str] = []

    def __init__(self, ctx: Context | None = None, name: str | None = None) -> None:
        self.ctx = ctx
        self._name = name or self.provide
        if ctx is not None and self._name:
            ctx.service(self)

    def start(self) -> None:  # noqa: B027 - intentional no-op hook
        pass

    def stop(self) -> None:  # noqa: B027 - intentional no-op hook
        pass
