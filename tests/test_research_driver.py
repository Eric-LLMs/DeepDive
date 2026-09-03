"""Tests for the Research auto-run driver (plugins/research/driver.py).

Uses the *real* file-backed :class:`ResearchService` on a tmp scratch dir so the single-flight
CAS (portalocker lock + project_revision) and the run slot semantics under test are the real
ones. Covers: the grade priority chain, no-progress→stall, turn/cost caps, transient
classification + backoff, duplicate-job drop, crash-recovery attempt bump, cancel between
turns, and the atomic CAS RevisionConflict.
"""
from __future__ import annotations

import time
from uuid import uuid4

import pytest
from core.config import settings

import plugins.research.driver as driver_module
from plugins.research.driver import (
    DriverOutcome,
    IllegalRunTransition,
    ProjectLockError,
    ResearchRunDriver,
    RevisionConflictError,
    RunState,
    RunTurnResult,
    TurnFacts,
    _backoff_s,
    auto_turn_prompt,
    check_transition,
    grade_turn,
    is_transient_error,
    iso_now,
    observe_run_state,
)
from plugins.research.plugin import ResearchService

OWNER = uuid4()

# Frozen staging so a "made progress" run_turn can bump the fingerprint cheaply.
_STAGES = ["FRAME", "EVIDENCE", "WRITE", "PUBLISH"]


def _iso(delta_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delta_s))


@pytest.fixture
def service(tmp_path) -> ResearchService:
    return ResearchService(drive=None, scratch_root=tmp_path / "scratch")


def _create_project(service: ResearchService, task_id: str, *, stage: str = "FRAME") -> None:
    pdir = service._project_dir(OWNER, task_id)
    pdir.mkdir(parents=True, exist_ok=True)
    service._save_json(
        pdir / "project.json",
        {
            "id": task_id,
            "owner_id": str(OWNER),
            "name": "driver-test",
            "profile": "research",
            "stage": stage,
            "gates": {},
            "project_revision": 0,
            "updated_at": _iso(0),
        },
    )
    service._save_json(pdir / "approvals.json", {"approvals": []})
    service._save_json(pdir / "executions.json", {"executions": []})
    service._save_json(pdir / "graph.json", {"nodes": [], "edges": []})


def _set_stage(service: ResearchService, task_id: str, stage: str) -> None:
    service.atomic_update_project(OWNER, task_id, lambda p: p.update(stage=stage))


def _ledger(service: ResearchService, task_id: str, **fields) -> None:
    """Rewrite the driver ledger directly (bypasses set_driver_checkpoint's forced now)."""
    base = service.get_driver_checkpoint(OWNER, task_id)

    def mutate(project: dict) -> None:
        merged = {**base, **fields}
        project["driver"] = merged

    service.atomic_update_project(OWNER, task_id, mutate)


# ── grade priority chain (pure) ──────────────────────────────────────────────
def test_grade_priority_cancel_beats_publish():
    grade = grade_turn(
        TurnFacts(stage="PUBLISH", cancel_requested=True, progress=True)
    )
    assert grade.state == RunState.CANCELLED


def test_grade_finish_on_publish():
    grade = grade_turn(TurnFacts(stage="PUBLISH", progress=True, max_turns=1))
    assert grade.state == RunState.FINISHED


def test_grade_pending_override_blocks_before_stall_and_caps():
    # Pending human gate beats the no-progress stall and the turn/cost caps.
    grade = grade_turn(
        TurnFacts(
            stage="EVIDENCE",
            pending_overrides=1,
            consecutive_no_progress=5,
            turn_index=99,
            cumulative_cost_usd=100.0,
            max_no_progress=2,
            max_turns=8,
            max_cost_usd=1.0,
        )
    )
    assert grade.state == RunState.BLOCKED
    assert "awaiting a human" in grade.reason


