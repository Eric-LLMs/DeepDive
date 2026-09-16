"""Public entry of the Presentation Brief workflow for the Toolkit pipeline.

``stage_generate``'s slides branch calls this with the ingested
:class:`DocumentRepresentation` and the request controls; it returns the canonical
:class:`PresentationBrief` (or raises loudly). The stats bundle mirrors the shipped
deck-stats convention so both engines land in one log vocabulary.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from . import structured as ST
from .schema import (
    DocumentRepresentation,
    PresentationBrief,
    PresentationControls,
    PresentationWorkflowConfig,
)
from .workflow_adapter import run_brief_workflow


async def run_presentation_workflow(
    llm: Any,
    doc_rep: DocumentRepresentation,
    controls: PresentationControls,
    *,
    deck_id: str,
    config: PresentationWorkflowConfig | None = None,
    cancel: Callable[[], bool] | None = None,
    pending_signals: Callable[[], int] | None = None,
    stats_out: dict | None = None,
) -> PresentationBrief:
    t0 = time.perf_counter()
    brief, stats = await run_brief_workflow(
        llm=llm, doc_rep=doc_rep, controls=controls, deck_id=deck_id,
        config=config, cancel=cancel, pending_signals=pending_signals,
    )
    if stats_out is not None:
        stats_out.update(stats)                # host seam: job-record per-stage stats
    ST.log_stats_summary(deck_id, stats, time.perf_counter() - t0)
    return brief
