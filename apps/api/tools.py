"""Built-in agent tools wired in the gateway.

``rag_search`` and ``translate`` live here (not in the plugin registry) because they depend
on gateway-scoped resources: the retrieval capability seam and the shared LLM client.
"""
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
