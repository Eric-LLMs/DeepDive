"""MCP Server: expose the tool runtime over the MCP protocol (FastMCP).

Direction one: expose DeepDive's tools externally so other AI clients can call them.
Direction two (MCP Client consuming external tools) is wired up on demand at the API layer,
and registered into the ToolRuntime the same way.
"""
from fastmcp import FastMCP

from agent.decisions import ToolExecution
from agent.runtime import ToolRuntime


def build_mcp_server(runtime: ToolRuntime, name: str = "deepdive") -> FastMCP:
    mcp = FastMCP(name)

    def _make_handler(tool):
        # factory function captures tool to avoid late binding in the closure
        async def _handler(**kwargs):
            exec = ToolExecution(call_id="mcp", name=tool.name, arguments=kwargs)
            result = await runtime.execute(exec)
            if result.is_error:
                raise RuntimeError(result.error.message)
            return result.value

        _handler.__name__ = tool.name
        _handler.__doc__ = tool.description
        return _handler

    for tool in runtime.all():
        # FastMCP infers the tool name and description from the function name/docstring
        mcp.tool()(_make_handler(tool))

    return mcp
