"""``translate``: translate English text into natural Chinese via the shared LLM."""
from __future__ import annotations

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
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
            output=ToolOutput(
                schema={"type": "string"}, render=lambda args, value: [text_block(value)]
            ),
            execute=translate,
        )
    )
