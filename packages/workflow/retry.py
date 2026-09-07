"""Retry policy: the error-bisection seam and the backoff curve.

Transient means "the SAME execution may try again after a wait" (a network hiccup, an
HTTP 429/5xx, a lock timeout); non-transient means "stop honestly". A lost lease contest
is never transient — it means another worker already owns the iteration, so the loser
drops instead of retrying. The classification itself is *injected* (each deployment
knows its own failure vocabulary); this module owns only the shape of the decision:

* attempt bound — retry only while ``attempt < max_attempts`` (Temporal max-attempts,
  SFN MaxAttempts);
* exponential backoff with a hard ceiling (Temporal backoff-coefficient, SFN
  BackoffRate) — default ``min(2^(attempt-1), 30s)``, the curve the current production
  driver already runs.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any


def default_backoff(attempt: int, *, factor: float = 2.0, cap: float = 30.0) -> float:
    """Exponential retry backoff: ``attempt`` is 1-based (1 -> 1s, 2 -> 2s, … capped)."""
    return min(factor ** max(attempt - 1, 0), cap)


def _never_transient(exc: BaseException) -> bool:
    return False


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff: Callable[[int], float] = default_backoff
    is_transient: Callable[[BaseException], bool] = _never_transient

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """May THIS execution try again? (attempt = the one that just failed.)"""
        return attempt < self.max_attempts and self.is_transient(exc)

    def wait_s(self, next_attempt: int) -> float:
        return self.backoff(next_attempt)

    def classified(self, exc: BaseException) -> dict[str, Any]:
        """A small audit-friendly view (adapters log this verbatim)."""
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "transient": self.is_transient(exc),
            "max_attempts": self.max_attempts,
        }
