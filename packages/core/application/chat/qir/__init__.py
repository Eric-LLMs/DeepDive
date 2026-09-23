"""QIR — the Intent-Routing stage of ``resolve_plan`` (NOT a second router).

Frozen pipeline position:

    resolve_plan
      (1) QIR [this package]   Query -> sentence-level Intent -> Capability ID
      (2) Argument Binding     Capability + Query -> structured arguments   [actions.py]
      (3) build_execution_plan Requirements -> PlanKind / Executor          [existing]

Hard boundaries (test-enforced):
* this package never imports ``api.*`` / ``agent.*`` and owns NO execution,
  NO argument extraction, NO authorization and NO second tool registry;
* output is ``RouteResult(capability_id, registry_version)`` only;
* the cascade Fail-Opens: exception, timeout, empty/ambiguous candidates,
  decision NONE or a disabled stage all return ``None`` — and ``None`` means
  "the existing Agent path proceeds, byte-identical, untouched".

Exact-matching lives exclusively in the existing L0 engine (``actions.py`` /
``understanding.py``) — this package runs only after L0 abstained from an action
certification, and adds Semantic (candidates) + Decision (single adjudication).
"""
from __future__ import annotations

import asyncio
import logging

from .types import RouteResult, Snapshot  # noqa: F401  (public vocabulary)

logger = logging.getLogger(__name__)

__all__ = ["route", "RouteResult", "Snapshot", "store"]


async def route(
    message: str, *, snapshot: Snapshot | None, embedder, llm,
    top_k: int, min_score: float, margin: float,
    decision_enabled: bool, timeout_seconds: float,
) -> RouteResult | None:
    """Run the cascade under one wall-clock budget. NEVER raises; NEVER executes."""
    if snapshot is None or not decision_enabled:
        # No published snapshot, or the decision stage is dark: semantic
        # similarity alone must never produce a routable verdict.
        return None
    try:
        return await asyncio.wait_for(
            _cascade(message, snapshot=snapshot, embedder=embedder, llm=llm,
                     top_k=top_k, min_score=min_score, margin=margin),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # timeout, embedder/LLM transport, anything: fail-open
        logger.info("qir.route fail-open: %r", exc)
        return None


async def _cascade(message: str, *, snapshot: Snapshot, embedder, llm,
                   top_k: int, min_score: float, margin: float) -> RouteResult | None:
    from . import decision as _decision
    from . import semantic as _semantic

    cands = await _semantic.candidates(
        embedder, snapshot, message, top_k=top_k, min_score=min_score, margin=margin,
    )
    if not cands:
        return None
    verdict = await _decision.adjudicate(llm, message, snapshot, cands)
    if verdict.capability_id is None:
        return None
    cap = snapshot.get(verdict.capability_id)
    if cap is None or not cap.enabled:
        # routing-metadata inconsistency: refuse, do not fabricate a route
        logger.error("qir integrity: decided capability %r missing/disabled", verdict.capability_id)
        return None
    logger.info(
        "qir.route hit capability=%s version=%s cands=%d rationale=%r",
        cap.id, snapshot.version, len(cands), verdict.rationale[:120],
    )
    return RouteResult(capability_id=cap.id, registry_version=snapshot.version)
