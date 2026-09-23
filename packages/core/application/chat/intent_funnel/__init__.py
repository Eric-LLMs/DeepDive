"""Intent Funnel — the independent, decoupled routing lane above the Agent.

Design (docs/temp.md, "Chat 意图路由重构"): a node funnel
Matcher -> Recall -> Judge -> Decision, escalating ONLY upward, with the Agent
as the byte-identical fallback consumer and the Shared Tool Runtime as the sole
execution point.

P0 scope, frozen: this package currently hosts the orchestration moved out of
``TurnOrchestrator`` (gate + QIR cascade + argument binding, behavior
byte-identical) and the node contracts every later phase implements against.
No new routing SEMANTICS live here yet.
"""
from . import contract
from .funnel import qir_live, route, run_intent_stage

__all__ = ["contract", "route", "qir_live", "run_intent_stage"]
