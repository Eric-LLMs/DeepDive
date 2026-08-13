"""Built-in plugins: ready-to-use default extensions."""
from core.agent.plugins.base import Plugin
from core.agent.plugins.hooks import Hook, HookContext, HookEvent, HookResult
from core.agent.plugins.manager import PluginManager


async def _block_destructive(ctx: HookContext) -> HookResult:
    return HookResult(action="block", message=f"Blocked destructive tool: {ctx.tool_name}")


def _is_destructive(manager: PluginManager, name: str | None) -> bool:
    if not name:
        return False
    tool = manager.tools.get(name)
    return bool(tool and tool.destructive)


def register_builtin_plugins(manager: PluginManager) -> None:
    """Built-in: destructive-tool interception + auditing."""
    manager.register(
        Plugin(
            name="tool_audit",
            description="拦截标记为 destructive 的工具调用。",
            hooks=[
                Hook(
                    event=HookEvent.PRE_TOOL_USE,
                    handler=_block_destructive,
                    matcher=lambda ctx: _is_destructive(manager, ctx.tool_name),
                )
            ],
        )
    )
