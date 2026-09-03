"""AgentKernel: composition root for the agent engine (microkernel orchestration).

The kernel wires the zone-partitioned :class:`CacheBoundaryAssembler`, the deferred-tool
:class:`ToolGateway`, the dual-track :class:`MemoryService`, the lazy :class:`SkillCatalog`,
and the :class:`Sandbox` permission gate around a :class:`ReactLoopAgent`.

Standard prompt layout (aligned with the cache-boundary spec):

- ``STATIC_PREFIX`` — SOUL.md identity + the compact tool catalog + the compressed skill
  catalog (byte-identical across requests → the provider reuses its prefix cache).
- ``PROJECT_CONTEXT`` — DEEPDIVE.md project conventions (stable per project; empty
  when none are present, in which case the zone renders nothing).
- ``DYNAMIC_SUFFIX`` — the session memory brief (loaded once per run via ``begin_session``)
  plus any ``agent.inject()`` content; only this segment is re-rendered per step.

The kernel also registers the core resident tools (``tool_search``, ``skill``,
``memory_search``, ``memory_save``) and installs the sandbox permission guard, so callers
only supply the domain tools (rag_search/translate/web_search/fs …) and the LLM port.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.config import settings

from agent.engine.context import AgentTurn, bind_turn, current_turn
from agent.engine.loop import AgentLLMPort, AgentResult, ReactLoopAgent
from agent.engine.runtime import ToolRuntime
from agent.engine.sessions import SessionLog
from agent.engine.telemetry import AuditSink
from agent.llm.llm_guard import ReliableLLM
from agent.memory.service import MemoryService, memory_save_tool, memory_search_tool
from agent.prompt.system_prompt import (
    HARNESS_IDENTITY_ORDER,
    MEMORY_ORDER,
    PERSONA_ORDER,
    PROJECT_CONTEXT_ORDER,
    SKILLS_ORDER,
    CacheBoundaryAssembler,
    PromptZone,
)
from agent.security.sandbox import Sandbox
from agent.skills.registry import SkillCatalog, SkillRegistry, SkillScopeEnforcer, skill_tool
from agent.tools.checkpoints import CheckpointStore, revert_to_checkpoint_tool
from agent.tools.plan_tool import plan_tool
from agent.tools.subagent import run_subagent_tool
from agent.tools.tool_gateway import (
    CATALOG_CAPACITY_CHARS,
    ToolGateway,
    check_index_capacity,
    tool_search_tool,
)

logger = logging.getLogger(__name__)


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
        checkpoints: CheckpointStore | None = None,
        config: KernelConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.sandbox = sandbox or Sandbox()
        self.skills = skills or SkillRegistry()
        self.memory = memory
        self.config = config or KernelConfig()
        self.assembler = CacheBoundaryAssembler()
        self.gateway = gateway or ToolGateway(runtime)
        self.checkpoints = checkpoints
        self._catalog = self.gateway.catalog

        self._register_core_tools()
        self._install_guard()
        self._assemble_sections(soul=soul, project_context=project_context)

        # Reliability wrapper: hard timeout + tenacity retry on temporary errors +
        # cancellation pass-through (see agent.llm.llm_guard). Wrapped once, kernel-wide.
        llm = ReliableLLM(
            llm,
            timeout_s=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            backoff=settings.llm_retry_backoff,
        )

        self.loop = ReactLoopAgent(
            llm=llm,
            runtime=runtime,
            system_prompt=self.assembler,
            gateway=self.gateway,
            session_log=SessionLog(),  # live event log (was dead before: loop._log had no sink)
            max_steps=self.config.max_steps,
            max_parallel_tool_calls=self.config.max_parallel_tool_calls,
        )

    # ── composition ──
    def _register_core_tools(self) -> None:
        """The resident meta-tools: tool_search, skill, memory_search, memory_save, plan,
        run_subagent, and (when checkpoints are wired) revert_to_checkpoint."""
        self.runtime.register(tool_search_tool(self._catalog, self.gateway))
        self.runtime.register(skill_tool(self.skills))
        if self.memory is not None:
            self.runtime.register(memory_search_tool(self.memory))
            self.runtime.register(memory_save_tool(self.memory))
        self.runtime.register(plan_tool())
        self.runtime.register(run_subagent_tool())
        if self.checkpoints is not None:
            self.runtime.register(revert_to_checkpoint_tool(self.checkpoints))

    def _install_guard(self) -> None:
        """PreToolUse gate: sandbox denies high-risk tools the session lacks permission for,
        and surfaces ASK decisions through the pre-execute approval path (human-in-the-loop).
        The skill scope guard then hard-enforces an active skill's allowed_tools allowlist."""
        self.runtime.guard(self.sandbox.guard())
        self.runtime.events.on("tools/pre-execute", self.sandbox.ask_listener())
        self.runtime.guard(SkillScopeEnforcer(self.skills).guard())

    def _assemble_sections(self, *, soul: str, project_context: str = "") -> None:
        if soul:
            self.assembler.section("soul", PERSONA_ORDER, soul, zone=PromptZone.STATIC_PREFIX)

        # Project conventions (DEEPDIVE.md) sit in their own stable zone; they become part of
        # the snapshot_key identity so project rules are part of the prefix-cache contract.
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

    def ensure_capacity(self) -> None:
        """Refuse to run when the full tool + skill index overflows the capacity ceiling.

        Called once at agent-runtime startup after every plugin/skill is discovered (see
        ``agent_factory.get_agent_kernel``). The catalog sections no longer truncate — the full
        tool + skill index is always emitted — so the only way to "lose" a tool would be to
        overflow the hard capacity. If that ever happens, startup fails loudly instead of
        silently hiding tools from the model.
        """
        tool_index = self._catalog.render_index()
        skill_index = SkillCatalog(self.skills).render()
        logger.info(
            "catalog capacity: tools=%d chars, skills=%d chars, total=%d/%d",
            len(tool_index), len(skill_index), len(tool_index) + len(skill_index),
            CATALOG_CAPACITY_CHARS,
        )
        check_index_capacity(tool_index, skill_index)

    def _memory_brief(self, context: dict) -> str:
        """The turn's short-term memory brief (MEMORY.md head, loaded once per turn)."""
        turn = context.get("turn") or current_turn()
        brief = turn.memory_brief if turn is not None else ""
        return f"## Session memory brief\n{brief}" if brief else ""

    async def _memory_recall_section(self, context: dict) -> str:
        """Proactive recall: top hits for the user's message, injected into the suffix.

        Computed once per turn (cached on ``AgentTurn.recall_hits``, reset when a fresh
        :class:`~agent.engine.context.AgentTurn` is built in :meth:`run`), so per-step
        ``refresh_dynamic`` reuses it instead of hitting the recall channels again.
        Recall is gated by ``MemoryService.should_recall`` (OpenClaw Lane-2 style): the
        expensive RRF query only runs on memory-seeking turns; every turn still gets the
        always-on Lane-1 ``MEMORY.md`` brief.
        """
        if self.memory is None:
            return ""
        turn = context.get("turn") or current_turn()
        if turn is None:
            return ""
        if turn.recall_hits is None:
            query = (context.get("user_msg") or "").strip()
            if query and self.memory.should_recall(query):
                turn.recall_hits = await self.memory.recall_all(
                    query, self.config.recall_top_k
                )
            else:
                turn.recall_hits = []
        if not turn.recall_hits:
            return ""
        lines = "\n".join(f"- {h.content}" for h in turn.recall_hits)
        return f"## Recalled memory\n{lines}"

    # ── public API (same signature as ReactLoopAgent.run) ──
    def _build_turn(
        self,
        user_msg: str,
        history: list[dict] | None,
        memory_keys: list[str] | None,
        session_memory: Any | None,
        model: str | None,
        base_url: str | None,
        api_key: str | None,
        progress_sink: Callable[[dict], None] | None = None,
        context: dict | None = None,
    ) -> AgentTurn:
        """Build the per-turn :class:`AgentTurn`, load its memory brief, and bind it.

        Binding the turn to the current task means the stateless assembler's sections
        (memory brief / recall / injected) resolve from ``current_turn()`` — never from
        shared kernel fields, so concurrent turns cannot race.
        """
        turn = AgentTurn(
            user_msg=user_msg,
            history=history,
            memory_keys=memory_keys,
            session_memory=session_memory,
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_budget_usd=settings.max_budget_per_turn_usd,
            context=context,
        )
        if progress_sink is not None:
            turn.progress_sink = progress_sink
        if self.memory is not None:
            turn.memory_brief = self.memory.begin_session()
        turn.audit = AuditSink(settings.audit_log_path)
        bind_turn(turn)
        return turn

    async def _snapshot_workspace(self, turn: AgentTurn) -> None:
        """Record a pre-turn workspace snapshot (best-effort; the turn runs regardless)."""
        if self.checkpoints is None:
            return
        try:
            turn.checkpoint_id = await self.checkpoints.snapshot("pre-turn")
        except Exception:  # noqa: BLE001 - checkpointing is advisory, never fatal
            turn.checkpoint_id = None

    async def run(
        self,
        user_msg: str,
        history: list[dict] | None = None,
        memory_keys: list[str] | None = None,
        session_memory: Any | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        progress_sink: Callable[[dict], None] | None = None,
        context: dict | None = None,
    ) -> AgentResult:
        """Run one turn through the assembled kernel (assemble → step loop → session-end).

        ``base_url`` / ``api_key`` optionally route every model call in this turn through a
        specific LLM channel (the credential pinned on the caller's access token).
        ``progress_sink`` receives structured turn events (plan / tool progress) for streaming.
        ``context`` carries machine-readable turn context (e.g. a handoff payload) that tools
        read at runtime via ``current_turn().context``.
        """
        turn = self._build_turn(
            user_msg, history, memory_keys, session_memory, model, base_url, api_key,
            progress_sink, context,
        )
        await self._snapshot_workspace(turn)
        return await self.loop.run(
            user_msg,
            history=history,
            memory_keys=memory_keys,
            session_memory=session_memory,
            model=model,
            base_url=base_url,
            api_key=api_key,
            turn=turn,
            progress_sink=progress_sink,
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
        progress_sink: Callable[[dict], None] | None = None,
        context: dict | None = None,
    ):
        """Streaming variant of :meth:`run` (same signature; see :meth:`ReactLoopAgent.run_stream`)."""
        turn = self._build_turn(
            user_msg, history, memory_keys, session_memory, model, base_url, api_key,
            progress_sink, context,
        )
        await self._snapshot_workspace(turn)
        async for evt in self.loop.run_stream(
            user_msg,
            history=history,
            memory_keys=memory_keys,
            session_memory=session_memory,
            model=model,
            base_url=base_url,
            api_key=api_key,
            turn=turn,
            progress_sink=progress_sink,
        ):
            yield evt