def test_grade_no_progress_continues_then_stalls():
    first = grade_turn(
        TurnFacts(
            stage="FRAME",
            consecutive_no_progress=0,
            max_no_progress=2,
            progress=False,
        )
    )
    assert first.state is None
    assert first.consecutive_no_progress == 1
    second = grade_turn(
        TurnFacts(
            stage="FRAME",
            consecutive_no_progress=first.consecutive_no_progress,
            max_no_progress=2,
            progress=False,
        )
    )
    assert second.state == RunState.STALLED
    assert "2 consecutive" in second.reason


def test_grade_progress_resets_no_progress_counter():
    grade = grade_turn(
        TurnFacts(
            stage="FRAME",
            consecutive_no_progress=1,
            progress=True,
            max_no_progress=2,
        )
    )
    assert grade.state is None
    assert grade.consecutive_no_progress == 0


def test_grade_turn_cap_blocks():
    grade = grade_turn(
        TurnFacts(stage="EVIDENCE", progress=True, turn_index=8, max_turns=8)
    )
    assert grade.state == RunState.BLOCKED
    assert "turn cap" in grade.reason


def test_grade_cost_cap_blocks():
    grade = grade_turn(
        TurnFacts(
            stage="EVIDENCE",
            progress=True,
            cumulative_cost_usd=5.0,
            max_cost_usd=5.0,
        )
    )
    assert grade.state == RunState.BLOCKED
    assert "cost cap" in grade.reason


# ── transient classification + backoff (pure) ────────────────────────────────
def test_is_transient_error():
    assert is_transient_error(TimeoutError("request timed out"))
    assert is_transient_error(RuntimeError("upstream returned 429"))
    assert is_transient_error(RuntimeError("502 Bad Gateway"))
    assert is_transient_error(ProjectLockError("could not lock"))
    assert not is_transient_error(RevisionConflictError("revision changed"))
    assert not is_transient_error(ValueError("bad argument"))


def test_backoff_is_exponential_capped():
    assert _backoff_s(1) == 1.0
    assert _backoff_s(2) == 2.0
    assert _backoff_s(3) == 4.0
    assert _backoff_s(9) == 30.0  # capped


# ── customs gate (pure) ──────────────────────────────────────────────────────
def test_run_state_terminal_is_irreversible():
    assert not RunState.IDLE.is_terminal
    assert not RunState.RUNNING.is_terminal
    for state in (RunState.FINISHED, RunState.BLOCKED, RunState.STALLED,
                  RunState.CANCELLED, RunState.ERROR):
        assert state.is_terminal


def test_check_transition_gate():
    check_transition(RunState.IDLE, RunState.RUNNING)
    check_transition(RunState.RUNNING, RunState.FINISHED)
    with pytest.raises(IllegalRunTransition):
        check_transition(RunState.FINISHED, RunState.RUNNING)
    with pytest.raises(IllegalRunTransition):
        check_transition(RunState.IDLE, RunState.FINISHED)


def test_observe_run_state():
    project = {"active_run": {"run_id": "r1", "status": "RUNNING"}}
    assert observe_run_state(project, "r1") == RunState.RUNNING
    assert observe_run_state(project, "r2") == RunState.IDLE
    assert observe_run_state({}, "r1") == RunState.IDLE


def test_auto_turn_prompt_embeds_state_and_never_asks_user():
    prompt = auto_turn_prompt(
        task_name="T", project_id="p1", stage="EVIDENCE", turn_index=3
    )
    assert "turn 3" in prompt and "EVIDENCE" in prompt and "`T`" in prompt
    assert "ask the user anything" in prompt
    assert "NOT create a new project" in prompt
    push = auto_turn_prompt(
        task_name="T", project_id="p1", stage="EVIDENCE", turn_index=4,
        consecutive_no_progress=1,
    )
    assert "made no visible progress" in push


