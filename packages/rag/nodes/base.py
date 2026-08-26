"""Pipeline node contract.

A node is a single pipeline stage: it reads from / writes to the shared
:class:`PipelineContext`, and returns a :class:`NodeStatus`. The pipeline executor
creates nodes from the configured name list, runs them in order, records a per-node
trace, and never lets one node's failure stop the downstream stages (degrade, never
stop on a silent empty).

``params`` are the per-node runtime parameters (from the admin console / config); the
pipeline-level dependencies live in ``deps`` so nodes stay constructible from a name +
params alone.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from rag.pipeline.context import PipelineContext


class NodeStatus(str, Enum):
    """Node run outcome. FAIL records an error and the pipeline continues."""
    OK = "OK"
    FAIL = "FAIL"
    SKIP = "SKIP"


class Node:
    name: str = "base"                                    # unique registry id
    display_name: str = "Base"                            # human label for the console
    stage: str = "transform"                              # "transform" | "ranking"
    params_schema: ClassVar[dict] = {                     # JSON Schema → console form
        "type": "object",
        "properties": {},
        "required": [],
    }
    default_params: ClassVar[dict] = {}                   # console pre-fill (mirrors runtime defaults)
    description: str = ""                                 # one-line "what this stage does" for the console

    def __init__(self, params: dict | None = None) -> None:
        self.params = params or {}

    async def run(self, ctx: PipelineContext, deps) -> NodeStatus:
        """Execute one stage: read/write ``ctx``, return the status.

        On failure return FAIL (or raise; the executor treats a raise as FAIL). The
        executor appends errors and keeps running downstream nodes.
        """
        raise NotImplementedError
