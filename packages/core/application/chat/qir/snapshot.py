"""Draft -> validate -> build immutable Snapshot (QIR stage-1 artifact factory).

Publish pipeline discipline (frozen constraint): any failure — unknown keys,
binding not in the existing action table, embedding error, vector-count mismatch —
raises ``SnapshotError`` BEFORE anything is written, so the new version never
becomes Active and the previous snapshot keeps serving. There is no partial or
half-ready publish path here: ``build_snapshot`` returns a value or raises.
"""
from __future__ import annotations

import time

from core.application.chat.actions import DIRECT_TOOLS

from .types import Capability, ExampleVector, Snapshot, SnapshotError, fingerprint

_DRAFT_KEYS = frozenset({"capabilities"})
_CAP_KEYS = frozenset({"id", "tool_binding", "description", "examples", "negatives", "enabled"})


def validate_draft(draft: dict) -> tuple[Capability, ...]:
    """Structure gate: reject unknown keys, duplicate ids and — critically — any
    capability whose ``tool_binding`` is not an existing Action Binding entry.
    A routable capability must already be executable by the existing machinery;
    QIR never invents bindings (it is a routing projection, not a registry)."""
    if not isinstance(draft, dict) or set(draft) - _DRAFT_KEYS or "capabilities" not in draft:
        raise SnapshotError(f"draft keys must be exactly {{capabilities}}, got {sorted(draft) if isinstance(draft, dict) else type(draft)}")
    raw = draft["capabilities"]
    if not isinstance(raw, list) or not raw:
        raise SnapshotError("capabilities must be a non-empty list")
    caps: list[Capability] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - _CAP_KEYS or "id" not in item \
                or "tool_binding" not in item or "description" not in item:
            raise SnapshotError(f"capability keys invalid: {sorted(item) if isinstance(item, dict) else item!r}")
        cap = Capability(
            id=str(item["id"]).strip(),
            tool_binding=str(item["tool_binding"]).strip(),
            description=str(item["description"]).strip(),
            examples=tuple(str(e).strip() for e in item.get("examples") or () if str(e).strip()),
            negatives=tuple(str(n).strip() for n in item.get("negatives") or () if str(n).strip()),
            enabled=bool(item.get("enabled", True)),
        )
        if not cap.id or cap.id in seen:
            raise SnapshotError(f"capability id missing/duplicate: {cap.id!r}")
        if cap.tool_binding not in DIRECT_TOOLS:
            raise SnapshotError(
                f"capability {cap.id!r}: tool_binding {cap.tool_binding!r} is not an "
                "existing DIRECT_TOOLS action binding (QIR cannot invent executables)"
            )
        if not cap.examples:
            raise SnapshotError(f"capability {cap.id!r}: examples must be non-empty")
        seen.add(cap.id)
        caps.append(cap)
    return tuple(caps)


async def build_snapshot(draft: dict, embedder) -> Snapshot:
    """Validate, then build ALL derived structures (example embeddings) into one
    immutable value. Embedding failure fails the whole build — the caller never
    receives a snapshot whose semantic index is incomplete."""
    caps = validate_draft(draft)
    flat: list[str] = []
    plan: list[tuple[str, int]] = []  # (capability_id, example_index) per flat row
    for cap in caps:
        for i, example in enumerate(cap.examples):
            flat.append(example)
            plan.append((cap.id, i))
    try:
        vectors = await embedder.embed(flat)
    except Exception as exc:
        raise SnapshotError(f"derived embedding index build failed: {exc!r}") from exc
    if not isinstance(vectors, list) or len(vectors) != len(flat):
        raise SnapshotError(
            f"embedder returned {len(vectors) if isinstance(vectors, list) else '?'} "
            f"vectors for {len(flat)} examples — refusing half-built snapshot"
        )
    example_vectors = tuple(
        ExampleVector(capability_id=cid, example_index=idx, vector=tuple(float(x) for x in vec))
        for (cid, idx), vec in zip(plan, vectors)
    )
    return Snapshot(
        version=fingerprint(caps),
        built_at=time.time(),
        capabilities=caps,
        example_vectors=example_vectors,
    )