# ── real-service: atomic CAS ─────────────────────────────────────────────────
def test_atomic_update_cas_revision_conflict(service):
    _create_project(service, "cas")
    service.atomic_update_project(OWNER, "cas", lambda p: p.setdefault("x", 1))
    with pytest.raises(RevisionConflictError):
        service.atomic_update_project(
            OWNER, "cas", lambda p: p.setdefault("y", 2), expected_revision=0
        )
    # The conflicting write never landed.
    project = service.read_project(OWNER, "cas")
    assert "y" not in project
    assert project["project_revision"] == 1


# ── real-service: claim / single-flight / crash recovery ─────────────────────
async def test_claim_duplicate_turn_drops(service):
    _create_project(service, "dup")
    run = service.begin_run(OWNER, "dup")
    run_id = run["run_id"]
    driver = ResearchRunDriver()
    # A completed turn 1 (the normal post-turn ledger).
    _ledger(service, "dup", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="done", execution_id=f"{run_id}:1:1")
    # A redelivered job for the same turn is a duplicate → dropped, never executed.
    outcome = driver._claim(service, OWNER, "dup", run_id=run_id, turn_index=1)
    assert outcome.dropped
    assert outcome.action == "dropped"
    assert "duplicate" in outcome.reason


async def test_claim_out_of_order_turn_drops(service):
    _create_project(service, "gap")
    run = service.begin_run(OWNER, "gap")
    run_id = run["run_id"]
    driver = ResearchRunDriver()
    _ledger(service, "gap", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="done", execution_id=f"{run_id}:1:1")
    outcome = driver._claim(service, OWNER, "gap", run_id=run_id, turn_index=5)
    assert outcome.dropped
    assert "out-of-order" in outcome.reason


async def test_claim_no_active_run_drops(service):
    _create_project(service, "idle")
    # No begin_run: slot never acquired.
    driver = ResearchRunDriver()
    outcome = driver._claim(service, OWNER, "idle", run_id="ghost", turn_index=1)
    assert outcome.dropped
    assert outcome.state == RunState.IDLE


async def test_claim_foreign_run_drops(service):
    _create_project(service, "foreign")
    run = service.begin_run(OWNER, "foreign")
    driver = ResearchRunDriver()
    outcome = driver._claim(
        service, OWNER, "foreign", run_id="another-run", turn_index=1
    )
    assert outcome.dropped
    assert "another run" in outcome.reason
    assert run["run_id"] != "another-run"


async def test_claim_live_duplicate_drops(service):
    _create_project(service, "live")
    run = service.begin_run(OWNER, "live")
    run_id = run["run_id"]
    driver = ResearchRunDriver()
    # A FRESH ``running`` ledger = a live twin already executing this turn.
    _ledger(service, "live", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{run_id}:1:1",
            updated_at=iso_now())
    outcome = driver._claim(service, OWNER, "live", run_id=run_id, turn_index=1)
    assert outcome.dropped
    assert "live duplicate" in outcome.reason


async def test_crash_recovery_bumps_attempt_same_turn(service):
    _create_project(service, "crash")
    run = service.begin_run(OWNER, "crash")
    run_id = run["run_id"]
    driver = ResearchRunDriver()
    # A STALE ``running`` remnant (heartbeat frozen > LEASE_STALE_S) = a crashed worker.
    _ledger(service, "crash", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{run_id}:1:1",
            updated_at=_iso(1000))
    outcome = driver._claim(service, OWNER, "crash", run_id=run_id, turn_index=1)
    assert not outcome.dropped
    assert outcome.action == "continue"
    assert outcome.turn_index == 1          # same business turn, never a new one
    assert outcome.turn_attempt == 2        # attempt bumped
    assert outcome.execution_id == f"{run_id}:1:2"


