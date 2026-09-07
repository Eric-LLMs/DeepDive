"""Workflow definitions and definition fingerprints (``wf1-``).

A :class:`WorkflowDefinition` is an immutable *structural* description of one workflow:
its name, its lifecycle transition table, the logical tasks it runs, the cap dimensions its
loop policy declares, and the hooks it uses. The fingerprint answers exactly one question —
"is this the same flow definition?" — so the spec carries structure ONLY.

Runtime-swappable configuration is structurally rejected by :func:`validate_spec`, which
whitelists the top-level keys and the activity record shape. Model / provider names, API
endpoints, temperature, token budgets, concurrency, worker instances, environments and cap
*values* have nowhere to live inside a spec — adding any of them fails validation (unknown
key or wrong type), not a silent fingerprint drift surprise at lease-acquire time.

The ``executor`` field of an activity is a **logical executor identity** (a stable,
human-chosen name like ``"doc-synthesizer"``), never a physical model or worker instance.
Re-pointing a logical executor at a different provider must not invalidate live executions.

Binding lifecycle (enforced by the adapter in Phase 2; specified here):
``begin`` mints the fingerprint into the slot at acquisition; every ``acquire`` re-checks
it inside the lease CAS; a mismatch terminalizes the execution as FAILED
(``cause="definition_drift"``) — no silent resume, no replay machinery, no version tables.
A new definition takes effect by starting a new execution.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

FINGERPRINT_PREFIX = "wf1-"

# Exact top-level key set — the whitelist IS the boundary between definition and runtime.
_SPEC_KEYS = frozenset({"name", "transitions", "activities", "caps", "hooks"})
_ACTIVITY_KEYS = frozenset({"task_name", "executor"})


@runtime_checkable
class WorkflowDefinition(Protocol):
    """The minimal contract: a name plus a structural spec export."""

    name: str

    def spec(self) -> Mapping[str, object]:
        """A JSON-canonical, structure-only snapshot (validated by :func:`validate_spec`)."""
        ...


def validate_spec(spec: Mapping[str, object]) -> dict:
    """Structurally validate a spec and return a plain-data normalized copy.

    Raises ``ValueError`` for unknown keys, missing keys, wrong types, empty names or
    duplicates — i.e. any attempt to smuggle runtime configuration into the definition.
    """
    if not isinstance(spec, Mapping):
        raise ValueError(f"spec must be a mapping, got {type(spec).__name__}")
    keys = frozenset(spec.keys())
    if keys != _SPEC_KEYS:
        missing = sorted(_SPEC_KEYS - keys)
        unknown = sorted(keys - _SPEC_KEYS)
        raise ValueError(
            f"invalid spec keys; unknown={unknown or '-'} missing={missing or '-'}"
        )

    name = spec["name"]
    if not isinstance(name, str) or not name:
        raise ValueError("spec['name'] must be a non-empty string")

    transitions = spec["transitions"]
    if not isinstance(transitions, Mapping) or not transitions:
        raise ValueError("spec['transitions'] must be a non-empty mapping")
    norm_transitions: dict[str, list[str]] = {}
    for source, targets in transitions.items():
        if not isinstance(source, str) or not isinstance(targets, (list, tuple, set)):
            raise ValueError(f"transition {source!r} must map str -> list[str]")
        if any(not isinstance(t, str) for t in targets):
            raise ValueError(f"transitions for {source!r} must contain only strings")
        norm_transitions[source] = sorted(set(targets))

    activities = spec["activities"]
    if not isinstance(activities, Sequence) or isinstance(activities, (str, bytes)):
        raise ValueError("spec['activities'] must be a sequence of records")
    norm_activities: list[dict[str, str]] = []
    seen_tasks: set[str] = set()
    for item in activities:
        if not isinstance(item, Mapping) or frozenset(item.keys()) != _ACTIVITY_KEYS:
            raise ValueError("activity records must have exactly {task_name, executor}")
        task_name, executor = item["task_name"], item["executor"]
        if not isinstance(task_name, str) or not task_name:
            raise ValueError("activity task_name must be a non-empty string")
        if not isinstance(executor, str) or not executor:
            raise ValueError("activity executor must be a non-empty string")
        if task_name in seen_tasks:
            raise ValueError(f"duplicate activity task_name: {task_name!r}")
        seen_tasks.add(task_name)
        norm_activities.append({"task_name": task_name, "executor": executor})
    norm_activities.sort(key=lambda a: a["task_name"])

    caps = spec["caps"]
    hooks = spec["hooks"]
    for label, value in (("caps", caps), ("hooks", hooks)):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError(f"spec['{label}'] must be a sequence of strings")
        if any(not isinstance(v, str) or not v for v in value):
            raise ValueError(f"spec['{label}'] must contain non-empty strings only")
    if len(set(caps)) != len(caps) or len(set(hooks)) != len(hooks):
        raise ValueError("caps and hooks must be duplicate-free")

    return {
        "name": name,
        "transitions": norm_transitions,
        "activities": norm_activities,
        "caps": sorted(caps),
        "hooks": sorted(hooks),
    }


def canonical_json(spec: Mapping[str, object]) -> str:
    """The single canonicalization rule: validated, sorted-keys, ASCII, strict JSON.

    No ``default=str`` fallback — any value that survived :func:`validate_spec` must still
    be plain JSON; anything else raises so a fingerprint can never silently absorb a
    stringified object.
    """
    normalized = validate_spec(spec)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    )


def compute_fingerprint(spec: Mapping[str, object]) -> str:
    """``wf1-`` + sha256 over :func:`canonical_json`. Algorithm-prefixed for future rotation."""
    return FINGERPRINT_PREFIX + hashlib.sha256(
        canonical_json(spec).encode("utf-8")
    ).hexdigest()
