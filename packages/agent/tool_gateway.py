"""Deferred tool loading: a compact catalog + per-request visibility + lazy schema mount.

With hundreds of registered tools, exposing every full schema each request bloats the
prompt and churns the provider's prefix cache. Instead the model always sees a compact
``name + blurb`` catalog plus a handful of core resident tools, and calls ``tool_search``
to pull the full schemas it needs — those are mounted into the model-visible ``tools``
array for the following steps (out-of-band in the OpenAI function-calling request).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent.decisions import ToolExecution, text_block
from agent.tool_permissions import permission_names
from agent.tools import ToolDefinition, ToolOutput, define_tool


def _blurb(tool: ToolDefinition, max_chars: int = 120) -> str:
    desc = tool.description.replace("\n", " ").strip()
    return desc if len(desc) <= max_chars else desc[: max_chars - 1].rstrip() + "…"


@dataclass(frozen=True)
class ToolIndexEntry:
    """One compact catalog line: name + one-line blurb + permission tags. No full schema."""

    name: str
    blurb: str
    permissions: tuple[str, ...] = ()


class ToolCatalog:
    """Compact name+blurb index over a :class:`ToolRuntime`. Never carries full schemas."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def entries(self, context: dict | None = None) -> list[ToolIndexEntry]:
        return [
            ToolIndexEntry(
                name=tool.name,
                blurb=_blurb(tool),
                permissions=permission_names(tool.permissions),
            )
            for tool in self._runtime.all()
        ]

    def render_index(
        self,
        *,
        limit: int = 100,
        budget_chars: int = 500,
        blurb_chars: int = 60,
    ) -> str:
        """One ``- name: blurb`` line per tool, truncated to a character budget.

        This is what lives in the static prompt prefix. Each line's blurb is capped at
        ``blurb_chars`` (compact — the full 120-char blurb still reaches the model via
        ``tool_search``), so the whole catalog advertises far more tools within the budget.
        """
        lines: list[str] = []
        used = 0
        for entry in self.entries():
            if len(lines) >= limit:
                break
            blurb = (
                entry.blurb
                if len(entry.blurb) <= blurb_chars
                else entry.blurb[: blurb_chars - 1].rstrip() + "…"
            )
            line = f"- {entry.name}: {blurb}"
            if used and used + len(line) > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    async def search(self, query: str, limit: int = 10) -> list[ToolIndexEntry]:
        """Deterministic scoring over name + blurb + permission tags (word-level).

        Multi-word queries score each token separately (a name hit beats a blurb hit),
        so ``"edit file"`` still matches ``edit_file`` and partial matches rank sensibly.
        """
        tokens = [t for t in (query or "").lower().split() if t]
        if not tokens:
            return []
        scored: list[tuple[int, ToolIndexEntry]] = []
        for entry in self.entries():
            name = entry.name.lower()
            blurb = entry.blurb.lower()
            score = 0
            for token in tokens:
                if token in name:
                    score += 3
                if token in blurb:
                    score += 1
                if any(token in p for p in entry.permissions):
                    score += 1
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]


class ToolVisibilityPolicy:
    """Per-request tool visibility: scope allowlist / denylist / presentAs modes.

    Every mutation returns a disposer so a scoped override can be rolled back. The
    gateway combines this with the deferred mount: allowlisted tools are always visible,
    denied tools are never visible (even if mounted).
    """

    def __init__(self) -> None:
        self._allow: set[str] = set()
        self._deny: set[str] = set()
        self._modes: dict[str, set[str]] = {}

    def allow(self, name: str) -> Callable[[], None]:
        """Expose a tool in the current scope without requiring a ``tool_search``."""
        self._allow.add(name)

        def dispose() -> None:
            self._allow.discard(name)

        return dispose

    def deny(self, name: str) -> Callable[[], None]:
        """Hide a tool entirely (overrides allow and mount)."""
        self._deny.add(name)

        def dispose() -> None:
            self._deny.discard(name)

        return dispose

    def present_as(self, mode: str, names: Iterable[str]) -> Callable[[], None]:
        """DSH-style presentAs: within ``mode`` the visible set is exactly ``names``."""
        self._modes[mode] = set(names)

        def dispose() -> None:
            self._modes.pop(mode, None)

        return dispose

    def allowlist(self, context: dict | None = None) -> set[str]:
        mode = (context or {}).get("tool_mode")
        if mode in self._modes:
            return set(self._modes[mode])
        return set(self._allow)

    def denylist(self, context: dict | None = None) -> set[str]:
        return set(self._deny)

    def visible(self, name: str, context: dict | None = None) -> bool:
        allowlist = self.allowlist(context)
        return name in allowlist and name not in self._deny