async def test_claim_cancel_between_turns_releases_slot(service):
    _create_project(service, "cancel")
    run = service.begin_run(OWNER, "cancel")
    run_id = run["run_id"]
    driver = ResearchRunDriver()
    # Turn 1 completed; Stop pressed before turn 2 started.
    _ledger(service, "cancel", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="done", execution_id=f"{run_id}:1:1",
            cancel_requested=True)
    outcome = driver._claim(service, OWNER, "cancel", run_id=run_id, turn_index=2)
    assert outcome.state == RunState.CANCELLED
    assert outcome.action == "cancelled"
    project = service.read_project(OWNER, "cancel")
    assert "active_run" not in project        # slot released atomically
    assert project["last_block"]["kind"] == "cancelled"


# ── real-service: end-to-end auto_turn ───────────────────────────────────────
async def _drive(
    service: ResearchService,
    task_id: str,
    run_turn,
    **driver_kwargs,
) -> tuple[DriverOutcome, dict]:
    """begin_run then drive exactly one auto_turn with a fresh driver."""
    run = service.begin_run(OWNER, task_id)
    driver = ResearchRunDriver(**driver_kwargs)
    outcome = await driver.auto_turn(
        service, owner_id=OWNER, task_id=task_id, run_id=run["run_id"],
        turn_index=1, run_turn=run_turn,
    )
    return outcome, driver


async def test_auto_turn_finishes_on_publish(service):
    _create_project(service, "pub", stage="WRITE")
    calls = []

    async def run_turn(prompt: str) -> RunTurnResult:
        calls.append(prompt)
        _set_stage(service, "pub", "PUBLISH")
        return RunTurnResult(final_answer="published", cost_usd=0.2)

    outcome, _ = await _drive(service, "pub", run_turn)
    assert outcome.action == "finished"
    assert outcome.state == RunState.FINISHED
    assert outcome.final_answer == "published"
    project = service.read_project(OWNER, "pub")
    assert "active_run" not in project                 # slot released
    assert project["last_block"]["kind"] == "finished"
    assert len(calls) == 1


async def test_auto_turn_continue_keeps_slot_and_schedules_next(service):
    _create_project(service, "cont", stage="FRAME")
    calls = []

    async def run_turn(prompt: str) -> RunTurnResult:
        calls.append(prompt)
        _set_stage(service, "cont", "EVIDENCE")        # visible progress
        return RunTurnResult(final_answer="advanced", cost_usd=0.1)

    outcome, _ = await _drive(service, "cont", run_turn)
    assert outcome.action == "continue"
    assert outcome.next_turn_index == 2
    assert outcome.progress is True
    project = service.read_project(OWNER, "cont")
    assert project["active_run"]["status"] == "RUNNING"   # slot kept for the chain
    assert project["driver"]["turn_state"] == "done"
    assert project["driver"]["consecutive_no_progress"] == 0


async def test_auto_turn_stalls_after_max_no_progress(service):
    _create_project(service, "stall", stage="FRAME")

    async def idle_turn(prompt: str) -> RunTurnResult:
        return RunTurnResult(final_answer="no change")    # makes no project mutation

    # Turn 1 makes no progress → continue (counter = 1).
    first, _ = await _drive(service, "stall", idle_turn, max_no_progress=2)
    assert first.action == "continue"
    assert first.consecutive_no_progress == 1
    assert service.read_project(OWNER, "stall")["active_run"]["status"] == "RUNNING"

    # Turn 2 (a fresh job on the kept slot) also makes none → stall (counter = 2).
    run = service.read_project(OWNER, "stall")["active_run"]
    driver = ResearchRunDriver(max_no_progress=2)
    second = await driver.auto_turn(
        service, owner_id=OWNER, task_id="stall", run_id=run["run_id"],
        turn_index=2, run_turn=idle_turn,
    )
    assert second.state == RunState.STALLED
    assert second.action == "stalled"
    project = service.read_project(OWNER, "stall")
    assert "active_run" not in project
    assert project["last_block"]["kind"] == "stalled"


