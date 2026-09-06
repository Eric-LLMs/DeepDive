"""Tests for the telemetry layer (cost estimate, TurnSpan, AuditSink JSONL)."""
import json
from decimal import Decimal

from agent.engine.telemetry import (
    AuditSink,
    TurnSpan,
    estimate_cost_usd,
    normalize_model_key,
    resolve_model_pricing,
)


def test_estimate_cost_usd_prices_a_known_model():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    assert cost == 0.75  # 0.15 prompt + 0.60 completion


def test_estimate_cost_usd_unknown_model_is_pricing_unknown_not_zero():
    """Tokens were spent but no price resolved → PRICING_UNKNOWN (``None``).

    ``None`` must stay distinguishable from ``0.0`` (nothing to bill); downstream
    (driver ledger, API usage) may never coerce it into a free turn.
    """
    assert estimate_cost_usd({"prompt_tokens": 1_000_000}, "no-such-model") is None


def test_estimate_cost_usd_empty_usage_is_zero():
    assert estimate_cost_usd({}, None) == 0.0
    # Even an unpriceable model yields $0 when there is genuinely nothing to bill.
    assert estimate_cost_usd({"prompt_tokens": 0, "completion_tokens": 0}, "no-such-model") == 0.0


# ── model-key normalization + authoritative-price resolution ─────────────────

def test_normalize_model_key_strips_dates_and_variants():
    assert normalize_model_key("gpt-4o-2024-08-06") == "gpt-4o"
    assert normalize_model_key("Qwen3-Max-2026-01-01") == "qwen3-max"
    assert normalize_model_key("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
    assert normalize_model_key("GPT-4O-Latest") == "gpt-4o"
    # Suffix stripping is per-suffix to a fixed point: the date is removed but a bare
    # family name is never rewritten into another suffixed key.
    assert normalize_model_key("gpt-4o-chat") == "gpt-4o"


def test_fallback_table_lookup_survives_suffixed_model_names():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert estimate_cost_usd(usage, "gpt-4o-mini-2024-07-18") == 0.75


def test_qwen38_flash_catalog_gap_entry():
    """Phase 2B single-entry maintenance: qwen3.8-flash had no priced catalog row, so
    every research turn costed as PRICING_UNKNOWN (``None``). One fallback-table entry
    closes the gap — resolution order and unknown-model behavior stay untouched.
    """
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert resolve_model_pricing("qwen3.8-flash") == (0.05, 0.40)
    assert estimate_cost_usd(usage, "qwen3.8-flash") == 0.45  # not None anymore
    # Precedence unchanged: an injected catalog price still overrides the builtin entry.
    injected = (Decimal("0.0001"), Decimal("0.0002"))
    assert estimate_cost_usd(usage, "qwen3.8-flash", pricing=injected) == 0.30
    # And a genuinely unknown model still yields PRICING_UNKNOWN, never a silent $0.
    assert estimate_cost_usd(usage, "no-such-model-either") is None


def test_resolve_model_pricing_precedence_injected_wins():
    injected = (Decimal("0.0025"), Decimal("0.0100"))
    assert resolve_model_pricing("gpt-4o", injected) is injected
    # No injection: builtin fallback by normalized key.
    assert resolve_model_pricing("gpt-4o-2024-08-06") == (2.50, 10.00)
    # Unpriceable → None (PRICING_UNKNOWN), never a fabricated pair.
    assert resolve_model_pricing("no-such-model") is None


def test_injected_per_1k_decimal_price_matches_billing_math():
    """Catalog prices are per-1k Decimals; the estimate must mirror billing.compute_cost.

    1000 prompt @ 0.0025 + 1000 completion @ 0.0100 → 0.0025 + 0.0100 = 0.0125 USD.
    """
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    cost = estimate_cost_usd(usage, "qwen-max", pricing=(Decimal("0.0025"), Decimal("0.0100")))
    assert cost == 0.0125


def test_injected_price_accepts_floats_without_decimal_errors():
    usage = {"prompt_tokens": 500, "completion_tokens": 500}
    cost = estimate_cost_usd(usage, "some-model", pricing=(0.002, 0.006))
    # 500/1000*0.002 + 500/1000*0.006 = 0.001 + 0.003
    assert cost == 0.004


def test_audit_sink_appends_jsonl_lines(tmp_path):
    sink = AuditSink(tmp_path / "audit.jsonl")
    sink.write({"type": "turn-end", "turn_id": "t1"})
    sink.write({"type": "llm-call", "turn_id": "t2"})

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["turn-end", "llm-call"]
    assert all(json.loads(line)["turn_id"] in {"t1", "t2"} for line in lines)


def test_turn_span_records_steps_tools_and_finishes():
    span = TurnSpan("t1")
    span.record_step(index=0, tool_calls=2, tokens=100, duration_ms=10.0)
    span.record_tool(name="bash", is_error=False, duration_ms=5.0)
    span.finish(cost_usd=0.5)

    d = span.to_dict()
    assert d["turn_id"] == "t1"
    assert d["steps"] == 1
    assert d["tools"] == [{"name": "bash", "is_error": False, "duration_ms": 5.0}]
    assert d["cost_usd"] == 0.5
    assert d["duration_s"] >= 0


def test_turn_span_finish_preserves_pricing_unknown_none():
    """``finish(cost_usd=None)`` keeps None — PRICING_UNKNOWN is not $0."""
    span = TurnSpan("t2")
    span.record_step(index=0, tool_calls=0, tokens=50, duration_ms=10.0)
    span.finish(cost_usd=None)
    assert span.cost_usd is None
    assert span.to_dict()["cost_usd"] is None
