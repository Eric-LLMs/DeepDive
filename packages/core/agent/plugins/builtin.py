"""Built-in plugins: destructive-tool interception (deny + monotonic guard) + audit observer.

Two ways to stop a tool are demonstrated side by side:

- ``deny_destructive`` is a ``tools/pre-execute`` waterfall listener that short-circuits with
  ``PreToolDecision.deny`` (it can be overridden by a later listener calling ``next()`` first).
- ``guard_destructive`` is a monotonic guard: once it returns a reason, the run is denied and
  no later listener can flip it back to allow.

``audit_result`` observes ``tools/result`` for logging.
"""
import logging

from core.agent.decisions import PreToolDecision, ToolExecution
from core.agent.plugins.base import Plugin
from core.agent.plugins.hooks import PRE_TOOL_USE, TOOL_RESULT, observe, waterfall
from core.agent.plugins.manager import PluginManager

logger = logging.getLogger(__name__)


def register_builtin_plugins(manager: PluginManager) -> None:
    runtime = manager.runtime

    async def deny_destructive(exec: ToolExecution, next_) -> PreToolDecision:
        tool = runtime.get(exec.name)
        if tool and tool.destructive:
            return PreToolDecision.deny(f"destructive tool blocked by pre-execute: {exec.name}")
        return await next_()

    async def guard_destructive(exec: ToolExecution) -> str | None:
        tool = runtime.get(exec.name)
        if tool and tool.destructive:
            return f"destructive tool blocked by guard: {exec.name}"
        return None

    async def audit_result(payload: dict) -> None:
        exec = payload["exec"]
        result = payload["result"]
        logger.info("tool %s -> %s", exec.name, "error" if result.is_error else "ok")

    manager.register(
        Plugin(
            name="tool_audit",
            description="Block destructive tools (deny + guard) and audit results.",
            listeners=[
                waterfall(PRE_TOOL_USE, deny_destructive),
                observe(TOOL_RESULT, audit_result),
            ],
            guards=[guard_destructive],
        )
    )
