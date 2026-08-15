"""SystemPrompt: an ordered, layered assembly of prompt sections (Cordis-style).

Sections are registered with an ``order`` and merged ascending, so different subsystems
(persona, tool guidance, memory, skills) can contribute independently without knowing each
other. A section's ``text`` may be a static string or an async callable receiving the
assemble context (e.g. ``{"user_msg": ...}``) — used by the memory/skills sections to do
on-demand retrieval. ``{{name}}`` placeholders are interpolated from registered variables.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Order conventions (matching Cordis's system-prompt ordering).
PERSONA_ORDER = 0
HARNESS_IDENTITY_ORDER = -100
TOOL_GUIDANCE_ORDER = 100
MEMORY_ORDER = 200
SKILLS_ORDER = 250

SectionText = str | Callable[[dict], str | Awaitable[str]]


@dataclass
class PromptSection:
    name: str
    order: int
    text: SectionText


@dataclass
class PromptContext:
    """A dynamic runtime-context entry (rendered as a user-role snapshot, not a section)."""

    name: str
    order: int
    text: SectionText


@dataclass
class PromptAssembly:
    sections: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)


async def _resolve(text: SectionText, context: dict) -> str:
    if callable(text):
        text = text(context)
        if inspect.isawaitable(text):
            text = await text
    return text or ""


class SystemPrompt:
    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}
        self._contexts: dict[str, PromptContext] = {}
        self._variables: dict[str, Callable[[dict], str | None]] = {}
        self._tool_providers: list[Callable[[dict], list[dict]]] = []

    def section(self, name: str, order: int, text: SectionText) -> Callable[[], None]:
        """Register a prompt section; duplicate names raise. Returns a disposer."""
        if name in self._sections:
            raise ValueError(f"prompt section already registered: {name}")
        self._sections[name] = PromptSection(name, order, text)

        def dispose() -> None:
            self._sections.pop(name, None)

        return dispose

    def context(self, name: str, order: int, text: SectionText) -> Callable[[], None]:
        if name in self._contexts:
            raise ValueError(f"prompt context already registered: {name}")
        self._contexts[name] = PromptContext(name, order, text)

        def dispose() -> None:
            self._contexts.pop(name, None)

        return dispose

    def variable(
        self, name: str, provider: Callable[[dict], str | None]
    ) -> Callable[[], None]:
        """Register a ``{{name}}`` variable provider. Names must be ``[a-z][a-z0-9_]*``."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"invalid variable name: {name!r}")
        self._variables[name] = provider

        def dispose() -> None:
            self._variables.pop(name, None)

        return dispose

    def tools(self, provider: Callable[[dict], list[dict]]) -> Callable[[], None]:
        """Register a tool-schema provider; merged into the assembly."""
        self._tool_providers.append(provider)

        def dispose() -> None:
            if provider in self._tool_providers:
                self._tool_providers.remove(provider)

        return dispose

    async def assemble(self, context: dict | None = None) -> PromptAssembly:
        """Build the ordered assembly (sections/contexts/tools/variables)."""
        context = context or {}

        sections: list[str] = []
        for section in sorted(self._sections.values(), key=lambda s: s.order):
            text = await _resolve(section.text, context)
            if text:
                sections.append(text)

        contexts: list[str] = []
        for entry in sorted(self._contexts.values(), key=lambda c: c.order):
            text = await _resolve(entry.text, context)
            if text:
                contexts.append(text)

        tools: list[dict] = []
        for provider in self._tool_providers:
            tools.extend(provider(context) or [])

        variables: dict[str, str] = {}
        for name, provider in self._variables.items():
            value = provider(context)
            if value is not None:
                variables[name] = value

        return PromptAssembly(
            sections=sections, contexts=contexts, tools=tools, variables=variables
        )


def render_prompt(assembly: PromptAssembly) -> str:
    """Render the assembled sections into the final system prompt.

    Interpolates ``{{name}}`` variables, drops empty sections, joins with blank lines.
    """

    def interpolate(text: str) -> str:
        for name, value in assembly.variables.items():
            text = text.replace("{{" + name + "}}", value or "")
        return text

    return "\n\n".join(interpolate(s) for s in assembly.sections if s)
