"""Plugin: a packaging unit of a set of hooks + tools + skills.

A plugin can extend all three capabilities at once; registration only mounts schemas, execution is lazily triggered.
"""
from dataclasses import dataclass, field

from core.agent.plugins.hooks import Hook
from core.agent.skills import Skill
from core.agent.tools import Tool


@dataclass
class Plugin:
    name: str
    description: str = ""
    hooks: list[Hook] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
