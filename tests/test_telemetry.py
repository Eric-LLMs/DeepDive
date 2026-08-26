"""Tests for the telemetry layer (cost estimate, TurnSpan, AuditSink JSONL)."""
import json

from agent.telemetry import AuditSink, TurnSpan, estimate_cost_usd


def test_estimate_cost_usd_prices_a_known_model():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    cost = estimate_cost_usd(usage, "gpt-4o-mini")
    assert cost == 0.75  # 0.15 prompt + 0.60 completion


def test_estimate_cost_usd_unknown_model_is_conservatively_zero():
    assert estimate_cost_usd({"prompt_tokens": 1_000_000}, "no-such-model") == 0.0


def test_estimate_cost_usd_empty_usage_is_zero():
    assert estimate_cost_usd({}, None) == 0.0


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
