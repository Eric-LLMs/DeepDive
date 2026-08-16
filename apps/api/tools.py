"""Built-in agent tools wired in the gateway.

``rag_search``, ``translate``, and ``web_search`` live here (not in the plugin registry)
because they depend on gateway-scoped resources: the retrieval capability seam, the
shared LLM client, and the web search provider.
"""
import asyncio
import json

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block


def register_builtin_tools(runtime: ToolRuntime, ctx: Context, llm) -> None:
    """Register the gateway's built-in tools onto ``runtime``."""

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

    async def translate(args: dict, exec: ToolExecution) -> str:
        return await llm.complete(
            args["text"], "You are a translator. Translate the text into natural Chinese."
        )

    runtime.register(
        define_tool(
            name="translate",
            description="Translate English text into natural, fluent Chinese.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "English text to translate."},
                },
                "required": ["text"],
            },
            output=ToolOutput(schema={"type": "string"}, render=lambda args, value: [text_block(value)]),
            execute=translate,
        )
    )

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
