"""Code-driven research pipeline: the orchestrator framework (sealed spec).

Per-node contract — one node, one honest attempt, then NEVER spin in place::

    Python input prep → LLM#1 structured decision → schema validate / local apply
    → LLM#2 repair (SAME input + violations only) → failure ledger + force advance

This module is the FRAMEWORK only: contracts, the decide()/repair-once model, the
fixed-schema failure ledger, per-node wall-clock floor (``asyncio.wait_for`` —
the worker image is Python 3.10, ``asyncio.timeout`` is FORBIDDEN), F2 fence
activation at node entry (:func:`node_entry_fence`), and the two stop classes:

* **Degradable faults** (stage budget spent, node timeout, handler error,
  per-source outages): recorded in the ledger and the stage transition is still
  forced — the chain advances honestly and the report surfaces the gaps.
* **StructuralStop** (a required deliverable is absent — no honest draft, no
  promotable report, no valid question): persisted to ``project["pipeline"]
  ["structural_stop"]``; the SAME grading pass terminates the run
  BLOCKED-once via ``CAUSE_STRUCTURAL`` (workflow/policy chain slot 4). The dead
  node is never re-run by a next iteration.

Every semantic LLM completion rides the pre-commit infrastructure: the run-level
:class:`RunBudget` hard fuse (>= cap → ``CostLimitExceeded`` power-cut BEFORE the
call) and the stage's DECLARED call budget (:class:`StageGate`). Handlers must
use ``ctx.decide``/``ctx.complete`` — no direct transport access.

The 10 business stage handlers land in batches on top of ``HANDLERS`` (each
registered with the stage it owns); the worker wiring swaps ONLY the
``run_turn`` closure — the workflow core (lease/fencing/settle) is untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from plugins.research.llm_budget import RunBudget, StageBudgetExceeded, StageGate
from plugins.research.plugin import (
    _gated_llm_complete,
    node_entry_fence,
    OwnershipLost,
)
from plugins.research.workflow_adapter import CostLimitExceeded

logger = logging.getLogger("research.pipeline")

# Pipeline decision-LLM seam (mirrors plugin._ADJ_LLM_CALL): tests inject a stub
# here; production leaves it None and completions ride _gated_llm_complete.
PIPELINE_LLM_CALL: Any | None = None

# ── stage DAG (single source stays in plugin; mirrored here for the framework) ──
_STAGE_ORDER = [
    "DISCOVER", "FRAME", "EVIDENCE", "DESIGN", "EXECUTE",
    "EXPLAIN", "WRITE", "REVIEW", "REPRODUCE", "PUBLISH",
]


@dataclass(frozen=True)
class StageContract:
    """A node's DECLARED, auditable envelope — constraint #1 (显式确权).

    ``llm_calls`` is the FULL stage budget: decision + repair pass + everything
    the server-side closures (adjudicate/review) may spend internally through the
    SAME gate. Deterministic work is free: pure Python never needs a call.
    """

    stage: str
    llm_calls: int
    thinking: bool = False
    node_budget_s: float = 60.0


CONTRACTS: dict[str, StageContract] = {
    "DISCOVER":  StageContract("DISCOVER", 2),                    # decide + repair
    "FRAME":     StageContract("FRAME", 2),
    "EVIDENCE":  StageContract("EVIDENCE", 6),                    # closures' internal passes
    "DESIGN":    StageContract("DESIGN", 2),
    "EXECUTE":   StageContract("EXECUTE", 2, node_budget_s=90.0), # two beats, full budget, no repair
    "EXPLAIN":   StageContract("EXPLAIN", 2),
    "WRITE":     StageContract("WRITE", 2, thinking=True, node_budget_s=120.0),
    "REVIEW":    StageContract("REVIEW", 2),                      # main + 1 repair, inside review_draft
    "REPRODUCE": StageContract("REPRODUCE", 0, node_budget_s=30.0), # integrity audit — code-only
    "PUBLISH":   StageContract("PUBLISH", 0, node_budget_s=30.0), # code-only
}

Handler = Callable[["NodeCtx"], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def register_handler(stage: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if stage in HANDLERS:
            raise ValueError(f"handler already registered for {stage}")
        HANDLERS[stage] = fn
        return fn
    return deco


# ── stop classes ──────────────────────────────────────────────────────────────

class StructuralStop(Exception):
    """A required deliverable is ABSENT — terminal on first occurrence, never retried."""

    def __init__(self, stage: str, missing: str, detail: str = "") -> None:
        super().__init__(f"{stage}: structural stop — missing {missing}: {detail}")
        self.stage = stage
        self.missing = missing
        self.detail = detail


class DegradedDecision(Exception):
    """Both decision attempts failed validation: degradable, ledger + advance."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("decision invalid: " + "; ".join(violations[:6]))
        self.violations = violations


