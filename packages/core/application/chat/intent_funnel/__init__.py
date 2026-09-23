"""Intent Funnel — the independent, decoupled routing lane above the Agent.

Design (docs/temp.md, "Chat 意图路由重构"): a node funnel
Matcher -> Recall -> Judge -> Decision, escalating ONLY upward, with the Agent
as the byte-identical fallback consumer and the Shared Tool Runtime as the sole
execution point.

Scope status: P0 moved the orchestration out of ``TurnOrchestrator`` (gate +
legacy QIR cascade + argument binding, behavior byte-identical); P1 added the
Registry/Matcher with its shadow hook; P2 implements the target cascade behind
its OWN gate (``chat_funnel_enabled``, default OFF — the legacy lane stays
historical, not the baseline). The Agent remains the byte-identical fallback
consumer and the Shared Tool Runtime the sole execution point.
"""
from . import contract
from .funnel import funnel_live, qir_live, route, run_intent_stage

__all__ = ["contract", "route", "qir_live", "funnel_live", "run_intent_stage"]
