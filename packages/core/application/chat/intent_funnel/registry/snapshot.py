"""Validation gate for live-table writes (migration 0014, final ruling 2026-09-26).

The Draft -> Validate -> Build -> Publish lane is retired: the live tables ARE
the runtime truth and admin writes go live directly. What survives is the
VALIDATE half, now the gate in front of every write path and the "check"
button of the admin console:

* ``validate_entries(entries, tool_schemas=...)`` — pure function, every rule
  as an issue string; empty list == consistent.
* The tool roster is NOT the code allowlist: the source of truth is the live
  ``ToolRuntime.schemas()`` projection passed in by the caller (admin API).
  A tool existing is NOT the same as an action being registered, and the
  DIRECT_TOOLS dispatch allowlist stays untouched — intent is never
  authorization.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from .entry import (
    RE_PREFIX,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DISABLED,
    VALID_KINDS,
    CapabilityEntry,
)
from .plugins import PLUGINS  # extractor roster (leaf)

# arg_slots.source minimal enum (P1 ruling 2 — no speculative additions).
# plugin:<name> is the seventh form: the table registers WHICH extractor, the
# extractor itself stays in code (8.1-b) — the name MUST exist in PLUGINS
# (roster membership = the "启停/版本归 Registry 管" discipline: an unregistered
# extractor can never pass the gate).
VALID_SOURCES = frozenset({
    "user_input", "viewer.current_page", "viewer.selection",
    "attachment", "turn_context", "fixed",
})
VALID_POLICIES = frozenset({"auto", "approval", "sandbox"})
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED, STATUS_DEPRECATED})


class RegistryValidationError(Exception):
    """Write gate closed: the entry set failed validation. Carries every issue
    (not just the first) so the editor fixes one round, not one page."""

    def __init__(self, issues: Sequence[str]):
        super().__init__("; ".join(issues))
        self.issues = list(issues)


# ── Validate ─────────────────────────────────────────────────────────────────────

def validate_entries(
    entries: Sequence[CapabilityEntry],
    *,
    tool_schemas: dict[str, dict[str, int]] | None = None,
) -> list[str]:
    """Every rule as a pure function; empty list == consistent. ``tool_schemas``
    maps runtime tool name -> {arg: max_len} (from ToolRuntime.schemas()); when
    None the tool-existence cross-check is skipped (unit tests)."""
    issues: list[str] = []
    if not entries:
        return ["refusing an empty Registry: no capability is registered"]
    seen: set[str] = set()
    for e in entries:
        cid = e.capability_id.strip()
        if not cid:
            issues.append("capability_id must not be blank")
            continue
        if cid in seen:
            issues.append(f"duplicate capability_id {cid!r}")
            continue
        seen.add(cid)
        if not e.tool_binding.strip():
            issues.append(f"{cid}: tool_binding is required")
        elif tool_schemas is not None and e.tool_binding not in tool_schemas:
            issues.append(
                f"{cid}: tool_binding {e.tool_binding!r} is not a tool in the "
                "live ToolRuntime roster (the Registry cannot invent executables)"
            )
        if not e.description.strip():
            issues.append(f"{cid}: description is required (ToolIntentModel card source)")
        if not e.intent_corpus and _routable(e):
            # A non-routable row (disabled/deprecated — e.g. freshly created
            # from the Catalog before its corpus is curated) may lawfully have
            # no sentences; a ROUTABLE one must never be inert.
            issues.append(
                f"{cid}: query corpus must be non-empty (capability_standard_"
                "queries / capability_similar_queries — request examples and "
                "negatives are never match or recall anchors)"
            )
        if _routable(e):
            langs = {r.language for r in e.standard_queries
                     if r.enabled and str(r.query or "").strip()}
            if langs != {"zh", "en"}:
                issues.append(
                    f"{cid}: routable capability needs exactly one enabled "
                    f"Standard per language, found {sorted(langs) or 'none'}"
                )
        issues.extend(f"{cid}: {msg}" for msg in
                      _parameter_issues(e, tool_schemas))
        if e.status not in VALID_STATUSES:
            issues.append(f"{cid}: status {e.status!r} not in {sorted(VALID_STATUSES)}")
        if e.enabled and e.status != STATUS_ACTIVE:
            issues.append(
                f"{cid}: enabled=True contradicts status={e.status!r} "
                "(disable by flipping status/enabled consistently, ruling 4)"
            )
        if e.replacement_capability_id:
            if e.status != STATUS_DEPRECATED:
                issues.append(f"{cid}: replacement_capability_id is only for deprecated entries")
            else:
                r = e.replacement_capability_id.strip()
                if r not in seen and all(o.capability_id.strip() != r for o in entries):
                    issues.append(f"{cid}: replacement {r!r} is not a capability in this set")
        if e.execution_policy not in VALID_POLICIES:
            issues.append(f"{cid}: execution_policy {e.execution_policy!r} not in {sorted(VALID_POLICIES)}")
        if e.intent_kind not in VALID_KINDS:
            issues.append(f"{cid}: intent_kind {e.intent_kind!r} not in {sorted(VALID_KINDS)}")
        if any(ch.isspace() for ch in e.permissions):
            issues.append(f"{cid}: permissions must be a single token (or empty)")
        for lit in (*e.patterns, *e.aliases):
            s = str(lit).strip()
            if s.startswith(RE_PREFIX):
                try:
                    re.compile(s[len(RE_PREFIX):])
                except re.error as exc:
                    issues.append(f"{cid}: un-compilable regex pattern {s!r}: {exc}")
        for slot, source in e.arg_slots.items():
            issues.extend(
                f"{cid}: arg_slots[{slot!r}] {msg}" for msg in _slot_issues(source)
            )
        for rec in (*e.standard_queries, *e.similar_queries):
            if not str(rec.query or "").strip():
                issues.append(f"{cid}: blank query row {rec.id}")
            elif rec.language != _derive(rec.query):
                issues.append(
                    f"{cid}: query {rec.id} language {rec.language!r} violates "
                    "the frozen derivation rule"
                )
        for sim in e.similar_queries:
            if not any(st.id == sim.standard_query_id for st in e.standard_queries):
                issues.append(
                    f"{cid}: similar query {sim.id} points outside this "
                    "capability's Standard set"
                )
    issues.extend(_corpus_conflicts(entries))
    return issues


def _derive(text: str) -> str:
    return "zh" if any("一" <= ch <= "鿿" for ch in str(text or "")) else "en"


def _slot_issues(source: Any) -> list[str]:
    src = source.get("source") if isinstance(source, dict) else source
    if not isinstance(src, str) or not src:
        return ["source must be a string or {source: str}"]
    if src in VALID_SOURCES:
        return []
    if src.startswith("plugin:"):
        name = src[len("plugin:"):].strip()
        if not name:
            return ["plugin: source needs a name"]
        if name not in PLUGINS:
            return [f"plugin:{name!r} is not registered in the extractor roster"]
        return []
    return [
        f"source {src!r} outside the minimal enum "
        f"{sorted(VALID_SOURCES)} or plugin:<name>"
    ]


def _routable(e: CapabilityEntry) -> bool:
    return e.enabled and e.status == STATUS_ACTIVE


def _parameter_issues(e: CapabilityEntry,
                      tool_schemas: dict[str, dict[str, int]] | None) -> list[str]:
    """Registry is the CANONICAL parameter-schema source; ToolRuntime.schemas()
    is the live tool roster. This gate is the only bridge: a mechanical
    cross-check against the passed-in projection, so no human sync burden
    exists. Rules: slot-name sets must agree, ``max_len`` (when given) must
    match the runtime bound, and every parameter carries the Card-minimal
    shape."""
    issues: list[str] = []
    runtime: dict[str, int] | None = None
    if tool_schemas is not None and e.tool_binding in tool_schemas:
        runtime = dict(tool_schemas[e.tool_binding] or {})
    if runtime is None:
        pass  # no roster (unit tests) or tool unlisted: membership reported above
    else:
        declared_slots = set(e.parameters or {})
        for slot in sorted(set(runtime) - declared_slots):
            issues.append(
                f"parameters missing runtime slot {slot!r} of {e.tool_binding!r} "
                "(Registry must carry the full schema — dual sources forbidden)"
            )
        for slot in sorted(declared_slots - set(runtime)):
            issues.append(
                f"parameters declares {slot!r}, unknown to runtime tool {e.tool_binding!r}"
            )
    declared: dict[str, Any] = dict(e.parameters or {})
    for slot, raw in sorted(declared.items()):
        if not isinstance(raw, dict):
            issues.append(f"parameters[{slot!r}] must be an object")
            continue
        if not str(raw.get("type") or "").strip():
            issues.append(f"parameters[{slot!r}].type is required")
        if not str(raw.get("description") or "").strip():
            issues.append(f"parameters[{slot!r}].description is required (Card source)")
        if not isinstance(raw.get("required"), bool):
            issues.append(f"parameters[{slot!r}].required must be a bool")
        max_len = raw.get("max_len")
        # The roster's maxLength is authoritative only when it states a bound;
        # a schema without maxLength carries no runtime bound to contradict.
        if (max_len is not None and runtime is not None and slot in runtime
                and runtime[slot] and int(max_len) != int(runtime[slot])):
            issues.append(
                f"parameters[{slot!r}].max_len={max_len} contradicts runtime "
                f"bound {runtime[slot]} of {e.tool_binding!r}"
            )
    return issues


def _corpus_conflicts(entries: Sequence[CapabilityEntry]) -> list[str]:
    """Deterministic-match collision: the same corpus sentence in two ROUTABLE
    capabilities makes the Matcher ambiguous by construction — report it."""
    owner: dict[str, str] = {}
    issues: list[str] = []
    for e in entries:
        if not _routable(e):
            continue
        for lit in (*e.intent_corpus, *e.patterns, *e.aliases):
            lit = str(lit).strip()
            if not lit:
                issues.append(f"{e.capability_id}: blank corpus/pattern entry")
                continue
            key = lit.casefold()
            prev = owner.setdefault(key, e.capability_id)
            if prev != e.capability_id:
                issues.append(
                    f"deterministic conflict: {lit!r} claimed by {prev!r} and "
                    f"{e.capability_id!r}"
                )
    return issues
