"""Lease mechanics: single-writer slot ownership for one workflow execution.

A run's progress ledger says which iteration is pending/running/done and who owns it.
Exactly one worker may hold the lease for a given iteration: :func:`acquire` is the pure
decision core of that contest (the adapter runs it inside its own atomic commit). The
three outcomes an arriving job can get mirror mature engines —

* ``granted``   — the ledger expects exactly this iteration (Temporal "start an activity
  task", Conductor "poll and receive the next task");
* ``reclaimed`` — the previous owner's heartbeat lapsed (crash): same iteration, next
  attempt — the lease analogue of an activity retry after heartbeat timeout;
* ``dropped``   — the job is a stale twin / duplicate / out-of-order delivery: do nothing
  (a fresh ``running`` lease means a LIVE owner is already executing this iteration —
  two workers must never run it in parallel);
* ``cancelled`` — an external cancel was requested and no live owner holds the slot, so
  the arriving job terminalizes the execution with it instead of running.

Heartbeats (:func:`renew`) keep a live owner from looking crashed; the adapter's watcher
task drives them. Attempt stays an integer *field* of the lease (no standalone entity).
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Literal

LeaseAction = Literal["granted", "reclaimed", "cancelled", "dropped"]

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"


@dataclasses.dataclass(frozen=True)
class LeaseConfig:
    refresh_s: float = 20.0   # heartbeat cadence while an iteration executes
    stale_s: float = 150.0    # a running lease quieter than this is presumed crashed

    def is_stale(self, updated_at_iso: str | None, *, now_epoch: float) -> bool:
        return heartbeat_age_s(updated_at_iso, now_epoch=now_epoch) > self.stale_s


@dataclasses.dataclass(frozen=True)
class ExecutionIdentity:
    """Deterministic identity of one iteration attempt: minted, stored, and re-minted on
    reclaim; never random (a crash rerun must replay the SAME id for idempotent ledgers).
    """

    run_id: str
    index: int
    attempt: int

    def as_str(self) -> str:
        return f"{self.run_id}:{self.index}:{self.attempt}"

    @classmethod
    def parse(cls, value: str) -> ExecutionIdentity:
        run_id, index, attempt = value.rsplit(":", 2)
        return cls(run_id=run_id, index=int(index), attempt=int(attempt))


@dataclasses.dataclass(frozen=True)
class LeaseLedger:
    """The neutral on-disk lease vocabulary; adapters translate their own field names."""

    run_id: str | None = None
    index: int = 0                  # last settled iteration index (0 = none yet)
    attempt: int = 1                # attempt of the CURRENT iteration
    state: str = STATE_DONE         # pending | running | done
    execution_id: str | None = None
    updated_at: str | None = None   # heartbeat stamp (fixed-width UTC iso)
    cancel_requested: bool = False


@dataclasses.dataclass(frozen=True)
class LeaseDecision:
    action: LeaseAction
    ledger: LeaseLedger
    identity: ExecutionIdentity | None = None
    reason: str | None = None


def heartbeat_age_s(updated_at_iso: str | None, *, now_epoch: float) -> float:
    if not updated_at_iso:
        return float("inf")
    try:
        # Fixed-width UTC "Z" suffix parses as a zone (py3.11+ fromisoformat).
        value = datetime.fromisoformat(updated_at_iso)
    except ValueError:
        return float("inf")
    return max(now_epoch - value.timestamp(), 0.0)


def acquire(
    ledger: LeaseLedger,
    *,
    run_id: str,
    index: int,
    config: LeaseConfig,
    now_iso: str,
    now_epoch: float,
) -> LeaseDecision:
    """The pure lease-contest decision (run inside the adapter's atomic section)."""
    if ledger.run_id not in (None, run_id):
        return _drop(ledger, "lease ledger belongs to another run")

    reclaimed = False
    if ledger.state == STATE_DONE:
        if index != ledger.index + 1:
            return _drop(
                ledger,
                "duplicate/delayed job" if index <= ledger.index
                else "out-of-order job (iteration gap)",
            )
        attempt = 1
    elif ledger.state in (STATE_RUNNING, STATE_PENDING):
        if ledger.index != index:
            return _drop(ledger, "lease is running a different iteration")
        if ledger.state == STATE_RUNNING and not config.is_stale(
            ledger.updated_at, now_epoch=now_epoch
        ):
            # A fresh ``running`` lease = a LIVE twin already executing this iteration.
            # It owns the cancel too, so stand aside rather than yank the slot from under it.
            return _drop(ledger, "live duplicate (iteration already leased)")
        attempt = int(ledger.attempt or 1) + 1
        reclaimed = True
    else:
        return _drop(ledger, f"unknown lease state: {ledger.state!r}")

    identity = ExecutionIdentity(run_id=run_id, index=index, attempt=attempt)
    if ledger.cancel_requested:
        # Only a legitimately-expected job reaches here — so a requested Stop means *no
        # live owner holds the slot*. Terminalize with the cancel instead of running.
        done = dataclasses.replace(
            ledger, state=STATE_DONE, execution_id=identity.as_str(), updated_at=now_iso
        )
        return LeaseDecision(
            action="cancelled", ledger=done, identity=identity,
            reason="cancel requested before this iteration started",
        )
    running = dataclasses.replace(
        ledger,
        run_id=run_id,
        index=index,
        attempt=attempt,
        state=STATE_RUNNING,
        execution_id=identity.as_str(),
        updated_at=now_iso,
    )
    return LeaseDecision(
        action="reclaimed" if reclaimed else "granted",
        ledger=running, identity=identity,
    )


def renew(
    ledger: LeaseLedger,
    *,
    now_iso: str,
    owner_execution: str | None = None,
) -> LeaseLedger:
    """Heartbeat: prove the current owner is alive without touching anything else.

    Fencing (F2): when the caller hands in its own ``owner_execution`` identity, a lease
    already stamped with a DIFFERENT execution id means ownership moved (a reclaim after
    a lapse) — the stale owner's heartbeat is refused as a no-op, never applied.
    """
    if owner_execution is not None and ledger.execution_id != owner_execution:
        return ledger
    return dataclasses.replace(ledger, updated_at=now_iso)


def mark_done(
    ledger: LeaseLedger,
    *,
    execution_id: str | None = None,
    now_iso: str,
    owner_execution: str | None = None,
) -> LeaseLedger:
    """Settle the current iteration (success path or terminalization bookkeeping).

    Fencing (F2): with ``owner_execution`` given, only the execution holding the lease
    may settle it; a superseded (zombie) owner's settle is a no-op, so its late decision
    can never flip a live successor's ``running`` lease.
    """
    if owner_execution is not None and ledger.execution_id != owner_execution:
        return ledger
    fields: dict = {"state": STATE_DONE, "updated_at": now_iso}
    if execution_id is not None:
        fields["execution_id"] = execution_id
    return dataclasses.replace(ledger, **fields)


def _drop(ledger: LeaseLedger, reason: str) -> LeaseDecision:
    return LeaseDecision(action="dropped", ledger=ledger, reason=reason)
