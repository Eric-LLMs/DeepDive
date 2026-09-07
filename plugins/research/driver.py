"""Compat facade + thin construction site for the Research Workflow (P4-1 final form).

**No control-plane logic lives in this file.** The Research flow is controlled by:

```text
Workflow Core   (workflow.runner.drive_iteration)  one iteration: claim -> execute
                                                   -> heartbeat/cancel -> retry
                                                   -> probe -> grade -> settle
Definition      (plugins/research/workflow_spec)   structure + fingerprint
Adapter         (plugins/research/workflow_adapter) every research-shaped port:
                                                   lease fold, agent executor, probe,
                                                   facts, prompt, vocabulary, settle
Worker          (apps/worker/tasks.py)             job delivery: one job == one iteration
Agent -> Skill/LLM -> Tool                         the reasoning inside one turn
```

This module only keeps the historical import path ``plugins.research.driver`` alive
for existing call sites and the frozen test surface. It adds exactly one thing: the
driver subclass binds ``backoff`` through THIS module's global :func:`_backoff_s`,
so the long-standing late-binding contract
(``monkeypatch.setattr(driver_module, "_backoff_s", ...)``) stays physically
effective after the implementation moved into the adapter. New code should import
from ``workflow_adapter`` / ``workflow_spec`` directly.
"""
from __future__ import annotations

from plugins.research.workflow_adapter import (  # noqa: F401 — re-exported compat surface
    DriverOutcome,
    IllegalRunTransition,
    ProjectLockError,
    ResearchRunDriver as _AdapterRunDriver,
    RevisionConflictError,
    RunState,
    RunTurnResult,
    TurnFacts,
    _SETTLE_ARTIFACT_ID,
    _backoff_s,
    auto_turn_prompt,
    build_settle_report,
    check_transition,
    grade_turn,
    is_transient_error,
    iso_now,
    observe_run_state,
)


class ResearchRunDriver(_AdapterRunDriver):
    """Thin construction site: injects the patchable module-global backoff.

    The adapter's default binds its OWN module namespace; tests and long-time
    operation bind this one. ``setdefault`` keeps any explicit ``backoff=`` kwarg
    authoritative.
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("backoff", lambda attempt: _backoff_s(attempt))
        super().__init__(*args, **kwargs)
