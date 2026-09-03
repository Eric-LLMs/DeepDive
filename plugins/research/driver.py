"""Research run driver: the auto-continue control chain for one task run.

A single "Run" click used to mean a single agent turn (``max_steps=5``) that stopped the
moment the model stopped talking — the task never reached PUBLISH on its own. This module is
the server-side chain that keeps driving an already-started run, one worker job at a time,
until PUBLISH, a human gate decision, a stall, a cap, a cancel, or an error.

Three ideas hold it together:

- **Single-writer discipline.** Only one live run owns a task at a time. Every job starts
  with an atomic CAS claim (:meth:`ResearchRunDriver._claim`) that validates the on-disk
  ``active_run`` + driver ledger under the project file lock. A stale/duplicate/out-of-order
  job is *dropped*, never run. A ``turn_state == running`` remnant whose heartbeat has gone
  stale is treated as a crash and re-run idempotently (attempt bumped, same ``turn_index``);
  a *fresh* ``running`` ledger belongs to a live twin and is dropped, so two jobs can never
  execute the same turn in parallel.

- **RunState: the customs gate.** ``RunState`` is the single authority over legal run
  transitions. Only :data:`RunState.IDLE` may become :data:`RunState.RUNNING` (and only
  ``begin_run`` — a fresh ``run_id`` — may do that; the driver never starts runs). Once a run
  lands in a terminal state (``FINISHED`` / ``BLOCKED`` / ``STALLED`` / ``CANCELLED`` /
  ``ERROR``) it is irreversible: the driver records ``last_block`` and releases the slot, and
  no later job can resurrect it.

- **Grade, never guess.** After each successful turn the driver re-reads the authoritative
  state and applies the fixed priority chain (:func:`grade_turn`): PUBLISH → finished, human
  gate override pending → blocked, consecutive no-progress → stalled, turn cap → blocked,
  cost cap → blocked, else → continue one more turn. Every stop is honest and visible — the
  run never silently skips a gate or loops forever.

The driver is deliberately I/O-lean: it owns *state transitions* (claim, ledger, last_block,
slot release). The worker job (`apps/worker/tasks.py: research_drive`) supplies the actual
``run_turn`` callable (kernel.run), mirrors the assistant text into the task's session
mirror, enqueues the next job, and publishes the wake-up event.
"""
from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

from plugins.research.plugin import (
    ProjectLockError,
    RevisionConflictError,
    _now_iso,
)

# Heartbeat / lease knobs (seconds). The driver refreshes ``driver.updated_at`` every
# ``LEASE_REFRESH_S`` while a turn is executing; a claim treats a ``running`` ledger whose
# heartbeat is older than ``LEASE_STALE_S`` as a crashed predecessor (attempt + 1) and a
# fresher one as a live twin (drop).
LEASE_REFRESH_S = 20
LEASE_STALE_S = 150


