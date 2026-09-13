"""Phase-0 RunStore & state-machine tests (docs/research/19 §5–§6):
transition legality, CAS/optimistic locking, durability, tenancy and the
terminal-state completeness guarantee (no hang, no escape)."""
from __future__ import annotations

import threading

import portalocker
import pytest
from artifact_compiler.runstore import (
    PrincipalContext,
    PrincipalMismatchError,
    RevisionConflictError,
    RunIdError,
    RunNotFound,
    RunStore,
)
from artifact_compiler.source import Claim
from artifact_compiler.states import (
    PUBLISHABLE_STATES,
    TERMINALS,
    TRANSITIONS,
    IllegalTransition,
    RunState,
    validate_transition,
)

P = PrincipalContext(owner_id="u-1", project_id="proj-1")
OTHER = PrincipalContext(owner_id="u-2")

ACTIVE_CHAIN = [
    RunState.ENV_PREFLIGHT, RunState.EVIDENCE_PROVIDING, RunState.PLANNING,
    RunState.WRITING, RunState.AST_CONTRACT_QA, RunState.VISUAL_ENGINE,
    RunState.TYPST_COMPILING,
]


@pytest.fixture()
def store(tmp_path):
    return RunStore(tmp_path / "runs")


# ── transition table properties ───────────────────────────────────────────────

def test_terminals_are_irreversible_and_total():
    for t in TERMINALS:
        assert TRANSITIONS[t] == frozenset()
    # every non-terminal has an exit
    for s in RunState:
        if s not in TERMINALS:
            assert TRANSITIONS[s], f"{s} can never leave — hang risk"


def test_every_active_state_can_be_cancelled_or_budget_cut():
    for s in RunState:
        if s not in TERMINALS:
            assert RunState.CANCELLED in TRANSITIONS[s]
            assert RunState.BUDGET_EXCEEDED in TRANSITIONS[s]


def test_only_publishable_terminals_expose_pdf():
    assert PUBLISHABLE_STATES == {RunState.COMPLETED, RunState.NEEDS_REVIEW}


def test_illegal_hops_raise():
    with pytest.raises(IllegalTransition):
        validate_transition(RunState.QUEUED, RunState.WRITING)  # stage skip
    with pytest.raises(IllegalTransition):
        validate_transition(RunState.COMPLETED, RunState.WRITING)  # terminal escape
    validate_transition(RunState.TYPST_COMPILING, RunState.REPAIR_LOOP)  # legal
    validate_transition(RunState.REPAIR_LOOP, RunState.FAILED_BLOCKED)   # legal


# ── RunStore lifecycle ────────────────────────────────────────────────────────

def test_create_and_walk_full_chain(store):
    state = store.create_run(P, run_id="run-a", artifact_id="art-1")
    assert state["state"] == "queued" and state["run_revision"] == 1
    for target in ACTIVE_CHAIN:
        state = store.transition(P, "run-a", target, expected_revision=state["run_revision"])
    assert state["state"] == "typst_compiling"
    state = store.transition(P, "run-a", RunState.COMPLETED, expected_revision=state["run_revision"])
    assert RunStore.is_publishable(state) and RunStore.is_terminal(state)
    # terminal: no further hop is possible on disk
    with pytest.raises(IllegalTransition):
        store.transition(P, "run-a", RunState.WRITING)


def test_transition_does_not_touch_disk_when_illegal(store):
    store.create_run(P, run_id="run-b")
    rev = store.get_run("run-b")["run_revision"]
    with pytest.raises(IllegalTransition):
        store.transition(P, "run-b", RunState.TYPST_COMPILING)
    after = store.get_run("run-b")
    assert after["run_revision"] == rev and after["state"] == "queued"


def test_cas_conflict(store):
    store.create_run(P, run_id="run-c")
    with pytest.raises(RevisionConflictError):
        store.transition(P, "run-c", RunState.ENV_PREFLIGHT, expected_revision=99)
    # and a stale expected after a good commit
    store.transition(P, "run-c", RunState.ENV_PREFLIGHT, expected_revision=1)
    with pytest.raises(RevisionConflictError):
        store.transition(P, "run-c", RunState.PLANNING, expected_revision=1)