# ── fixed-schema failure ledger (constraint #3) ───────────────────────────────
_ERROR_CLASSES = frozenset({
    "llm_budget_exceeded",   # stage declared call budget spent
    "degraded_decision",     # decide() both attempts invalid
    "node_timeout",          # wall-clock floor fired (asyncio.wait_for)
    "source_unavailable",    # per-source fetch degrade (never kills the node)
    "handler_error",         # unexpected handler fault
    "transition_refused",    # force-advance itself blocked
    "missing_handler",       # wiring gap for this stage
})


def make_ledger_entry(
    *, stage: str, attempt: int, error_class: str,
    detail: str, missing: str = "", impact: str,
) -> dict:
    if error_class not in _ERROR_CLASSES:
        raise ValueError(f"error_class must be one of {sorted(_ERROR_CLASSES)}")
    return {
        "stage": stage, "attempt": int(attempt), "error_class": error_class,
        "detail": detail[:500], "missing": missing, "impact": impact,
    }


def render_ledger(
    entries: list[dict], *, reviewed: bool | None = None, promoted: str | None = None,
) -> str:
    """Honest human-readable block appended to the node's turn value."""
    lines = ["", "## Pipeline failure ledger"]
    if not entries:
        lines.append("(no failures recorded)")
    for e in entries:
        lines.append(
            f"- [{e['stage']} a{e['attempt']}] {e['error_class']}: {e['detail']}"
            + (f" (missing: {e['missing']})" if e.get("missing") else "")
            + (f" — impact: {e['impact']}" if e.get("impact") else "")
        )
    if reviewed is False:
        lines.append("- REVIEW did not complete: the published edition is UNREVIEWED.")
    if promoted:
        lines.append(f"- Promoted artifact: {promoted}")
    return "\n".join(lines)


# ── JSON reply parsing (model replies are fenced, prose-wrapped, or trailing-commas) ──
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_reply(raw: str) -> dict:
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                obj = json.loads(text[start:i + 1])
                if not isinstance(obj, dict):
                    raise ValueError("top-level JSON is not an object")
                return obj
    raise ValueError("unbalanced JSON object braces")


# ── node context (the handler's whole world) ─────────────────────────────────

@dataclass
class NodeCtx:
    service: Any
    owner_id: Any
    project_id: str
    stage: str
    contract: StageContract
    run: RunBudget
    gate: StageGate
    ledger: list[dict] = field(default_factory=list)
    project: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)   # handler → orchestrator extras

    def record(self, *, attempt: int, error_class: str, detail: str,
               missing: str = "", impact: str) -> dict:
        entry = make_ledger_entry(
            stage=self.stage, attempt=attempt, error_class=error_class,
            detail=detail, missing=missing, impact=impact,
        )
        self.ledger.append(entry)
        return entry

    async def complete(self, prompt: str, *, system: str) -> str:
        """The handler's ONLY LLM path: every completion rides the stage gate."""
        if PIPELINE_LLM_CALL is not None:  # test seam
            self.gate.admit()
            try:
                raw = await PIPELINE_LLM_CALL(prompt, system)
            except BaseException:
                self.gate.settle(prompt, None)
                raise
            self.gate.settle(prompt, raw)
            return raw
        return await _gated_llm_complete(
            prompt, system, enable_thinking=self.contract.thinking,
            llm_gate=self.gate,
        )

    async def decide(
        self, user_prompt: str, *, system: str, validate: Callable[[dict], list[str]],
    ) -> dict:
        """Attempt 1 → schema validate → Attempt 2 REPAIR (same input + violations
        ONLY) → validate. Two failed attempts is a DEGRADABLE outcome, never a
        third LLM call."""
        prompt = user_prompt
        violations: list[str] = []
        for attempt in (1, 2):
            if attempt == 2:
                prompt = (
                    user_prompt
                    + "\n\nVIOLATIONS (your previous reply; fix ONLY these, same task):"
                    + "".join(f"\n- {v}" for v in violations[:10])
                    + "\nReply with ONLY the corrected JSON object."
                )
            raw = await self.complete(prompt, system=system)
            try:
                payload = parse_json_reply(raw)
                violations = list(validate(payload) or [])
            except (ValueError, json.JSONDecodeError) as exc:
                violations = [f"reply is not a parseable JSON object: {exc}"]
            if not violations:
                self.facts[f"decision_attempt_{self.stage}"] = attempt
                return payload
        raise DegradedDecision(violations)


