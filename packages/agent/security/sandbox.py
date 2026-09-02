"""Sandbox: declarative permission rules for tool execution (Read / Write / Network).

A :class:`Sandbox` holds the session's granted permission level (READ by default) plus
overridable per-permission rules. :meth:`Sandbox.check` returns an ALLOW / DENY / ASK
decision for a tool given its arguments; :meth:`Sandbox.guard` wraps that as a monotonic
``ToolRuntime.guard`` so the permission gate runs inside the existing pre-execute pipeline
(an ASK with no human approver degrades to DENY — safe by default).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agent.engine.context import current_turn
from agent.engine.decisions import Guard, PreToolDecision, ToolExecution
from agent.tools.definition import ToolDefinition, classify_permissions
from agent.tools.tool_permissions import ToolPermission, permission_names


class SandboxDecision(Enum):
    """What the sandbox says about running a tool."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class SandboxRule:
    """A declarative policy entry: ``permission`` behaves as ``decision`` in this sandbox."""

    permission: ToolPermission
    decision: SandboxDecision = SandboxDecision.ASK


class Sandbox:
    """Decides whether a tool may run under the session's granted permissions."""

    def __init__(self, permissions: set[ToolPermission] | None = None) -> None:
        self._permissions: set[ToolPermission] = set(
            permissions if permissions is not None else {ToolPermission.READ}
        )
        self._rules: dict[ToolPermission, SandboxDecision] = {}

    # ── session permission level ──
    def _effective_permissions(self) -> set[ToolPermission]:
        """The granted permission classes for this turn.

        The base set is the session default (READ). A turn that carries a research handoff
        (a session driving a Research OS task) additionally grants WRITE + NETWORK, so the
        agent can advance the task's state machine, write scratch artifacts, and run its
        search tools (``web_search`` / ``search_social``) without a human approval for every
        call. The handoff is sunk into ``current_turn().context`` by the chat router and is
        durable: it is re-synthesized for a session already bound to a task, so turns after
        the first keep the grant.
        """
        perms: set[ToolPermission] = set(self._permissions)
        turn = current_turn()
        if turn is not None:
            handoff = (turn.context or {}).get("handoff") or {}
            if handoff.get("kind") == "research":
                perms.update((ToolPermission.WRITE, ToolPermission.NETWORK))
        return perms

    def session_permissions(self) -> set[ToolPermission]:
        """The permission classes the current session has been granted."""
        return self._effective_permissions()

    def grant(self, permission: ToolPermission) -> None:
        self._permissions.add(permission)

    def revoke(self, permission: ToolPermission) -> None:
        self._permissions.discard(permission)

    # ── per-permission overrides ──
    def add_rule(self, rule: SandboxRule) -> Callable[[], None]:
        """Override the decision for one permission; returns a disposer."""
        prev = self._rules.get(rule.permission)
        self._rules[rule.permission] = rule.decision

        def dispose() -> None:
            if prev is None:
                self._rules.pop(rule.permission, None)
            else:
                self._rules[rule.permission] = prev

        return dispose

    def decision_for(self, permission: ToolPermission) -> SandboxDecision:
        if permission in self._rules:
            return self._rules[permission]
        return (
            SandboxDecision.ALLOW
            if permission in self._effective_permissions()
            else SandboxDecision.ASK
        )

    # ── decision ──
    def check(self, tool: ToolDefinition, args: dict) -> SandboxDecision:
        """The *least permissive* decision across the tool's required permissions."""
        required = classify_permissions(tool)
        if not required:
            return SandboxDecision.ALLOW
        worst = SandboxDecision.ALLOW
        for p in required:
            decision = self.decision_for(p)
            if decision is SandboxDecision.DENY:
                return SandboxDecision.DENY
            if decision is SandboxDecision.ASK:
                worst = SandboxDecision.ASK
        return worst

    def guard(self) -> Guard:
        """A monotonic PreToolUse guard enforcing DENY inside the runtime.

        Returns a deny *reason* string (or ``None`` to pass). ASK is *not* collapsed here:
        it passes through to :meth:`ask_listener` (a ``tools/pre-execute`` listener) so a
        human approver can allow it. ASK with no approver bound degrades to deny — the
        default stays safe.
        """

        async def _guard(exec: ToolExecution) -> str | None:
            runtime = getattr(getattr(exec, "agent", None), "runtime", None)
            tool = runtime.get(exec.name) if runtime is not None else None
            if tool is None:
                return None
            if self.check(tool, exec.arguments) is SandboxDecision.DENY:
                tag = ",".join(permission_names(classify_permissions(tool)))
                return f"sandbox denied: {exec.name} needs [{tag}] but the session has [{','.join(permission_names(self._effective_permissions()))}]"
            return None

        return _guard

    def ask_listener(self) -> Callable[[ToolExecution, Callable], object]:
        """A ``tools/pre-execute`` listener surfacing ASK as an approval request.

        The runtime resolves an ASK through its approval bridge (a human-in-the-loop
        approver); with no approver bound it degrades to DENY (see
        :meth:`~agent.engine.runtime.ToolRuntime._resolve_ask`).
        """

        async def _ask(exec: ToolExecution, next_: Callable) -> PreToolDecision:
            runtime = getattr(getattr(exec, "agent", None), "runtime", None)
            tool = runtime.get(exec.name) if runtime is not None else None
            if tool is None:
                return await next_()
            if self.check(tool, exec.arguments) is SandboxDecision.ASK:
                tag = ",".join(permission_names(classify_permissions(tool)))
                return PreToolDecision.ask(
                    f"sandbox approval required for {exec.name} (needs [{tag}])"
                )
            return await next_()

        return _ask
