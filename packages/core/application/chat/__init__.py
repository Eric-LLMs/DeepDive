"""Chat control plane: TurnOrchestrator + Understanding/Policy contracts + Executors.

The router (``apps/api/routers/chat.py``) stays a thin HTTP/SSE adapter; everything
between "request resolved" and "SSE frames emitted" lives here:

    understanding.py   -> TurnRequirements   (what capabilities the user needs)
    execution_plan.py  -> ExecutionPlan      (pure policy mapping, no I/O)
    context.py         -> base context + lazy hydration (history/viewer/research)
    lifecycle.py       -> persistence / usage metering / finalize bookkeeping
    turn_orchestrator.py -> the control state machine + stream commit guard
    executors/         -> DIRECT / VIEWER / LOCAL_RAG / WEB / ACTION / COMPOSITE / AGENT

Capability systems (AgentKernel, RAG, Viewer, Memory, Research) are reused unchanged;
this package only changes the top-level scheduling.
"""
