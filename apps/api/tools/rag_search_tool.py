"""``rag_search``: search the learning-material corpus for relevant chunks."""
from __future__ import annotations

import json

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def rag_search(args: dict, exec: ToolExecution) -> list[dict]:
        retriever = ctx.resolve("retrieval")
        return await retriever.retrieve(args.get("query", ""), args.get("top_k", 5))

    runtime.register(
        define_tool(
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
            output=ToolOutput(
                schema={"type": "array"},
                render=lambda args, value: [
                    text_block(json.dumps(value, ensure_ascii=False, default=str))
                ],
            ),
            execute=rag_search,
        )
    )