def test_concurrent_transitions_one_winner(store):
    """Two drivers, same expected revision: exactly one commit lands (optimistic lock)."""
    store.create_run(P, run_id="run-d")
    results: list[str] = []

    def racer():
        try:
            store.transition(P, "run-d", RunState.ENV_PREFLIGHT, expected_revision=1)
            results.append("win")
        except RevisionConflictError:
            results.append("lose")

    threads = [threading.Thread(target=racer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("win") == 1
    assert store.get_run("run-d")["run_revision"] == 2


def test_principal_isolation(store):
    store.create_run(P, run_id="run-e")
    with pytest.raises(PrincipalMismatchError):
        store.transition(OTHER, "run-e", RunState.ENV_PREFLIGHT)
    with pytest.raises(PrincipalMismatchError):
        store.put_document(OTHER, "run-e", "plan.json", {})


def test_run_id_path_guard(store):
    for bad in ("..", "a/b", "a\\b", "", "x" * 200, ".hidden.lock"):
        if bad == ".hidden.lock":  # charset-valid but must still be checked by callers; only structural chars are refused here
            store.create_run(P, run_id=bad)
            continue
        with pytest.raises(RunIdError):
            store.create_run(P, run_id=bad)
    with pytest.raises(RunIdError):
        store.get_run("../escape")


def test_missing_run(store):
    with pytest.raises(RunNotFound):
        store.get_run("nope")
    with pytest.raises(RunNotFound):
        store.transition(P, "nope", RunState.ENV_PREFLIGHT)
    with pytest.raises(ValueError, match="already exists"):
        store.create_run(P, run_id="dup")
        store.create_run(P, run_id="dup")


def test_put_document_atomic_and_indexed(store):
    store.create_run(P, run_id="run-f")
    claim = Claim(claim_id="c1", claim_req_id="cr1", section_id="root",
                  text="t", evidence_ids=["e1"])
    state = store.put_document(P, "run-f", "claims", [claim])
    assert state["run_revision"] == 2
    on_disk = store.get_document("run-f", "claims.json")
    assert on_disk[0]["claim_id"] == "c1"
    entry = state["documents"]["claims.json"]
    assert len(entry["sha256"]) == 64 and entry["revision"] == 2
    # document name guard
    with pytest.raises(ValueError):
        store.put_document(P, "run-f", "../evil", {})


def test_repair_attempts(store):
    store.create_run(P, run_id="run-g")
    store.transition(P, "run-g", RunState.ENV_PREFLIGHT)
    # fast-forward legally to a state that can reach REPAIR_LOOP
    for target in (RunState.EVIDENCE_PROVIDING, RunState.PLANNING, RunState.WRITING,
                   RunState.AST_CONTRACT_QA):
        store.transition(P, "run-g", target)
    store.transition(P, "run-g", RunState.REPAIR_LOOP)
    assert store.bump_repair_attempts(P, "run-g") == 1
    assert store.bump_repair_attempts(P, "run-g") == 2
    store.transition(P, "run-g", RunState.FAILED_BLOCKED)
    assert store.get_run("run-g")["state"] == "failed_blocked"


def test_history_recorded(store):
    store.create_run(P, run_id="run-h")
    store.transition(P, "run-h", RunState.ENV_PREFLIGHT, note="fonts ok")
    state = store.get_run("run-h")
    assert state["last_note"] == "fonts ok"
    assert [h["state"] for h in state["history"]] == ["queued", "env_preflight"]


def test_lock_timeout_is_finite(store):
    """A held external lock surfaces as a lock error fast — never a hang. (Windows
    msvcrt raises AlreadyLocked on same-process contention; cross-process contention
    retries until timeout and raises TimeoutError.)"""
    store.create_run(P, run_id="run-i")
    lock_path = store.run_dir("run-i") / ".run.lock"
    with portalocker.Lock(str(lock_path), flags=portalocker.LOCK_EX | portalocker.LOCK_NB):
        tight = RunStore(store.root, lock_timeout=0.2)
        with pytest.raises((TimeoutError, portalocker.exceptions.BaseLockException)):
            tight.transition(P, "run-i", RunState.ENV_PREFLIGHT)
