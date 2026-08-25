"""Pipeline blackboard: request, typed store, per-node trace.

A :class:`PipelineContext` is the single object every node reads from and writes to
during one retrieval. It is the type-safe shared "blackboard": nodes never call each
other directly — they only exchange state through the context, so adding / removing /
reordering nodes never touches node code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag.types import SearchHit


@dataclass
class RagRequest:
    """A single retrieval request carried through the pipeline."""

    query: str
    top_k: int = 5
    # filters maps capability keys to values. ``user_id`` (owner / workspace / ACL
    # scoping) and, when domain filtering is enabled, ``domain_id`` are honored by the
    # recall nodes.
    filters: dict | None = None


@dataclass
class NodeTrace:
    """One node's observability record for the admin test console."""

    name: str
    status: str            # OK | FAIL | SKIP
    ms: float
    note: str = ""
    out: Any = None        # per-node product summary shown in the console


class PipelineContext:
    """Blackboard shared by all nodes in one retrieval run."""

    def __init__(self, request: RagRequest) -> None:
        self.request = request
        self.store: dict[str, Any] = {}
        self.trace: list[NodeTrace] = []
        self.errors: list[str] = []
        self._outs: dict[str, Any] = {}

    # ── typed blackboard access ──
    def set(self, key: str, value: Any) -> None:
        self.store[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.store.get(key, default)

    # ── per-node summary for the console ──
    def set_out(self, name: str, summary: Any) -> None:
        self._outs[name] = summary

    def get_out(self, name: str) -> Any:
        return self._outs.get(name)

    # ── final result ──
    def final_hits(self) -> list[SearchHit]:
        """The current ranked hit list (set by the last ranking-stage node)."""
        return self.store.get("hits", [])
