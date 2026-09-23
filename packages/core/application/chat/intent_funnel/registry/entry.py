"""Registry value types — the in-code mirror of the ``capabilities`` /
``registry_versions`` tables (QIR P1 step 1).

Two distinct roles, never conflated:

* ``CapabilityEntry``  — one ROW of the editable Draft set (table ``capabilities``).
  Mutable working copy; carries the optimistic-concurrency ``row_version``.
* A published ``registry_versions`` row is an immutable projection; the store hands
  it back as ``RegistryVersionView`` whose ``capabilities`` are the frozen
  ``qir.types.Capability`` the runtime already consumes.

The runtime reads ONLY an active version view, never the Draft table (frozen
discipline: Draft -> Validate -> Build -> Publish).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.application.chat.qir.types import Capability


# Lifecycle states for a Draft capability (docs/temp.md 8.6 — no hard delete).
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_DEPRECATED = "deprecated"

# Pattern literals matching this prefix are regexes for the Matcher; everything
# else is an exact phrase. Shared so the publish gate and the Matcher agree.
RE_PREFIX = "re:"

# Intent kinds (P3, full-intent-space ruling): the candidate space beyond plain
# ACTION. Each kind has its OWN rollout gate (settings.chat_funnel_*_enabled) —
# registering a capability in the table never enables routing it (8.2/灰度令).
KIND_ACTION = "action"
KIND_PRIVATE = "private"
KIND_WEB = "web"
VALID_KINDS = frozenset({KIND_ACTION, KIND_PRIVATE, KIND_WEB})


@dataclass(frozen=True)
class CapabilityEntry:
    """One Registry capability as authored in the Draft table. Field-for-field the
    docs ``CapabilityEntry`` contract plus the storage bookkeeping (row_version)."""

    capability_id: str
    tool_binding: str
    description: str = ""
    patterns: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    negatives: tuple[str, ...] = ()
    arg_slots: dict[str, Any] = field(default_factory=dict)
    permissions: str = ""
    execution_policy: str = "auto"
    intent_kind: str = KIND_ACTION
    enabled: bool = True
    status: str = STATUS_ACTIVE
    replacement_capability_id: str | None = None
    # Optimistic-concurrency token: a write must present the row_version it read.
    row_version: int = 0

    def to_capability(self) -> Capability:
        """Project to the runtime's frozen Capability (routing metadata only —
        Matcher patterns/aliases and arg_slots stay in the Registry row, they are
        consumed by their own nodes, never smuggled through the QIR contract)."""
        return Capability(
            id=self.capability_id,
            tool_binding=self.tool_binding,
            description=self.description,
            examples=tuple(self.examples),
            negatives=tuple(self.negatives),
            enabled=self.enabled,
        )

    @classmethod
    def from_row(cls, row: Any) -> "CapabilityEntry":
        return cls(
            capability_id=row.capability_id,
            tool_binding=row.tool_binding,
            description=row.description or "",
            patterns=tuple(row.patterns or ()),
            aliases=tuple(row.aliases or ()),
            examples=tuple(row.examples or ()),
            negatives=tuple(row.negatives or ()),
            arg_slots=dict(row.arg_slots or {}),
            permissions=row.permissions or "",
            execution_policy=row.execution_policy or "auto",
            intent_kind=getattr(row, "intent_kind", None) or KIND_ACTION,
            enabled=bool(row.enabled),
            status=row.status or STATUS_ACTIVE,
            replacement_capability_id=row.replacement_capability_id,
            row_version=int(row.row_version or 0),
        )

    def to_payload(self) -> dict:
        """JSON-safe form for a published version snapshot."""
        return {
            "capability_id": self.capability_id,
            "tool_binding": self.tool_binding,
            "description": self.description,
            "patterns": list(self.patterns),
            "aliases": list(self.aliases),
            "examples": list(self.examples),
            "negatives": list(self.negatives),
            "arg_slots": dict(self.arg_slots),
            "permissions": self.permissions,
            "execution_policy": self.execution_policy,
            "intent_kind": self.intent_kind,
            "enabled": self.enabled,
            "status": self.status,
            "replacement_capability_id": self.replacement_capability_id,
        }

    @classmethod
    def from_payload(cls, raw: dict) -> "CapabilityEntry":
        # Published payloads carry no row_version (that is Draft-only bookkeeping).
        return cls(
            capability_id=str(raw["capability_id"]),
            tool_binding=str(raw["tool_binding"]),
            description=str(raw.get("description") or ""),
            patterns=tuple(str(p) for p in raw.get("patterns") or ()),
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            examples=tuple(str(e) for e in raw.get("examples") or ()),
            negatives=tuple(str(n) for n in raw.get("negatives") or ()),
            arg_slots=dict(raw.get("arg_slots") or {}),
            permissions=str(raw.get("permissions") or ""),
            execution_policy=str(raw.get("execution_policy") or "auto"),
            # payloads published before P3 carry no kind: ACTION is the historical
            # and safe default (no old version can route a widened kind)
            intent_kind=str(raw.get("intent_kind") or KIND_ACTION),
            enabled=bool(raw.get("enabled", True)),
            status=str(raw.get("status") or STATUS_ACTIVE),
            replacement_capability_id=raw.get("replacement_capability_id"),
            row_version=0,
        )


# registry_versions.state values (Build-Then-Swap lifecycle; 8.3 + ruling 7).
STATE_STAGED = "staged"
STATE_ACTIVE = "active"
STATE_FAILED = "failed"
STATE_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class RegistryVersionView:
    """An immutable published version, read back for the runtime / audit."""

    version: int
    state: str
    fingerprint: str
    capabilities: tuple[Capability, ...]
    entries: tuple[CapabilityEntry, ...] = ()
    source_version: int | None = None
    actor_username: str | None = None
    note: str | None = None
    error: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "RegistryVersionView":
        payload = row.payload or {}
        entries = tuple(
            CapabilityEntry.from_payload(e) for e in payload.get("capabilities") or ()
        )
        # Ruling 4: a disabled/deprecated capability is projected OUT of the runtime
        # view — it can never be an executable candidate; ``entries`` keeps the full
        # published set for admin/audit.
        return cls(
            version=int(row.version),
            state=row.state,
            fingerprint=row.fingerprint,
            capabilities=tuple(
                e.to_capability() for e in entries
                if e.enabled and e.status == STATUS_ACTIVE
            ),
            entries=entries,
            source_version=row.source_version,
            actor_username=row.actor_username,
            note=row.note,
            error=row.error,
        )
