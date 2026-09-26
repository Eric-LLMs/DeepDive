"""Registry value types — the in-code mirror of the LIVE tables (migration 0014,
final ruling 2026-09-26).

One role only:

* ``CapabilityEntry`` — one capability hydrated from ``capabilities`` JOIN
  ``capability_standard_queries`` / ``capability_similar_queries`` /
  ``capability_negatives``. Those tables ARE the runtime truth: there is no
  Draft -> Publish -> Projection lane, and ``registry_versions`` is history.

``RegistryLiveView`` is the runtime's read model: the full entry set plus a
content fingerprint the Matcher cache and the executor TOCTOU re-validation
key on. The optimistic-concurrency ``row_version`` stays on the capability
row (intent fields only) — query rows are edited through the admin corpus
plane, which re-embeds atomically.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Lifecycle states for a capability (docs/temp.md 8.6 — no hard delete).
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_DEPRECATED = "deprecated"

# Pattern literals matching this prefix are regexes for the Matcher; everything
# else is an exact phrase. Shared so the validation gate and the Matcher agree.
RE_PREFIX = "re:"

# Intent kinds (P3, full-intent-space ruling): the candidate space beyond plain
# ACTION. Each kind has its OWN rollout gate (settings.chat_funnel_*_enabled) —
# registering a capability in the table never enables routing it (8.2/灰度令).
KIND_ACTION = "action"
KIND_PRIVATE = "private"
KIND_WEB = "web"
VALID_KINDS = frozenset({KIND_ACTION, KIND_PRIVATE, KIND_WEB})

# Frozen language derivation rule (ruling 2026-09-26): a sentence containing a
# Han char (U+4E00..U+9FFF) is 'zh', everything else is 'en'. No languages
# table — the value is materialized on the query rows.
def derive_language(text: str) -> str:
    return "zh" if any("一" <= ch <= "鿿" for ch in str(text or "")) else "en"


@dataclass(frozen=True)
class QueryRecord:
    """One live-table sentence with its provenance. ``kind`` is 'standard' or
    'similar'; ``standard_query_id`` is set on Similar rows (the ONLY relation
    a Similar has to its Capability — A3 ruling)."""

    id: str
    query: str
    language: str
    enabled: bool = True
    position: int = 0
    standard_query_id: str | None = None


@dataclass(frozen=True)
class CapabilityEntry:
    """One capability: intent information (row) + the hydrated live corpus."""

    capability_id: str
    tool_binding: str
    description: str = ""
    # Legacy matcher columns — NOT match data any more (Matcher truth is the
    # live query tables); kept only because the DB still carries them.
    patterns: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # Card context only (renamed from ``examples`` by 0014): never embedded,
    # never a Matcher/Recall anchor.
    request_query_examples: tuple[str, ...] = ()
    standard_queries: tuple[QueryRecord, ...] = ()
    similar_queries: tuple[QueryRecord, ...] = ()
    # capability_negatives rows (card boundary context; never in Recall).
    negatives: tuple[str, ...] = ()
    # CANONICAL parameter schema — the ToolIntentModel Candidate Card assembles from
    # HERE. Shape:
    # {name: {"type": str, "description": str, "required": bool, "max_len": int?}}
    parameters: dict[str, Any] = field(default_factory=dict)
    arg_slots: dict[str, Any] = field(default_factory=dict)
    permissions: str = ""
    execution_policy: str = "auto"
    intent_kind: str = KIND_ACTION
    enabled: bool = True
    status: str = STATUS_ACTIVE
    replacement_capability_id: str | None = None
    # Optimistic-concurrency token: a write must present the row_version it read.
    row_version: int = 0

    @property
    def intent_corpus(self) -> tuple[str, ...]:
        """THE intent-expression corpus: enabled Standard queries + enabled
        Similar queries (order: standards by position, then similars by
        position), blanks out, deduped. Single source of truth for BOTH the
        Matcher exact set AND the Recall corpus — one property, no drift.
        ``request_query_examples`` and negatives are deliberately NOT here:
        card context / card boundary, never match or recall anchors."""
        out: list[str] = []
        for rec in (*self.standard_queries, *self.similar_queries):
            if not rec.enabled:
                continue
            s = str(rec.query or "").strip()
            if s and s not in out:
                out.append(s)
        return tuple(out)

    @classmethod
    def from_rows(cls, row: Any, standard_rows: Sequence = (),
                  similar_rows: Sequence = (),
                  negative_rows: Sequence = ()) -> CapabilityEntry:
        def _q(r: Any) -> QueryRecord:
            return QueryRecord(
                id=str(r.id), query=r.query, language=r.language,
                enabled=bool(r.enabled), position=int(r.position or 0),
                standard_query_id=str(getattr(r, "standard_query_id", None) or "")
                or None,
            )
        return cls(
            capability_id=row.capability_id,
            tool_binding=row.tool_binding,
            description=row.description or "",
            patterns=tuple(row.patterns or ()),
            aliases=tuple(row.aliases or ()),
            request_query_examples=tuple(row.request_query_examples or ()),
            standard_queries=tuple(_q(r) for r in standard_rows),
            similar_queries=tuple(_q(r) for r in similar_rows),
            negatives=tuple(str(r.query) for r in negative_rows if r.enabled),
            parameters=dict(row.parameters or {}),
            arg_slots=dict(row.arg_slots or {}),
            permissions=row.permissions or "",
            execution_policy=row.execution_policy or "auto",
            intent_kind=row.intent_kind or KIND_ACTION,
            enabled=bool(row.enabled),
            status=row.status or STATUS_ACTIVE,
            replacement_capability_id=row.replacement_capability_id,
            row_version=int(row.row_version or 0),
        )

    def to_payload(self) -> dict:
        """JSON-safe form for the live-table content fingerprint AND for a
        registry_versions history snapshot (pre-write audit rows)."""
        def _q(rec: QueryRecord) -> dict:
            d = {"id": rec.id, "query": rec.query, "language": rec.language,
                 "enabled": rec.enabled, "position": rec.position}
            if rec.standard_query_id:
                d["standard_query_id"] = rec.standard_query_id
            return d
        return {
            "capability_id": self.capability_id,
            "tool_binding": self.tool_binding,
            "description": self.description,
            "patterns": list(self.patterns),
            "aliases": list(self.aliases),
            "request_query_examples": list(self.request_query_examples),
            "standard_queries": [_q(r) for r in self.standard_queries],
            "similar_queries": [_q(r) for r in self.similar_queries],
            "negatives": list(self.negatives),
            "parameters": dict(self.parameters),
            "arg_slots": dict(self.arg_slots),
            "permissions": self.permissions,
            "execution_policy": self.execution_policy,
            "intent_kind": self.intent_kind,
            "enabled": self.enabled,
            "status": self.status,
            "replacement_capability_id": self.replacement_capability_id,
        }

    @classmethod
    def from_payload(cls, raw: dict) -> CapabilityEntry:
        """Rebuild from a history-snapshot payload (no row ids / row_version:
        those are live-table bookkeeping, restored by re-insertion)."""
        def _q(d: dict) -> QueryRecord:
            return QueryRecord(
                id=str(d.get("id") or ""), query=str(d.get("query") or ""),
                language=str(d.get("language") or derive_language(d.get("query") or "")),
                enabled=bool(d.get("enabled", True)),
                position=int(d.get("position") or 0),
                standard_query_id=d.get("standard_query_id"),
            )
        return cls(
            capability_id=str(raw["capability_id"]),
            tool_binding=str(raw["tool_binding"]),
            description=str(raw.get("description") or ""),
            patterns=tuple(str(p) for p in raw.get("patterns") or ()),
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            request_query_examples=tuple(
                str(x) for x in raw.get("request_query_examples") or ()),
            standard_queries=tuple(_q(d) for d in raw.get("standard_queries") or ()),
            similar_queries=tuple(_q(d) for d in raw.get("similar_queries") or ()),
            negatives=tuple(str(n) for n in raw.get("negatives") or ()),
            parameters=dict(raw.get("parameters") or {}),
            arg_slots=dict(raw.get("arg_slots") or {}),
            permissions=str(raw.get("permissions") or ""),
            execution_policy=str(raw.get("execution_policy") or "auto"),
            # payloads written before P3 carry no kind: ACTION is the historical
            # and safe default (no old snapshot can restore a widened kind)
            intent_kind=str(raw.get("intent_kind") or KIND_ACTION),
            enabled=bool(raw.get("enabled", True)),
            status=str(raw.get("status") or STATUS_ACTIVE),
            replacement_capability_id=raw.get("replacement_capability_id"),
            row_version=0,
        )


@dataclass(frozen=True)
class RegistryLiveView:
    """The runtime read model over the live tables: the full entry set plus
    the content fingerprint every cache / re-validation keys on."""

    fingerprint: str
    entries: tuple[CapabilityEntry, ...]
