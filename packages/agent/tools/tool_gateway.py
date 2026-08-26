"""Deferred tool loading: a compact catalog + per-request visibility + lazy schema mount.

With hundreds of registered tools, exposing every full schema each request bloats the
prompt and churns the provider's prefix cache. Instead the model always sees a compact
``name + blurb`` catalog plus a handful of core resident tools, and calls ``tool_search``
to pull the tools it needs — those are mounted into the model-visible ``tools`` array as
stable ``name + description`` stubs (defer_loading style), keeping the cached array small
and byte-stable, while each tool's full schema reaches the model through the ``tool_search``
result (below the cache boundary).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent.engine.decisions import ToolExecution, text_block
from agent.tools.definition import ToolDefinition, ToolOutput, define_tool
from agent.tools.tool_permissions import permission_names


def _blurb(tool: ToolDefinition, max_chars: int = 120) -> str:
    desc = tool.description.replace("\n", " ").strip()
    return desc if len(desc) <= max_chars else desc[: max_chars - 1].rstrip() + "…"


# Deferred-loading stub description cap. A stub's description is longer than the compact catalog
# blurb so the model can form arguments; the full schema still arrives via the tool_search result.
STUB_DESC_MAX = 400


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
        """core ∪ mounted ∪ scope-allowlist, minus denylist, in deterministic order.

        Stable tools (core + scope-allowlisted) carry full schemas; deferred tools that were
        mounted mid-run appear as stable ``name + description`` stubs with an empty parameter
        shape. Once mounted, a stub never changes across steps, so the provider's prefix cache
        stays warm — the full schema reaches the model through the ``tool_search`` result instead
        of being injected into the cached tools array.
        """
        context = context or {}
        names = set(self._core) | self._mounted | self.policy.allowlist(context)
        names -= self.policy.denylist(context)
        full = set(self._core) | self.policy.allowlist(context)
        out: list[dict] = []
        for name in self._runtime_order():
            if name not in names:
                continue
            out.append(self._schemas_of([name])[0] if name in full else self._stub_schema(name))
        return out

    def mount(self, name: str) -> bool:
        """Lazily expose a tool after the model asked for it (via ``tool_search``).

        Adds the tool as a deferred-loading stub to the visible set; the model can call it
        directly next step, and the full schema travels in the ``tool_search`` result.
        """
        if self._runtime.get(name) is None:
            return False
        self._mounted.add(name)
        return True

    def schema_of(self, name: str) -> dict | None:
        """The full tool schema (for the ``tool_search`` result), or ``None`` when unknown."""
        tool = self._runtime.get(name)
        return tool.schema() if tool is not None else None

    # ── internals ──
    def _schemas_of(self, names: Iterable[str]) -> list[dict]:
        out: list[dict] = []
        for name in names:
            tool = self._runtime.get(name)
            if tool is not None:
                out.append({"type": "function", "function": tool.schema()})
        return out

    def _stub_schema(self, name: str) -> dict:
        """A deferred-loading stub: name + rich description, empty parameter shape."""
        tool = self._runtime.get(name)
        desc = _blurb(tool, STUB_DESC_MAX) if tool is not None else name
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {"type": "object", "properties": {}},
            },
        }

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
            {
                "name": hit.name,
                "description": hit.blurb,
                "permissions": list(hit.permissions),
                # the full parameter schema rides in the result message (below the cache
                # boundary), so the cached tools array only ever carries stable stubs
                "parameters": (gateway.schema_of(hit.name) or {}).get("parameters", {}),
            }
            for hit in hits
        ]

    return define_tool(
        name="tool_search",
        description=(
            "Search the tool catalog for tools relevant to your goal. Each match becomes "
            "directly callable and its result includes the full parameter schema, so you can "
            "call it with correct arguments on a later step. Use this instead of guessing a "
            "tool's name or arguments."
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
