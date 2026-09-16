"""In-process lease ledger for ONE presentation-brief execution.

Implements the generic ``workflow.ports.LeaseStore`` capability (atomic
read-modify-write over the neutral :class:`LeaseLedger`) with a plain object under a
lock: a deck run is one asyncio task inside the Toolkit worker process, so there is
no cross-process contest to mediate — yet the core lease semantics (duplicate
delivery drop, cancel-before-start, fenced settle) stay fully in force, which is
exactly why this is a port implementation and not a bypass.

One store instance per run gives state isolation structurally: a second execution
never sees this ledger, and a stale twin arriving with the wrong index is *dropped*
by :func:`workflow.leases.acquire` rather than corrupting the live run.
"""
from __future__ import annotations

import dataclasses
import threading

from workflow.leases import LeaseLedger


class DeckLeaseStore:
    """LeaseStore port: memory-backed, serialized by an RLock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ledger = LeaseLedger()

    # ── LeaseStore port ───────────────────────────────────────────────────────
    def atomic(self, mutate) -> LeaseLedger:
        with self._lock:
            self._ledger = mutate(self._ledger)
            return self._ledger

    def read(self) -> LeaseLedger:
        with self._lock:
            return self._ledger

    # ── host-side control (the UI stop button's seam) ─────────────────────────
    def request_cancel(self) -> LeaseLedger:
        """Flag the ledger; an expected arrival terminalizes with the cancel and a
        running iteration observes it at grading time (the runner ORs the flag in)."""
        return self.atomic(
            lambda ledger: dataclasses.replace(ledger, cancel_requested=True)
        )