async def test_auto_turn_blocks_at_turn_cap(service):
    _create_project(service, "cap", stage="FRAME")

    async def progress_turn(prompt: str) -> RunTurnResult:
        _set_stage(service, "cap", "EVIDENCE")
        return RunTurnResult(final_answer="progress", cost_usd=0.1)

    outcome, _ = await _drive(service, "cap", progress_turn, max_turns=1)
    assert outcome.state == RunState.BLOCKED
    assert "turn cap" in outcome.reason
    assert "active_run" not in service.read_project(OWNER, "cap")


async def test_auto_turn_redelivered_job_drops_without_running(service):
    _create_project(service, "redeliver", stage="FRAME")
    calls = []

    async def progress_turn(prompt: str) -> RunTurnResult:
        calls.append(prompt)
        _set_stage(service, "redeliver", "EVIDENCE")
        return RunTurnResult(final_answer="ok", cost_usd=0.1)

    first, _ = await _drive(service, "redeliver", progress_turn)
    assert first.action == "continue"
    # arq redelivers the SAME job (turn 1) after the first completed it.
    run = service.read_project(OWNER, "redeliver")["active_run"]
    driver = ResearchRunDriver()
    duplicate = await driver.auto_turn(
        service, owner_id=OWNER, task_id="redeliver", run_id=run["run_id"],
        turn_index=1, run_turn=progress_turn,
    )
    assert duplicate.dropped
    assert duplicate.action == "dropped"
    assert len(calls) == 1                              # the model was never re-invoked


async def test_auto_turn_crash_reruns_same_turn_idempotently(service):
    _create_project(service, "rerun", stage="FRAME")
    run = service.begin_run(OWNER, "rerun")
    run_id = run["run_id"]
    # Simulate the crashed predecessor's frozen running ledger at turn 1/attempt 1.
    _ledger(service, "rerun", run_id=run_id, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{run_id}:1:1",
            updated_at=_iso(1000))
    calls = []

    async def progress_turn(prompt: str) -> RunTurnResult:
        calls.append(prompt)
        _set_stage(service, "rerun", "EVIDENCE")
        return RunTurnResult(final_answer="recovered", cost_usd=0.1)

    driver = ResearchRunDriver()
    outcome = await driver.auto_turn(
        service, owner_id=OWNER, task_id="rerun", run_id=run_id,
        turn_index=1, run_turn=progress_turn,
    )
    assert outcome.turn_attempt == 2                    # crash rerun, attempt bumped
    assert outcome.execution_id == f"{run_id}:1:2"
    assert outcome.action == "continue"
    assert len(calls) == 1
    # The rerun is a single execution: the ledger records one attempt, not a duplicated turn.
    project = service.read_project(OWNER, "rerun")
    assert project["driver"]["turn_index"] == 1
    assert project["driver"]["turn_attempt"] == 2


async def test_auto_turn_transient_exhausted_becomes_error(service, monkeypatch):
    _create_project(service, "xerr", stage="FRAME")
    monkeypatch.setattr(driver_module, "_backoff_s", lambda attempt: 0.0)
    calls = []

    async def failing_turn(prompt: str) -> RunTurnResult:
        calls.append(prompt)
        raise TimeoutError("upstream request timed out")

    # max_attempts=2 → first attempt fails (transient, retry #2), second attempt fails too.
    outcome, _ = await _drive(service, "xerr", failing_turn, max_attempts=2)
    assert outcome.state == RunState.ERROR
    assert outcome.action == "error"
    assert len(calls) == 2
    assert "exhausted" in outcome.reason
    assert "active_run" not in service.read_project(OWNER, "xerr")   # slot released


def test_settings_driver_defaults_present():
    # Guard the config knobs the driver reads at construction actually exist.
    assert settings.research_driver_max_turns >= 1
    assert settings.research_driver_max_no_progress_turns >= 1
    assert settings.research_driver_turn_max_steps >= 1
