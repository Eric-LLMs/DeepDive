"""Plugin manager: register plugins into the ToolRuntime + EventBus + SkillRegistry.

Registration is reversible: every mount returns a disposer, collected per-plugin so that
``unregister`` can roll a plugin back cleanly.

Dependency resolution is delegated to the :class:`Context` / :class:`Fiber` state machine
(``inject``/``provides``): a plugin stays PENDING until all injected capabilities are ACTIVE,
then mounts. Load order therefore falls out of dependencies, not registration order.

Hot reload: ``watch`` polls a directory and applies file add/change/remove as
register/reload/unregister, so plugins can be edited or dropped without restarting.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from agent.di import CapabilityError, Context, FiberState
from agent.plugins.base import Plugin
from agent.runtime import ToolRuntime
from agent.skills import SkillRegistry


class PluginManager:
    def __init__(
        self,
        runtime: ToolRuntime,
        skills: SkillRegistry,
        ctx: Context | None = None,
    ) -> None:
        self.runtime = runtime
        self.skills = skills
        self.ctx = ctx or Context()
        self._plugins: dict[str, Plugin] = {}  # plugin name -> Plugin (for get())
        self._disposers: dict[str, list] = {}
        self._sources: dict[Path, str] = {}  # plugin file path -> plugin name
        self._mtimes: dict[Path, int] = {}  # plugin file path -> mtime_ns

    # ── registration ──
    def register(self, plugin: Plugin) -> None:
        """Mount a plugin once its ``inject`` deps are satisfiable, else hold it PENDING."""
        if plugin.name in self._plugins:
            raise ValueError(f"plugin already registered: {plugin.name}")
        self._plugins[plugin.name] = plugin
        self.ctx.plugin(
            plugin,
            mount=lambda: self._mount(plugin),
            unmount=lambda: self._unmount(plugin),
        )

    def _mount(self, plugin: Plugin) -> None:
        """Mount a plugin's parts into the runtime (provides are handled by the Context)."""
        disposers: list = []
        for tool in plugin.tools:
            disposers.append(self.runtime.register(tool))
        for skill in plugin.skills:
            self.skills.register(skill)
        for guard in plugin.guards:
            disposers.append(self.runtime.guard(guard))
        for kind, event, handler in plugin.listeners:
            if kind == "waterfall":
                disposers.append(self.runtime.events.on(event, handler))
            else:
                disposers.append(self.runtime.events.observe(event, handler))
        self._disposers[plugin.name] = disposers

    def _unmount(self, plugin: Plugin) -> None:
        """Roll back a plugin by running its collected disposers."""
        for dispose in self._disposers.pop(plugin.name, []):
            dispose()

    def unregister(self, name: str) -> None:
        self.ctx.unregister(name)
        self._plugins.pop(name, None)

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return [n for n in self._plugins if self.ctx.state_of(n) is FiberState.ACTIVE]

    def pending_names(self) -> list[str]:
        return [n for n in self._plugins if self.ctx.state_of(n) is FiberState.PENDING]

    # ── validation (Cordis-style FAILED stage) ──
    def validate(self) -> None:
        """Reject a broken plugin set before mounting, instead of silently stalling PENDING."""
        known = self.ctx.provided_names()
        for fiber in self.ctx.fibers():
            for name in fiber.inject:
                if name not in known:
                    raise CapabilityError(
                        f"plugin {fiber.name!r} injects unknown capability {name!r}"
                    )
        self._check_cycles()

    def _check_cycles(self) -> None:
        """Detect dependency cycles between plugins (DFS three-colour)."""
        fibers = self.ctx.fibers()
        graph: dict[str, set[str]] = {f.name: set() for f in fibers}
        for fiber in fibers:
            for name in fiber.inject:
                provider = self.ctx.provider_of(name)
                if provider is not None and provider.name != fiber.name:
                    graph[fiber.name].add(provider.name)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {name: WHITE for name in graph}
        stack: list[str] = []

        def visit(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for neighbour in graph[node]:
                if color[neighbour] == GRAY:
                    cycle = " -> ".join(stack[stack.index(neighbour):] + [neighbour])
                    raise ValueError(f"plugin dependency cycle: {cycle}")
                if color[neighbour] == WHITE:
                    visit(neighbour)
            stack.pop()
            color[node] = BLACK

        for node in graph:
            if color[node] == WHITE:
                visit(node)

    # ── discovery + hot reload ──
    def discover(self, directory: Path) -> int:
        """Scan the directory for */plugin.py and load the module-level PLUGIN object."""
        count = 0
        for plugin_file in sorted(Path(directory).rglob("plugin.py")):
            plugin = self._load_plugin_file(plugin_file)
            if plugin is not None:
                self._sources[plugin_file] = plugin.name
                self._mtimes[plugin_file] = plugin_file.stat().st_mtime_ns
                self.register(plugin)
                count += 1
        self.validate()
        return count

    def reload(self, path: Path) -> bool:
        """Load a plugin file's current code and swap it in (keep the old one if the new fails)."""
        plugin = self._load_plugin_file(path)
        if plugin is None:
            return False
        name = self._sources.get(path)
        if name:
            self.unregister(name)
        self._sources[path] = plugin.name
        self.register(plugin)
        self.validate()
        return True

    def remove(self, path: Path) -> bool:
        """Unregister the plugin previously loaded from ``path`` (file deleted)."""
        name = self._sources.pop(path, None)
        self._mtimes.pop(path, None)
        if name is None:
            return False
        self.unregister(name)
        return True

    async def watch(self, directory: Path, poll_interval: float = 1.0) -> None:
        """Poll ``directory`` and apply file add/change/remove as register/reload/unregister.

        Runs forever (intended as an asyncio background task). Watchdog-free: mtime polling,
        so no extra dependency. A failing plugin file is skipped, not fatal.
        """
        directory = Path(directory)
        while True:
            try:
                self._sync(directory)
            except Exception:  # noqa: BLE001 - a bad plugin must not kill the watcher
                pass
            await asyncio.sleep(poll_interval)

    def _sync(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        on_disk = {p for p in directory.rglob("plugin.py") if p.is_file()}
        known = set(self._sources)

        for path in sorted(on_disk - known):  # newly added
            plugin = self._load_plugin_file(path)
            if plugin is not None:
                self._sources[path] = plugin.name
                self._mtimes[path] = path.stat().st_mtime_ns
                self.register(plugin)

        for path in sorted(on_disk & known):  # possibly changed
            mtime = path.stat().st_mtime_ns
            if self._mtimes.get(path) != mtime:
                self.reload(path)
                self._mtimes[path] = path.stat().st_mtime_ns

        for path in sorted(known - on_disk):  # deleted
            self.remove(path)

    @staticmethod
    def _load_plugin_file(path: Path) -> Plugin | None:
        module_name = f"deepdive_plugin_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "PLUGIN", None)
