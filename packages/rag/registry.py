"""Node registry: name → class, with single-point X-macro registration.

Registering a new node = one line in ``_import_and_register`` + its file in ``rag.nodes``.
The configured pipeline is just a list of registered names; unknown names are rejected
at config validation time, so a typo cannot silently produce a no-op pipeline.
"""
from __future__ import annotations

from rag.nodes.base import Node

_registry: dict[str, type[Node]] = {}


class NodeRegistry:
    """Singleton registry mapping node names to their classes."""

    def __init__(self) -> None:
        self._nodes: dict[str, type[Node]] = {}

    def register(self, cls: type[Node]) -> None:
        if cls.name in self._nodes:
            raise ValueError(f"node '{cls.name}' already registered")
        self._nodes[cls.name] = cls

    def create(self, name: str, params: dict | None = None) -> Node:
        cls = self._nodes.get(name)
        if cls is None:
            raise KeyError(f"no pipeline node registered as '{name}'")
        return cls(params)

    def get(self, name: str) -> type[Node] | None:
        return self._nodes.get(name)

    def available(self) -> list[str]:
        return sorted(self._nodes)

    def all(self) -> dict[str, type[Node]]:
        return dict(self._nodes)

    def metadata(self) -> list[dict]:
        """Per-node console metadata: name, labels, schema, and runtime defaults.

        ``default_params`` mirrors each node's runtime fallback so the admin console
        can pre-fill the params editor instead of showing an empty ``{}``.
        """
        return [
            {
                "name": cls.name,
                "display_name": cls.display_name,
                "stage": cls.stage,
                "params_schema": cls.params_schema,
                "default_params": dict(cls.default_params),
                "description": cls.description,
            }
            for cls in sorted(self._nodes.values(), key=lambda c: c.name)
        ]


registry = NodeRegistry()


def _import_and_register() -> None:
    """Import all node modules and register their classes (X-macro style).

    Importing a node module is a pure class definition (no side effects), so this is
    safe to call repeatedly; the registry raises if a name were ever registered twice.
    """
    from rag.nodes import (
        crg_check,
        cross_encoder,
        keyword_recall,
        parent_expand,
        query_rewrite,
        rrf_fusion,
        vector_recall,
    )

    for cls in (
        query_rewrite.QueryRewriteNode,
        vector_recall.VectorRecallNode,
        keyword_recall.KeywordRecallNode,
        parent_expand.ParentExpandNode,
        rrf_fusion.RrfFusionNode,
        cross_encoder.CrossEncoderNode,
        crg_check.CrgCheckNode,
    ):
        registry.register(cls)


_import_and_register()