class ToolGateway:
    """Computes the per-step model-visible tool schemas: core + allowlisted + mounted.

    ``mount(name)`` lazily pulls a tool's full schema into the visible set after the model
    asked for it via ``tool_search``. The mounted set resets per session.
    """

    def __init__(
        self,
        runtime,
        catalog: ToolCatalog | None = None,
        policy: ToolVisibilityPolicy | None = None,
        core_names: Iterable[str] = ("tool_search", "skill", "memory_search"),
    ) -> None:
        self._runtime = runtime
        self.catalog = catalog or ToolCatalog(runtime)
        self.policy = policy or ToolVisibilityPolicy()
        self._core: set[str] = set(core_names)
        self._mounted: set[str] = set()

    # ── session lifecycle ──
    def reset_session(self) -> None:
        """Clear the mounted set at the start of each agent run."""
        self._mounted.clear()

    # ── visibility ──
    def core_schemas(self) -> list[dict]:
        """Schemas of the core resident tools (always in the tools array)."""
        return self._schemas_of(self._core)

    def visible_schemas(self, context: dict | None = None) -> list[dict]:
        """core ∪ mounted ∪ scope-allowlist, minus denylist, in deterministic order."""
        context = context or {}
        names = set(self._core) | self._mounted | self.policy.allowlist(context)
        names -= self.policy.denylist(context)
        ordered = [n for n in self._runtime_order() if n in names]
        return self._schemas_of(ordered)

    def mount(self, name: str) -> bool:
        """Lazily expose a tool's full schema (after the model asked for it)."""
        if self._runtime.get(name) is None:
            return False
        self._mounted.add(name)
        return True

    # ── internals ──
    def _schemas_of(self, names: Iterable[str]) -> list[dict]:
        out: list[dict] = []
        for name in names:
            tool = self._runtime.get(name)
            if tool is not None:
                out.append({"type": "function", "function": tool.schema()})
        return out

    def _runtime_order(self) -> list[str]:
        return [tool.name for tool in self._runtime.all()]


def tool_search_tool(catalog: ToolCatalog, gateway: ToolGateway) -> ToolDefinition:
    """The builtin ``tool_search`` meta-tool: query the catalog, mount full schemas.

    The model sees only the compact catalog by default; calling this mounts the matched
    tools' schemas so they become directly callable in subsequent steps.
    """

    async def execute(args: dict, exec: ToolExecution) -> list[dict]:
        hits = await catalog.search(args.get("query", ""), args.get("limit", 10))
        for hit in hits:
            gateway.mount(hit.name)
        return [
            {"name": hit.name, "description": hit.blurb, "permissions": list(hit.permissions)}
            for hit in hits
        ]

    return define_tool(
        name="tool_search",
        description=(
            "Search the tool catalog for tools relevant to your goal. The matches' full "
            "schemas are loaded for subsequent steps, so you can call them directly. "
            "Use this instead of guessing a tool's name or arguments."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The capability you need."},
                "limit": {"type": "integer", "description": "Maximum results to return."},
            },
            "required": ["query"],
        },
        output=ToolOutput(
            schema={"type": "array"},
            render=lambda args, value: [
                text_block(json.dumps(value, ensure_ascii=False, default=str))
            ],
        ),
        execute=execute,
        is_concurrency_safe=True,
    )
