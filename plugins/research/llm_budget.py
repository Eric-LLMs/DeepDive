"""Unified LLM budget metering for the code-driven research pipeline.

Sealed-spec constraint #1 (显式确权): every semantic LLM call a research run makes —
the pipeline's own decision calls AND the server-side closures' internal passes
(``adjudicate_evidence`` / ``review_draft``) — must report through ONE run-level
budget. No hidden tokens, no un-metered calls.

Constraint #5 (全局成本硬闸绝不退役): the run's cumulative spend is checked
BEFORE every would-be call; once it reaches ``max_cost_usd`` (settings
``research_driver_max_cost_usd``, default $0.40) the gate raises
:class:`CostLimitExceeded` and the next completion is never issued — 超额即断电.

The gate protocol is duck-typed so ``plugin.py`` never imports this module (no
cycle; plugin-side call sites use the narrow surface only)::

    gate.admit()                  # before each completion; may raise
    gate.settle(prompt, reply)    # after; reply=None marks a failed attempt

Token accounting is ESTIMATED from character counts (chars // 4, the same
constant the adjudication budget uses) because the streaming transport does not
surface usage deltas; estimates are declared as such wherever they land. Costs
resolve through :func:`agent.engine.telemetry.estimate_cost_usd`, so an
un-priced model stays PRICING_UNKNOWN (counted explicitly, never laundered
into $0) — the same discipline as the driver ledger.
"""
from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN_EST = 4  # matches plugin._ADJ_CHARS_PER_TOKEN (the 4-char convention)


class StageBudgetExceeded(RuntimeError):
    """A stage's DECLARED LLM call budget is spent and another call was attempted.

    Degradable class: the node handler records the breach in the failure ledger and
    advances honestly — it must never trigger an in-place retry loop.
    """


class RunBudget:
    """One run's authoritative LLM spend meter (cost + calls + tokens + unknowns).

    ``start_spent_usd`` seeds the cumulative from the driver ledger so the meter
    composes with the pre-existing per-turn accumulators (the pipeline node's spend
    is also returned as ``RunTurnResult.cost_usd``; the seeding prevents a
    double-spend blindness WITHIN a node, the return prevents it ACROSS nodes).
    """

    def __init__(
        self,
        *,
        cap_usd: float | None = None,
        start_spent_usd: float = 0.0,
        model: str | None = None,
        pricing: tuple[Any, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.cap_usd = cap_usd
        self.spent = float(start_spent_usd)
        self.model = model
        self.pricing = pricing
        self.run_id = run_id
        self.calls = 0
        self.failed_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.pricing_unknown_calls = 0

    # ── hard fuse ────────────────────────────────────────────────────────────
    def check_hard(self) -> None:
        """PRE-CALL power cut: cumulative spend at/over the cap → no call proceeds.

        Raises the same ``CostLimitExceeded`` the driver's turn-seam gate raises, so
        downstream transient-classification and terminalization stay single-sourced.
        """
        if self.cap_usd is not None and self.spent >= self.cap_usd:
            # late import: workflow_adapter pulls plugin.py; keep this module leaf-pure
            from plugins.research.workflow_adapter import CostLimitExceeded

            raise CostLimitExceeded(
                f"research run {self.run_id}: cumulative ${self.spent:.4f} >= cap "
                f"${self.cap_usd:.4f} — no LLM call is made"
            )

    # ── accounting ───────────────────────────────────────────────────────────
    def report(
        self, *, prompt_chars: int, reply_chars: int | None, model: str | None = None
    ) -> None:
        """Meter one completed (or failed) call. ``reply_chars=None`` = failure."""
        tok_in = prompt_chars // CHARS_PER_TOKEN_EST
        tok_out = (reply_chars or 0) // CHARS_PER_TOKEN_EST
        self.tokens_in += tok_in
        self.tokens_out += tok_out
        if reply_chars is None:
            self.failed_calls += 1
            # a failed call may still have consumed input tokens; price what we know
        cost = self._price(tok_in, tok_out, model)
        if cost is None:
            self.pricing_unknown_calls += 1
        else:
            self.spent += cost

    def _price(self, tok_in: int, tok_out: int, model: str | None) -> float | None:
        if not tok_in and not tok_out:
            return 0.0
        from agent.engine.telemetry import estimate_cost_usd

        return estimate_cost_usd(
            {"prompt_tokens": tok_in, "completion_tokens": tok_out},
            model or self.model or self._channel_model(),
            self.pricing,
        )

    @staticmethod
    def _channel_model() -> str | None:
        try:
            from core.infrastructure.request_context import get_request_llm_channel
        except Exception:  # pragma: no cover - transport absent in odd envs
            return None
        channel = get_request_llm_channel()
        return channel[0] if channel else None

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "tokens_in_est": self.tokens_in,
            "tokens_out_est": self.tokens_out,
            "cost_usd": round(self.spent, 6),
            "pricing_unknown_calls": self.pricing_unknown_calls,
            "cap_usd": self.cap_usd,
        }


class StageGate:
    """Per-stage declared call budget riding on one :class:`RunBudget`.

    ``admit`` order is deliberate: the run-level hard fuse fires FIRST (an exceeded
    run must die even if the stage still has declared calls), then the stage's own
    declared call ceiling. ``max_calls`` is the FULL budget (normal + repair passes
    of server-side closures included) — the declaration, not the conversation, is
    the authority on how many completions a stage may issue.
    """

    def __init__(self, run: RunBudget, *, stage: str, max_calls: int) -> None:
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        self.run = run
        self.stage = stage
        self.max_calls = max_calls
        self.calls = 0

    def admit(self) -> None:
        self.run.check_hard()
        if self.calls >= self.max_calls:
            raise StageBudgetExceeded(
                f"stage {self.stage}: declared LLM budget ({self.max_calls} calls) "
                "exhausted — record the ledger entry and advance honestly"
            )
        self.calls += 1
        self.run.calls += 1

    def settle(self, prompt: str, reply: str | None) -> None:
        self.run.report(prompt_chars=len(prompt), reply_chars=len(reply) if reply is not None else None)

    @property
    def remaining(self) -> int:
        return max(self.max_calls - self.calls, 0)
