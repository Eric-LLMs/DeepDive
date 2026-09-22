"""AgentExecutor: the fallback branch — a thin wrapper over AgentKernel.run[_stream].

The kernel and its ReAct loop are UNTOUCHED; this class only adapts the executor
contract to the kernel signature. In Phase 1 every turn lands here, so the control
plane is behavior-identical to the legacy router.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.application.chat.execution_plan import PlanKind
from core.application.chat.executors.base import ChatEvent, ProgressSink, TurnRequest


class AgentExecutor:
    kind = PlanKind.AGENT

    async def stream(
        self, req: TurnRequest, *, progress_sink: ProgressSink
    ) -> AsyncIterator[ChatEvent]:
        ctx = req.ctx
        async for evt in req.deps.agent.run_stream(
            ctx.user_text,
            ctx.history,
            session_memory=ctx.session_memory,
            model=ctx.model,
            base_url=ctx.base_url or None,
            api_key=ctx.api_key or None,
            context=ctx.agent_context,
            progress_sink=progress_sink,
            # Voice-call turns + video FOCUS Q&A skip the reasoning prefill tax.
            disable_thinking=ctx.disable_thinking,
        ):
            yield evt

    async def run(self, req: TurnRequest) -> Any:
        ctx = req.ctx
        return await req.deps.agent.run(
            ctx.user_text,
            ctx.history,
            session_memory=ctx.session_memory,
            model=ctx.model,
            base_url=ctx.base_url or None,
            api_key=ctx.api_key or None,
            context=ctx.agent_context,
        )
