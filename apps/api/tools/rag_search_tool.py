"""``rag_search``: search the learning-material corpus for relevant chunks."""
from __future__ import annotations

import json
import logging

from agent import Context, ToolExecution, ToolOutput, ToolRuntime, define_tool, text_block
from core.infrastructure.request_context import get_request_user_id

logger = logging.getLogger(__name__)

# Returned as a *successful* tool result when the retrieval stack is down (e.g. the
# embedding service returns 503). A normal result lets the model answer from knowledge
# in this round; a raised error would surface as a tool error and burn another round
# on a pointless retry.
_UNAVAILABLE = [{"notice": "学习资料库检索服务暂时不可用,请直接基于你的知识回答,不要重试检索。"}]


def register(runtime: ToolRuntime, ctx: Context, llm) -> None:
    async def rag_search(args: dict, exec: ToolExecution) -> list[dict]:
        retriever = ctx.resolve("retrieval")
        # Tenant isolation: bind the current request's user so retrieval only sees their
        # assets (owner / workspace / ACL). A guest (None) sees public-link assets only.
        filters = {"user_id": get_request_user_id()}
        try:
            return await retriever.retrieve(
                args.get("query", ""), args.get("top_k", 5), filters
            )
        except Exception as exc:  # noqa: BLE001 - degrade to knowledge-only, don't retry
            logger.warning("rag_search degraded (retrieval unavailable): %s", exc)
            return _UNAVAILABLE

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
