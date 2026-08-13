"""Plugin manager: registration + directory discovery + hook dispatch.

Registration != execution: register() only mounts schemas (hook/tool/skill) into their registries at very low cost;
actual execution happens when the Agent loop triggers a hook / calls a tool.
"""
import importlib.util
from pathlib import Path

from core.agent.plugins.base import Plugin
from core.agent.plugins.hooks import HookContext, HookEvent, HookResult
from core.agent.skills import SkillRegistry
from core.agent.tools import ToolRegistry


class PluginManager:
    def __init__(self, tools: ToolRegistry, skills: SkillRegistry) -> None:
        self.tools = tools
        self.skills = skills
        self._plugins: dict[str, Plugin] = {}
        self._hooks: dict[HookEvent, list] = {}

    def register(self, plugin: Plugin) -> None:
        """Register a plugin (lazy: only mounts schemas, executes no handlers)."""
        self._plugins[plugin.name] = plugin
        for tool in plugin.tools:
            self.tools.register(tool)
        for skill in plugin.skills:
            self.skills.register(skill)
        for hook in plugin.hooks:
            self._hooks.setdefault(hook.event, []).append(hook)

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

    async def dispatch(
        self, event: HookEvent, ctx: HookContext
    ) -> tuple[bool, dict | None, list[dict]]:
        """Trigger all hooks for this event in registration order, returns (blocked, updated_args, new_messages)."""
        blocked = False
        updated_args: dict | None = None
        new_messages: list[dict] = []
        for hook in self._hooks.get(event, []):
            result: HookResult = await hook.run(ctx)
            if result.action == "block":
                blocked = True
                if result.message:
                    new_messages.append({"role": "system", "content": result.message})
                break
            if result.updated_args:
                updated_args = {**(updated_args or {}), **result.updated_args}
            new_messages.extend(result.new_messages)
        return blocked, updated_args, new_messages
