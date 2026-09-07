"""Lease acquisition semantics: granted / reclaimed / dropped / cancelled verdicts."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from workflow.leases import (
    STATE_DONE,
    STATE_PENDING,
    STATE_RUNNING,
    ExecutionIdentity,
    LeaseConfig,
    LeaseLedger,
    acquire,
    heartbeat_age_s,
    mark_done,
    renew,
)

CONFIG = LeaseConfig(refresh_s=20, stale_s=150)
NOW = "2026-09-07T12:00:00Z"


def _epoch(iso: str = NOW) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _done_ledger(index: int = 0) -> LeaseLedger:
    return LeaseLedger(
        run_id="r1", index=index, attempt=1, state=STATE_DONE,
        updated_at=NOW,
    )


def _acquire(ledger: LeaseLedger, index: int, *, iso: str = NOW, epoch: float | None = None):
    return acquire(
        ledger, run_id="r1", index=index, config=CONFIG,
        now_iso=iso, now_epoch=_epoch() if epoch is None else epoch,
    )


class TestIdentity:
    def test_mint_format_is_stable_and_reversible(self):
        ident = ExecutionIdentity(run_id="r1", index=3, attempt=2)
        assert ident.as_str() == "r1:3:2"
        assert ExecutionIdentity.parse("r1:3:2") == ident

    def test_identity_is_minted_right_aligned(self):
        # Mint format is index/attempt-pinned on the right; a colon-bearing run_id is
        # parsed right-aligned too (real run ids are UUIDs, so round-trip is exact).
        ident = ExecutionIdentity.parse("a:b:4:1")
        assert (ident.run_id, ident.index, ident.attempt) == ("a:b", 4, 1)


class TestGrantAndDrop:
    def test_next_index_grants_attempt_one(self):
        d = _acquire(_done_ledger(index=2), 3)
        assert d.action == "granted"
        assert d.identity == ExecutionIdentity("r1", 3, 1)
        assert d.ledger.state == STATE_RUNNING
        assert d.ledger.execution_id == "r1:3:1"
        assert d.ledger.updated_at == NOW

    def test_input_ledger_is_never_mutated(self):
        ledger = _done_ledger()
        d = _acquire(ledger, 1)
        assert dataclasses.replace(ledger) == ledger  # frozen + functional replace only
        assert d.ledger is not ledger

    @pytest.mark.parametrize("requested", [0, 1])
    def test_duplicate_or_delayed_is_dropped(self, requested):
        d = _acquire(_done_ledger(index=2), requested)
        assert d.action == "dropped"
        assert d.ledger is not None and d.ledger.index == 2  # untouched
        assert "duplicate" in (d.reason or "")

    def test_iteration_gap_is_dropped_as_out_of_order(self):
        d = _acquire(_done_ledger(index=0), 2)
        assert d.action == "dropped"
        assert "out-of-order" in (d.reason or "")

    def test_foreign_run_ledger_drops(self):
        foreign = dataclasses.replace(_done_ledger(), run_id="other")
        d = _acquire(foreign, 1)
        assert d.action == "dropped"
        assert "another run" in (d.reason or "")

    def test_live_twin_running_is_dropped_not_stolen(self):
        running = LeaseLedger(
            run_id="r1", index=2, attempt=1, state=STATE_RUNNING,
            execution_id="r1:2:1", updated_at=NOW,
        )
        d = _acquire(running, 2)  # fresh heartbeat -> live owner
        assert d.action == "dropped"
        assert "live duplicate" in (d.reason or "")
        assert d.ledger.state == STATE_RUNNING  # the twin's lease intact

    def test_running_a_different_iteration_is_dropped(self):
        running = LeaseLedger(
            run_id="r1", index=2, attempt=1, state=STATE_RUNNING, updated_at=NOW,
        )
        d = _acquire(running, 3)
        assert d.action == "dropped"
        assert "different iteration" in (d.reason or "")

    def test_unknown_state_is_dropped(self):
        weird = dataclasses.replace(_done_ledger(), state="levitating")
        d = _acquire(weird, 1)
        assert d.action == "dropped"
        assert "unknown lease state" in (d.reason or "")


class TestReclaim:
    def test_stale_running_reclaims_same_index_next_attempt(self):
        crashed = LeaseLedger(
            run_id="r1", index=2, attempt=1, state=STATE_RUNNING,
            execution_id="r1:2:1", updated_at="2026-09-07T11:00:00Z",
        )
        d = _acquire(crashed, 2)  # now_epoch = 12:00 > stale window
        assert d.action == "reclaimed"  # may execute, but as a crash-successor...
        assert d.identity == ExecutionIdentity("r1", 2, 2)  # ...with the attempt bumped
        assert d.ledger.execution_id == "r1:2:2"

    def test_pending_same_index_reclaims(self):
        parked = LeaseLedger(
            run_id="r1", index=2, attempt=1, state=STATE_PENDING, updated_at=NOW,
        )
        d = _acquire(parked, 2)
        assert d.action == "reclaimed"
        assert d.identity.attempt == 2

    def test_fresh_running_boundary_exactly_stale_s_grants_nothing(self):
        # A heartbeat exactly at the window edge is NOT stale (strict >).
        t0 = _epoch()
        edge = datetime.fromtimestamp(t0 - CONFIG.stale_s, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        running = LeaseLedger(
            run_id="r1", index=1, attempt=1, state=STATE_RUNNING, updated_at=edge,
        )
        d = _acquire(running, 1, epoch=t0)
        assert d.action == "dropped"


class TestCancelDecision:
    def test_cancel_on_next_expected_iteration_terminalizes_instead_of_running(self):
        armed = dataclasses.replace(_done_ledger(index=1), cancel_requested=True)
        d = _acquire(armed, 2)
        assert d.action == "cancelled"
        assert d.ledger.state == STATE_DONE  # settled, adapter now releases the slot
        assert d.identity == ExecutionIdentity("r1", 2, 1)
        assert "cancel requested" in (d.reason or "")

    def test_cancel_never_revives_a_dropped_job(self):
        armed = dataclasses.replace(_done_ledger(index=5), cancel_requested=True)
        d = _acquire(armed, 1)  # duplicate first — drop wins over cancel
        assert d.action == "dropped"
        assert d.ledger.cancel_requested is True  # flag preserved for the real next job


class TestRenewAndDone:
    def test_renew_only_touches_the_heartbeat(self):
        ledger = LeaseLedger(run_id="r1", index=3, attempt=2, state=STATE_RUNNING,
                             execution_id="r1:3:2", updated_at=NOW)
        later = "2026-09-07T12:00:20Z"
        out = renew(ledger, now_iso=later)
        assert out.updated_at == later
        assert dataclasses.replace(out, updated_at=ledger.updated_at) == ledger

    def test_mark_done_keeps_counters(self):
        ledger = LeaseLedger(run_id="r1", index=3, attempt=1, state=STATE_RUNNING,
                             updated_at=NOW)
        out = mark_done(ledger, execution_id="r1:3:1", now_iso=NOW)
        assert out.state == STATE_DONE and out.index == 3 and out.attempt == 1

    def test_heartbeat_age_handles_garbage(self):
        assert heartbeat_age_s(None, now_epoch=0.0) == float("inf")
        assert heartbeat_age_s("not-a-time", now_epoch=0.0) == float("inf")
        fresh = datetime.fromtimestamp(1000.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert heartbeat_age_s(fresh, now_epoch=1030.0 + 1) <= 31.0 + 1
        # never negative even if the stamp is from the "future"
        future = datetime.fromtimestamp(2000.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert heartbeat_age_s(future, now_epoch=1000.0) == 0.0
