"""P3-2A content-addressed fetch cache (CAS) for research scrapes.

One global, tenant-free store for *public* page drafts. The key binds the page identity to
the exact fetch policy and parser that produced the bytes, so a cached draft can only be
served to a request that would have fetched it identically:

    key_id = sha256(canonical_url "\\x00" fetch_policy_fingerprint "\\x00" parser_version)

Layout (all under ``<research_scratch>/fetch_store/`` — NOTE: this file only defines the
store; the caller decides the root)::

    index/<hh>/<key_id>.json      publish point: metadata + blob integrity digest
    blobs/<hh>/<h2>/<key_id>      the full cleaned draft bytes (utf-8 of envelope["body"])

Guarantees pinned by the P3-2A design review:

* **Cache ≠ provenance.** A hit never fabricates run authority: the caller must still
  materialize a *current-run* drive asset and write the ledger entry itself. This module
  stores and returns bytes + fetch-time metadata, nothing else.
* **Integrity on read.** The blob is re-hashed against the index digest at every lookup;
  a mismatch (tampered/truncated/corrupted file) self-heals by purging both files and
  reporting a miss — a corrupt entry can never be served, and never lingers.
* **Freshness = ``fetched_at`` vs ``max_age`` only.** ``retrieved_at`` (a per-run read
  stamp the *caller* records) deliberately does not exist here and can never extend a
  cache entry's life.
* **Two-phase publish order** (crash-safe): blob ``tmp → fsync → self-verify → replace``
  first, then the index. A crash between the two leaves at most an orphan blob that no
  index references (GC-collectable), never a dangling index pointing at missing bytes.
* **Eligibility is server-decided.** Only ``classify_eligibility`` says ``"public"`` may
  enter this global store; any URL carrying a credential-like query parameter (on the
  request *or* the redirect chain's final URL) is ``"no_cache"``. ``"tenant"`` is a
  reserved tier: no writer produces it today (all fetches are anonymous), and nothing in
  this module reads tenant state.
* ``text_target`` is NOT part of the policy fingerprint: the model-facing ``text`` slice
  depends on the *current call's* batch size, so views are re-sliced from the stored body
  by the caller — the cached artifact is the body, never a view.

GC is design-only for P3-2A: an mtime sweep of orphan/aged files under this root is a
safe future addition (no refcounting is needed because every blob is fully re-derivable
by re-fetching), and is deliberately left unimplemented here.

**P3-2B — in-process single-flight** (``FlightRegistry``, below): concurrent MISS fetches
of the same *cache identity* collapse to one real network call, process/event-loop only
(no Redis, no cross-thread sharing). Lease-expiry is takeover-able state, never an error
broadcast; the takeover race is a generation CAS so exactly one successor refetches; a
superseded leader's late result is fenced out (never resolved, never published); follower
cancellation is shielded from the shared future; failures are broadcast and never cached.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from core.infrastructure.web_fetch import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CHARS,
    DEFAULT_TIMEOUT_S,
    MAX_REDIRECTS,
    _USER_AGENT,
)

# Bump whenever the *bytes-producing* pipeline changes meaning: _clean_html / _decode_html
# / _content_status / interstitial rules / anything that would make old stored bodies
# semantically different from what the current parser would produce.
PARSER_VERSION = 1

# How long a stored draft may serve hits. Past this the entry is simply not eligible —
# the caller re-fetches and the next ``store`` overwrites the slot.
DEFAULT_MAX_AGE_S = 24.0 * 3600.0

# Kept in sync with ``fetch_clean_urls``' header block (core/infrastructure/web_fetch.py,
# a P3-2A red line — imported, never edited). If those literals change without a parser
# bump, old entries keep their (correct) old fingerprint and simply expire.
_ACCEPT_LANGUAGE = "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"
_ACCEPT = "text/html,application/xhtml+xml,*/*;q=0.8"

# Query-parameter names that mark a URL as credential-bearing / signed-context.
# Exact (case-insensitive) name match on purpose — substring matching would over-reject
# ordinary params and invite callers to "tune" it away.
SECRET_QUERY_PARAMS = frozenset({
    "sig", "signature", "token", "access_token", "api_key", "apikey",
    "key", "auth", "authorization", "session", "secret",
})


def fetch_policy_fingerprint() -> str:
    """Digest of everything that shapes the stored bytes except the URL itself.

    Deliberately excludes ``text_target`` (a per-call view budget) and includes
    ``max_chars`` (it truncates the stored body), the network budget, the redirect
    policy, and the request header identity.
    """
    policy = {
        "timeout_s": DEFAULT_TIMEOUT_S,
        "max_body_bytes": DEFAULT_MAX_BODY_BYTES,
        "max_chars": DEFAULT_MAX_CHARS,
        "max_redirects": MAX_REDIRECTS,
        "headers": {
            "User-Agent": _USER_AGENT,
            "Accept-Language": _ACCEPT_LANGUAGE,
            "Accept": _ACCEPT,
        },
    }
    blob = json.dumps(policy, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


_POLICY_FP = fetch_policy_fingerprint()


def classify_eligibility(*urls: str | None) -> str:
    """Server-side eligibility tier for a fetch: ``"public"`` or ``"no_cache"``.

    Checks every URL in the chain (the request and the final post-redirect URL): one
    secret-named query parameter anywhere downgrades the whole fetch to ``"no_cache"``.
    ``"tenant"`` is reserved for a future authenticated-fetch writer; this function
    never returns it, so the global store can never receive tenant content by accident.
    """
    for u in urls:
        try:
            query = urlsplit(u or "").query
        except ValueError:  # unparseable → refuse to cache, never guess
            return "no_cache"
        for pair in query.split("&"):
            name = pair.split("=", 1)[0].strip().lower()
            if name and name in SECRET_QUERY_PARAMS:
                return "no_cache"
    return "public"


def cache_index_id(canonical_url: str) -> str:
    """Full cache identity: page URL × fetch policy × parser version."""
    joined = "\x00".join([canonical_url, _POLICY_FP, str(PARSER_VERSION)])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync (rename durability); a no-op on Windows."""
    try:
        dir_fd = os.open(directory, os.O_DIRECTORY)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """``tmp → flush+fsync → os.replace`` for raw bytes (never a torn final file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)  # no-op once replace consumed it


class FetchStore:
    """Disk CAS for public page drafts. Pure stdlib, no engine, no Redis, no network.

    ``now`` is injectable so freshness is testable without sleeping. Every public method
    is total: storage faults degrade to ``None``/``False`` (a miss, a skipped write) and
    never raise into a fetch batch — the cache must never be able to fail a run.
    """

    def __init__(
        self,
        root_dir: Path | str,
        *,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.root = Path(root_dir)
        self.max_age_s = float(max_age_s)
        self._now = now or time.time

    # ── paths ──

    def _index_path(self, key_id: str) -> Path:
        return self.root / "index" / key_id[:2] / f"{key_id}.json"

    def _blob_path(self, key_id: str) -> Path:
        return self.root / "blobs" / key_id[:2] / key_id[2:4] / key_id

    # ── read ──

    def lookup(self, canonical_url: str) -> dict[str, Any] | None:
        """Return ``{**index_meta, "body_bytes": bytes}`` on a verified fresh hit, else None.

        Freshness and integrity are both re-checked on every read; a corrupt pair is
        purged so the next ``store`` starts clean.
        """
        key_id = cache_index_id(canonical_url)
        index = self._read_index(key_id)
        if index is None:
            return None
        fetched_at = index.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            self._purge(key_id)
            return None
        if (self._now() - float(fetched_at)) > self.max_age_s:
            return None  # stale: an honest miss; the slot is overwritten on the next store
        try:
            body = self._blob_path(key_id).read_bytes()
        except OSError:
            self._purge(key_id)  # index without its blob (foreign blob GC) → heal + miss
            return None
        if (
            len(body) != index.get("len_bytes")
            or hashlib.sha256(body).hexdigest() != index.get("sha256")
        ):
            self._purge(key_id)  # tampered/truncated → never served, never lingering
            return None
        return {**index, "body_bytes": body}

    def _read_index(self, key_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._index_path(key_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _purge(self, key_id: str) -> None:
        for path in (self._index_path(key_id), self._blob_path(key_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # read-only fs etc.: the verified-miss above already protected serving

    # ── write ──

    def store(self, canonical_url: str, body: bytes, meta: dict[str, Any]) -> bool:
        """Publish one draft under ``canonical_url`` (caller-verified eligible).

        Two-phase order (see module docstring): blob first — write, re-hash *the staged
        bytes*, only then replace — then the index as the single publish point.
        Best-effort: any fault returns False; the fetch batch must not care.
        """
        key_id = cache_index_id(canonical_url)
        sha = hashlib.sha256(body).hexdigest()
        blob_path = self._blob_path(key_id)
        try:
            # Phase 1: blob. If the slot already holds byte-identical content, skip the
            # rewrite (same key × same digest ⇒ same bytes). Otherwise stage, self-verify
            # the *staged file*, then atomically replace.
            try:
                existing = blob_path.read_bytes()
            except OSError:
                existing = None
            if existing is None or hashlib.sha256(existing).hexdigest() != sha:
                _atomic_write_bytes(blob_path, body)
                # Re-verify what actually landed on disk before publishing the index.
                if hashlib.sha256(blob_path.read_bytes()).hexdigest() != sha:
                    return False
            # Phase 2: index — the publish point. Written last, fully durable.
            index = {
                "v": 1,
                "canonical_url": canonical_url,
                "policy_fingerprint": _POLICY_FP,
                "parser_version": PARSER_VERSION,
                "sha256": sha,
                "len_bytes": len(body),
                "fetched_at": float(self._now()),
                "fetched_at_iso": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now())
                ),
                **{
                    k: meta.get(k)
                    for k in (
                        "title", "http_status", "final_url",
                        "content_status", "full_char_len",
                    )
                },
            }
            _atomic_write_bytes(
                self._index_path(key_id),
                json.dumps(index, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            return True
        except Exception:  # noqa: BLE001 - the cache never fails a fetch batch
            return False


# ══════════════════════════════════════════════════════════════════════════════
# P3-2B — in-process single-flight with exclusive lease takeover + generation
# fencing. Process-local and event-loop-local by construction: the registry holds
# asyncio futures, so it is only ever touched from the loop that runs it. No Redis,
# no threads, no cross-process guarantees (that tier is explicitly out of scope).
# ══════════════════════════════════════════════════════════════════════════════


def default_lease_s() -> float:
    """The anti-stampede OWNERSHIP lease — deliberately NOT a network wall-clock bound.

    Semantics, pinned by final review: ``httpx.Timeout(timeout_s)`` is a per-operation
    *inactivity* timeout (connect/read/write/pool; the read timer resets on every chunk),
    and ``_read_bounded`` caps the body by *size*, not time — so the transport layer has
    NO true per-hop total wall clock, and no formula here can bound a fetch's worst-case
    duration (that would require transport changes, a P3-2 red line).

    This lease is instead the ownership protocol's liveness window: while a current
    generation's owner may legitimately still be fetching (slow-drip pages included),
    waiters must NOT conclude it is dead before the lease lapses. The size
    ``DEFAULT_TIMEOUT_S × (MAX_REDIRECTS+1) + 5s`` keeps that window strictly above the
    common hang/dead-fault envelope (connect stall / no-response, each hop ≤
    ``timeout_s`` of *inactivity*), trading detection latency for never falsely
    succeeding over a live leader. A pathological slow page CAN outlive the lease:
    takeover + generation fencing keep that case correct (the ghost is discarded, the
    successor's outcome published); it costs at most one duplicated fetch per lapsed
    generation — an efficiency boundary, not a safety one.
    """
    return DEFAULT_TIMEOUT_S * (MAX_REDIRECTS + 1) + 5.0


class _Flight:
    """Shared state for one in-flight fetch of one flight key (single event loop)."""

    __slots__ = ("fut", "generation", "lease_deadline", "waiting")

    def __init__(self, fut: "asyncio.Future[Any]", lease_deadline: float) -> None:
        self.fut = fut               # THE shared future — never re-minted for life
        self.generation = 1          # bumped once per takeover — the fencing token
        self.lease_deadline = lease_deadline
        self.waiting = 0             # live waiters (exception broadcast skips zero)


class FlightRegistry:
    """Single-flight dedup for identical fetches, per the P3-2B frozen protocol.

    Protocol invariants (each pinned by a test in tests/test_research_fetch_cache.py):

    1. One real ``fetch()`` per generation; the ``_flights`` dict is cleaned with an
       identity+generation check — a superseded owner can never delete its successor's
       flight, and every terminal path (success / error / owner-cancel) releases its slot.
    2. Lease expiry is *takeover-able state*, never an error: waiters that outlive the
       lease race in :meth:`_try_takeover` (a synchronous, await-free CAS) — exactly one
       becomes the SUCCESSOR owner and refetches; the losers keep queueing on the ONE
       shared future (never re-minted, pinned by ``test_takeover_preserves_future_
       identity``). N-way degradation-to-direct-fetch is structurally impossible.
    3. Generation fencing: a stale leader G1 whose generation moved must not resolve the
       shared future, must not publish, must not overwrite the successor's authority —
       its late result is discarded and it re-joins behind the successor.
    4. Follower cancellation is shielded (the shared future survives it); owner failure
       broadcasts the exception to live waiters and leaves no negative cache — the next
       request re-leads a fresh flight and really fetches again.
    """

    def __init__(
        self,
        *,
        lease_s: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flights: dict[str, _Flight] = {}
        self._lease_s = float(lease_s) if lease_s is not None else default_lease_s()
        self._now = now

    async def run(
        self, key: str, fetch: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Await ``fetch()`` through the flight for ``key`` (the caller's
        ``cache_index_id`` — i.e. byte-identical to the P3-2A cache identity).

        The winner of a takeover runs *its own* ``fetch`` closure; every consumer still
        post-processes the shared result independently (per-run materialization stays in
        the caller — single-flight collapses only the network call).
        """
        seen_fut: asyncio.Future[Any] | None = None
        pending_own = False  # won a takeover last turn — own the flight next iteration
        while True:
            flight = self._flights.get(key)
            if flight is None:
                # A flight resolved-and-released while we were mid-race? Hand back its
                # authoritative outcome instead of refetching (never the ghost of a
                # fenced leader — released flights are always current-generation ones).
                if seen_fut is not None and seen_fut.done():
                    return seen_fut.result()
                flight = _Flight(
                    asyncio.get_running_loop().create_future(),
                    self._now() + self._lease_s,
                )
                self._flights[key] = flight
                pending_own = True
            owning = pending_own
            pending_own = False
            gen = flight.generation
            fut = flight.fut
            seen_fut = fut

            if owning:
                try:
                    result = await fetch()
                except asyncio.CancelledError:
                    # A dead owner must not resolve *or* cancel the shared future — that
                    # would conflate its death into every waiter's own cancellation.
                    # Release the slot (identity+gen fenced); waiters re-lead on expiry.
                    self._release(key, flight, gen)
                    raise
                except BaseException as exc:
                    if self._is_current(key, flight, gen):
                        self._release(key, flight, gen)
                        if flight.waiting and not fut.done():
                            fut.set_exception(exc)  # broadcast; failure ≠ cache
                    # Superseded owner's failure is its own problem — the successor's
                    # flight stays untouched.
                    raise
                if self._is_current(key, flight, gen):
                    if not fut.done():
                        fut.set_result(result)
                    self._release(key, flight, gen)
                    return result
                # ── GENERATION FENCED ── our generation moved on while we fetched:
                # a successor owns authority now. Discard this result entirely (never
                # resolve, never publish) and re-join behind the successor — carrying
                # the successor's live future so a resolved-and-released flight hands
                # its authoritative outcome back without one extra refetch.
                seen_fut = flight.fut
                continue

            # ── waiter: sleep to the lease deadline, never past it into direct fetch ──
            timeout = max(0.0, flight.lease_deadline - self._now())
            flight.waiting += 1
            try:
                # shield(): our own cancellation unwinds us but leaves the shared
                # future — and the leader's fetch — completely intact.
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            finally:
                flight.waiting -= 1
            # Lease window closed without resolution → exclusive takeover race. On a
            # win, the CAS above bumped the generation and re-armed the lease — on the
            # SAME shared future — and the winner proceeds to run its OWN fetch as the
            # next generation's owner. Losers simply re-queue (still on that one
            # future) — never a direct fetch. Tracking ``flight.fut`` in ``seen_fut``
            # makes a flight that resolved-and-released between our timeout and here
            # hand back its authoritative outcome instead of being refetched (and a
            # broadcast exception propagates the same way).
            seen_fut = flight.fut
            if self._try_takeover(key, flight, gen):
                pending_own = True

    def _try_takeover(self, key: str, flight: _Flight, expected_gen: int) -> bool:
        """Atomic CAS (no awaits inside ⇒ at most one winner per event-loop turn).

        Win ⇒ generation++ and re-armed lease ONLY — ``flight.fut`` is NEVER re-minted.
        The shared future lives for the whole flight, so when the eventual successor
        resolves it, EVERY cross-generation waiter (still shielded on that one object)
        wakes instantly instead of polling its own lease timer. Ownership fencing is done
        by ``generation`` + the ``_is_current`` check at resolve time, NOT by swapping the
        future. This method never broadcasts an exception for mere expiry.
        """
        cur = self._flights.get(key)
        if (
            cur is not flight
            or cur.generation != expected_gen
            or self._now() < cur.lease_deadline
        ):
            return False  # replaced, already succeeded over, or not expired → requeue
        cur.generation += 1
        # flight.fut stays the SAME object across every generation (P3-2B core invariant).
        cur.lease_deadline = self._now() + self._lease_s
        return True

    def _is_current(self, key: str, flight: _Flight, gen: int) -> bool:
        return self._flights.get(key) is flight and flight.generation == gen

    def _release(self, key: str, flight: _Flight, gen: int) -> None:
        """Fenced cleanup: drop the registry slot only if it is still OUR flight+gen."""
        if self._is_current(key, flight, gen):
            del self._flights[key]
