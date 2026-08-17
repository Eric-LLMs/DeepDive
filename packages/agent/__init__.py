"""Agent module: Cordis-style DI + microkernel orchestration + loop + plugins + memory + skills.

Exposes composable parts for assembly/testing/replacement in deps.
"""
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
from agent.di import CapabilityError, Context, Fiber, FiberState, Service
from agent.events import EventBus
from agent.fs_tools import register_fs_tools
from agent.harness import FakeLLM, assistant, tool_call
from agent.kernel import AgentKernel, KernelConfig
from agent.loop import AgentLLMPort, AgentResult, ReactLoopAgent
from agent.memory import MEMORY_TYPES, FileMemoryStore, Memory, MemoryStore
from agent.memory.retrieval import MemoryHit, RRFMemoryRetriever
from agent.memory.service import MemoryService, memory_save_tool, memory_search_tool
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
from agent.sandbox import Sandbox, SandboxDecision, SandboxRule
from agent.sessions import SessionEvent, SessionLog
from agent.skills import Skill, SkillCatalog, SkillRegistry, skill_tool
from agent.system_prompt import (
    CACHE_BOUNDARY,
    HARNESS_IDENTITY_ORDER,
    MEMORY_ORDER,
    PERSONA_ORDER,
    SKILLS_ORDER,
    TOOL_GUIDANCE_ORDER,
    CacheBoundaryAssembler,
    PromptAssembly,
    PromptSection,
    PromptZone,
    SystemPrompt,
    render_prompt,
)
from agent.tool_gateway import (
    ToolCatalog,
    ToolGateway,
    ToolIndexEntry,
    ToolVisibilityPolicy,
    tool_search_tool,
)
from agent.tool_permissions import ToolPermission
from agent.tools import (
    ToolArgsError,
    ToolDefinition,
    ToolOutput,
    ToolOutputError,
    define_tool,
)

__all__ = [
    "CACHE_BOUNDARY",
    "HARNESS_IDENTITY_ORDER",
    "MEMORY_ORDER",
    "MEMORY_TYPES",
    "PERSONA_ORDER",
    "POST_TOOL_USE",
    "PRE_TOOL_USE",
    "SESSION_END",
    "SESSION_START",
    "SKILLS_ORDER",
    "TOOL_EXECUTE",
    "TOOL_GUIDANCE_ORDER",
    "TOOL_RESULT",
    "AgentKernel",
    "AgentLLMPort",
    "AgentResult",
    "CacheBoundaryAssembler",
    "CapabilityError",
    "ContentBlock",
    "Context",
    "EventBus",
    "FakeLLM",
    "Fiber",
    "FiberState",
    "FileMemoryStore",
    "KernelConfig",
    "Memory",
    "MemoryHit",
    "MemoryService",
    "MemoryStore",
    "Plugin",
    "PluginManager",
    "PostToolDecision",
    "PreToolDecision",
    "PromptAssembly",
    "PromptSection",
    "PromptZone",
    "RRFMemoryRetriever",
    "ReactLoopAgent",
    "Sandbox",
    "SandboxDecision",
    "SandboxRule",
    "Service",
    "SessionEvent",
    "SessionLog",
    "Skill",
    "SkillCatalog",
    "SkillRegistry",
    "SystemPrompt",
    "ToolArgsError",
    "ToolCatalog",
    "ToolDefinition",
    "ToolExecution",
    "ToolExecutionFailure",
    "ToolExecutionResult",
    "ToolExecutionSuccess",
    "ToolFailure",
    "ToolGateway",
    "ToolIndexEntry",
    "ToolOutput",
    "ToolOutputError",
    "ToolPermission",
    "ToolRuntime",
    "ToolVisibilityPolicy",
    "assistant",
    "define_tool",
    "memory_save_tool",
    "memory_search_tool",
    "observe",
    "register_builtin_plugins",
    "register_fs_tools",
    "render_prompt",
    "skill_tool",
    "text_block",
    "tool_call",
    "tool_search_tool",
    "waterfall",
]
