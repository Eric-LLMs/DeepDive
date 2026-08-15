"""Agent module: Cordis-style DI + plugin runtime + loop + plugins + memory + skills + prompt.

Exposes composable parts for assembly/testing/replacement in deps.
"""
from agent.di import CapabilityError, Context, Fiber, FiberState, Service
from agent.decisions import (
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
from agent.events import EventBus
from agent.harness import FakeLLM, assistant, tool_call
from agent.loop import AgentLLMPort, AgentResult, ReactLoopAgent
from agent.memory import MEMORY_TYPES, FileMemoryStore, Memory, MemoryStore
from agent.plugins import (
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
from agent.runtime import ToolRuntime
from agent.sessions import SessionEvent, SessionLog
from agent.skills import Skill, SkillRegistry
from agent.system_prompt import (
    HARNESS_IDENTITY_ORDER,
    MEMORY_ORDER,
    PERSONA_ORDER,
    SKILLS_ORDER,
    TOOL_GUIDANCE_ORDER,
    PromptAssembly,
    PromptSection,
    SystemPrompt,
    render_prompt,
)
from agent.tools import (
    ToolArgsError,
    ToolDefinition,
    ToolOutput,
    ToolOutputError,
    define_tool,
)

__all__ = [
    "Context",
    "Service",
    "Fiber",
    "FiberState",
    "CapabilityError",
    "ReactLoopAgent",
    "AgentLLMPort",
    "AgentResult",
    "SystemPrompt",
    "PromptSection",
    "PromptAssembly",
    "render_prompt",
    "PERSONA_ORDER",
    "HARNESS_IDENTITY_ORDER",
    "TOOL_GUIDANCE_ORDER",
    "MEMORY_ORDER",
    "SKILLS_ORDER",
    "define_tool",
    "ToolDefinition",
    "ToolOutput",
    "ToolArgsError",
    "ToolOutputError",
    "ToolRuntime",
    "EventBus",
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
    "Memory",
    "MEMORY_TYPES",
    "SessionLog",
    "SessionEvent",
    "FakeLLM",
    "assistant",
    "tool_call",
]
