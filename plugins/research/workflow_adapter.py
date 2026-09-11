"""The Research Workflow adapter: every research port the generic core drives.

Research is ONE concrete workflow on the generic core (:mod:`packages.workflow`).
This module is that workflow's adapter layer — it is NOT the controller: the core
runner (:func:`workflow.runner.drive_iteration`) orchestrates each iteration, the
worker (``apps/worker/tasks.py: research_drive``) delivers one job per iteration,
the agent moves stages through its tools, and :mod:`plugins.research.workflow_spec`
declares the flow's structure. What lives here is everything that answers a generic
port question in research terms:

- **Ports:** :class:`ResearchLeaseStore` folds the disk vocabulary
  (``active_run`` slot + ``project["driver"]`` checkpoint) into a core
  :class:`workflow.leases.LeaseLedger` behind the service CAS; the progress probe
  diffs the stage/gate milestone; the executor wraps one agent-kernel turn as the
  opaque black box the core executes; prompt and business-facts closures compose the
  research brief and the fresh authoritative facts.
- **Single-writer discipline.** The lease contest decides: stale/duplicate/
  out-of-order jobs are *dropped*, never run; a stale ``running`` heartbeat is a
  crash recovered at attempt+1; a fresh one is a live twin.
- **Vocabulary translation only.** Legality of run transitions is owned
  exclusively by Workflow Core (:mod:`workflow.states`); ``RunState`` is the
  on-disk / UI vocabulary the gate speaks, and :func:`grade_turn` re-expresses a
  core :class:`~workflow.policy.LoopPolicy` verdict in the exact human-visible
  words on disk. Zero legality rules live here.
- **Consequences, not decisions.** After ``drive_iteration`` returns a graded stop,
  the research-side follow-up runs here: deterministic auto-settle for progressive
  runs (:meth:`ResearchRunDriver._try_settle` — walk the legal chain to PUBLISH,
  record un-passed gates as diagnostics, write a model-free report), ``last_block``
  persistence and slot release.
- **Definition drift:** every turn re-checks the fingerprint ``begin_run`` minted
  against :mod:`plugins.research.workflow_spec`; a deploy that rewrites the flow
  terminalizes live runs honestly instead of resuming them under a new definition.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from plugins.research.plugin import (
    OwnershipLost,
    ProjectLockError,
    RevisionConflictError,
    _now_iso,
    clear_auto_run_fence,
    set_auto_run_fence,
)
from plugins.research.workflow_spec import (
    AUTO_TURN_TASK,
    RESEARCH_EXECUTOR_ID,
    RESEARCH_WORKFLOW,
)
from workflow.leases import LeaseConfig, LeaseLedger, acquire
from workflow.ports import NoopPublisher, TaskResult
from workflow.policy import (
    CAUSE_CANCEL,
    CAUSE_FINISHED,
    CAUSE_NO_PROGRESS,
    CAUSE_PENDING_SIGNAL,
    CAUSE_SPEND_CAP,
    CAUSE_STRUCTURAL,
    CAUSE_TURN_CAP,
    IterationFacts,
    LoopCaps,
    LoopPolicy,
)
from workflow.retry import RetryPolicy, default_backoff
from workflow.runner import (
    ExecutionFailed,
    IterationRequest,
    RunCounters,
    drive_iteration,
)
from workflow.runtime import MappingRegistry, build_deps
from workflow.states import (
    IllegalTransition,
    WorkflowState,
    observe_state,
    validate_transition,
)

# Heartbeat / lease knobs (seconds) — single source of truth is the core LeaseConfig
# below; the module-level constants stay exported for external compatibility.
# The driver refreshes ``driver.updated_at`` every ``LEASE_REFRESH_S`` while a turn is
# executing; a claim treats a ``running`` ledger whose heartbeat is older than
# ``LEASE_STALE_S`` as a crashed predecessor (attempt + 1) and a fresher one as a live
# twin (drop).
_LEASE = LeaseConfig(refresh_s=20.0, stale_s=150.0)
LEASE_REFRESH_S = _LEASE.refresh_s
LEASE_STALE_S = _LEASE.stale_s


# ── RunState: the run's legal-state customs gate ─────────────────────────────
class RunState(str, enum.Enum):
    """On-disk / UI run vocabulary (compat since P4-1; legality lives in Core).

    Terminal states remain irreversible — but the *rule* is owned by
    :mod:`workflow.states`; this enum and :data:`RUN_TO_CORE` only name the outcomes.

    The state is *derived* from the persisted slot each time a job arrives: ``RUNNING`` iff
    ``active_run`` is present with the job's ``run_id``; ``IDLE`` otherwise. Terminal
    outcomes are written to ``project.json["last_block"]`` *and* the slot is released, so a
    later job observes ``IDLE`` and must drop — it can never resume a finished run.
    """

    IDLE = "idle"                # no live slot (only begin_run may leave this → RUNNING)
    RUNNING = "running"          # a run owns the slot and is making progress
    FINISHED = "finished"        # terminal: reached PUBLISH
    BLOCKED = "blocked"          # terminal: needs a human (gate override / turn cap / cost cap)
    STALLED = "stalled"          # terminal: consecutive no-progress turns
    CANCELLED = "cancelled"      # terminal: user requested stop
    ERROR = "error"              # terminal: unexpected / unrecoverable failure

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunState.FINISHED,
            RunState.BLOCKED,
            RunState.STALLED,
            RunState.CANCELLED,
            RunState.ERROR,
        }


# ── Lifecycle vocabulary: RunState demoted to pure compat vocabulary ─────────
# Since P4-1 Step 6A the ONLY authority over transition legality is Workflow Core
# (:mod:`workflow.states`). ``RunState`` survives as on-disk / UI vocabulary only
# (checkpoints, ``last_block.kind``, driver outcomes, tests). The tables below are
# one-way vocabulary translators and contain ZERO legality rules of their own.
#
# RUN_TO_CORE is deliberately not a bijection: STALLED and ERROR collapse onto
# WorkflowState.FAILED, BLOCKED onto WAITING. CORE_TO_RUN is therefore a FALLBACK
# default for the reverse direction only — the authoritative disk/UI vocabulary of
# a graded stop is resolved by ``(WorkflowState, Grade.cause)`` via
# :data:`_GRADE_CAUSE_TO_RUN`, which is what keeps the distinct ``stalled`` /
# ``blocked`` on-disk meanings intact.

RUN_TO_CORE: dict[RunState, WorkflowState] = {
    RunState.IDLE: WorkflowState.IDLE,
    RunState.RUNNING: WorkflowState.RUNNING,
    RunState.FINISHED: WorkflowState.SUCCEEDED,
    RunState.BLOCKED: WorkflowState.WAITING,
    RunState.STALLED: WorkflowState.FAILED,
    RunState.CANCELLED: WorkflowState.CANCELLED,
    RunState.ERROR: WorkflowState.FAILED,
}

CORE_TO_RUN: dict[WorkflowState, RunState] = {  # fallback default only
    WorkflowState.IDLE: RunState.IDLE,
    WorkflowState.RUNNING: RunState.RUNNING,
    WorkflowState.WAITING: RunState.BLOCKED,
    WorkflowState.SUCCEEDED: RunState.FINISHED,
    WorkflowState.FAILED: RunState.ERROR,
    WorkflowState.CANCELLED: RunState.CANCELLED,
}


class IllegalRunTransition(IllegalTransition):
    """Compat wrapper of the core refusal (terminal escape / non-IDLE start).

    Legal-verbs are decided exclusively by core :func:`validate_transition`; this
    exists so existing import sites and ``except`` clauses keep working verbatim.
    """


def observe_run_state(project: dict, run_id: str) -> RunState:
    """Derive the run state a job should see from the persisted slot. Pure read.

    Delegates to core :func:`workflow.states.observe_state`; observation yields only
    RUNNING/IDLE here, which map one-to-one back, so terminal outcomes (slot already
    released by ``end_run``) still show up as ``IDLE`` and drop late jobs.
    """
    return CORE_TO_RUN[observe_state(project.get("active_run"), run_id)]


def check_transition(current: RunState, target: RunState) -> None:
    """Customs check as a delegating shell — no private rules, no side effects.

    Translates both vocabularies, asks the core, and restates a refusal in research
    words. Nothing is read from or written to disk beyond the arguments.
    """
    try:
        validate_transition(RUN_TO_CORE[current], RUN_TO_CORE[target])
    except IllegalTransition as exc:
        raise IllegalRunTransition(
            f"illegal run transition: {current.value} -> {target.value}"
        ) from exc


# ── no-progress grading (pure, unit-testable) ────────────────────────────────
@dataclass
class TurnFacts:
    """Everything the priority chain needs to grade one completed auto-turn."""

    stage: str                       # the task's stage after the turn
    pending_overrides: int = 0       # approvals.json rows still awaiting a human
    cancel_requested: bool = False
    progress: bool = False           # fingerprint changed during this turn
    consecutive_no_progress: int = 0  # ledger value *before* this turn
    turn_index: int = 1              # this auto turn (1-based; interactive turn 0 is free)
    cumulative_cost_usd: float = 0.0  # including this turn's cost
    max_turns: int | None = None
    max_no_progress: int | None = None
    max_cost_usd: float | None = None
    structural_stop: bool = False   # adapter-declared inability to complete honestly


@dataclass
class Grade:
    """Result of the priority chain: stop (terminal state + reason) or continue."""

    state: RunState | None           # terminal state to stop in, or None → continue
    reason: str | None = None
    consecutive_no_progress: int = 0


# The disk/UI vocabulary of a graded stop resolves through the STABLE core cause, never
# through the collapsed WorkflowState alone: the core folds the stall brake and the cap
# parkings into FAILED/WAITING — only ``cause`` distinguishes STALLED from BLOCKED.
_GRADE_CAUSE_TO_RUN: dict[str, RunState] = {
    CAUSE_CANCEL: RunState.CANCELLED,
    CAUSE_FINISHED: RunState.FINISHED,
    CAUSE_PENDING_SIGNAL: RunState.BLOCKED,
    CAUSE_NO_PROGRESS: RunState.STALLED,
    CAUSE_TURN_CAP: RunState.BLOCKED,
    CAUSE_SPEND_CAP: RunState.BLOCKED,
    # Structural absence (no honest draft / no promotable artifact) is a FIRST-AND-
    # ONLY terminal stop parked on the human who must supply the missing input —
    # never an in-place retry of the dead node (sealed-spec ruling #1).
    CAUSE_STRUCTURAL: RunState.BLOCKED,
}


def _research_stop_text(cause: str, facts: TurnFacts, consecutive: int) -> str:
    """Restate a core stop in the historical human-visible wording (assertion-exact)."""
    if cause == CAUSE_CANCEL:
        return "stop requested by the user"
    if cause == CAUSE_FINISHED:
        return "research task reached PUBLISH"
    if cause == CAUSE_PENDING_SIGNAL:
        return f"{facts.pending_overrides} gate override(s) awaiting a human decision"
    if cause == CAUSE_NO_PROGRESS:
        return (
            f"no stage or gate advance across {consecutive} consecutive auto turns "
            f"(stuck at {facts.stage})"
        )
    if cause == CAUSE_TURN_CAP:
        return f"reached the auto-run turn cap ({facts.max_turns} turns)"
    if cause == CAUSE_STRUCTURAL:
        return (
            "blocked on a structural stop — a required deliverable is missing at "
            f"{facts.stage} (see the pipeline failure ledger; the run ended on the "
            "first occurrence, the dead node was not re-run)"
        )
    return f"reached the auto-run cost cap (${facts.cumulative_cost_usd:.4f})"


def grade_turn(facts: TurnFacts) -> Grade:
    """The fixed stop/continue priority chain (see module docstring) — core-graded.

    The machine (chain order cancel → finished → pending signal → no-progress →
    turn cap → spend cap → continue, plus counter semantics) now lives in
    :class:`workflow.policy.LoopPolicy`; this shell only (a) translates
    :class:`TurnFacts` into :class:`IterationFacts`, preserving the historical
    disable semantics (a falsy ``max_turns``/``max_no_progress`` means *no cap* —
    hence ``or None`` where the core checks ``is not None``), and (b) resolves the
    verdict to ``(RunState, reason)`` through the stable ``Grade.cause``, restating
    the exact human-visible texts. Decisions and ordering are identical to the
    pre-6A local chain.

    ``cap_outcome`` / ``signal_outcome`` are caller-injected generic policy
    configuration: the CORE default keeps a cap firing at FAILED (a policy-detected
    inability to continue). The research adapter declares WAITING here only because
    it explicitly judges a cap crossing in an auto-run to be parked on a human /
    approval condition. The disk vocabulary is cause-driven, so this declaration
    does not alter the BLOCKED mapping — it states the definition's intent.
    """
    policy = LoopPolicy(
        caps=LoopCaps(
            max_turns=facts.max_turns or None,
            max_no_progress=facts.max_no_progress or None,
            max_spend=facts.max_cost_usd,
        ),
        cap_outcome=WorkflowState.WAITING,
        signal_outcome=WorkflowState.WAITING,
    )
    grade = policy.grade(IterationFacts(
        finished=facts.stage == "PUBLISH",
        pending_signals=facts.pending_overrides,
        cancel_requested=facts.cancel_requested,
        progress=facts.progress,
        consecutive_no_progress=facts.consecutive_no_progress,
        index=facts.turn_index,
        total_spend=facts.cumulative_cost_usd,
        structural_stop=facts.structural_stop,
    ))
    if grade.state is None:
        return Grade(None, None, grade.consecutive_no_progress)
    return Grade(
        _GRADE_CAUSE_TO_RUN[grade.cause],
        _research_stop_text(grade.cause, facts, grade.consecutive_no_progress),
        grade.consecutive_no_progress,
    )


# ── transient classification ─────────────────────────────────────────────────
class CostLimitExceeded(RuntimeError):
    """PRE-CALL hard gate fired: the run's cumulative spend already reached
    ``max_cost_usd``, so NO further LLM call may be made. Raised by the turn
    executor before invoking ``run_turn`` (turn-start gate) and by the worker
    before composing the kernel turn — never a passive billing stat. Explicitly
    graded NON-transient in :func:`is_transient_error` (the "$5xx" text hint would
    otherwise match digits inside the dollar amounts of this message).
    """


_TRANSIENT_HINTS = (
    "timed out", "timeout", "timedout", "connection", "refused", "reset", "broken pipe",
    "server error", "internal server error", "unavailable", "rate limit", "too many requests",
    "429", "5", "overloaded", "temporarily", "temporary failure",
)


def is_transient_error(exc: BaseException) -> bool:
    """Best-effort grade of ``exc``: transient (retryable) or not.

    A Transient error is one the run can retry with backoff: an LLM network hiccup, an HTTP
    429/5xx, a brief Redis dropout, or a project-lock timeout (:class:`ProjectLockError`). A
    CAS conflict is *not* a reason to retry this execution — it means another job already won
    the slot, so the loser must drop. Everything else is treated as unexpected and terminal.
    """
    if isinstance(exc, ProjectLockError):
        return True
    if isinstance(exc, RevisionConflictError):
        return False
    if isinstance(exc, CostLimitExceeded):
        return False  # a spent budget never un-spends; retrying only wastes steps
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(h in text for h in _TRANSIENT_HINTS)


# ── outcome payload ──────────────────────────────────────────────────────────
@dataclass
class RunTurnResult:
    """What one successful ``run_turn`` returned."""

    final_answer: str
    # ``float | None``: ``None`` = PRICING_UNKNOWN (tokens spent, no price resolved).
    # Strictly kept distinct from ``0.0`` (genuinely nothing to bill).
    cost_usd: float | None = 0.0


@dataclass
class DriverOutcome:
    """What one drive execution decided, for the worker to act on.

    ``action`` is ``continue`` (schedule the next turn) or a terminal verb; ``dropped`` means
    the job was a stale/duplicate and must do nothing. ``final_answer`` carries the model's
    answer when a turn actually ran (the worker mirrors it into the task's session mirror).
    """

    state: RunState
    action: str                      # continue | finished | blocked | stalled | cancelled | error | dropped
    dropped: bool = False
    reason: str | None = None
    run_id: str | None = None
    turn_index: int = 0
    turn_attempt: int = 1
    execution_id: str | None = None
    progress: bool = False
    cumulative_cost_usd: float = 0.0
    final_answer: str | None = None
    next_turn_index: int | None = None   # set only when action == "continue"
    consecutive_no_progress: int = 0

    @property
    def publish_kind(self) -> str:
        return f"run.{self.action}"


# ── deterministic auto-settle (progressive runs only) ────────────────────────
# A progressive run that is about to stop short of PUBLISH for a NON-human reason (a
# no-progress stall or a turn/cost cap) is finished deterministically instead of being left
# mid-way: the driver walks the legal stage chain to PUBLISH — each un-passed guarding gate
# records its failed checks as a diagnostic and the move is granted, exactly the progressive
# semantics the agent itself uses — and writes an auto-settled report that aggregates the
# produced artifacts, the graph, and every recorded diagnostic as the report's "Known gaps /
# unverified items". This is what makes "ran out of usable material" tasks still end at
# PUBLISH with an honest per-stage failure record. Strict runs never auto-settle: an un-passed
# gate there is a real stop that needs the human override path.
_SETTLE_ARTIFACT_ID = "settle_report.md"
_MAX_SETTLE_HOPS = 12  # the chain is 9 hops; generous headroom guards against a corrupt project


def build_settle_report(
    *,
    task_name: str,
    mode: str,
    created_at: str,
    reached_stage: str,
    reason: str,
    node_counts: list[tuple[str, int]],
    edge_count: int,
    artifacts: list[dict],
    diagnostics: list[dict],
) -> str:
    """Deterministic, model-free report for a run the driver auto-settled to PUBLISH.

    Pure text templating — no LLM, no fabricated findings. It records what the run actually
    established (the produced artifacts and the graph) and closes with the recorded gate
    diagnostics as the "Known gaps / unverified items" list the report contract requires.
    """
    lines = [
        f"# {task_name or '(untitled research task)'} — research report",
        "",
        f"> Auto-settled by the Research OS driver · mode: {mode} · created: {created_at}",
        f"> Reason: {reason}",
        "",
        (
            "This run could not advance to PUBLISH on its own, so the driver finished it "
            "deterministically: it walked the remaining stages, recording each un-passed gate's "
            "failed checks into the project diagnostics, and wrote this report from what the run "
            "actually produced. Nothing below is filled in or fabricated."
        ),
        "",
        "## What this run established",
        "",
    ]
    if reached_stage and reached_stage != "PUBLISH":
        lines.append(
            f"- Reached **{reached_stage}** under its own power before the auto-settle."
        )
    total_nodes = sum(n for _, n in node_counts)
    if node_counts:
        breakdown = " · ".join(f"{t}: {n}" for t, n in node_counts)
        lines.append(
            f"- Graph: **{total_nodes}** node(s), {edge_count} edge(s) — {breakdown}."
        )
    else:
        lines.append(f"- Graph: no nodes recorded ({edge_count} edge(s)).")
    lines.append("- Artifacts:")
    if artifacts:
        lines.extend(
            f"  - `{a['artifact_id']}` v{a.get('version', 1)} ({a.get('status', '?')})"
            for a in artifacts
        )
    else:
        lines.append("  - none")
    lines += ["", "## Known gaps / unverified items", ""]
    if not diagnostics:
        lines.append(
            "No gate diagnostics were recorded — the run simply ran out of runway before "
            "PUBLISH."
        )
    for d in diagnostics:
        gate = d.get("gate", "?")
        lines.append(
            f"- **{gate}** — did not pass when leaving {d.get('stage', '?')} → "
            f"{d.get('target', '?')}:"
        )
        failed = [c for c in d.get("failed_checks") or [] if not c.get("ok")]
        if failed:
            for check in failed:
                detail = check.get("detail")
                if detail:
                    lines.append(f"  - {check.get('name') or 'check'}: {detail}")
                else:
                    lines.append(f"  - {check.get('name') or 'check'}")
        else:
            lines.append("  - (gate checks failed — no per-check detail recorded)")
        lines.append("    *unverified*")
    lines.append("")
    lines.append("_End of auto-settled report._")
    return "\n".join(lines)


# ── the auto-turn prompt ─────────────────────────────────────────────────────
def auto_turn_prompt(
    *,
    task_name: str,
    project_id: str,
    stage: str,
    turn_index: int,
    consecutive_no_progress: int = 0,
    has_materials: bool = False,
) -> str:
    """The driver directive handed to the model for one autonomous continuation turn.

    The interactive first turn (turn 0) already resumed the task and did some work; each auto
    turn re-arms the same resume contract but tells the model to keep going without asking the
    user, and to be honest about stopping (it may request a gate override only when a human
    decision is genuinely required).

    ``has_materials`` gates the EVIDENCE materials hint: it is injected ONLY when the task
    actually carries materials — an empty materials list must never bait a wasted
    ``fetch_materials`` round-trip.
    """
    push = (
        "\nNOTE: The previous auto turn made no visible progress on the task files — the stage "
        f"did not advance (still {stage}) and no gate newly passed. Do not just talk — act: "
        "inspect the current state (research_project action snapshot) and cross a real "
        "milestone this turn. If repeated retrieval attempts keep yielding no usable material "
        "for the current stage, STOP gathering and call research_state get_handoff, then "
        "transition_stage with the exact next_stage it returns: progressive mode records any "
        "un-passed gate checks into the project diagnostics and grants the move. Never loop "
        "inside a single stage."
        if consecutive_no_progress > 0
        else ""
    )
    materials_line = (
        "In EVIDENCE, if the task carries materials, FIRST call research_scrape action "
        "\"fetch_materials\" — task materials land in the run's provenance ledger under "
        "material:// urls and are first-class sources with the same standing as web pages: "
        "pass those urls to adjudicate alongside web urls, and read them back with "
        "research_scrape read before citing. source_type is identity only — it never "
        "changes how evidence is verified. "
        if has_materials
        else ""
    )
    return (
        f"[Research auto-run, turn {turn_index} of the same run]\n"
        f"Continue driving the existing Research OS task `{task_name}` (project_id "
        f"{project_id}), currently at stage {stage}, forward toward PUBLISH — fully "
        "autonomously. Do NOT create a new project and do NOT ask the user anything. The "
        "authoritative stage-by-stage procedure is the deep_research skill — load it now via "
        "the `skill` tool if it is not already in your context, and follow it.\n"
        "The task advances along ONE legal chain (DISCOVER then FRAME then EVIDENCE then DESIGN "
        "then EXECUTE then EXPLAIN then WRITE then REVIEW then REPRODUCE then PUBLISH) and each "
        "stage has exactly one legal next stage. To move on, call research_state action "
        "get_handoff to read that exact next_stage (and gate_required) for THIS project, then "
        "call research_state transition_stage passing ONLY that next_stage as target. "
        "Transitioning to any other target is refused as an illegal transition and wastes the "
        "turn — never guess the stage name, never skip a stage. When transition_stage reports "
        "transition=\"ADVANCED\", the stage change is committed and this turn ends "
        "automatically: the next turn opens at the new stage, so do NOT re-do or pad the old "
        "stage's work after a successful transition.\n"
        "Drive the stage work with the research tools (research_project snapshot, "
        "research_state incl. get_state/get_handoff/transition_stage, research_evidence "
        "record_node/adjudicate, research_gate check, research_artifact, research_run, "
        "research_scrape, rag_search, web_search, search_social), reading the task state each "
        "turn. " + materials_line + "In EVIDENCE run the wholesale ATOMIC pipeline — pending -> ONE "
        "research_evidence \"adjudicate\" call per chunk — never per-claim "
        "retail: (1) call research_state get_state first: its claims digest marks each "
        "claim pending (no valid committed verdict, or evidence fingerprint moved since "
        "the last commit) and groups the pending ones by chunk_hint. Only pending "
        "claims get work; with ZERO pending claims make NO verify-family call at all. "
        "Reuse a listed claim's exact id and record_node ONLY claims not yet listed — "
        "never mint a shadow id; match by id, never by label. "
        "(2) Per chunk: gather its claims' candidate source URLs and close the WHOLE "
        "loop in ONE research_evidence action \"adjudicate\" call "
        "(urls=[every candidate URL, any count], claim_ids=[the chunk's pending ids]): "
        "server-side it fetches every page, extracts one deterministic representative "
        "chunk each, judges relevance+verdict in a single batched LLM pass (splitting "
        "internally only on token-budget overflow) and commits ALL verdicts in ONE "
        "atomic verify — you never call research_scrape fetch for adjudication, never "
        "hand-adjudicate snippets and never hand-build a verify_batch in EVIDENCE. "
        "(3) One compact per-claim verdict summary comes back (supports/contradicts/"
        "insufficient + the URLs used per claim); insufficient / unrelated pairs add "
        "no ticket edge. If a claim got NO verdict, gather better source URLs and "
        "adjudicate again with just that claim_id — at most once more: after two dry "
        "rounds the server locks the claim as evidence_exhausted, a KNOWN GAP to "
        "report in the draft (never a refutation), not pending work. "
        "Citations and strength belong on the claim BEFORE you adjudicate (set them "
        "at record_node); NEVER patch the graph after a commit. Single-claim \"verify\" "
        "and raw \"verify_batch\" are manual-path only — do not use them in EVIDENCE. "
        "A 403/empty/interstitial source "
        "is terminal for that source in this "
        "run — note the gap and move on, never retry-loop it or stall the task (a "
        "search_social terminal_for_run item means: stop querying that platform this "
        "run). Adjudication "
        "only turns sources into edges for URLs its own fetch ledger "
        "confirmed fetched-ok and usable — never fabricate a source; do NOT "
        "hand-record Source/Evidence nodes or hand-link claim edges. In "
        "WRITE, before you quote or cite a fetched source, read its full draft back with "
        "research_scrape action \"read\" (canonical_url from fetch) so the citation is grounded "
        "in the whole page, not just the snippet. In REVIEW, close the WHOLE stage in ONE "
        "research_artifact action \"review_draft\" call (artifact_id = the "
        "primary_report_artifact_id reported by research_state get_state — REVIEW and "
        "PUBLISH force-bind to that id, any other guess is rebound server-side): "
        "server-side it feeds the full draft + claim graph to one LLM pass, receives only "
        "{\"changes\": [...]} correction rows, pre-checks every anchor (unique-match iron "
        "rule), applies them in staging and atomically commits the corrected version — you "
        "never re-read the draft page by page and never hand-write a review create_version "
        "unless review_draft reports rejected rows (then fix ONLY the listed passages). "
        "Follow each tool's own schema — using the "
        "wrong field names errors and wastes steps.\n"
        "Anti-stall rule: when ~2-3 retrieval attempts for the current stage come back empty "
        "or keep failing, do NOT keep retrying in place — call research_state get_handoff and "
        "then transition_stage with that exact next_stage. In progressive mode an un-passed "
        "guarding gate records its failed checks into the project diagnostics and the "
        "transition is granted, so advance and keep going. Never loop inside a single stage and "
        "never fabricate evidence to force a pass.\n"
        "Check gates when a transition needs one (research_gate action check); if a gate "
        "fails, first use explain_failure and actually fix the underlying work; request a "
        f"human override only if a real human decision is required.{push}\n"
        "Keep going to PUBLISH: write the report artifact (the first report-named write "
        "becomes the primary_report_artifact_id) and promote THAT id — promote_to_drive "
        "binds to the primary's latest version, and a run whose primary report stays "
        "un-promoted cannot settle as finished. When you write it, read the project "
        "diagnostics (research_project snapshot) and "
        "close the report with a 'Known gaps / unverified items' list — one entry per recorded "
        "diagnostic (gate + stage + the failed checks), each labeled unverified, so the user "
        "sees exactly which stage could not be sourced and why.\n"
        "When the task reaches PUBLISH, or there is genuinely nothing more you can do "
        "unassisted, stop and report a concise summary of what you did this turn and the "
        "current state / next step. Keep the summary under ~200 words."
    )


# ── time helpers (fixed-width UTC, lexicographically comparable) ─────────────
def iso_now() -> str:
    return _now_iso()


def _is_stale(updated_at_iso: str | None, window_s: float = LEASE_STALE_S) -> bool:
    """Crash judgment by heartbeat age — the core ``LeaseConfig.is_stale`` rule (strict >)."""
    lease = _LEASE if window_s == _LEASE.stale_s else LeaseConfig(stale_s=window_s)
    return lease.is_stale(updated_at_iso, now_epoch=datetime.now(UTC).timestamp())


def _backoff_s(attempt: int) -> float:
    """Exponential backoff for a transient retry (2s, 4s, … capped at 30s).

    Curve source: core :func:`workflow.retry.default_backoff` (identical formula).
    Stays a module global so tests can late-bind it (``monkeypatch.setattr``).
    """
    return default_backoff(attempt)


# ── 6C: the adapter components Workflow Core drives the iteration with ───────
# Everything below translates the research disk vocabulary (``project["driver"]``
# checkpoint keys, ``active_run`` slot, stage/gate milestone) into the generic ports
# of :mod:`workflow.ports`. The core never sees a research word.

# Core drop-reason wording → the exact strings this driver has always put on jobs.
_CORE_DROP_REASONS = {
    "lease ledger belongs to another run": "driver ledger belongs to another run",
    "lease is running a different iteration": "ledger is running a different turn",
    "live duplicate (iteration already leased)": "live duplicate (turn already claimed)",
    "out-of-order job (iteration gap)": "out-of-order job (turn gap)",
}

# Synthetic slot-state markers used by ResearchLeaseStore (never persisted).
_STATE_NO_ACTIVE = "__no_active_run__"
_STATE_FOREIGN = "__foreign__"


def _now_epoch() -> float:
    return datetime.now(UTC).timestamp()


class ResearchLeaseStore:
    """:class:`workflow.ports.LeaseStore` over ``atomic_update_project`` CAS commits.

    ``atomic(mutate)`` runs the whole contest under the project file lock: read the
    project, fold it into a :class:`LeaseLedger`, let the core mutate the lease, and
    write the lease fields back into ``project["driver"]`` — all extra checkpoint keys
    (cumulative cost, pricing-unknown turns, run_version, cloud assets...) are kept
    verbatim; the core only ever touches the seven lease fields.

    Two research facts the lease ledger cannot express (``active_run`` presence and
    ownership) are folded *in*: an absent/foreign slot maps to a synthetic ledger whose
    ``run_id`` forces a core drop, and the precise research-vocabulary reason is kept
    in :attr:`drop_note` for the translation layer.
    """

    def __init__(self, service, owner_id: UUID, task_id: str, *, run_id: str) -> None:
        self._service = service
        self._owner = owner_id
        self._task = task_id
        self._run_id = run_id
        self.drop_note: str | None = None  # research-worded reason for a folded drop

    def _fold(self, project: dict) -> LeaseLedger:
        """project.json → LeaseLedger (may synthesize a foreign/absent marker)."""
        active = project.get("active_run")
        if not active or active.get("status") != "RUNNING":
            self.drop_note = "no active run for this task"
            return LeaseLedger(run_id=self._run_id, state=_STATE_NO_ACTIVE)
        if active.get("run_id") != self._run_id:
            self.drop_note = "stale job: active_run belongs to another run"
            return LeaseLedger(run_id=str(active.get("run_id")), state=_STATE_FOREIGN)
        led = project.get("driver")
        if not isinstance(led, dict):
            self.drop_note = "driver ledger belongs to another run"
            return LeaseLedger(run_id="__missing__", state=_STATE_FOREIGN)
        if led.get("run_id") not in (None, self._run_id):
            self.drop_note = "driver ledger belongs to another run"
            return LeaseLedger(run_id=str(led.get("run_id")), state=_STATE_FOREIGN)
        return LeaseLedger(
            run_id=led.get("run_id"),
            index=int(led.get("turn_index") or 0),
            attempt=int(led.get("turn_attempt") or 1),
            state=led.get("turn_state") or "done",
            execution_id=led.get("execution_id"),
            updated_at=led.get("updated_at"),
            cancel_requested=bool(led.get("cancel_requested")),
        )

    @staticmethod
    def _unfold(project: dict, ledger: LeaseLedger) -> None:
        """LeaseLedger → the seven lease keys of ``project["driver"]`` (extras intact)."""
        led = project.get("driver")
        if not isinstance(led, dict):
            led = {}
            project["driver"] = led
        led.update(
            run_id=ledger.run_id,
            turn_index=ledger.index,
            turn_attempt=ledger.attempt,
            turn_state=ledger.state,
            execution_id=ledger.execution_id,
            updated_at=ledger.updated_at,
            cancel_requested=ledger.cancel_requested,
        )

    def atomic(self, mutate):
        def outer(project: dict) -> None:
            before = self._fold(project)
            after = mutate(before)
            if after is before or before.state in (_STATE_NO_ACTIVE, _STATE_FOREIGN):
                return  # core dropped without touching the lease — persist nothing
            self._unfold(project, after)

        return self._service.atomic_update_project(self._owner, self._task, outer)

    def read(self) -> LeaseLedger:
        """Checkpoint-only view (no ``active_run`` fold): used for the post-turn cancel
        check and the heartbeat watcher's ownership test."""
        led = self._service.get_driver_checkpoint(self._owner, self._task)
        return LeaseLedger(
            run_id=led.get("run_id"),
            index=int(led.get("turn_index") or 0),
            attempt=int(led.get("turn_attempt") or 1),
            state=led.get("turn_state") or "done",
            execution_id=led.get("execution_id"),
            updated_at=led.get("updated_at"),
            cancel_requested=bool(led.get("cancel_requested")),
        )


class _RunTurnExecutor:
    """:class:`workflow.ports.Executor`: the agent turn is a black box to the core.

    One opaque prompt in, one opaque value + spend out. A ``None`` spend stays
    ``None`` (PRICING_UNKNOWN) — the core meters it as UNKNOWN, never as $0.
    """

    def __init__(
        self,
        run_turn: Callable[[str], Awaitable[RunTurnResult]],
        pre_gate: Callable[[], None] | None = None,
    ) -> None:
        self._run_turn = run_turn
        # Pre-call hard gate: invoked at the exact seam between "about to spend" and
        # "spending" — raises CostLimitExceeded when the run budget is already gone.
        self._pre_gate = pre_gate

    async def execute(self, request) -> TaskResult:
        if self._pre_gate is not None:
            self._pre_gate()
        result = await self._run_turn(request.prompt)
        return TaskResult(value=result.final_answer, spend=result.cost_usd)


class _ResearchProgressProbe:
    """:class:`workflow.policy.ProgressProbe`: milestone diffs live here, 100% adapter-side.

    The core only ever sees a bool. "Progress" is the structural milestone the stall
    brake has always meant — stage moved or a gate newly passed/overrode — NOT
    arbitrary file churn (growing the evidence graph in DISCOVER must keep counting
    toward a stall, exactly as before 6C).
    """

    def __init__(self, service, owner_id: UUID, task_id: str) -> None:
        self._service = service
        self._owner = owner_id
        self._task = task_id

    async def snapshot(self):
        return await self._service.project_fingerprint(self._owner, self._task)

    def changed(self, before, after) -> bool:
        return (
            before.get("stage") != after.get("stage")
            or before.get("gates") != after.get("gates")
        )


# ── the driver ───────────────────────────────────────────────────────────────
class ResearchRunDriver:
    """Owns the state transitions of one auto-continue run.

    Configured from ``core.config`` by default; knobs can be overridden for tests. Instances
    are stateless between ``auto_turn`` calls (all run state lives on disk), so one driver
    object may drive many tasks/workers.
    """

    def __init__(
        self,
        *,
        max_turns: int | None = None,
        max_no_progress: int | None = None,
        max_attempts: int | None = None,
        max_cost_usd: float | None = None,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        from core.config import settings

        self.max_turns = settings.research_driver_max_turns if max_turns is None else max_turns
        self.max_no_progress = (
            settings.research_driver_max_no_progress_turns
            if max_no_progress is None
            else max_no_progress
        )
        self.max_attempts = (
            settings.research_driver_max_attempts if max_attempts is None else max_attempts
        )
        self.max_cost_usd = (
            settings.research_driver_max_cost_usd if max_cost_usd is None else max_cost_usd
        )
        # Late-bound by construction site: the compat facade injects a lambda over
        # ITS module global so a monkeypatch of ``driver._backoff_s`` stays effective.
        self._backoff = backoff or (lambda attempt: _backoff_s(attempt))

    # -- single-flight lease contest (compat shell over the core acquire) ------
    def _claim(
        self, service, owner_id: UUID, task_id: str, *, run_id: str, turn_index: int
    ) -> DriverOutcome:
        """[6C compat shell] Run the pure core lease contest and translate its verdict.

        The hand-written claim decision is gone: :func:`workflow.leases.acquire` is the
        single authority over granted / reclaimed / cancelled / dropped. ``auto_turn``
        no longer calls this (the contest runs inside :func:`drive_iteration`); it
        stays exported so the single-flight semantics remain directly testable.
        """
        store = ResearchLeaseStore(service, owner_id, task_id, run_id=run_id)
        box: dict[str, Any] = {}

        def contest(ledger: LeaseLedger) -> LeaseLedger:
            box["decision"] = acquire(
                ledger, run_id=run_id, index=turn_index, config=_LEASE,
                now_iso=_now_iso(), now_epoch=_now_epoch(),
            )
            return box["decision"].ledger

        try:
            store.atomic(contest)
        except RevisionConflictError:
            # Another process committed between our read and this CAS — a competing job won.
            return DriverOutcome(
                state=RunState.RUNNING, action="dropped", reason="revision conflict (another job claimed)"
            )
        decision = box["decision"]
        if decision.action == "dropped":
            return self._dropped_outcome(service, owner_id, task_id, run_id, turn_index, store, decision.reason)
        if decision.action == "cancelled":
            return self._finalize_pre_turn_cancel(
                service, owner_id, task_id, run_id=run_id, turn_index=turn_index,
                execution_id=decision.identity.as_str() if decision.identity else None,
            )
        return DriverOutcome(
            state=RunState.RUNNING,
            action="continue",
            run_id=run_id,
            turn_index=turn_index,
            turn_attempt=decision.identity.attempt,
            execution_id=decision.identity.as_str(),
        )

    @staticmethod
    def _translate_drop_reason(store: ResearchLeaseStore, core_reason: str | None) -> str | None:
        """Restore the historical research-vocabulary drop reason from the core verdict."""
        if store.drop_note:
            return store.drop_note
        if core_reason is None:
            return None
        if core_reason in _CORE_DROP_REASONS:
            return _CORE_DROP_REASONS[core_reason]
        if core_reason.startswith("unknown lease state: "):
            return f"unknown ledger turn_state: {core_reason[len('unknown lease state: '):]}"
        return core_reason

    def _dropped_outcome(
        self, service, owner_id, task_id, run_id, turn_index, store, core_reason
    ) -> DriverOutcome:
        try:
            state = observe_run_state(service.read_project(owner_id, task_id), run_id)
        except Exception:  # noqa: BLE001 - task may have been deleted under us
            state = RunState.IDLE
        return DriverOutcome(
            state=state, action="dropped", dropped=True, run_id=run_id,
            turn_index=turn_index, reason=self._translate_drop_reason(store, core_reason),
        )

    def _fence_dropped_outcome(
        self, service, owner_id, task_id, run_id, turn_index, reason
    ) -> DriverOutcome:
        """Fold a fenced (zombie) execution into a silent drop — the lease fence tripped
        after a successor reclaimed this iteration: the successor owns the fate, so this
        job writes NOTHING and reports no verdict."""
        try:
            state = observe_run_state(service.read_project(owner_id, task_id), run_id)
        except Exception:  # noqa: BLE001 - task may have been deleted under us
            state = RunState.IDLE
        return DriverOutcome(
            state=state, action="dropped", dropped=True, run_id=run_id,
            turn_index=turn_index, reason=reason,
        )

    def _finalize_pre_turn_cancel(
        self, service, owner_id: UUID, task_id: str, *, run_id: str, turn_index: int,
        execution_id: str | None,
    ) -> DriverOutcome:
        """Record the between-turn Stop and release the slot (the lease itself was
        already settled ``done`` by the core contest inside the same CAS shape)."""
        def mutate(project: dict) -> None:
            project["last_block"] = {
                "kind": "cancelled",
                "reason": "stop requested before this turn started",
                "at": _now_iso(),
                "run_id": run_id,
                "execution_id": execution_id,
            }
            project.pop("active_run", None)

        service.atomic_update_project(owner_id, task_id, mutate)
        return DriverOutcome(
            state=RunState.CANCELLED, action="cancelled", run_id=run_id,
            turn_index=turn_index, reason="cancel requested before this turn started",
        )

    # -- persistence helpers ---------------------------------------------------
    def _persist_ledger(
        self, service, owner_id: UUID, task_id: str, *, patch: dict
    ) -> None:
        service.set_driver_checkpoint(owner_id, task_id, patch=patch)

    def _finish_run(
        self,
        service,
        owner_id: UUID,
        task_id: str,
        *,
        outcome: DriverOutcome,
        reason: str,
        cumulative_cost_usd: float,
        consecutive_no_progress: int,
    ) -> None:
        """Terminalize the run: customs-gate the transition, record it, release the slot.

        Order matters: the ledger is flipped to ``done`` and ``last_block`` recorded in one
        atomic commit, then ``end_run`` pops ``active_run``. A duplicate job arriving in the
        tiny window between the two still sees ``turn_state == done`` and a matching
        ``turn_index``, so its claim bumps an attempt and re-runs — but the run is already
        graded terminal, and the *next* claim will observe no slot and drop. No job can ever
        resurrect a finished run.
        """
        state = outcome.state
        check_transition(RunState.RUNNING, state)  # the customs gate

        def mutate(project: dict) -> None:
            ledger = project.get("driver")
            if isinstance(ledger, dict):
                ledger.update(
                    turn_state="done",
                    cumulative_cost_usd=cumulative_cost_usd,
                    consecutive_no_progress=consecutive_no_progress,
                    updated_at=_now_iso(),
                )
                project["driver"] = ledger
            project["last_block"] = {
                "kind": state.value,
                "reason": reason,
                "at": _now_iso(),
                "run_id": outcome.run_id,
                "execution_id": outcome.execution_id,
            }

        service.atomic_update_project(owner_id, task_id, mutate)
        service.end_run(owner_id, task_id)

    # -- deterministic auto-settle to PUBLISH -----------------------------------
    async def _try_settle(
        self,
        service,
        owner_id: UUID,
        task_id: str,
        *,
        run_id: str,
        turn_index: int,
        turn_attempt: int,
        execution_id: str,
        grade: Grade,
        cumulative: float,
        consecutive: int,
    ) -> DriverOutcome | None:
        """Finish a stuck progressive run deterministically, or return ``None`` to stop as graded.

        Only called for progressive runs whose graded stop is NOT a human decision. The G2
        pre-walk gate first: the run may only be finished once its PRIMARY report is bound and
        PROMOTED (real ``drive_asset_id``) — a run that never wrote or never published its
        report is refused a fake PUBLISH and stops as graded instead. When the gate passes,
        walks the legal ``transition_stage`` chain from the current stage to PUBLISH (each
        guarded hop whose gate has not passed records its failed checks as a diagnostic and is
        granted), then writes a model-free :func:`build_settle_report` into the task's
        artifacts (it is mirrored to cloud ``outputs/`` but never RAG-promoted, so a gaps-only
        record can't surface as evidence later) and terminalizes the run as FINISHED. Returns
        ``None`` (caller falls back to the graded stop) when the mode is strict, the run is
        parked on a real human decision, the G2 published-report precondition is unmet, the
        chain refuses a hop, or the report write fails — the run is never half-finished
        silently.
        """
        project = service.read_project(owner_id, task_id)
        if project.get("execution_mode", "strict") != "progressive":
            return None
        if project.get("stage") == "PUBLISH":
            return None

        # G2 hard gate (Run-14 lesson): auto-settle may only terminalize a run as
        # FINISHED when the run ACTUALLY produced a published object — a primary report
        # bound in state whose latest version is PROMOTED with a real drive_asset_id.
        # The previous check short-circuited on ``if _primary:`` — a run whose agent
        # never wrote a report (primary=None) walked the chain to PUBLISH and finished
        # as a fake success. Now refused before a single hop: no bound primary, or a
        # primary that was never promoted, stops as graded. No walk, no FINISHED,
        # no settle_report written over an unpromised run.
        _primary = project.get("primary_report_artifact_id")
        if not _primary:
            logger.warning(
                "settle.refused task %s: G2 — no primary report bound for run_seq=%s; "
                "a run that never wrote its report cannot settle to success",
                task_id, project.get("run_seq"),
            )
            return None
        _prec = next(
            (
                a for a in service.list_artifacts(owner_id, task_id)
                if a["artifact_id"] == _primary
            ),
            None,
        )
        if (
            _prec is None
            or _prec.get("status") != "PROMOTED"
            or not _prec.get("drive_asset_id")
        ):
            logger.warning(
                "settle.refused task %s: G2 — primary report '%s' not promoted "
                "(status=%s drive_asset=%s); stopping as graded, never a fake PUBLISH",
                task_id, _primary, (_prec or {}).get("status"),
                bool((_prec or {}).get("drive_asset_id")),
            )
            return None

        pre_walk_stage = project.get("stage", "DISCOVER")
        diagnostics_before = len(project.get("diagnostics") or [])

        # Walk to PUBLISH. Each hop re-reads the handoff so it always asks the state machine
        # for the legal next stage — never guesses, never skips (mirror of auto_turn_prompt).
        hops = 0
        while project.get("stage") != "PUBLISH":
            hops += 1
            if hops > _MAX_SETTLE_HOPS:
                logger.warning(
                    "settle walk exceeded %d hops for task %s; stopping as graded", _MAX_SETTLE_HOPS, task_id
                )
                return None
            handoff = service.get_handoff(owner_id, task_id)
            nxt = handoff.get("next_stage")
            if not nxt:
                logger.warning(
                    "settle hit a dead-end stage %r for task %s; stopping as graded",
                    project.get("stage"), task_id,
                )
                return None
            granted = service.transition_stage(owner_id, task_id, target=nxt)
            if not granted.get("granted"):
                logger.warning(
                    "settle transition %s -> %s refused (%s) for task %s; stopping as graded",
                    project.get("stage"), nxt, granted.get("reason"), task_id,
                )
                return None
            project = service.read_project(owner_id, task_id)
        diagnostics = project.get("diagnostics") or []
        new_diagnostics = len(diagnostics) - diagnostics_before

        # Synthesize + persist the auto-settled report from what the run ACTUALLY produced.
        graph = service._load_graph(owner_id, task_id)
        node_counts: dict[str, int] = {}
        for node in graph["nodes"]:
            node_type = node.get("type") or "node"
            node_counts[node_type] = node_counts.get(node_type, 0) + 1
        artifacts = service.list_artifacts(owner_id, task_id)
        content = build_settle_report(
            task_name=project.get("name", task_id),
            mode=project.get("execution_mode", "strict"),
            created_at=project.get("created_at", ""),
            reached_stage=pre_walk_stage,
            reason=grade.reason or "run could not advance to PUBLISH on its own",
            node_counts=sorted(node_counts.items(), key=lambda kv: (-kv[1], kv[0])),
            edge_count=len(graph["edges"]),
            artifacts=artifacts,
            diagnostics=diagnostics,
        )
        try:
            # The report is deliberately NOT promoted to the drive/RAG queue: a "gaps only"
            # record of what could not be established must never surface as evidence in a later
            # rag_search. write_scratch still mirrors it into the task's cloud outputs/ (best
            # effort) so the desktop shows it next to the other artifacts.
            if any(a["artifact_id"] == _SETTLE_ARTIFACT_ID for a in artifacts):
                made = await service.create_version(
                    owner_id, task_id, artifact_id=_SETTLE_ARTIFACT_ID, content=content,
                    idempotency_key=f"settle:{execution_id}",
                )
            else:
                made = await service.write_scratch(
                    owner_id, task_id, artifact_id=_SETTLE_ARTIFACT_ID, content=content,
                    generated_by_execution=execution_id,
                )
            version = int(made.get("version") or 1)
        except Exception as exc:  # noqa: BLE001 - best-effort settle report; the run still stops
            logger.warning(
                "settle report write failed for task %s; stopping as graded: %s", task_id, exc
            )
            return None

        reason = (
            f"reached PUBLISH via auto-settle — the run could not advance on its own "
            f"({grade.reason or 'no stage or gate advance'}); {new_diagnostics} gate "
            f"diagnostic(s) recorded into the report"
        )
        final_answer = (
            f"[auto-settled] {project.get('name', task_id)} reached PUBLISH deterministically — "
            f"the run could not advance on its own ({grade.reason or 'no progress'}). Wrote "
            f"`{_SETTLE_ARTIFACT_ID}` (v{version}) recording {new_diagnostics} new gap "
            f"diagnostic(s) from the un-passed gate(s); the report closes with the 'Known gaps "
            f"/ unverified items' list."
        )
        outcome = DriverOutcome(
            state=RunState.FINISHED, action="finished",
            run_id=run_id, turn_index=turn_index, turn_attempt=turn_attempt,
            execution_id=execution_id, progress=False,
            cumulative_cost_usd=cumulative, final_answer=final_answer,
            reason=reason, consecutive_no_progress=consecutive,
        )
        self._finish_run(
            service, owner_id, task_id,
            outcome=outcome, reason=reason,
            cumulative_cost_usd=cumulative, consecutive_no_progress=consecutive,
        )
        return outcome

    # -- the one-job orchestration ---------------------------------------------
    async def auto_turn(
        self,
        service,
        *,
        owner_id: UUID,
        task_id: str,
        run_id: str,
        turn_index: int,
        run_turn: Callable[[str], Awaitable[RunTurnResult]],
    ) -> DriverOutcome:
        """Drive exactly one auto-continue turn (one worker job = one execution).

        Returns an outcome the worker acts on; never raises for graded stops (stall, caps,
        cancel, drop, finish). Unexpected non-transient errors terminalize the run to ERROR
        (slot released) and are re-raised so the job row fails honestly.
        """
        # ── 6C: one generic iteration, choreographed by the core runner ──
        # The lease contest, execution behind heartbeat + cooperative cancel, the retry
        # bisection, grading and settling all live in workflow.runner.drive_iteration.
        # This method only (a) assembles the adapter ports, (b) hands in the pre-turn
        # counters read from the authoritative on-disk ledger, and (c) translates the
        # IterationOutcome back into the research disk/UI vocabulary.
        store = ResearchLeaseStore(service, owner_id, task_id, run_id=run_id)

        try:
            project = service.read_project(owner_id, task_id)
            ledger = service.get_driver_checkpoint(owner_id, task_id)
        except Exception:  # noqa: BLE001 - a deleted/absent task surfaces via the contest
            project, ledger = {}, {}

        # L2 definition drift: begin_run stamps the workflow-definition fingerprint
        # into the checkpoint; a deploy that rewrites the flow mid-flight must NOT
        # resume live runs under the new definition — the turn terminalizes as an
        # honest error before any execution. Stamps are minted at begin_run, so the
        # (impossible-in-practice) window where active_run exists but the checkpoint
        # lacks a stamp is not a mismatch and passes through.
        stamped = ledger.get("definition_fp")
        current_fp = RESEARCH_WORKFLOW.fingerprint()
        if stamped is not None and stamped != current_fp:
            return self._terminal_error(
                service, owner_id, task_id,
                reason=(
                    f"workflow definition drift: run stamped {stamped}, "
                    f"current definition is {current_fp}"
                ),
                outcome=DriverOutcome(
                    state=RunState.RUNNING, action="continue", run_id=run_id,
                    turn_index=turn_index, turn_attempt=1,
                ),
                cumulative=float(ledger.get("cumulative_cost_usd") or 0.0),
                consecutive=int(ledger.get("consecutive_no_progress") or 0),
            )

        consecutive = int(ledger.get("consecutive_no_progress") or 0)
        cumulative = float(ledger.get("cumulative_cost_usd") or 0.0)
        # Turns whose cost could not be computed (PRICING_UNKNOWN) are counted explicitly
        # instead of being laundered into the $0 cumulative total.
        pricing_unknown = int(ledger.get("pricing_unknown_turns") or 0)
        counters = RunCounters(
            consecutive_no_progress=consecutive,
            total_spend=cumulative,
            unknown_spend_count=pricing_unknown,
        )

        # Fresh authoritative facts for grading; also remembered so the terminal
        # translation can restate stop texts in the historical human-visible words.
        facts_box: dict[str, Any] = {"stage": project.get("stage", "DISCOVER"), "pending": 0}
        # Fence tokens minted per attempt in ``compose_prompt`` (see there); reset in
        # the ``finally`` below so post-return worker settle writes are never fenced.
        _fence_tokens: list[contextvars.Token] = []

        def business_facts() -> dict[str, Any]:
            fresh = service.read_project(owner_id, task_id)
            pending = len(service.pending_overrides(owner_id, task_id))
            facts_box["stage"] = fresh.get("stage", "DISCOVER")
            facts_box["pending"] = pending
            # Pipeline structural stop: written by run_node on the FIRST honest
            # inability to produce a required deliverable. It suppresses the
            # success predicate (arriving at PUBLISH without a promotable report
            # is NOT finished) and terminates the run in the same grading pass.
            structural = bool((fresh.get("pipeline") or {}).get("structural_stop"))
            return {
                "finished": (
                    fresh.get("stage", "DISCOVER") == "PUBLISH" and not structural
                ),
                "pending_signals": pending,
                "cancel_requested": False,  # the core ORs the fresh lease ledger itself
                "structural_stop": structural,
            }

        def compose_prompt(req, attempt) -> str:
            # F2 fence: minted per attempt on THIS task, immediately before the core
            # ``create_task(executor.execute(...))`` — asyncio snapshots the context, so
            # every tool commit inside the turn carries this execution's identity. The
            # authority seam (plugin.atomic_update_project) and the write-entry asserts
            # refuse any commit whose fence no longer matches the on-disk lease: a
            # reclaimed (zombie) execution can at most leave temp files, never finalize.
            _fence_tokens.append(set_auto_run_fence(
                owner_id=owner_id, task_id=task_id, run_id=run_id,
                execution_id=f"{run_id}:{turn_index}:{attempt}",
            ))
            return auto_turn_prompt(
                task_name=project.get("name", task_id),
                project_id=task_id,
                stage=project.get("stage", "DISCOVER"),
                turn_index=turn_index,
                consecutive_no_progress=counters.consecutive_no_progress,
                has_materials=bool(project.get("materials")),
            )

        retry = RetryPolicy(
            max_attempts=self.max_attempts or 1,
            is_transient=is_transient_error,   # research predicate injected; core owns the shape
            backoff=self._backoff,             # late-bound via construction site
        )
        # L2: the structural wiring comes from the workflow DEFINITION, not from
        # hand-assembled knobs — the generic runtime resolves the activity's logical
        # executor id through the registry and joins the definition's declared cap
        # dimensions with this run's config values. The core still never sees a
        # research word: the registry binding and every port are adapter closures.
        # PRE-CALL cost hard gate: checked on the executor's seam, i.e. right before
        # each would-be LLM turn; counters.total_spend is the run's ledger cumulative.
        def _cost_pre_gate() -> None:
            if self.max_cost_usd is not None and counters.total_spend >= self.max_cost_usd:
                raise CostLimitExceeded(
                    f"research run {run_id} turn {turn_index}: cumulative "
                    f"${counters.total_spend:.4f} >= cap ${self.max_cost_usd:.4f} — "
                    "no LLM call is made"
                )

        registry = MappingRegistry({
            RESEARCH_EXECUTOR_ID: _RunTurnExecutor(run_turn, pre_gate=_cost_pre_gate)
        })
        deps = build_deps(
            definition=RESEARCH_WORKFLOW,
            registry=registry,
            task_name=AUTO_TURN_TASK,   # the auto-run flow's single declared activity
            store=store,
            probe=_ResearchProgressProbe(service, owner_id, task_id),
            business_facts=business_facts,
            compose_prompt=compose_prompt,
            retry=retry,
            caps_values={
                # Cap VALUES are runtime config — changing a budget is not drift.
                "max_turns": self.max_turns or None,
                "max_no_progress": self.max_no_progress or None,
                "max_spend": self.max_cost_usd,
            },
            # Caller-injected generic policy config (calibration #2): the CORE default
            # keeps caps at FAILED; the research definition declares WAITING because it
            # judges a cap crossing in an auto-run parked on a human decision. Disk
            # vocabulary is cause-driven, so BLOCKED is unaffected either way.
            cap_outcome=WorkflowState.WAITING,
            signal_outcome=WorkflowState.WAITING,
            lease=_LEASE,
            publisher=NoopPublisher(),
        )

        try:
            try:
                out = await drive_iteration(
                    deps, IterationRequest(run_id=run_id, index=turn_index, counters=counters)
                )
            except ExecutionFailed as ef:
                if isinstance(ef.cause, OwnershipLost):
                    # A tool-side write tripped the authority seam: our execution was
                    # reclaimed while the turn ran — a zombie must NOT terminalize a
                    # live successor's run. Fold to a silent drop.
                    return self._fence_dropped_outcome(
                        service, owner_id, task_id, run_id, turn_index,
                        "execution fenced (OwnershipLost) during the turn",
                    )
                # Non-transient fault: the core already settled the lease honestly —
                # record the research terminalization, then fail the job row loudly.
                self._terminal_error(
                    service, owner_id, task_id,
                    reason=f"turn failed: {ef.cause}",
                    outcome=DriverOutcome(
                        state=RunState.RUNNING, action="continue", run_id=run_id,
                        turn_index=turn_index, turn_attempt=ef.outcome.attempt,
                        execution_id=ef.outcome.execution_id,
                    ),
                    cumulative=ef.outcome.counters.total_spend, consecutive=consecutive,
                )
                raise ef.cause
            except RevisionConflictError:
                # A competing job committed between our read and this CAS — it won.
                return DriverOutcome(
                    state=RunState.RUNNING, action="dropped", reason="revision conflict (another job claimed)"
                )
            except OwnershipLost as ol:
                # Post-iteration commit raced a reclaim: ownership moved before we could
                # settle — the successor owns the fate. Verdict discarded, no writes.
                return self._fence_dropped_outcome(
                    service, owner_id, task_id, run_id, turn_index,
                    f"ownership lost before post-iteration settle: {ol}",
                )

            run_counters = out.counters

            if out.dropped:
                return self._dropped_outcome(
                    service, owner_id, task_id, run_id, turn_index, store, out.reason
                )
            if out.reason == "cancel requested before this iteration started":
                # Pre-turn Stop observed by the contest: no execution ever ran.
                return self._finalize_pre_turn_cancel(
                    service, owner_id, task_id, run_id=run_id, turn_index=turn_index,
                    execution_id=out.execution_id,
                )
            if out.cause == "transient_exhausted":
                # Retries exhausted → a graded terminal stop (slot released, job succeeds
                # with run.error — the run never silently retries forever).
                return self._terminal_error(
                    service, owner_id, task_id,
                    reason=out.reason,
                    outcome=DriverOutcome(
                        state=RunState.RUNNING, action="continue", run_id=run_id,
                        turn_index=turn_index, turn_attempt=out.attempt,
                        execution_id=out.execution_id,
                    ),
                    cumulative=run_counters.total_spend, consecutive=consecutive,
                )

            if out.action == "continue":
                # The core already flipped the lease to done; write the research-private
                # meters and let the worker enqueue N+1.
                self._persist_ledger(
                    service, owner_id, task_id,
                    patch={
                        "turn_state": "done",
                        "cumulative_cost_usd": run_counters.total_spend,
                        "pricing_unknown_turns": run_counters.unknown_spend_count,
                        "consecutive_no_progress": run_counters.consecutive_no_progress,
                        "execution_id": out.execution_id,
                        "next_scheduled": _now_iso(),
                        "updated_at": _now_iso(),
                    },
                )
                return DriverOutcome(
                    state=RunState.RUNNING, action="continue",
                    run_id=run_id, turn_index=turn_index, turn_attempt=out.attempt,
                    execution_id=out.execution_id, progress=out.progress,
                    cumulative_cost_usd=run_counters.total_spend, final_answer=out.value,
                    next_turn_index=out.next_index,
                    consecutive_no_progress=run_counters.consecutive_no_progress,
                )

            # ── graded terminal stop → research vocabulary ──
            state = _GRADE_CAUSE_TO_RUN[out.cause]
            tf = TurnFacts(
                stage=facts_box["stage"], pending_overrides=facts_box["pending"],
                cancel_requested=False, progress=out.progress,
                consecutive_no_progress=consecutive, turn_index=turn_index,
                cumulative_cost_usd=run_counters.total_spend,
                max_turns=self.max_turns, max_no_progress=self.max_no_progress,
                max_cost_usd=self.max_cost_usd,
            )
            reason = _research_stop_text(out.cause, tf, run_counters.consecutive_no_progress)

            # ── deterministic auto-settle (progressive runs only) ──
            # A progressive run that would otherwise stop short of PUBLISH for a NON-human
            # reason (no-progress stall or a turn/cost cap) is finished deterministically
            # instead: walk the legal chain to PUBLISH recording gate diagnostics, then write
            # an auto-settled report. Strict runs — and runs parked on a real pending human
            # decision — keep the graded stop below. On any failure _try_settle returns None
            # and we fall through to it.
            if (
                state in (RunState.STALLED, RunState.BLOCKED)
                and out.cause != CAUSE_PENDING_SIGNAL
                and out.cause != CAUSE_STRUCTURAL
                and facts_box["pending"] == 0
            ):
                settled = await self._try_settle(
                    service, owner_id, task_id,
                    run_id=run_id, turn_index=turn_index, turn_attempt=out.attempt,
                    execution_id=out.execution_id,
                    grade=Grade(state, reason, run_counters.consecutive_no_progress),
                    cumulative=run_counters.total_spend, consecutive=consecutive,
                )
                if settled is not None:
                    return settled

            final_answer = out.value
            if not final_answer:
                final_answer = _terminal_message(state, reason)
            outcome = DriverOutcome(
                state=state, action=state.value,
                run_id=run_id, turn_index=turn_index, turn_attempt=out.attempt,
                execution_id=out.execution_id, progress=out.progress,
                cumulative_cost_usd=run_counters.total_spend, final_answer=final_answer,
                reason=reason,
                consecutive_no_progress=run_counters.consecutive_no_progress,
            )
            self._finish_run(
                service, owner_id, task_id,
                outcome=outcome, reason=reason or "",
                cumulative_cost_usd=run_counters.total_spend,
                consecutive_no_progress=run_counters.consecutive_no_progress,
            )
            return outcome
        finally:
            # The fence must never outlive this execution: the worker's post-return
            # settle writes (next-turn enqueue bookkeeping, mirrors) belong to no
            # iteration owner. Tokens were minted in THIS task's context, so
            # resetting them here is well-formed; already-ended run_task snapshots
            # simply vanish.
            for token in reversed(_fence_tokens):
                with contextlib.suppress(ValueError):
                    clear_auto_run_fence(token)

    def _terminal_error(
        self, service, owner_id, task_id, *, reason, outcome, cumulative, consecutive
    ) -> DriverOutcome:
        """Terminalize to ERROR after an unexpected/untreatable failure (slot released)."""
        err = DriverOutcome(
            state=RunState.ERROR, action="error", run_id=outcome.run_id,
            turn_index=outcome.turn_index, turn_attempt=outcome.turn_attempt,
            execution_id=outcome.execution_id, cumulative_cost_usd=cumulative,
            reason=reason,
        )
        self._finish_run(
            service, owner_id, task_id, outcome=err, reason=reason,
            cumulative_cost_usd=cumulative, consecutive_no_progress=consecutive,
        )
        return err

    def abort_run(
        self,
        service,
        owner_id: UUID,
        task_id: str,
        *,
        run_id: str,
        execution_id: str | None,
        state: RunState = RunState.ERROR,
        reason: str = "run aborted",
    ) -> DriverOutcome:
        """Release a stuck slot from *outside* a turn (e.g. an enqueue failure in the worker).

        This is the customs-gate escape hatch for infra errors the driver itself could not see
        (the continuation job could not be scheduled). It re-reads the ledger so the recorded
        cumulative cost / no-progress counters are preserved, records ``last_block``, and pops
        ``active_run`` so the task is never stranded RUNNING.
        """
        ledger = service.get_driver_checkpoint(owner_id, task_id)
        outcome = DriverOutcome(
            state=state, action=state.value, run_id=run_id,
            execution_id=execution_id, reason=reason,
            turn_index=int(ledger.get("turn_index") or 0),
            cumulative_cost_usd=float(ledger.get("cumulative_cost_usd") or 0.0),
        )
        self._finish_run(
            service, owner_id, task_id,
            outcome=outcome, reason=reason,
            cumulative_cost_usd=outcome.cumulative_cost_usd,
            consecutive_no_progress=int(ledger.get("consecutive_no_progress") or 0),
        )
        return outcome


def _terminal_message(state: RunState, reason: str | None) -> str:
    """A short human-readable assistant note for a terminal stop (mirrored to the session)."""
    label = {
        RunState.FINISHED: "Research finished",
        RunState.BLOCKED: "Research paused",
        RunState.STALLED: "Research stalled",
        RunState.CANCELLED: "Research stopped",
        RunState.ERROR: "Research run errored",
    }[state]
    return f"⏹ {label}: {reason}." if reason else f"⏹ {label}."
