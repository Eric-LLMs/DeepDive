"""Plugin: a packaging unit of tools + skills + listeners + guards.

A plugin extends every capability at once; the manager mounts each part into its runtime
(tools → ToolRuntime, guards → ToolRuntime, listeners → EventBus, skills → SkillRegistry).
"""
from dataclasses import dataclass, field

from core.agent.decisions import Guard
from core.agent.skills import Skill
from core.agent.tools import ToolDefinition


@dataclass
class Plugin:
    name: str
    description: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    listeners: list = field(default_factory=list)  # (kind, event, handler) tuples
    guards: list[Guard] = field(default_factory=list)