# ── RunState: the run's legal-state customs gate ─────────────────────────────
class RunState(str, enum.Enum):
    """The run-level state machine. Terminal states are irreversible.

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


# Legal transitions: only IDLE may become RUNNING; a terminal state has no way out. The
# driver never transitions *to* IDLE explicitly — releasing the slot *is* the run returning
# to IDLE on disk — so every in-code transition is either RUNNING→RUNNING (continue) or
# RUNNING→terminal (stop). Anything else is a bug and is refused loudly.
_RUN_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.IDLE: {RunState.RUNNING},
    RunState.RUNNING: {RunState.RUNNING, RunState.FINISHED, RunState.BLOCKED,
                       RunState.STALLED, RunState.CANCELLED, RunState.ERROR},
    RunState.FINISHED: set(),
    RunState.BLOCKED: set(),
    RunState.STALLED: set(),
    RunState.CANCELLED: set(),
    RunState.ERROR: set(),
}


class IllegalRunTransition(RuntimeError):
    """A state change the customs gate forbade (terminal escape / non-IDLE start)."""


def observe_run_state(project: dict, run_id: str) -> RunState:
    """Derive the run state a job should see from the persisted slot.

    ``RUNNING`` iff the job's ``run_id`` still owns the slot. A terminal outcome has already
    released the slot (``end_run``), so it shows up as ``IDLE`` — the customs gate then drops
    any later job instead of letting it resume.
    """
    active = project.get("active_run")
    if active and active.get("run_id") == run_id and active.get("status") == "RUNNING":
        return RunState.RUNNING
    return RunState.IDLE


def check_transition(current: RunState, target: RunState) -> None:
    """Central customs check: raise unless ``current -> target`` is legal."""
    if target not in _RUN_TRANSITIONS.get(current, set()):
        raise IllegalRunTransition(f"illegal run transition: {current.value} -> {target.value}")


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


@dataclass
class Grade:
    """Result of the priority chain: stop (terminal state + reason) or continue."""

    state: RunState | None           # terminal state to stop in, or None → continue
    reason: str | None = None
    consecutive_no_progress: int = 0


def grade_turn(facts: TurnFacts) -> Grade:
    """The fixed stop/continue priority chain (see module docstring).

    Returns a terminal :class:`RunState` when the run must stop (with the human-visible
    ``reason``) or ``state=None`` to schedule one more turn. Never bypasses a gate: a pending
    human override is a hard stop, and reaching PUBLISH is a clean finish.
    """
    consecutive = facts.consecutive_no_progress
    if facts.cancel_requested:
        return Grade(RunState.CANCELLED, "stop requested by the user", consecutive)
    if facts.stage == "PUBLISH":
        return Grade(RunState.FINISHED, "research task reached PUBLISH", consecutive)
    if facts.pending_overrides:
        return Grade(
            RunState.BLOCKED,
            f"{facts.pending_overrides} gate override(s) awaiting a human decision",
            consecutive,
        )
    new_consecutive = consecutive + 1 if not facts.progress else 0
    if facts.max_no_progress and new_consecutive >= facts.max_no_progress:
        return Grade(
            RunState.STALLED,
            f"no visible progress across {new_consecutive} consecutive auto turns",
            new_consecutive,
        )
    if facts.max_turns and facts.turn_index >= facts.max_turns:
        return Grade(
            RunState.BLOCKED,
            f"reached the auto-run turn cap ({facts.max_turns} turns)",
            new_consecutive,
        )
    if facts.max_cost_usd is not None and facts.cumulative_cost_usd >= facts.max_cost_usd:
        return Grade(
            RunState.BLOCKED,
            f"reached the auto-run cost cap (${facts.cumulative_cost_usd:.4f})",
            new_consecutive,
        )
    return Grade(None, None, new_consecutive)


# ── transient classification ─────────────────────────────────────────────────
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
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(h in text for h in _TRANSIENT_HINTS)


# ── outcome payload ──────────────────────────────────────────────────────────
@dataclass
class RunTurnResult:
    """What one successful ``run_turn`` returned."""

    final_answer: str
    cost_usd: float = 0.0


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


# ── the auto-turn prompt ─────────────────────────────────────────────────────
def auto_turn_prompt(
    *,
    task_name: str,
    project_id: str,
    stage: str,
    turn_index: int,
    consecutive_no_progress: int = 0,
) -> str:
    """The driver directive handed to the model for one autonomous continuation turn.

    The interactive first turn (turn 0) already resumed the task and did some work; each auto
    turn re-arms the same resume contract but tells the model to keep going without asking the
    user, and to be honest about stopping (it may request a gate override only when a human
    decision is genuinely required).
    """
    push = (
        "\nNOTE: The previous auto turn made no visible progress on the task files. Do not "
        "just talk — act: inspect the current state with research_project (action snapshot) "
        "and advance at least one concrete step (record/update nodes or evidence, write "
        "scratch, produce artifact versions, or transition the stage)."
        if consecutive_no_progress > 0
        else ""
    )
    return (
        f"[Research auto-run, turn {turn_index} of the same run]\n"
        f"Continue driving the existing Research OS task `{task_name}` (project_id "
        f"{project_id}), currently at stage {stage}, forward toward PUBLISH — fully "
        "autonomously. Do NOT create a new project and do NOT ask the user anything. Use the "
        "deep_research skill and the research tools exactly as a researcher would: consult "
        "the task state (research_project action snapshot), then keep producing the evidence, "
        "graph nodes/edges, artifacts, and stage transitions the current gate demands. "
        "Check gates when a transition needs one (research_gate action check); if a gate "
        "fails, first use explain_failure and actually fix the underlying work; request a "
        f"human override only if a real human decision is required.{push}\n"
        "When the task reaches PUBLISH, or there is genuinely nothing more you can do "
        "unassisted, stop and report a concise summary of what you did this turn and the "
        "current state / next step. Keep the summary under ~200 words."
    )


# ── time helpers (fixed-width UTC, lexicographically comparable) ─────────────
def iso_now() -> str:
    return _now_iso()


def _heartbeat_age_s(updated_at_iso: str | None) -> float:
    if not updated_at_iso:
        return float("inf")
    try:
        # ``fromisoformat`` (py3.11) accepts the fixed-width UTC "Z" suffix as a zone.
        value = datetime.fromisoformat(updated_at_iso)
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - value).total_seconds()


def _is_stale(updated_at_iso: str | None, window_s: float = LEASE_STALE_S) -> bool:
    return _heartbeat_age_s(updated_at_iso) > window_s


def _backoff_s(attempt: int) -> float:
    """Exponential backoff for a transient retry (2s, 4s, … capped at 30s)."""
    return min(2.0 ** max(attempt - 1, 0), 30.0)


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

    # -- single-flight claim (atomic CAS under the project lock) --------------
    def _claim(
        self, service, owner_id: UUID, task_id: str, *, run_id: str, turn_index: int
    ) -> DriverOutcome:
        """Atomically claim the right to run ``turn_index``, or return a drop/cancel outcome.

        Runs inside :meth:`ResearchService.atomic_update_project`, so two racing jobs are
        serialized by the file lock: exactly one may set ``turn_state = running``; the other
        observes the fresh ledger and drops. A ``running`` ledger whose heartbeat is stale is
        a crashed predecessor → attempt bumped (idempotent re-run of the same ``turn_index``).
        """
        box: dict[str, Any] = {"action": "claim"}

        def mutate(project: dict) -> None:
            active = project.get("active_run")
            if not active or active.get("status") != "RUNNING":
                box.update(action="dropped", reason="no active run for this task")
                return
            if active.get("run_id") != run_id:
                box.update(action="dropped", reason="stale job: active_run belongs to another run")
                return
            ledger = project.get("driver")
            if not isinstance(ledger, dict) or ledger.get("run_id") not in (None, run_id):
                box.update(action="dropped", reason="driver ledger belongs to another run")
                return

            state = ledger.get("turn_state")
            led_turn = int(ledger.get("turn_index") or 0)
            if state == "done":
                if turn_index != led_turn + 1:
                    box.update(
                        action="dropped",
                        reason=(
                            "duplicate/delayed job"
                            if turn_index <= led_turn
                            else "out-of-order job (turn gap)"
                        ),
                    )
                    return
                attempt = 1
            elif state in ("running", "pending"):
                if led_turn != turn_index:
                    box.update(action="dropped", reason="ledger is running a different turn")
                    return
                if state == "running" and not _is_stale(ledger.get("updated_at")):
                    # A fresh ``running`` ledger = a live twin job already executing this turn.
                    # It owns the cancel too (its lease watcher observes ``cancel_requested``),
                    # so stand aside rather than yanking the slot from under it.
                    box.update(action="dropped", reason="live duplicate (turn already claimed)")
                    return
                # Stale ``running`` remnant → crash recovery: bump the attempt, same turn.
                attempt = int(ledger.get("turn_attempt") or 1) + 1
            else:
                box.update(action="dropped", reason=f"unknown ledger turn_state: {state!r}")
                return

            if ledger.get("cancel_requested"):
                # Only a job this run is legitimately expecting (its next turn, or a crash
                # recovery of a stale remnant) reaches this point — so a requested Stop here
                # means *no live execution owns the slot* (the live owner would have observed
                # the cancel itself). Terminalize + release in the same atomic commit so a Stop
                # between turns never strands the task RUNNING.
                execution_id = f"{run_id}:{turn_index}:{attempt}"
                project["last_block"] = {
                    "kind": "cancelled",
                    "reason": "stop requested before this turn started",
                    "at": _now_iso(),
                    "run_id": run_id,
                    "execution_id": execution_id,
                }
                ledger.update(turn_state="done", updated_at=_now_iso())
                project["driver"] = ledger
                project.pop("active_run", None)
                box.update(action="cancelled")
                return

            execution_id = f"{run_id}:{turn_index}:{attempt}"
            ledger.update(
                turn_index=turn_index,
                turn_attempt=attempt,
                turn_state="running",
                execution_id=execution_id,
                updated_at=_now_iso(),
            )
            project["driver"] = ledger
            box.update(
                action="claim",
                turn_attempt=attempt,
                execution_id=execution_id,
                revision=int(project.get("project_revision", 0)),
            )

        try:
            service.atomic_update_project(owner_id, task_id, mutate)
        except RevisionConflictError:
            # Another process committed between our read and this CAS — a competing job won.
            return DriverOutcome(
                state=RunState.RUNNING, action="dropped", reason="revision conflict (another job claimed)"
            )

        action = box["action"]
        if action == "claim":
            return DriverOutcome(
                state=RunState.RUNNING,
                action="continue",
                run_id=run_id,
                turn_index=turn_index,
                turn_attempt=box["turn_attempt"],
                execution_id=box["execution_id"],
            )
        if action == "cancelled":
            return DriverOutcome(
                state=RunState.CANCELLED, action="cancelled", run_id=run_id,
                turn_index=turn_index, reason="cancel requested before this turn started",
            )
        try:
            state = observe_run_state(service.read_project(owner_id, task_id), run_id)
        except Exception:  # noqa: BLE001 - task may have been deleted under us
            state = RunState.IDLE
        return DriverOutcome(
            state=state,
            action="dropped", dropped=True, run_id=run_id, turn_index=turn_index,
            reason=box.get("reason"),
        )

    # -- lease/cancel watcher -------------------------------------------------
    async def _lease_watcher(
        self,
        service,
        owner_id: UUID,
        task_id: str,
        *,
        run_id: str,
        turn_index: int,
        cancel_event: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        """Refresh the heartbeat while a turn runs and flag an external cancel.

        While the turn executes this loop (a) refreshes ``driver.updated_at`` every
        ``LEASE_REFRESH_S`` so a live twin/dedup can't mistake us for a crash, and (b) polls
        ``driver.cancel_requested`` and raises ``cancel_event`` when the user pressed Stop.
        It stops the moment the turn finishes (``stop`` set) so it never touches the ledger
        of a *later* turn.
        """
        while not stop.is_set():
            try:
                await asyncio.sleep(LEASE_REFRESH_S)
            except asyncio.CancelledError:
                return
            try:
                ledger = service.get_driver_checkpoint(owner_id, task_id)
            except Exception as exc:  # noqa: BLE001 - watcher is advisory
                logger.warning("research lease watcher read failed: %s", exc)
                continue
            if ledger.get("run_id") not in (None, run_id) or ledger.get("turn_index") != turn_index:
                return  # ownership moved (new run / next turn) — stand down
            if ledger.get("cancel_requested"):
                cancel_event.set()
                return
            try:
                service.set_driver_checkpoint(
                    owner_id, task_id, patch={"updated_at": _now_iso()}
                )
            except Exception as exc:  # noqa: BLE001 - heartbeat is advisory
                logger.warning("research lease heartbeat failed: %s", exc)
                continue

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
        claimed = self._claim(service, owner_id, task_id, run_id=run_id, turn_index=turn_index)
        if claimed.action != "continue":
            return claimed

        attempt = claimed.turn_attempt
        execution_id = claimed.execution_id
        project = service.read_project(owner_id, task_id)
        before = await service.project_fingerprint(owner_id, task_id)

        # The on-disk ledger is authoritative for pre-turn facts.
        ledger = service.get_driver_checkpoint(owner_id, task_id)
        consecutive = int(ledger.get("consecutive_no_progress") or 0)
        cumulative = float(ledger.get("cumulative_cost_usd") or 0.0)

        final_answer: str | None = None
        cost_usd = 0.0
        interrupted_by_cancel = False

        # Retry loop for the LLM turn itself (transient errors only, exponential backoff).
        while True:
            prompt = auto_turn_prompt(
                task_name=project.get("name", task_id),
                project_id=task_id,
                stage=project.get("stage", "DISCOVER"),
                turn_index=turn_index,
                consecutive_no_progress=consecutive,
            )
            cancel_event = asyncio.Event()
            stop = asyncio.Event()
            watcher = asyncio.create_task(
                self._lease_watcher(
                    service, owner_id, task_id,
                    run_id=run_id, turn_index=turn_index,
                    cancel_event=cancel_event, stop=stop,
                )
            )
            run_task = asyncio.create_task(run_turn(prompt))
            cancel_waiter = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {run_task, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                # Whatever did not finish is torn down here so no task is left pending.
                stop.set()
                watcher.cancel()
                if not cancel_waiter.done():
                    cancel_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_waiter

            if run_task in done:
                try:
                    result = await run_task  # surface exceptions
                except asyncio.CancelledError:
                    interrupted_by_cancel = True
                    break
                except Exception as exc:
                    if is_transient_error(exc) and attempt < (self.max_attempts or 1):
                        attempt += 1
                        execution_id = f"{run_id}:{turn_index}:{attempt}"
                        self._persist_ledger(
                            service, owner_id, task_id,
                            patch={"turn_attempt": attempt, "execution_id": execution_id,
                                   "turn_state": "running", "updated_at": _now_iso()},
                        )
                        await asyncio.sleep(_backoff_s(attempt))
                        continue
                    # Transient retries exhausted → a *graded* terminal stop (slot released, job
                    # succeeds with run.error — the run never silently retries forever). A
                    # non-transient failure is equally terminalized, but re-raised so the job
                    # row fails honestly. Either way the slot is already released above.
                    transient = is_transient_error(exc)
                    reason = (
                        f"transient failures exhausted after {attempt} attempt(s): {exc}"
                        if transient
                        else f"turn failed: {exc}"
                    )
                    err_outcome = self._terminal_error(
                        service, owner_id, task_id,
                        reason=reason,
                        outcome=claimed, cumulative=cumulative, consecutive=consecutive,
                    )
                    if transient:
                        return err_outcome
                    raise
                else:
                    final_answer = result.final_answer
                    cost_usd = float(result.cost_usd or 0.0)
                    break
            else:
                # cancel_event fired → the watcher saw cancel_requested mid-turn.
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task
                interrupted_by_cancel = True
                break

        # ── post-turn grading ──
        cumulative += cost_usd

        # Re-read authoritative state: stage may have advanced, a human may have resolved an
        # override, or Stop may have been pressed while we ran.
        project = service.read_project(owner_id, task_id)
        ledger = service.get_driver_checkpoint(owner_id, task_id)
        facts = TurnFacts(
            stage=project.get("stage", "DISCOVER"),
            pending_overrides=len(service.pending_overrides(owner_id, task_id)),
            cancel_requested=bool(ledger.get("cancel_requested")) or interrupted_by_cancel,
            progress=before != await service.project_fingerprint(owner_id, task_id),
            consecutive_no_progress=consecutive,
            turn_index=turn_index,
            cumulative_cost_usd=cumulative,
            max_turns=self.max_turns,
            max_no_progress=self.max_no_progress,
            max_cost_usd=self.max_cost_usd,
        )
        grade = grade_turn(facts)

        if interrupted_by_cancel and not grade.state:
            grade = Grade(RunState.CANCELLED, "stop requested by the user", facts.consecutive_no_progress)

        if grade.state is None:
            # Continue: mark done (this turn), keep the slot, and let the worker enqueue N+1.
            self._persist_ledger(
                service, owner_id, task_id,
                patch={
                    "turn_state": "done",
                    "cumulative_cost_usd": cumulative,
                    "consecutive_no_progress": grade.consecutive_no_progress,
                    "execution_id": execution_id,
                    "next_scheduled": _now_iso(),
                    "updated_at": _now_iso(),
                },
            )
            return DriverOutcome(
                state=RunState.RUNNING, action="continue",
                run_id=run_id, turn_index=turn_index, turn_attempt=attempt,
                execution_id=execution_id, progress=facts.progress,
                cumulative_cost_usd=cumulative, final_answer=final_answer,
                next_turn_index=turn_index + 1,
                consecutive_no_progress=grade.consecutive_no_progress,
            )

        # Terminal stop.
        if not final_answer:
            final_answer = _terminal_message(grade.state, grade.reason)
        outcome = DriverOutcome(
            state=grade.state, action=grade.state.value,
            run_id=run_id, turn_index=turn_index, turn_attempt=attempt,
            execution_id=execution_id, progress=facts.progress,
            cumulative_cost_usd=cumulative, final_answer=final_answer,
            reason=grade.reason,
            consecutive_no_progress=grade.consecutive_no_progress,
        )
        self._finish_run(
            service, owner_id, task_id,
            outcome=outcome, reason=grade.reason or "",
            cumulative_cost_usd=cumulative,
            consecutive_no_progress=grade.consecutive_no_progress,
        )
        return outcome

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
