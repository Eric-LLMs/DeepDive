"""P3-2B single-flight state machine — offline concurrency tests (mock delays only).

Six dimensions pinned, straight against ``plugins.research.fetch_cache.FlightRegistry``
(no network, no disk; small injected leases + asyncio events keep every race
deterministic):

1. basic concurrent collapse — N joiners, ONE real fetch, one shared result;
2. lease takeover / no stampede — a hung leader is succeeded by exactly ONE winner,
   the losers keep queueing (no N-way degradation to direct fetch); and the takeover
   NEVER re-mints the shared future (identity pinned; a successor's resolve wakes all
   generations instantly);
3. cancellation isolation — a follower's cancel never touches the shared future;
4. leader-exception broadcast + next-request retry — every live waiter gets the same
   failure, the registry self-cleans, and no negative cache survives;
5. registry cleanup — every terminal path (success / error / owner-cancel) removes the
   slot, with the generation/identity fence so a stale owner never deletes its
   successor's flight;
6. stale-leader generation fencing — a superseded leader's late result is discarded:
   it never resolves the shared future and the successor's outcome is authoritative.
"""
from __future__ import annotations

import asyncio

import pytest

from plugins.research.fetch_cache import FlightRegistry

KEY = "flight-key-x"


def _counter_fetch(value: str, *, block: asyncio.Event | None = None, calls: list | None = None,
                   error: Exception | None = None):
    async def fetch():
        if calls is not None:
            calls.append(value)
        if block is not None:
            await block.wait()
        if error is not None:
            raise error
        return value

    return fetch


