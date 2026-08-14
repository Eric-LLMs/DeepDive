"""Plugin manager: register plugins into the ToolRuntime + EventBus + SkillRegistry.

Registration is reversible: every mount returns a disposer, collected per-plugin so that
``unregister`` can roll a plugin back cleanly.
"""
import importlib.util
from pathlib import Path

from core.agent.plugins.base import Plugin
from core.agent.runtime import ToolRuntime
from core.agent.skills import SkillRegistry


class PluginManager:
    def __init__(self, runtime: ToolRuntime, skills: SkillRegistry) -> None:
        self.runtime = runtime
        self.skills = skills
        self._plugins: dict[str, Plugin] = {}
        self._disposers: dict[str, list] = {}

    def register(self, plugin: Plugin) -> None:
        """Mount a plugin's tools/guards/listeners/skills; collect disposers."""
        self._plugins[plugin.name] = plugin
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

    def unregister(self, name: str) -> None:
        """Roll back a plugin by running its collected disposers."""
        for dispose in self._disposers.pop(name, []):
            dispose()
        self._plugins.pop(name, None)

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return list(self._plugins)

    def discover(self, directory: Path) -> int:
        """Scan the directory for */plugin.py and load the module-level PLUGIN object."""
        count = 0
        for plugin_file in sorted(Path(directory).rglob("plugin.py")):
            plugin = self._load_plugin_file(plugin_file)
            if plugin:
                self.register(plugin)
                count += 1
        return count

    @staticmethod
    def _load_plugin_file(path: Path) -> Plugin | None:
        module_name = f"deepgloss_plugin_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "PLUGIN", None)
