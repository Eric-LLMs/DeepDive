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
        outcome = await asyncio.to_thread(provider.search, query, top_k)
        if outcome.get("status") == "degraded":
            # A real engine outage must surface as a tool failure — never masquerade as a
            # normal "0 results" search, so gate diagnostics can tell the two apart.
            err = outcome.get("error") or {}
            raise RuntimeError(
                f"web search degraded (provider={outcome.get('provider')}, "
                f"{err.get('type', 'error')}): {err.get('message', 'no details')}"
            )
        return outcome.get("results") or []

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