class TestSingleFlightStateMachine:
    # ── 1. basic concurrent collapse ──────────────────────────────────────────
    async def test_n_concurrent_joiners_one_real_fetch(self):
        reg = FlightRegistry(lease_s=10)
        gate = asyncio.Event()
        calls: list[str] = []
        fetch = _counter_fetch("V1", block=gate, calls=calls)

        tasks = [asyncio.create_task(reg.run(KEY, fetch)) for _ in range(8)]
        await asyncio.sleep(0.02)  # everyone joins while the leader blocks
        assert len(calls) == 1
        gate.set()
        results = await asyncio.gather(*tasks)
        assert results == ["V1"] * 8
        assert len(calls) == 1  # collapse held to the end
        assert reg._flights == {}  # terminal cleanup

    # ── 2. lease takeover, exactly one successor ──────────────────────────────
    async def test_lease_expiry_yields_exactly_one_successor_not_a_stampede(self):
        reg = FlightRegistry(lease_s=0.05)
        hang = asyncio.Event()
        calls: list[str] = []
        leader = asyncio.create_task(reg.run(KEY, _counter_fetch("G1", block=hang, calls=calls)))
        await asyncio.sleep(0.005)
        successors = [
            asyncio.create_task(reg.run(KEY, _counter_fetch("G2-ok", calls=calls)))
            for _ in range(3)
        ]
        # The shared future never resolves (leader hangs past the lease): waiters may
        # ONLY take over, never degrade. Exactly one successor's fetch runs.
        results = await asyncio.gather(*successors)
        assert results == ["G2-ok"] * 3
        assert calls == ["G1", "G2-ok"]  # one successor total — no N-way stampede
        flight = reg._flights.get(KEY)
        assert flight is None or flight.generation == 2  # CAS bumped the generation once

        # Release the zombie leader: it must exit without touching anything (its own
        # caller is fenced to the successor's authority — dimension 6 covers the value).
        hang.set()
        assert await leader == "G2-ok"

    # ── 3. cancellation isolation ─────────────────────────────────────────────
    async def test_follower_cancel_does_not_poison_the_flight(self):
        reg = FlightRegistry(lease_s=10)
        gate = asyncio.Event()
        calls: list[str] = []
        fetch = _counter_fetch("V", block=gate, calls=calls)
        leader = asyncio.create_task(reg.run(KEY, fetch))
        await asyncio.sleep(0.005)
        w1 = asyncio.create_task(reg.run(KEY, fetch))
        w2 = asyncio.create_task(reg.run(KEY, fetch))
        await asyncio.sleep(0.005)

        w1.cancel()  # one follower walks away mid-wait
        gate.set()
        assert await leader == "V"          # leader lands normally...
        assert await w2 == "V"              # ...and the surviving waiter receives it
        with pytest.raises(asyncio.CancelledError):
            await w1
        assert len(calls) == 1
        assert reg._flights == {}

    async def test_owner_cancel_releases_slot_for_takeover(self):
        reg = FlightRegistry(lease_s=0.05)
        hang = asyncio.Event()
        calls: list[str] = []
        leader = asyncio.create_task(reg.run(KEY, _counter_fetch("G1", block=hang, calls=calls)))
        await asyncio.sleep(0.005)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        assert reg._flights == {}  # dead owner fenced-released its slot
        # A later request re-leads with a REAL fetch — no ghost state left behind.
        out = await reg.run(KEY, _counter_fetch("V2", calls=calls))
        assert out == "V2"
        assert calls == ["G1", "V2"]

    # ── 4. exception broadcast + next-request retry ───────────────────────────
    async def test_leader_exception_broadcasts_then_next_request_refetches(self):
        reg = FlightRegistry(lease_s=10)
        gate = asyncio.Event()
        boom = RuntimeError("boom")
        calls: list[str] = []
        leader = asyncio.create_task(
            reg.run(KEY, _counter_fetch("G1", block=gate, calls=calls, error=boom))
        )
        await asyncio.sleep(0.005)
        w = asyncio.create_task(reg.run(KEY, _counter_fetch("never-runs", calls=calls)))
        await asyncio.sleep(0.005)
        gate.set()
        with pytest.raises(RuntimeError, match="boom"):
            await leader
        with pytest.raises(RuntimeError, match="boom"):  # same broadcast exception
            await w
        assert calls == ["G1"]  # the waiter's own closure never fetched
        assert reg._flights == {}  # failure ≠ negative cache: slot cleaned
        # The NEXT request re-leads a fresh flight and really fetches again.
        out = await reg.run(KEY, _counter_fetch("V-retry", calls=calls))
        assert out == "V-retry"
        assert calls == ["G1", "V-retry"]

    # ── 5. registry cleanup on every terminal path ────────────────────────────
    async def test_registry_is_empty_after_every_terminal_path(self):
        reg = FlightRegistry(lease_s=5)
        assert await reg.run("a", _counter_fetch("ok")) == "ok"
        with pytest.raises(ValueError):
            await reg.run("b", _counter_fetch(None, error=ValueError("x")))
        assert reg._flights == {}  # success + error both released

    async def test_stale_owner_cannot_delete_successor_flight(self):
        """Cleanup fence: release requires identity AND generation match."""
        reg = FlightRegistry(lease_s=0.2)
        hang = asyncio.Event()
        calls: list[str] = []
        leader = asyncio.create_task(reg.run(KEY, _counter_fetch("G1", block=hang, calls=calls)))
        await asyncio.sleep(0.01)
        flight = reg._flights[KEY]
        assert flight.generation == 1
        # Force the lease to lapse, then take over (single CAS winner).
        flight.lease_deadline = reg._now() - 1
        assert reg._try_takeover(KEY, flight, 1) is True
        assert flight.generation == 2
        assert reg._try_takeover(KEY, flight, 1) is False  # stale gen can never win again
        # G1's fenced release must be a no-op on the live successor slot.
        reg._release(KEY, flight, 1)
        assert reg._flights.get(KEY) is flight  # successor's slot intact
        # Authoritative G2 outcome + G1 finishing late ⇒ G1 is fenced to G2's result.
        hang.set()
        flight.fut.set_result("G2-authoritative")
        assert await leader == "G2-authoritative"
        assert reg._flights.get(KEY) is flight  # nobody deleted it by mistake
        reg._release(KEY, flight, 2)  # (white-box: the manual successor's own release)
        assert reg._flights == {}
        assert calls == ["G1"]  # the stale owner fetched, but its result was discarded

    # ── 6. stale-leader generation fencing ────────────────────────────────────
    async def test_takeover_preserves_future_identity(self):
        """FINAL-REVIEW invariant: the shared future is NEVER re-minted by takeover —
        generations flip, the ONE future lives; a successor's resolve therefore wakes
        every cross-generation waiter immediately (no self-timer polling).
        """
        reg = FlightRegistry(lease_s=0.1)
        hang_g1, hold_g2 = asyncio.Event(), asyncio.Event()
        calls: list[str] = []

        async def ghost():
            calls.append("GHOST")
            await hang_g1.wait()
            return "GHOST-RESULT"

        async def successor():
            calls.append("REAL")
            await hold_g2.wait()
            return "REAL-RESULT"

        leader = asyncio.create_task(reg.run(KEY, ghost))
        await asyncio.sleep(0.01)
        flight = reg._flights[KEY]
        fut_before = flight.fut

        w1 = asyncio.create_task(reg.run(KEY, successor))
        await asyncio.sleep(0.13)  # past the lease → exactly one takeover has happened
        assert flight.generation == 2
        assert flight.fut is fut_before  # ← THE pinned identity (was re-minted pre-fix)

        # A second-generation joiner queues behind the successor's blocked fetch…
        w2 = asyncio.create_task(reg.run(KEY, successor))
        await asyncio.sleep(0.01)

        # …the successor's resolve wakes EVERY generation on the one shared future well
        # before w2's own lease window (≈0.06s away) could have expired → instant wake.
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        hold_g2.set()
        assert await w1 == "REAL-RESULT"
        assert await w2 == "REAL-RESULT"
        assert loop.time() - t0 < 0.05

        hang_g1.set()  # the ghost lands late → fenced, discarded, no third fetch
        assert await leader == "REAL-RESULT"
        assert calls == ["GHOST", "REAL"]  # one successor fetch only; ghost re-joined
        assert reg._flights == {}

    async def test_stale_leader_result_is_discarded_successor_wins(self):
        reg = FlightRegistry(lease_s=0.1)
        hang_g1, hold_g2 = asyncio.Event(), asyncio.Event()
        calls: list[str] = []

        async def ghost():
            calls.append("GHOST")
            await hang_g1.wait()
            return "GHOST-RESULT"

        async def successor():
            calls.append("REAL")
            await hold_g2.wait()
            return "REAL-RESULT"

        leader = asyncio.create_task(reg.run(KEY, ghost))
        await asyncio.sleep(0.01)
        follower = asyncio.create_task(reg.run(KEY, successor))
        # Past the lease: the follower wins the exclusive takeover (gen 2) and its own
        # fetch is now the authoritative in-flight one, still blocked on hold_g2.
        await asyncio.sleep(0.11)
        flight = reg._flights[KEY]
        assert flight.generation == 2
        assert calls == ["GHOST", "REAL"]

        hang_g1.set()  # G1 lands LATE, while the successor's fetch is still live
        await asyncio.sleep(0.02)
        assert not flight.fut.done()   # the ghost never resolved the shared future
        assert flight.waiting >= 1     # G1 fenced: it requeued BEHIND the successor

        hold_g2.set()  # successor completes authoritatively
        assert await follower == "REAL-RESULT"
        assert await leader == "REAL-RESULT"  # stale result discarded end-to-end
        assert reg._flights == {}