# ── outcome + persistence ────────────────────────────────────────────────────

@dataclass
class NodeOutcome:
    kind: str                 # "advanced" | "blocked"
    stage: str
    next_stage: str | None
    turn_value: str
    cost_usd: float | None    # None = PRICING_UNKNOWN for part of this node
    ledger: list[dict]
    structural: dict | None = None

    def as_turn_value(self) -> str:
        return self.turn_value


def _persist(ctx: NodeCtx, *, structural: dict | None) -> None:
    """Commit the node's ledger (+optional structural verdict) atomically.

    Runs INSIDE the node_entry_fence context: a superseded worker is refused
    (OwnershipLost) before anything lands.
    """
    def mutate(project: dict) -> None:
        pipe = project.setdefault("pipeline", {})
        if ctx.ledger:
            merged = (pipe.get("failure_ledger") or []) + ctx.ledger
            pipe["failure_ledger"] = merged[-100:]
        pipe["last_node"] = {
            "stage": ctx.stage, "run_id": ctx.facts.get("run_id"),
            "turn_index": ctx.facts.get("turn_index"),
            "budget": ctx.run.snapshot(), "at": time.time(),
        }
        if structural is not None:
            pipe["structural_stop"] = structural
    ctx.service.atomic_update_project(ctx.owner_id, ctx.project_id, mutate)


def _next_of(stage: str) -> str | None:
    i = _STAGE_ORDER.index(stage)
    return _STAGE_ORDER[i + 1] if i + 1 < len(_STAGE_ORDER) else None


# ── the orchestrator: ONE node per call, honest advance or immediate BLOCKED ──

