"""Plugin system: hook + plugin + manager + built-in plugins."""
from core.agent.plugins.base import Plugin
from core.agent.plugins.builtin import register_builtin_plugins
from core.agent.plugins.hooks import Hook, HookContext, HookEvent, HookResult
from core.agent.plugins.manager import PluginManager

__all__ = [
    "Plugin",
    "PluginManager",
    "Hook",
    "HookContext",
    "HookEvent",
    "HookResult",
    "register_builtin_plugins",
]
