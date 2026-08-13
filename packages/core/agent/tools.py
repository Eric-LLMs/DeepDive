"""Tool registry: single source of truth for Agent / RAG / MCP.

A tool is defined once (name + JSON Schema + handler) and shared by three consumers:
- Agent (function-calling): get_for_agent()
- MCP (FastMCP): all()
- Direct internal invocation: call()

Registration != execution: register() only mounts the schema; the handler actually runs only when called.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class ToolResult:
    """Tool execution result. Either data or error; new_messages lets the tool inject extra messages."""

    data: Any = None
    error: str | None = None
    new_messages: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_json(self) -> str:
        """Serialize into a tool message string to feed the LLM."""
        payload = {"error": self.error} if self.error else {"result": self.data}
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema (OpenAI function-calling format)
    handler: Callable[..., Awaitable[Any]]
    readonly: bool = True       # read-only tools do not modify external state
    destructive: bool = False   # destructive tools require hook approval before execution


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_for_agent(self) -> list[dict]:
        """Convert into the OpenAI function-calling tools parameter."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, args: dict) -> ToolResult:
        """Execute a tool, folding exceptions into ToolResult(error)."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"Unknown tool: {name}")
        try:
            return ToolResult(data=await tool.handler(**args))
        except Exception as exc:  # noqa: BLE001 - tool errors need to be converted into readable results fed back to the LLM
            return ToolResult(error=str(exc))


def build_default_tools(retriever, llm) -> ToolRegistry:
    """Assemble default tools: rag_search + translate. Add vocabulary/sentence tools following this pattern."""
    registry = ToolRegistry()

    registry.register(
        Tool(
            name="rag_search",
            description="Search learning material (text chunks) for information relevant "
            "to the query. Returns matching chunks.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "top_k": {"type": "integer", "description": "Number of results."},
                },
                "required": ["query"],
            },
            handler=retriever.retrieve,
        )
    )

    registry.register(
        Tool(
            name="translate",
            description="Translate English text into natural, fluent Chinese.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "English text to translate."}},
                "required": ["text"],
            },
            handler=lambda text: llm.complete(
                text, "You are a translator. Translate the text into natural Chinese."
            ),
        )
    )

    return registry
