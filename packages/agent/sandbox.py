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

from agent.decisions import Guard, ToolExecution
from agent.tool_permissions import ToolPermission, permission_names
from agent.tools import ToolDefinition, classify_permissions


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
    def session_permissions(self) -> set[ToolPermission]:
        """The permission classes the current session has been granted."""
        return set(self._permissions)

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
            if permission in self._permissions
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
        """A monotonic PreToolUse guard enforcing this sandbox inside the runtime.

        Returns a deny *reason* string (or ``None`` to pass). ASK without a human
        approver degrades to deny so the default is safe.
        """

        async def _guard(exec: ToolExecution) -> str | None:
            runtime = getattr(getattr(exec, "agent", None), "runtime", None)
            tool = runtime.get(exec.name) if runtime is not None else None
            if tool is None:
                return None
            decision = self.check(tool, exec.arguments)
            if decision is SandboxDecision.ALLOW:
                return None
            tag = ",".join(permission_names(classify_permissions(tool)))
            if decision is SandboxDecision.DENY:
                return f"sandbox denied: {exec.name} needs [{tag}] but the session has [{','.join(permission_names(self._permissions))}]"
            return f"sandbox approval required for {exec.name} (needs [{tag}])"

        return _guard
