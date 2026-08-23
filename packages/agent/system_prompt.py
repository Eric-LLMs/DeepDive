"""SystemPrompt: an ordered, layered assembly of prompt sections (Cordis-style).

Sections are registered with an ``order`` and merged ascending, so different subsystems
(persona, tool guidance, memory, skills) can contribute independently without knowing each
other. A section's ``text`` may be a static string or an async callable receiving the
assemble context (e.g. ``{"user_msg": ...}``) — used by the memory/skills sections to do
on-demand retrieval. ``{{name}}`` placeholders are interpolated from registered variables.

The ``CacheBoundaryAssembler`` subclass adds a cache-boundary partition: ``STATIC_PREFIX``
(SOUL.md identity + core tool guidance, byte-identical across requests/steps) and
``PROJECT_CONTEXT`` (DEEPDIVE.md, stable within a project) render once and sit
above a fixed boundary marker, while only the ``DYNAMIC_SUFFIX`` (memory/skills/injected
runtime state) is re-rendered per step — so LLM providers can hit their prefix cache on the
stable head and the request stays small.
"""
from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

# Order conventions (matching Cordis's system-prompt ordering).
PERSONA_ORDER = 0
HARNESS_IDENTITY_ORDER = -100
PROJECT_CONTEXT_ORDER = -90
TOOL_GUIDANCE_ORDER = 100
MEMORY_ORDER = 200
SKILLS_ORDER = 250

SectionText = str | Callable[[dict], str | Awaitable[str]]


class PromptZone(Enum):
    """The cache-boundary partition of the system prompt.

    - ``STATIC_PREFIX``: byte-identical across requests/steps (SOUL.md identity + core
      tool guidance). Sits above the cache boundary; the provider reuses its KV cache here.
    - ``PROJECT_CONTEXT``: stable within a project (DEEPDIVE.md conventions).
    - ``DYNAMIC_SUFFIX``: changes every step (memory recall, skill directory, sandbox
      state, injected runtime context). Sits below the cache boundary.
    """

    STATIC_PREFIX = "static_prefix"
    PROJECT_CONTEXT = "project_context"
    DYNAMIC_SUFFIX = "dynamic_suffix"


# Internal-only separator marking the split between the stable head and the dynamic suffix.
# Kept as a constant for the zone partition's identity, but never rendered into the prompt —
# ``_render_zoned`` joins the zones with plain newlines so the literal never reaches the model.
CACHE_BOUNDARY = "\n\n<CACHE_BOUNDARY/>\n\n"


@dataclass
class PromptSection:
    name: str
    order: int
    text: SectionText
    zone: PromptZone = PromptZone.DYNAMIC_SUFFIX


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
    # Cache-boundary zone partition (populated by CacheBoundaryAssembler).
    static_prefix: str = ""
    project_context: str = ""
    dynamic_suffix: str = ""


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

    def section(
        self,
        name: str,
        order: int,
        text: SectionText,
        zone: PromptZone = PromptZone.DYNAMIC_SUFFIX,
    ) -> Callable[[], None]:
        """Register a prompt section; duplicate names raise. Returns a disposer.

        ``zone`` picks the cache-boundary partition (``PromptZone.DYNAMIC_SUFFIX`` by
        default for backward compatibility); it only matters to ``CacheBoundaryAssembler``.
        """
        if name in self._sections:
            raise ValueError(f"prompt section already registered: {name}")
        self._sections[name] = PromptSection(name, order, text, zone)

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


