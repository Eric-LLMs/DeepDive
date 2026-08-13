"""Hook system: insert extension logic at key points in the Agent lifecycle.

Modeled after claude-code / openclaw: the Agent main flow is a hardcoded while loop (small, testable),
with extension injected via hooks rather than splitting the flow into config nodes. Only RAG uses a config-node DAG.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_COMPACT = "pre_compact"


@dataclass
class HookContext:
    """Context snapshot passed to a hook."""

    event: HookEvent
    tool_name: str | None = None
    tool_args: dict | None = None
    session_id: str | None = None
    messages: list[dict] = field(default_factory=list)


@dataclass
class HookResult:
    """The hook's decision result.

    - continue: allow (default)
    - block: deny (e.g. intercepting a destructive tool)
    - modify: continue after rewriting arguments (e.g. auto-completion)
    """

    action: str = "continue"
    updated_args: dict | None = None
    new_messages: list[dict] = field(default_factory=list)
    message: str | None = None


@dataclass
class Hook:
    """A hook = the event it listens to + a handler + an optional matcher."""

    event: HookEvent
    handler: Callable[[HookContext], Awaitable[HookResult]]
    matcher: Callable[[HookContext], bool] | None = None  # only takes effect on matching contexts

    async def run(self, ctx: HookContext) -> HookResult:
        if self.matcher and not self.matcher(ctx):
            return HookResult()
        return await self.handler(ctx)
