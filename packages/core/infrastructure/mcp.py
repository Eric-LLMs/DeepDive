"""MCP Server: expose the tool registry over the MCP protocol (FastMCP).

Direction one: expose DeepGloss's tools externally so other AI clients can call them.
Direction two (MCP Client consuming external tools) is wired up on demand at the API layer, and registered into ToolRegistry the same way.
"""
from fastmcp import FastMCP

from core.agent import ToolRegistry


def build_mcp_server(registry: ToolRegistry, name: str = "deepgloss") -> FastMCP:
    mcp = FastMCP(name)

    def _make_handler(tool):
        # factory function captures tool to avoid late binding in the closure
        async def _handler(**kwargs):
            return await tool.handler(**kwargs)

        _handler.__name__ = tool.name
        _handler.__doc__ = tool.description
        return _handler

    for tool in registry.all():
        # FastMCP infers the tool name and description from the function name/docstring
        mcp.tool()(_make_handler(tool))

    return mcp
