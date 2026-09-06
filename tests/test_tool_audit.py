"""Phase 2B P0 — per-tool-call audit instrumentation tests.

The acceptance bar is field completeness and truthfulness, NOT fewer failures:
observability must not change execution semantics. Pinned here:

1. Every tool call (success, handler raise, schema-validation reject, guard deny)
   produces exactly one ``tool-call-detail`` line carrying ALL contract fields.
2. Error fields: ``null`` on success; on failure verbatim from the engine's real
   ``ToolFailure`` (exception text / Draft7 message / deny reason) — never fabricated.
3. ``sanitized_args``: redact-then-truncate — sensitive keys become ``[REDACTED]`` and
   the whole serialized field stays ≤200 chars (no per-key budgets, no page bodies).
4. Context fields: tool/action/call_id from the execution, turn/step from the bound
   AgentTurn, stage from the project's scratch project.json (null for non-research).
5. Purity: the installed chain returns the same decision/result objects as before.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent import (
    ToolExecution,
    ToolOutput,
    ToolRuntime,
    define_tool,
    text_block,
)
from agent.engine.context import AgentTurn, bind_turn
from agent.engine.telemetry import AuditSink, TraceContext
from apps.worker import tool_audit
from core.config import settings


def _echo_tool(name: str = "echo", *, action_enum: bool = False):
    async def body(args, exec):
        if name == "boom":
            raise ValueError("handler exploded")
        return {"ok": True, "echo": args}

    parameters: dict = {"type": "object", "properties": {"q": {"type": "string"}}}
    if action_enum:
        parameters["properties"]["action"] = {"type": "string", "enum": ["verify", "record_node"]}
    return define_tool(
        name=name,
        description="test tool",
        parameters=parameters,
        output=ToolOutput(schema={"type": "object"}, render=lambda a, v: [text_block("ok")]),
        execute=body,
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A fresh runtime + mounted hooks writing into a tmp audit file.

    Also pins a bound turn with one completed step so turn/step fields resolve.
    """
    monkeypatch.setattr(settings, "research_scratch_dir", tmp_path / "scratch")
    sink_path = tmp_path / "audit.jsonl"
    rt = ToolRuntime()
    rt.register(_echo_tool("echo"))
    rt.register(_echo_tool("boom"))
    rt.register(_echo_tool("evidence", action_enum=True))
    kernel = SimpleNamespace(runtime=rt)
    assert tool_audit.install(kernel, sink=AuditSink(sink_path)) is True
    turn = AgentTurn(user_msg="go")
    turn.step_usage = [{"prompt_tokens": 10, "completion_tokens": 2}]
    bind_turn(turn)
    TraceContext.bind(turn_id=turn.turn_id, user_id="u-test")
    yield rt, sink_path, turn
    tool_audit.uninstall()


def _detail_lines(sink_path: Path) -> list[dict]:
    lines = [json.loads(l) for l in sink_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in lines if r.get("type") == "tool-call-detail"]


CONTRACT_FIELDS = {
    "type", "ts", "trace_id", "turn_id", "user_id", "session_id",
    "tool", "action", "stage", "turn", "step", "call_id",
    "is_error", "duration_ms", "error_type", "error_message", "sanitized_args",
}


