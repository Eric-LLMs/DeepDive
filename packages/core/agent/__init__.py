"""Agent module: loop + tools + plugins + memory + skills + context assembly.

Exposes composable parts for assembly/testing/replacement in deps.
"""
from core.agent.context import ContextBuilder
from core.agent.harness import FakeLLM, assistant, tool_call
from core.agent.loop import Agent, AgentLLMPort, AgentResult
from core.agent.memory import FileMemoryStore, MemoryStore
from core.agent.plugins import (
    Hook,
    HookContext,
    HookEvent,
    HookResult,
    Plugin,
    PluginManager,
    register_builtin_plugins,
)
from core.agent.skills import Skill, SkillRegistry
from core.agent.tools import Tool, ToolRegistry, ToolResult, build_default_tools

__all__ = [
    "Agent",
    "AgentLLMPort",
    "AgentResult",
    "ContextBuilder",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_default_tools",
    "Plugin",
    "PluginManager",
    "Hook",
    "HookContext",
    "HookEvent",
    "HookResult",
    "register_builtin_plugins",
    "Skill",
    "SkillRegistry",
    "MemoryStore",
    "FileMemoryStore",
    "FakeLLM",
    "assistant",
    "tool_call",
]
