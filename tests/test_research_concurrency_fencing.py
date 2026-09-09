"""Concurrency / fencing tests for the Stop-latency fixes (F1-a/F1-b, F2, F3, W3).

Every test uses the *real* file-backed :class:`ResearchService` (portalocker +
project_revision CAS) so the claims exercised here are the production claims:

* F1-b/F1-a — ``plan_cancel_wake`` replays exactly the ``acquire`` state machine
  (live = no wake; stale-running = SAME index; done = NEXT index), and the woken
  job is consumed by the lease (cancelled verdict / live-duplicate drop);
* F2 — lease-layer fencing (renew / mark_done / re-arm no-op for a superseded
  execution), the authority seam inside ``atomic_update_project`` (mutate never
  runs when the fence is broken), the pre-write entry assert, and the adapter's
  OwnershipLost → silent-drop folding inside ``auto_turn``;
* F3 — read-only staleness derivation (``_run_stale`` / ``_task_view["run_stale"]``);
* W3 — settled-but-unfinalized cancel is recovered IMMEDIATELY at adoption while
  ordinary crashed-resumable slots keep the stale window (red line 8).

Matrix-cell coverage for the audit's 7-cell concurrency table:
live-twin×wake (drop), stale-twin×wake (reclaim), cancel×pre-turn, cancel×mid-turn
settle (seam), zombie×terminal settle (fenced mark_done), Run×Run (slot mutex),
Stop×Stop (idempotent flag + single-consumer verdict).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from uuid import uuid4

import pytest
from workflow.leases import (
    STATE_DONE,
    STATE_RUNNING,
    LeaseLedger,
    mark_done,
    renew,
)

import plugins.research.driver as driver_module
from plugins.research.driver import ResearchRunDriver, RunState, RunTurnResult
from plugins.research.plugin import (
    OwnershipLost,
    ResearchService,
    _run_stale,
    _settled_cancel_needs_finalize,
    clear_auto_run_fence,
    get_auto_run_fence,
    plan_cancel_wake,
    set_auto_run_fence,
)

OWNER = uuid4()


def _iso(delta_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delta_s))


@pytest.fixture
def service(tmp_path) -> ResearchService:
    return ResearchService(drive=None, scratch_root=tmp_path / "scratch")


def _create_project(service: ResearchService, task_id: str, *, stage: str = "FRAME") -> None:
    pdir = service._project_dir(OWNER, task_id)
    pdir.mkdir(parents=True, exist_ok=True)
    service._save_json(pdir / "project.json", {
        "id": task_id, "owner_id": str(OWNER), "name": "fence-test",
        "profile": "research", "stage": stage, "status": "ACTIVE", "gates": {},
        "project_revision": 0, "updated_at": _iso(0),
    })
    service._save_json(pdir / "approvals.json", {"approvals": []})
    service._save_json(pdir / "executions.json", {"executions": []})
    service._save_json(pdir / "graph.json", {"nodes": [], "edges": []})


def _ledger(service: ResearchService, task_id: str, **fields) -> None:
    base = service.get_driver_checkpoint(OWNER, task_id)

    def mutate(project: dict) -> None:
        project["driver"] = {**base, **fields}

    service.atomic_update_project(OWNER, task_id, mutate)


# ── F1-b: the wake planner replays the acquire state machine ─────────────────
def test_plan_live_lease_no_nudge(service):
    _create_project(service, "live")
    run = service.begin_run(OWNER, "live")
    rid = run["run_id"]
    service.request_cancel(OWNER, "live")
    _ledger(service, "live", run_id=rid, turn_index=3, turn_attempt=1,
            turn_state="running", execution_id=f"{rid}:3:1", updated_at=_iso(5))
    plan = plan_cancel_wake(service.read_project(OWNER, "live"))
    assert plan["nudge"] is False and plan["effective"] == "live"


def test_plan_stale_running_replays_same_index(service):
    _create_project(service, "stale")
    run = service.begin_run(OWNER, "stale")
    rid = run["run_id"]
    service.request_cancel(OWNER, "stale")
    _ledger(service, "stale", run_id=rid, turn_index=4, turn_attempt=2,
            turn_state="running", execution_id=f"{rid}:4:2", updated_at=_iso(400))
    plan = plan_cancel_wake(service.read_project(OWNER, "stale"))
    # NEVER a blind +1: the reclaim target is the SAME iteration index.
    assert plan == {
        "nudge": True, "effective": "pending-reclaim", "turn_index": 4, "run_id": rid,
        "reason": plan["reason"],
    }


def test_plan_done_lease_next_index(service):
    _create_project(service, "done")
    run = service.begin_run(OWNER, "done")
    rid = run["run_id"]
    service.request_cancel(OWNER, "done")
    _ledger(service, "done", run_id=rid, turn_index=2, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:2:1", updated_at=_iso(1))
    plan = plan_cancel_wake(service.read_project(OWNER, "done"))
    assert plan["nudge"] and plan["effective"] == "pending-arrival"
    assert plan["turn_index"] == 3  # the NEXT *expected* index per acquire's done rule


def test_plan_idle_no_nudge(service):
    _create_project(service, "idle")
    plan = plan_cancel_wake(service.read_project(OWNER, "idle"))
    assert plan["nudge"] is False and plan["effective"] == "idle"


# ── F1-b: a woken job is consumed by the lease exactly as planned ────────────
def test_wake_on_stale_running_terminalizes_cancel(service):
    _create_project(service, "wr")
    run = service.begin_run(OWNER, "wr")
    rid = run["run_id"]
    service.request_cancel(OWNER, "wr")
    _ledger(service, "wr", run_id=rid, turn_index=4, turn_attempt=2,
            turn_state="running", execution_id=f"{rid}:4:2", updated_at=_iso(400))
    plan = plan_cancel_wake(service.read_project(OWNER, "wr"))
    outcome = ResearchRunDriver()._claim(
        service, OWNER, "wr", run_id=rid, turn_index=plan["turn_index"])
    assert outcome.state == RunState.CANCELLED and not outcome.dropped
    assert "cancel" in (outcome.reason or "")


def test_wake_on_done_lease_terminalizes_without_executing(service):
    _create_project(service, "wd")
    run = service.begin_run(OWNER, "wd")
    rid = run["run_id"]
    service.request_cancel(OWNER, "wd")
    _ledger(service, "wd", run_id=rid, turn_index=2, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:2:1", updated_at=_iso(1))
    plan = plan_cancel_wake(service.read_project(OWNER, "wd"))
    outcome = ResearchRunDriver()._claim(
        service, OWNER, "wd", run_id=rid, turn_index=plan["turn_index"])
    assert outcome.state == RunState.CANCELLED


def test_duplicate_wake_drops_as_live_twin(service):
    _create_project(service, "dw")
    run = service.begin_run(OWNER, "dw")
    rid = run["run_id"]
    service.request_cancel(OWNER, "dw")
    _ledger(service, "dw", run_id=rid, turn_index=3, turn_attempt=1,
            turn_state="running", execution_id=f"{rid}:3:1", updated_at=_iso(5))
    # Two wake jobs racing a LIVE owner: the first one that arrives would see the
    # owner's fresh heartbeat and must drop — never yank the slot from under it.
    for _ in range(2):
        outcome = ResearchRunDriver()._claim(
            service, OWNER, "dw", run_id=rid, turn_index=3)
        assert outcome.dropped and "live duplicate" in outcome.reason


async def test_stop_stop_single_consumer_finalizes_once(service):
    # Stop×Stop: the flag is idempotent; whichever job claims the lease terminalizes
    # and pops the slot; every later job (and every later Stop) observes no slot.
    _create_project(service, "ss")
    run = service.begin_run(OWNER, "ss")
    rid = run["run_id"]
    service.request_cancel(OWNER, "ss")
    service.request_cancel(OWNER, "ss")  # idempotent
    _ledger(service, "ss", run_id=rid, turn_index=1, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:1:1", updated_at=_iso(1))
    driver = ResearchRunDriver()
    first = await driver.auto_turn(
        service, owner_id=OWNER, task_id="ss", run_id=rid, turn_index=2,
        run_turn=_never_turn,
    )
    assert first.state == RunState.CANCELLED and first.action == "cancelled"
    second = await driver.auto_turn(
        service, owner_id=OWNER, task_id="ss", run_id=rid, turn_index=2,
        run_turn=_never_turn,
    )
    assert second.dropped
    project = service.read_project(OWNER, "ss")
    assert project.get("active_run") is None
    assert project["last_block"]["kind"] == "cancelled"
    assert project["last_block"]["run_id"] == rid


async def _never_turn(prompt: str) -> RunTurnResult:
    raise AssertionError("a cancelled claim must never reach the executor")


# ── F2: lease-layer fencing (pure) ───────────────────────────────────────────
def test_renew_and_mark_done_fenced_for_superseded_execution():
    cfg_owner = LeaseLedger(run_id="r", index=1, attempt=2, state=STATE_RUNNING,
                            execution_id="r:1:2", updated_at=_iso(0))
    # A zombie (attempt 1) heartbeat must be a no-op against the live holder (attempt 2).
    assert renew(cfg_owner, now_iso=_iso(0), owner_execution="r:1:1") is cfg_owner
    assert mark_done(cfg_owner, now_iso=_iso(0), owner_execution="r:1:1") is cfg_owner
    # The rightful owner still settles.
    settled = mark_done(cfg_owner, now_iso=_iso(0), owner_execution="r:1:2")
    assert settled.state == STATE_DONE
    fresh = renew(cfg_owner, now_iso=_iso(0), owner_execution="r:1:2")
    assert fresh.updated_at == _iso(0) and fresh.state == STATE_RUNNING


# ── F2: the authority seam inside atomic_update_project ──────────────────────
def test_seam_refuses_commit_after_reclaim(service):
    _create_project(service, "seam")
    run = service.begin_run(OWNER, "seam")
    rid = run["run_id"]
    _ledger(service, "seam", run_id=rid, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{rid}:1:1", updated_at=_iso(0))
    token = set_auto_run_fence(owner_id=OWNER, task_id="seam", run_id=rid,
                               execution_id=f"{rid}:1:1")
    try:
        # Owner still holds the lease → the fenced commit is accepted untouched.
        service.atomic_update_project(
            OWNER, "seam", lambda p: p.update(marker="owner-ok"))
        assert service.read_project(OWNER, "seam")["marker"] == "owner-ok"
        # A successor reclaims this iteration (attempt 2).
        _ledger(service, "seam", turn_attempt=2, execution_id=f"{rid}:1:2")
        with pytest.raises(OwnershipLost):
            service.atomic_update_project(
                OWNER, "seam", lambda p: p.update(marker="zombie-dirty"))
        assert service.read_project(OWNER, "seam")["marker"] == "owner-ok"  # mutate NEVER ran
    finally:
        clear_auto_run_fence(token)
    assert get_auto_run_fence() is None
    # Outside a turn (no fence) the same commit is accepted.
    service.atomic_update_project(OWNER, "seam", lambda p: p.update(marker="api-path"))
    assert service.read_project(OWNER, "seam")["marker"] == "api-path"


def test_entry_assert_refores_superseded_writer(service):
    _create_project(service, "ea")
    run = service.begin_run(OWNER, "ea")
    rid = run["run_id"]
    _ledger(service, "ea", run_id=rid, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{rid}:1:1", updated_at=_iso(0))
    token = set_auto_run_fence(owner_id=OWNER, task_id="ea", run_id=rid,
                               execution_id=f"{rid}:1:1")
    try:
        service.assert_auto_run_authority(OWNER, "ea")  # intact → passes
        _ledger(service, "ea", execution_id=f"{rid}:1:2")  # reclaim
        with pytest.raises(OwnershipLost):
            service.assert_auto_run_authority(OWNER, "ea")
        # A fence targeting ANOTHER task exempts this one.
        other = set_auto_run_fence(owner_id=OWNER, task_id="elsewhere", run_id="x",
                                   execution_id="x:1:1")
        try:
            service.assert_auto_run_authority(OWNER, "ea")
        finally:
            clear_auto_run_fence(other)
    finally:
        clear_auto_run_fence(token)


async def test_auto_turn_folds_mid_turn_ownership_loss_to_drop(service):
    # The successor reclaims the iteration WHILE our turn executes: every post-
    # iteration commit is fenced → the job folds to a silent drop and writes
    # NOTHING (the successor owns the fate).
    _create_project(service, "mr")
    run = service.begin_run(OWNER, "mr")
    rid = run["run_id"]

    async def run_turn(prompt: str) -> RunTurnResult:
        # Simulate the reclaim landing during execution (lease heartbeat lapsed +
        # a successor identity on the same index).
        _ledger(service, "mr", turn_attempt=2, turn_state="running",
                execution_id=f"{rid}:1:2", updated_at=_iso(0))
        return RunTurnResult(final_answer="done", cost_usd=0.01)

    outcome = await ResearchRunDriver().auto_turn(
        service, owner_id=OWNER, task_id="mr", run_id=rid, turn_index=1,
        run_turn=run_turn,
    )
    assert outcome.dropped and outcome.action == "dropped"
    assert "ownership lost" in (outcome.reason or "")
    project = service.read_project(OWNER, "mr")
    # The successor's identity stands — the zombie wrote no meters, no verdict.
    assert project["driver"]["execution_id"] == f"{rid}:1:2"
    assert project["driver"].get("turn_state") == "running"
    assert project.get("active_run") is not None
    assert project["driver"].get("cumulative_cost_usd") in (None, 0, 0.0)


async def test_auto_turn_folds_tool_side_fence_trip(service):
    # A tool's write trips the seam mid-turn (OwnershipLost escapes the executor):
    # ExecutionFailed(OwnershipLost) must fold to a drop, NOT a terminal ERROR.
    _create_project(service, "ts")
    run = service.begin_run(OWNER, "ts")
    rid = run["run_id"]
    _ledger(service, "ts", run_id=rid, turn_index=0, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:0:1", updated_at=_iso(0))

    async def run_turn(prompt: str) -> RunTurnResult:
        raise OwnershipLost("simulated seam refusal inside a tool commit")

    outcome = await ResearchRunDriver().auto_turn(
        service, owner_id=OWNER, task_id="ts", run_id=rid, turn_index=1,
        run_turn=run_turn,
    )
    assert outcome.dropped
    assert "fenced" in (outcome.reason or "")
    project = service.read_project(OWNER, "ts")
    assert project.get("active_run") is not None
    assert project.get("last_block") is None  # never terminalized by a zombie


# ── F3: read-only staleness derivation ───────────────────────────────────────
def test_run_stale_derivation(service):
    _create_project(service, "rs")
    assert _run_stale(service.read_project(OWNER, "rs")) is False
    run = service.begin_run(OWNER, "rs")
    rid = run["run_id"]
    _ledger(service, "rs", run_id=rid, turn_index=1, turn_attempt=1,
            turn_state="running", execution_id=f"{rid}:1:1", updated_at=_iso(5))
    assert _run_stale(service.read_project(OWNER, "rs")) is False
    _ledger(service, "rs", updated_at=_iso(400))
    project = service.read_project(OWNER, "rs")
    assert _run_stale(project) is True
    # The view carries it for the desktop badge; no state was changed to say so.
    assert ResearchService._task_view(project)["run_stale"] is True
    assert service.read_project(OWNER, "rs")["active_run"]["run_id"] == rid


# ── W3: settled-but-unfinalized cancel recovered at adoption ────────────────
def test_w3_signature_only_for_matching_run(service):
    _create_project(service, "w3")
    run = service.begin_run(OWNER, "w3")
    rid = run["run_id"]
    service.request_cancel(OWNER, "w3")
    _ledger(service, "w3", run_id=rid, turn_index=2, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:2:1", updated_at=_iso(0))
    project = service.read_project(OWNER, "w3")
    assert _settled_cancel_needs_finalize(project, project["active_run"]) is True
    # The moment terminalization lands (last_block names the run) it is no longer a
    # crash signature — adoption of a FINISHED run is a different question entirely.
    service.atomic_update_project(
        OWNER, "w3",
        lambda p: p.update(last_block={"kind": "cancelled", "run_id": rid}))
    project = service.read_project(OWNER, "w3")
    assert _settled_cancel_needs_finalize(project, project["active_run"]) is False


def test_w3_begin_run_recovers_immediately_but_not_ordinary_stale(service):
    _create_project(service, "w3b")
    run = service.begin_run(OWNER, "w3b")
    rid = run["run_id"]
    service.request_cancel(OWNER, "w3b")
    _ledger(service, "w3b", run_id=rid, turn_index=2, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:2:1", updated_at=_iso(0))
    # Young slot (started_at now) — a plain begin_run would 409 without W3.
    new_run = service.begin_run(OWNER, "w3b")
    assert new_run["run_id"] != rid
    project = service.read_project(OWNER, "w3b")
    assert project["last_block"]["kind"] == "cancelled"
    assert project["last_block"]["run_id"] == rid
    assert project["active_run"]["run_id"] == new_run["run_id"]
    # Edition isolation: the adopted run's slot is live and its cancel flag was
    # RESET by the fresh ledger — the next begin_run 409s like any live run.
    with pytest.raises(ValueError, match="already running"):
        service.begin_run(OWNER, "w3b")

    # Red line 8: an ordinary crashed-resumable slot is NOT rescued early.
    _create_project(service, "w3o")
    run2 = service.begin_run(OWNER, "w3o")
    with pytest.raises(ValueError, match="already running"):
        service.begin_run(OWNER, "w3o", stale_after_seconds=3600)
    # ...and after the window it is adopted exactly as before. (fixed-width second
    # stamps can tie at 0 — -2s guarantees the cutoff passes started_at.)
    adopted = service.begin_run(OWNER, "w3o", stale_after_seconds=-2)
    assert adopted["run_id"] != run2["run_id"]


# ── Run×Run: the single-slot mutex ───────────────────────────────────────────
async def test_two_auto_turns_same_index_single_winner(service):
    _create_project(service, "rr")
    run = service.begin_run(OWNER, "rr")
    rid = run["run_id"]
    _ledger(service, "rr", run_id=rid, turn_index=0, turn_attempt=1,
            turn_state="done", execution_id=f"{rid}:0:1", updated_at=_iso(0))

    async def run_turn(prompt: str) -> RunTurnResult:
        await asyncio.sleep(0.05)
        return RunTurnResult(final_answer="ok", cost_usd=0.0)

    driver = ResearchRunDriver(max_turns=10)
    a, b = await asyncio.gather(
        driver.auto_turn(service, owner_id=OWNER, task_id="rr", run_id=rid,
                         turn_index=1, run_turn=run_turn),
        driver.auto_turn(service, owner_id=OWNER, task_id="rr", run_id=rid,
                         turn_index=1, run_turn=run_turn),
    )
    dropped = [o for o in (a, b) if o.dropped]
    # Exactly one holds the lease for the iteration; the twin folds to a drop.
    assert len(dropped) >= 1
    survivor = (b if a.dropped else a)
    assert survivor.action in ("continue", "dropped")
    project = service.read_project(OWNER, "rr")
    assert project["driver"]["run_id"] == rid
    assert project.get("active_run") is not None
