"""Plugin system: plugin + manager + built-in plugins + event listeners."""
from core.agent.plugins.base import Plugin
from core.agent.plugins.builtin import register_builtin_plugins
from core.agent.plugins.hooks import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    TOOL_EXECUTE,
    TOOL_RESULT,
    observe,
    waterfall,
)
from core.agent.plugins.manager import PluginManager

__all__ = [
    "Plugin",
    "PluginManager",
    "register_builtin_plugins",
    "SESSION_START",
    "SESSION_END",
    "PRE_TOOL_USE",
    "TOOL_EXECUTE",
    "POST_TOOL_USE",
    "TOOL_RESULT",
    "waterfall",
    "observe",
]
