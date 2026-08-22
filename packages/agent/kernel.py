"""AgentKernel: composition root for the agent engine (microkernel orchestration).

The kernel wires the zone-partitioned :class:`CacheBoundaryAssembler`, the deferred-tool
:class:`ToolGateway`, the dual-track :class:`MemoryService`, the lazy :class:`SkillCatalog`,
and the :class:`Sandbox` permission gate around a :class:`ReactLoopAgent`.

Standard prompt layout (aligned with the cache-boundary spec):

- ``STATIC_PREFIX`` — SOUL.md identity + the compact tool catalog + the compressed skill
  catalog (byte-identical across requests → the provider reuses its prefix cache).
- ``PROJECT_CONTEXT`` — CLAUDE.md / AGENTS.md project conventions (stable per project; empty
  when none are present, in which case the zone renders nothing).
- ``DYNAMIC_SUFFIX`` — the session memory brief (loaded once per run via ``begin_session``)
  plus any ``agent.inject()`` content; only this segment is re-rendered per step.

The kernel also registers the core resident tools (``tool_search``, ``skill``,
``memory_search``, ``memory_save``) and installs the sandbox permission guard, so callers
only supply the domain tools (rag_search/translate/web_search/fs …) and the LLM port.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.loop import AgentLLMPort, AgentResult, ReactLoopAgent
from agent.memory.service import MemoryService, memory_save_tool, memory_search_tool
from agent.runtime import ToolRuntime
from agent.sandbox import Sandbox
from agent.skills import SkillCatalog, SkillRegistry, skill_tool
from agent.system_prompt import (
    HARNESS_IDENTITY_ORDER,
    MEMORY_ORDER,
    PERSONA_ORDER,
    PROJECT_CONTEXT_ORDER,
    SKILLS_ORDER,
    CacheBoundaryAssembler,
    PromptZone,
)
from agent.tool_gateway import ToolGateway, tool_search_tool


@dataclass
class KernelConfig:
    """Tuning knobs for the agent loop."""

    max_steps: int = 5
    max_parallel_tool_calls: int = 10
    recall_top_k: int = 5   # proactive recall: hits injected into the prompt memory section


class AgentKernel:
    def __init__(
        self,
        llm: AgentLLMPort,
        runtime: ToolRuntime,
        *,
        soul: str = "",
        project_context: str = "",
        memory: MemoryService | None = None,
        skills: SkillRegistry | None = None,
        sandbox: Sandbox | None = None,
        gateway: ToolGateway | None = None,
        config: KernelConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.sandbox = sandbox or Sandbox()
        self.skills = skills or SkillRegistry()
        self.memory = memory
        self.config = config or KernelConfig()
        self.assembler = CacheBoundaryAssembler()
        self.gateway = gateway or ToolGateway(runtime)
        self._catalog = self.gateway.catalog
        self._recall_hits: list | None = None

        self._register_core_tools()
        self._install_guard()
        self._assemble_sections(soul=soul, project_context=project_context)

        self.loop = ReactLoopAgent(
            llm=llm,
            runtime=runtime,
            system_prompt=self.assembler,
            gateway=self.gateway,
            max_steps=self.config.max_steps,
            max_parallel_tool_calls=self.config.max_parallel_tool_calls,
        )

    # ── composition ──
    def _register_core_tools(self) -> None:
        """The resident meta-tools: tool_search, skill, memory_search, memory_save."""
        self.runtime.register(tool_search_tool(self._catalog, self.gateway))
        self.runtime.register(skill_tool(self.skills))
        if self.memory is not None:
            self.runtime.register(memory_search_tool(self.memory))
            self.runtime.register(memory_save_tool(self.memory))

    def _install_guard(self) -> None:
        """PreToolUse gate: sandbox denies high-risk tools the session lacks permission for."""
        self.runtime.guard(self.sandbox.guard())

    def _assemble_sections(self, *, soul: str, project_context: str = "") -> None:
        if soul:
            self.assembler.section("soul", PERSONA_ORDER, soul, zone=PromptZone.STATIC_PREFIX)

        # Project conventions (CLAUDE.md / AGENTS.md) sit in their own stable zone; they become
        # part of the snapshot_key identity so project rules are part of the prefix-cache contract.
        if project_context:
            self.assembler.section(
                "project_context",
                PROJECT_CONTEXT_ORDER,
                project_context,
                zone=PromptZone.PROJECT_CONTEXT,
            )

        def tool_index(context: dict) -> str:
            index = self._catalog.render_index()
            return f"## Tool catalog\n{index}" if index else ""

        self.assembler.section(
            "tool_catalog", HARNESS_IDENTITY_ORDER, tool_index, zone=PromptZone.STATIC_PREFIX
        )

        skill_catalog = SkillCatalog(self.skills)

        def skill_index(context: dict) -> str:
            index = skill_catalog.render()
            return f"## Skills\n{index}" if index else ""

        self.assembler.section(
            "skills", SKILLS_ORDER, skill_index, zone=PromptZone.STATIC_PREFIX
        )

        if self.memory is not None:
            self.assembler.section(
                "memory",
                MEMORY_ORDER,
                self._memory_brief,
                zone=PromptZone.DYNAMIC_SUFFIX,
            )
            self.assembler.section(
                "memory_recall",
                MEMORY_ORDER + 10,
                self._memory_recall_section,
                zone=PromptZone.DYNAMIC_SUFFIX,
            )

    def _memory_brief(self, context: dict) -> str:
        """The session's short-term memory brief (MEMORY.md head, loaded per run)."""
        brief = self.memory.session_brief() if self.memory is not None else ""
        return f"## Session memory brief\n{brief}" if brief else ""

    async def _memory_recall_section(self, context: dict) -> str:
        """Proactive recall: top hits for the user's message, injected into the suffix.

        Computed once per run (cached in ``self._recall_hits``, reset in :meth:`run`), so
        per-step ``refresh_dynamic`` reuses it instead of hitting the recall channels again.
        Recall is gated by ``MemoryService.should_recall`` (OpenClaw Lane-2 style): the
        expensive RRF query only runs on memory-seeking turns; every turn still gets the
        always-on Lane-1 ``MEMORY.md`` brief.
        """
        if self.memory is None:
            return ""
        if self._recall_hits is None:
            query = (context.get("user_msg") or "").strip()
            if query and self.memory.should_recall(query):
                self._recall_hits = await self.memory.recall_all(
                    query, self.config.recall_top_k
                )
            else:
                self._recall_hits = []
        if not self._recall_hits:
            return ""
        lines = "\n".join(f"- {h.content}" for h in self._recall_hits)
        return f"## Recalled memory\n{lines}"

    # ── public API (same signature as ReactLoopAgent.run) ──
    async def run(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> AgentResult:
        """Run one turn through the assembled kernel (assemble → step loop → session-end).

        ``base_url`` / ``api_key`` optionally route every model call in this turn through a
        specific LLM channel (the credential pinned on the caller's access token).
        """
        if self.memory is not None:
            self.memory.begin_session()
        self._recall_hits = None  # proactive recall is computed once per run, lazily
        return await self.loop.run(
            user_msg,
            history=history,
            memory_keys=memory_keys,
            session_memory=session_memory,
            model=model,
            base_url=base_url,
            api_key=api_key,
        )

    async def run_stream(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        """Streaming variant of :meth:`run` (same signature; see :meth:`ReactLoopAgent.run_stream`)."""
        if self.memory is not None:
            self.memory.begin_session()
        self._recall_hits = None
        async for evt in self.loop.run_stream(
            user_msg,
            history=history,
            memory_keys=memory_keys,
            session_memory=session_memory,
            model=model,
            base_url=base_url,
            api_key=api_key,
        ):
            yield evt
