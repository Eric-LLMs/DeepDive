"""Intent Funnel — the independent, decoupled routing lane above the Agent.

Design (docs/temp.md, "Chat 意图路由重构"), chain ruling 2026-09-24: one model
hop per turn — Matcher HIT -> ToolIntentModel, Matcher MISS/AMBIGUOUS -> Recall ->
ToolIntentModel (ONE call: capability selection + argument extraction from Candidate
Cards) -> Binder (normalize/validate) -> Shared Tool Runtime. Every failure
exits to the Agent, byte-identical. ToolIntentModel is a swappable provider
(local primary, online fallback, stub) behind the tool_intent/ ladder.

Scope status: P0 moved the orchestration out of ``TurnOrchestrator`` (gate +
legacy QIR cascade + argument binding, behavior byte-identical); P1 added the
Registry/Matcher with its shadow hook; the funnel chain runs behind its OWN
gate (``chat_funnel_enabled``, default OFF — the legacy lane stays historical,
not the baseline).
"""
from . import contract

# LAZY re-exports (2026-09-24 structure rulings): ``funnel`` imports
# ``understanding`` which imports ``actions`` — eagerly importing funnel here
# would detonate a cycle whenever ``actions``' façade resolves its moved names
# (actions -> registry/binder -> this package -> funnel -> understanding ->
# actions, with understanding still half-built). Attribute access happens at
# CALL time, when every module in that chain is fully initialized.

def __getattr__(name: str):
    if name in ("funnel_live", "qir_live", "route", "run_intent_stage"):
        from . import funnel as _funnel
        return getattr(_funnel, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["contract", "funnel_live", "qir_live", "route", "run_intent_stage"]