class CacheBoundaryAssembler(SystemPrompt):
    """Zone-aware prompt assembly with a stable cache boundary.

    The static/project zones render once and are reused across steps; only the dynamic
    suffix is re-rendered per step. ``inject()`` appends durable session-level content
    below the boundary (mirrors DSH's ``agent.inject()``); the loop calls ``begin_session()``
    at the start of each run so injected content lives within one turn. ``snapshot_key()``
    exposes the stable-prefix identity for prefix-cache observability.
    """

    def __init__(self) -> None:
        super().__init__()
        self._injected: list[tuple[str, str]] = []
        self._cached_static = ""
        self._cached_project = ""

    # ── session lifecycle ──
    def begin_session(self) -> None:
        """Reset per-session injected content (called at the start of each run)."""
        self._injected.clear()

    def inject(self, text: str, *, name: str | None = None) -> None:
        """Append durable dynamic content to the suffix; survives across steps."""
        self._injected.append((name or f"inject:{len(self._injected)}", text))

    # ── zone resolution ──
    async def _resolve_zone(self, zone: PromptZone, context: dict) -> str:
        parts: list[str] = []
        for section in sorted(self._sections.values(), key=lambda s: s.order):
            if section.zone is not zone:
                continue
            text = await _resolve(section.text, context)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _render_injected(self) -> str:
        if not self._injected:
            return ""
        return "\n\n".join(f"[{name}]\n{text}" for name, text in self._injected)

    async def assemble(self, context: dict | None = None) -> PromptAssembly:
        """Build the zone-partitioned assembly, caching the stable static/project head."""
        context = context or {}
        static = await self._resolve_zone(PromptZone.STATIC_PREFIX, context)
        project = await self._resolve_zone(PromptZone.PROJECT_CONTEXT, context)
        self._cached_static = static
        self._cached_project = project
        dynamic = await self.refresh_dynamic(context)

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
            sections=[],
            contexts=contexts,
            tools=tools,
            variables=variables,
            static_prefix=static,
            project_context=project,
            dynamic_suffix=dynamic,
        )

    async def refresh_dynamic(self, context: dict | None = None) -> str:
        """Re-render only the dynamic suffix (dynamic sections + injected content)."""
        context = context or {}
        dynamic_sections = await self._resolve_zone(PromptZone.DYNAMIC_SUFFIX, context)
        injected = self._render_injected()
        return "\n\n".join(p for p in (dynamic_sections, injected) if p)

    def snapshot_key(self) -> str:
        """Identity of the stable prefix (static + project) — prefix-cache observability."""
        return hashlib.sha256(
            (self._cached_static + "\n\n" + self._cached_project).encode("utf-8")
        ).hexdigest()[:16]


def _interpolate(assembly: PromptAssembly, text: str) -> str:
    for name, value in assembly.variables.items():
        text = text.replace("{{" + name + "}}", value or "")
    return text


def _render_zoned(assembly: PromptAssembly) -> str:
    """Join the stable head and the dynamic suffix with plain newlines.

    The ``CACHE_BOUNDARY`` marker is an internal-only separator for the zone partition; it is
    deliberately not rendered — the model should never see it. The stable head and the dynamic
    suffix are joined with ``"\\n\\n"`` so the token-position split still holds for the provider's
    prefix cache without leaking a markup literal into the prompt.
    """
    stable = "\n\n".join(p for p in (assembly.static_prefix, assembly.project_context) if p)
    stable = _interpolate(assembly, stable)
    dynamic = _interpolate(assembly, assembly.dynamic_suffix)
    if not dynamic:
        return stable
    if stable:
        return stable + "\n\n" + dynamic
    return dynamic


def render_prompt(assembly: PromptAssembly) -> str:
    """Render the assembled sections into the final system prompt.

    Zone-partitioned assemblies (from :class:`CacheBoundaryAssembler`) join static + project
    with the dynamic suffix via plain newlines (the ``CACHE_BOUNDARY`` marker is an internal
    separator only and never appears in the output); legacy assemblies fall back to the flat
    join. ``{{name}}`` variables interpolate everywhere.
    """
    if assembly.static_prefix or assembly.project_context or assembly.dynamic_suffix:
        return _render_zoned(assembly)

    return "\n\n".join(
        _interpolate(assembly, s) for s in assembly.sections if s
    )