class TestFieldCompleteness:
    async def test_success_line_carries_every_field_and_null_errors(self, runtime):
        rt, sink_path, turn = runtime
        result = await rt.execute(ToolExecution(call_id="c1", name="echo", arguments={"q": "hi"}))
        assert result.is_error is False
        lines = _detail_lines(sink_path)
        assert len(lines) == 1
        rec = lines[0]
        assert CONTRACT_FIELDS <= set(rec)
        assert rec["tool"] == "echo"
        assert rec["action"] is None
        assert rec["stage"] is None
        assert rec["turn"] == turn.turn_id
        assert rec["turn_id"] == turn.turn_id
        assert rec["step"] == 1
        assert rec["call_id"] == "c1"
        assert rec["is_error"] is False
        assert rec["duration_ms"] is not None and rec["duration_ms"] >= 0
        # Fixed schema: success MUST carry null error fields.
        assert rec["error_type"] is None
        assert rec["error_message"] is None

    async def test_handler_raise_reports_the_real_exception(self, runtime):
        rt, sink_path, _ = runtime
        result = await rt.execute(ToolExecution(call_id="c2", name="boom", arguments={}))
        assert result.is_error
        rec = _detail_lines(sink_path)[0]
        assert rec["is_error"] is True
        assert rec["error_type"] == "tool_error"          # the engine's own info name
        assert rec["error_message"] == "handler exploded"  # verbatim exception string

    async def test_schema_validation_error_is_the_real_draft7_message(self, runtime):
        rt, sink_path, _ = runtime
        result = await rt.execute(
            ToolExecution(call_id="c3", name="evidence", arguments={"action": "link_edge"})
        )
        assert result.is_error
        rec = _detail_lines(sink_path)[0]
        assert rec["action"] == "link_edge"  # action captured even when rejected
        assert rec["error_type"] == "invalid_args"
        assert "is not one of" in rec["error_message"]  # Draft7 text, not our invention

    async def test_guard_deny_is_recorded_as_failure(self, runtime):
        rt, sink_path, _ = runtime

        async def deny(exec_):
            return "scoped: tool not allowed" if exec_.name == "echo" else None

        rt.guard(deny)
        result = await rt.execute(ToolExecution(call_id="c4", name="echo", arguments={}))
        assert result.is_error
        lines = _detail_lines(sink_path)
        assert len(lines) == 1  # denied calls still produce exactly one line
        rec = lines[0]
        assert rec["is_error"] is True
        assert rec["error_message"] == "scoped: tool not allowed"
        assert rec["error_type"] is None  # guard path carries no info name — null, not guessed

    async def test_one_line_per_call_under_idempotent_install(self, runtime):
        rt, sink_path, _ = runtime
        assert tool_audit.install(SimpleNamespace(runtime=rt)) is False  # idempotent
        for i in range(3):
            await rt.execute(ToolExecution(call_id=f"x{i}", name="echo", arguments={}))
        assert len(_detail_lines(sink_path)) == 3


class TestArgsHygiene:
    async def test_sensitive_keys_redacted_before_serialization(self, runtime):
        rt, sink_path, _ = runtime
        await rt.execute(ToolExecution(
            call_id="s1", name="echo",
            arguments={"api_key": "sk-live-secret", "nested": {"access_token": "tok-999"}, "q": "fine"},
        ))
        rec = _detail_lines(sink_path)[0]
        assert "[REDACTED]" in rec["sanitized_args"]
        assert "sk-live-secret" not in rec["sanitized_args"]
        assert "tok-999" not in rec["sanitized_args"]
        assert "fine" in rec["sanitized_args"]

    async def test_whole_field_capped_at_200_chars(self, runtime):
        rt, sink_path, _ = runtime
        await rt.execute(ToolExecution(
            call_id="s2", name="echo",
            arguments={"a": "X" * 5000, "b": "Y" * 5000},
        ))
        rec = _detail_lines(sink_path)[0]
        assert len(rec["sanitized_args"]) <= 200
        assert "XXXXXYYYYY" not in rec["sanitized_args"]  # not per-key 200 budgets

    def test_redact_then_truncate_order(self):
        # A secret sitting at char >200 must still be redacted, proving sanitization
        # runs on the full structure before truncation touches the serialized string.
        args = {"filler": "f" * 300, "api_key": "sk-DO-NOT-LEAK"}
        s = tool_audit._render_args(args)
        assert "sk-DO-NOT-LEAK" not in s


class TestStageResolution:
    async def test_stage_from_scratch_project_json(self, runtime, tmp_path):
        rt, sink_path, turn = runtime
        pid = "proj-123"
        project = tmp_path / "scratch" / "u-test" / pid / "project.json"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(json.dumps({"stage": "EVIDENCE"}), encoding="utf-8")
        bind_turn(turn)
        turn.context = {"handoff": {"kind": "research", "project_id": pid}}
        await rt.execute(ToolExecution(call_id="g1", name="evidence", arguments={"action": "verify"}))
        rec = _detail_lines(sink_path)[0]
        assert rec["stage"] == "EVIDENCE"  # falls back to the turn handoff project_id

    async def test_project_id_arg_wins_and_missing_project_yields_null(self, runtime):
        rt, sink_path, _ = runtime
        await rt.execute(ToolExecution(
            call_id="g2", name="evidence",
            arguments={"action": "verify", "project_id": "ghost"},
        ))
        rec = _detail_lines(sink_path)[0]
        assert rec["stage"] is None  # no project.json → honest null, never a guess


class TestPurity:
    async def test_instrumentation_changes_nothing_about_the_result(self, runtime):
        rt, _, _ = runtime
        result = await rt.execute(ToolExecution(call_id="p1", name="echo", arguments={"q": "x"}))
        assert result.value == {"ok": True, "echo": {"q": "x"}}
        assert not result.is_error