async def run_node(
    service: Any,
    owner_id: Any,
    project_id: str,
    *,
    run_id: str,
    execution_id: str | None,
    turn_index: int,
    max_cost_usd: float | None = None,
    start_spent_usd: float = 0.0,
    handlers: dict[str, Handler] | None = None,
    extras: dict[str, Any] | None = None,
) -> NodeOutcome:
    """Execute exactly one stage node, then advance it or stop the run.

    ``extras`` is merged into ``ctx.facts`` before the handler runs — the ONE
    sanctioned injection point for deployment surfaces (search/retrieval
    channels in tests and worker wiring); handlers never import fakes.

    Called from the worker's ``run_turn`` seam INSIDE ``ResearchRunDriver.
    auto_turn`` — the per-attempt fence composed by the driver is inherited here;
    standalone calls mint their own. A structural stop is persisted so the very
    same grading pass sees ``business_facts.structural_stop`` and terminalizes
    BLOCKED: no follow-up iteration ever re-runs the dead node.
    """
    with node_entry_fence(
        owner_id=owner_id, task_id=project_id, run_id=run_id,
        execution_id=execution_id,
    ):
        project = service.read_project(owner_id, project_id)
        stage = project.get("stage", "DISCOVER")
        existing = (project.get("pipeline") or {}).get("structural_stop")
        if existing:
            # Defensive re-entry guard (double safety beyond CAUSE_STRUCTURAL):
            # a successor must never redo a node the ledger already condemned.
            return NodeOutcome(
                "blocked", stage, None,
                f"PIPELINE {stage}: already structurally blocked "
                f"({existing.get('missing')}) — run must terminalize",
                None, [], existing,
            )

        contract = CONTRACTS[stage]
        run = RunBudget(
            cap_usd=max_cost_usd, start_spent_usd=start_spent_usd, run_id=run_id,
        )
        gate = StageGate(run, stage=stage, max_calls=contract.llm_calls)
        ctx = NodeCtx(
            service=service, owner_id=owner_id, project_id=project_id, stage=stage,
            contract=contract, run=run, gate=gate, project=project,
        )
        ctx.facts.update({"run_id": run_id, "turn_index": turn_index})
        if extras:
            ctx.facts.update(extras)  # deployment surfaces: channels etc. (see docstring)
        t0 = time.monotonic()

        handler = (handlers or HANDLERS).get(stage)
        if handler is None:
            ctx.record(
                attempt=1, error_class="missing_handler",
                detail=f"no handler registered for stage {stage}",
                missing="handler",
                impact="cannot run pipeline — deploy error, refusing silent pass",
            )
            structural = {"stage": stage, "missing": "handler", "detail": "wiring gap"}
            _persist(ctx, structural=structural)
            return NodeOutcome(
                "blocked", stage, _next_of(stage),
                f"PIPELINE {stage}: BLOCKED (missing handler)",
                None, ctx.ledger, structural,
            )

        fault: dict | None = None
        try:
            await asyncio.wait_for(handler(ctx), timeout=contract.node_budget_s)
        except (CostLimitExceeded, OwnershipLost):
            # Run-level fences never degrade to a ledger line: the hard cost fuse
            # powers the whole run down; OwnershipLost is a drop signal.
            _persist(ctx, structural=None)
            raise
        except StructuralStop as st:
            structural = {"stage": st.stage, "missing": st.missing, "detail": st.detail}
            _persist(ctx, structural=structural)
            logger.warning("pipeline.structural stage=%s missing=%s", st.stage, st.missing)
            return NodeOutcome(
                "blocked", stage, _next_of(stage),
                f"PIPELINE {stage}: STRUCTURAL STOP — {st}" + render_ledger(ctx.ledger),
                run.spent, ctx.ledger, structural,
            )
        except asyncio.TimeoutError:
            fault = ctx.record(
                attempt=1, error_class="node_timeout",
                detail=f"node exceeded {contract.node_budget_s:.0f}s floor",
                impact="stage outputs partial; advancing with honest gaps",
            )
        except StageBudgetExceeded as exc:
            fault = ctx.record(
                attempt=1, error_class="llm_budget_exceeded", detail=str(exc),
                impact="declared stage budget spent; further semantic work skipped",
            )
        except DegradedDecision as exc:
            fault = ctx.record(
                attempt=2, error_class="degraded_decision", detail=str(exc),
                impact="fallback content used for this stage",
            )
        except Exception as exc:  # noqa: BLE001 — degrade honestly, never stall the chain
            fault = ctx.record(
                attempt=1, error_class="handler_error",
                detail=f"{type(exc).__name__}: {exc}",
                impact="stage work lost; advancing with honest gaps",
            )

        # ── force advance: the chain NEVER parks mid-stage on a degradable fault ──
        nxt = _next_of(stage)
        if nxt is None:
            _persist(ctx, structural=None)
            return NodeOutcome(
                "advanced", stage, None,
                f"PIPELINE {stage}: terminal stage reached" + render_ledger(ctx.ledger),
                run.spent, ctx.ledger,
            )
        res = service.transition_stage(
            owner_id, project_id, target=nxt, expected_current_stage=stage,
        )
        verb = res.get("transition")
        if verb != "ADVANCED":
            ctx.record(
                attempt=1, error_class="transition_refused",
                detail=f"transition {stage}->{nxt} reported {verb!r}: "
                       f"{str(res.get('reason') or res.get('message') or '')[:200]}",
                missing="stage advance",
                impact="chain cannot continue honestly; terminalizing BLOCKED",
            )
            structural = {"stage": stage, "missing": "stage advance",
                          "detail": f"transition refused: {verb}"}
            _persist(ctx, structural=structural)
            return NodeOutcome(
                "blocked", stage, nxt,
                f"PIPELINE {stage}: BLOCKED (advance refused: {verb})"
                + render_ledger(ctx.ledger),
                run.spent, ctx.ledger, structural,
            )
        _persist(ctx, structural=None)
        wall = time.monotonic() - t0
        snap = run.snapshot()
        line = (
            f"PIPELINE {stage} -> {nxt}: ADVANCED "
            f"(llm_calls={snap['calls']} est_in={snap['tokens_in_est']} "
            f"est_out={snap['tokens_out_est']} cost=${snap['cost_usd']:.4f} "
            f"wall={wall:.1f}s ledger={len(ctx.ledger)})"
        )
        logger.info("pipeline.node %s", line)
        return NodeOutcome(
            "advanced", stage, nxt, line + render_ledger(ctx.ledger),
            run.spent, ctx.ledger,
        )
