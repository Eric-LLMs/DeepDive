"""Plugin: a packaging unit of tools + skills + listeners + guards.

A plugin extends every capability at once; the manager mounts each part into its runtime
(tools → ToolRuntime, guards → ToolRuntime, listeners → EventBus, skills → SkillRegistry).

Dependencies are declared, not hard-wired: ``inject`` lists capability names the plugin
requires before it can mount; ``provides`` exports named services into the shared
:class:`Context` so other plugins can inject them. Plugins that declare nothing behave exactly
as before (flat, order-independent).
"""
from dataclasses import dataclass, field
from typing import Any

from agent.decisions import Guard
from agent.skills import Skill
from agent.tools import ToolDefinition


@dataclass
class Plugin:
    name: str
    description: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    listeners: list = field(default_factory=list)  # (kind, event, handler) tuples
    guards: list[Guard] = field(default_factory=list)
    inject: list[str] = field(default_factory=list)  # capability names required before mount
    provides: dict[str, Any] = field(default_factory=dict)  # named services exported on mount
