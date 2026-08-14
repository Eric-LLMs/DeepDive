"""Agent module: plugin-based tool runtime + loop + plugins + memory + skills + context.

Exposes composable parts for assembly/testing/replacement in deps.
"""
from core.agent.capabilities import Capabilities, CapabilityError
from core.agent.context import ContextBuilder
from core.agent.decisions import (
    ContentBlock,
    PostToolDecision,
    PreToolDecision,
    ToolExecution,
    ToolExecutionFailure,
    ToolExecutionResult,
    ToolExecutionSuccess,
    ToolFailure,
    text_block,
)
from core.agent.events import EventBus
from core.agent.harness import FakeLLM, assistant, tool_call
from core.agent.loop import Agent, AgentLLMPort, AgentResult
from core.agent.memory import FileMemoryStore, MemoryStore
from core.agent.plugins import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    TOOL_EXECUTE,
    TOOL_RESULT,
    Plugin,
    PluginManager,
    observe,
    register_builtin_plugins,
    waterfall,
)
from core.agent.runtime import ToolRuntime
from core.agent.skills import Skill, SkillRegistry
from core.agent.tools import (
    ToolArgsError,
    ToolDefinition,
    ToolOutput,
    ToolOutputError,
    define_tool,
)

__all__ = [
    "Agent",
    "AgentLLMPort",
    "AgentResult",
    "ContextBuilder",
    "define_tool",
    "ToolDefinition",
    "ToolOutput",
    "ToolArgsError",
    "ToolOutputError",
    "ToolRuntime",
    "EventBus",
    "Capabilities",
    "CapabilityError",
    "ContentBlock",
    "text_block",
    "PreToolDecision",
    "PostToolDecision",
    "ToolFailure",
    "ToolExecution",
    "ToolExecutionResult",
    "ToolExecutionSuccess",
    "ToolExecutionFailure",
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
    "Skill",
    "SkillRegistry",
    "MemoryStore",
    "FileMemoryStore",
    "FakeLLM",
    "assistant",
    "tool_call",
]
