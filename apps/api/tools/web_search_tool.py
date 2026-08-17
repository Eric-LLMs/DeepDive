"""``web_search``: search the web for up-to-date information via the provider seam."""
from __future__ import annotations

import asyncio
import json

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def web_search(args: dict, exec: ToolExecution) -> list[dict]:
        provider = ctx.resolve("web_search")
        if provider is None:
            raise RuntimeError("web search is not configured")
        query = args["query"]
        top_k = args.get("top_k", 5)
        return await asyncio.to_thread(provider.search, query, top_k)

    runtime.register(
        define_tool(
            name="web_search",
            description="Search the web for up-to-date information. Returns a list of "
            "results with title, url, and snippet. Use this when the answer needs "
            "external or recent knowledge beyond the local learning material.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "top_k": {"type": "integer", "description": "Number of results."},
                },
                "required": ["query"],
            },
            output=ToolOutput(
                schema={"type": "array"},
                render=lambda args, value: [
                    text_block(json.dumps(value, ensure_ascii=False, default=str))
                ],
            ),
            execute=web_search,
        )
    )
