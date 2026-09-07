"""Execution ledger: deterministic-id idempotency and SUCCESS immutability (pure doc ops)."""
from __future__ import annotations

import pytest

from workflow.ledger import STATUS_RUNNING, STATUS_SUCCESS, finish_into, record_into

NOW = "2026-09-07T12:00:00Z"


def _doc() -> dict:
    return {"executions": []}


class TestRecordInto:
    def test_first_record_appends_running_row(self):
        doc = _doc()
        row, created = record_into(
            doc, execution_id="r:1:1", tool="t.op", args={"a": 1}, now_iso=NOW,
            extra_fields={"project_id": "p1"},
        )
        assert created is True
        assert doc["executions"] == [row]
        assert row["status"] == STATUS_RUNNING
        assert row["result"] is None
        assert row["project_id"] == "p1"  # adapter column stamped verbatim
        assert row["created_at"] == NOW

    def test_repeated_deterministic_id_is_a_noop_replay(self):
        doc = _doc()
        first, _ = record_into(doc, execution_id="r:1:1", tool="t", args={}, now_iso=NOW)
        again, created = record_into(
            doc, execution_id="r:1:1", tool="OTHER", args={"evil": True}, now_iso=NOW + "X"
        )
        assert created is False
        assert len(doc["executions"]) == 1  # no second row, no mutation
        assert again == first

    def test_generated_id_when_none_given(self):
        doc = _doc()
        r1, c1 = record_into(doc, execution_id=None, tool="t", args={}, now_iso=NOW)
        r2, _ = record_into(doc, execution_id=None, tool="t", args={}, now_iso=NOW)
        assert c1 is True
        assert r1["execution_id"] != r2["execution_id"]  # random ids never collide/dedup

    def test_args_snapshot_is_a_copy(self):
        doc = _doc()
        args = {"mutable": [1]}
        row, _ = record_into(doc, execution_id="e1", tool="t", args=args, now_iso=NOW)
        args["mutable"].append(2)
        assert row["args"] == {"mutable": [1]}  # stored row frozen at record time


class TestFinishInto:
    def test_success_closes_and_freezes(self):
        doc = _doc()
        record_into(doc, execution_id="e1", tool="t", args={}, now_iso=NOW)
        row = finish_into(doc, execution_id="e1", result={"ok": 1}, now_iso=NOW)
        assert row["status"] == STATUS_SUCCESS
        assert row["result"] == {"ok": 1}
        assert row["finished_at"] == NOW

    def test_success_is_immutable(self):
        doc = _doc()
        record_into(doc, execution_id="e1", tool="t", args={}, now_iso=NOW)
        finish_into(doc, execution_id="e1", result="A", now_iso=NOW)
        with pytest.raises(ValueError, match="immutable"):
            finish_into(doc, execution_id="e1", result="B", now_iso=NOW)

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="not found"):
            finish_into(_doc(), execution_id="ghost", result=None, now_iso=NOW)

    def test_non_success_rows_can_be_finished(self):
        doc = _doc()
        record_into(doc, execution_id="e1", tool="t", args={}, now_iso=NOW)
        # a crash-recovery pass could re-finish a never-closed RUNNING row — allowed:
        row = finish_into(doc, execution_id="e1", result=None, now_iso=NOW)
        assert row["status"] == STATUS_SUCCESS
