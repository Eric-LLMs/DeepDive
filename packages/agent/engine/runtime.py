"""Tool runtime: the lifecycle around tool execution.

The full pipeline:

    tools/pre-execute  (waterfall; base = allow)
      ├─ deny → fail fast
      └─ ask  → resolve via approval handler (missing handler degrades to deny)
    guard               (monotonic, deny-only; a reason string blocks)
    tools/execute       (waterfall; base = dispatch body)
    tools/post-execute  (waterfall; base = accept)
    tools/result        (serial observer)

Registration is reversible: ``register`` and ``guard`` both return a disposer callable.
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

from agent.engine.decisions import (
    ContentBlock,
    Guard,
    PostToolDecision,
    PreToolDecision,
    ToolExecution,
    ToolExecutionFailure,
    ToolExecutionResult,
    ToolExecutionSuccess,
    ToolFailure,
    text_block,
)
from agent.engine.events import EventBus
from agent.tools.definition import ToolArgsError, ToolDefinition, ToolOutputError


class ToolRuntime:
    def __init__(self, approval: Callable[[ToolExecution, PreToolDecision], Awaitable[PreToolDecision]] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._guards: list[Guard] = []
        self.events = EventBus()
        self.approval = approval

    # ── registration ──
    def register(self, definition: ToolDefinition) -> Callable[[], None]:
        """Register a tool; returns a disposer. Re-registering a name raises."""
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = definition

        def dispose() -> None:
            if self._tools.get(definition.name) is definition:
                del self._tools[definition.name]

        return dispose

    def guard(self, fn: Guard) -> Callable[[], None]:
        """Register a monotonic guard; returns a disposer."""
        self._guards.append(fn)

        def dispose() -> None:
            if fn in self._guards:
                self._guards.remove(fn)

        return dispose

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """Model-visible projection of every tool (name/description/parameters only)."""
        return [t.schema() for t in self._tools.values()]

    # ── execution ──
    async def execute(self, exec: ToolExecution) -> ToolExecutionResult:
        """Run the full lifecycle, isolating each decision stage so a raising hook can never
        escape the loop. Stage defaults are fail-closed: a broken pre-execute chain denies the
        tool, a broken execute chain is a failure (never a fabricated success), and a broken
        post-execute chain blocks — so a buggy approval/guard hook cannot let a tool run
        unchecked. Each failure is logged with a traceback.
        """
        # ① pre-execute
        try:
            pre = await self.events.waterfall(
                "tools/pre-execute", exec, base=PreToolDecision.allow()
            )
        except Exception as exc:  # noqa: BLE001 - a broken decision hook must not escape the loop
            logger.exception("pre-execute chain failed for tool %s", exec.name)
            return await self._finish(
                exec, ToolExecutionFailure(ToolFailure(f"pre-execute failed: {exc}"))
            )
        if pre.kind == "ask":
            pre = await self._resolve_ask(exec, pre)
        if pre.kind == "deny":
            return await self._finish(
                exec, ToolExecutionFailure(ToolFailure(pre.reason or "tool use denied"))
            )

        # ② monotonic guard (deny-only; a raising guard fails closed)
        reason = await self._guard_reason(exec)
        if reason is not None:
            return await self._finish(exec, ToolExecutionFailure(ToolFailure(reason)))

        # ③ execute (waterfall; base dispatches the body)
        async def dispatch_body() -> ToolExecutionResult:
            return await self._dispatch_body(exec)

        try:
            result = await self.events.waterfall("tools/execute", exec, base=dispatch_body)
        except Exception as exc:  # noqa: BLE001
            logger.exception("execute chain failed for tool %s", exec.name)
            return await self._finish(
                exec, ToolExecutionFailure(ToolFailure(f"execute failed: {exc}"))
            )

        # ④ post-execute
        try:
            post = await self.events.waterfall(
                "tools/post-execute", exec, result, base=PostToolDecision.accept()
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("post-execute chain failed for tool %s", exec.name)
            return await self._finish(
                exec, ToolExecutionFailure(ToolFailure(f"post-execute failed: {exc}"))
            )
        result = self._apply_post(exec, result, post)

        return await self._finish(exec, result)

    async def _resolve_ask(self, exec: ToolExecution, decision: PreToolDecision) -> PreToolDecision:
        if self.approval is None:
            return PreToolDecision.deny(decision.reason or "approval required but no approver registered")
        return await self.approval(exec, decision)

    async def _guard_reason(self, exec: ToolExecution) -> str | None:
        for guard in self._guards:
            try:
                reason = await guard(exec)
            except Exception as exc:  # noqa: BLE001 - a broken guard must fail closed
                logger.exception("sandbox guard %r failed for tool %s", guard, exec.name)
                return f"sandbox guard error: {exc}"
            if reason is not None:
                return reason
        return None

    async def _dispatch_body(self, exec: ToolExecution) -> ToolExecutionResult:
        tool = self._tools.get(exec.name)
        if tool is None:
            return ToolExecutionFailure(
                ToolFailure(f"unknown tool: {exec.name}", info={"name": "unknown_tool"})
            )
        try:
            value = await tool.execute(exec.arguments, exec)
        except ToolArgsError as exc:
            return ToolExecutionFailure(ToolFailure(str(exc), info={"name": "invalid_args"}))
        except ToolOutputError as exc:
            return ToolExecutionFailure(ToolFailure(str(exc), info={"name": "invalid_output"}))
        except Exception as exc:  # noqa: BLE001 - tool errors become readable results fed back to the LLM
            return ToolExecutionFailure(ToolFailure(str(exc), info={"name": "tool_error"}))

        rendered = tool.output.render(exec.arguments, value)
        if inspect.isawaitable(rendered):
            rendered = await rendered
        content = [b if isinstance(b, ContentBlock) else text_block(str(b)) for b in rendered]
        return ToolExecutionSuccess(value=value, content=content)

    def _apply_post(
        self, exec: ToolExecution, result: ToolExecutionResult, post: PostToolDecision
    ) -> ToolExecutionResult:
        for ctx in post.additional_contexts:
            exec.defer_context(ctx)
        if post.kind != "block":
            if isinstance(result, ToolExecutionFailure):
                return result
            if post.content is not None:
                result = ToolExecutionSuccess(value=result.value, content=post.content, meta=result.meta)
            elif post.value is not None:
                result = ToolExecutionSuccess(value=post.value, content=result.content, meta=result.meta)
            return result

        feedback = post.feedback or "blocked by post-execute"
        content = post.content or [text_block(feedback)]
        return ToolExecutionFailure(
            ToolFailure(feedback, info={"name": "post_blocked"}), content=content
        )

    async def _finish(self, exec: ToolExecution, result: ToolExecutionResult) -> ToolExecutionResult:
        await self.events.serial("tools/result", {"exec": exec, "result": result})
        return result
