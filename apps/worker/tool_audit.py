"""Phase 2B P0 — per-tool-call observability, mounted worker-side.

Observability only: nothing here changes execution semantics. Two hooks ride the
existing tool lifecycle (see ``agent.engine.runtime.ToolRuntime.execute``):

- ``tools/pre-execute`` (waterfall): stamps a start time per ``call_id`` and delegates
  straight to ``next()`` — it never produces or alters a decision. Registered last, so
  it sees every call that reaches the end of the pre-execute chain (including guard
  denies and everything downstream).
- ``tools/result`` (serial observer): fires on every lifecycle exit path, so exactly one
  ``"tool-call-detail"`` JSONL line is written per tool call, to the same audit file the
  loop writes turn-ends to.

Field contract (every line, success or failure): ``tool``, ``action``, ``stage``,
``turn``, ``step``, ``call_id``, ``is_error``, ``duration_ms``, ``error_type``,
``error_message``, ``sanitized_args``. The error fields are ``null`` on success and, on
failure, are taken verbatim from the engine's real ``ToolFailure`` (exception string,
Draft7 validation message, or deny reason) — never reinterpreted or derived. Args are
redacted first, then the *serialized* field is capped at ``MAX_ARGS_CHARS`` overall, so
page bodies / HTML can never bloat the audit trail.

Known coverage limit (honest): a tool call whose arguments JSON fails to parse inside
the loop never reaches the runtime lifecycle, so it produces no detail line here — and a
deny issued by a pre-execute handler registered before ours has no start stamp
(``duration_ms`` is ``null``). Both would need a loop.py change, which Phase 2B freezes.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.engine.context import current_turn
from agent.engine.telemetry import AuditSink, TraceContext
from core.config import settings

# Hard caps. MAX_ARGS_CHARS bounds the *whole* serialized field (per the redact-first,
# truncate-after rule) — never a per-key budget.
MAX_ARGS_CHARS = 200
MAX_ERROR_CHARS = 500
_REDACTED = "[REDACTED]"

# Keys matched case-insensitively as substrings, so nested variants (access_token,
# X-Api-Key style dict keys from tool args, …) are caught too.
_SENSITIVE_MARKERS = ("password", "token", "api_key", "apikey", "authorization", "secret", "credential")

_starts: dict[str, float] = {}
_disposers: list = []
_installed = False


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(marker in k for marker in _SENSITIVE_MARKERS)


def _sanitize(obj: Any) -> Any:
    """Redact sensitive keys recursively (before any truncation)."""
    if isinstance(obj, dict):
        return {
            k: _REDACTED if _is_sensitive_key(str(k)) else _sanitize(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _render_args(args: Any) -> str:
    """Sanitized, serialized, and globally truncated to MAX_ARGS_CHARS."""
    try:
        s = json.dumps(_sanitize(args), ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        s = _REDACTED
    if len(s) > MAX_ARGS_CHARS:
        s = s[: MAX_ARGS_CHARS - 1] + "…"
    return s


def _project_id(args: dict, turn: Any) -> str | None:
    pid = args.get("project_id")
    if isinstance(pid, str) and pid:
        return pid
    ctx = getattr(turn, "context", None) or {}
    handoff = ctx.get("handoff") if isinstance(ctx.get("handoff"), dict) else {}
    pid = (handoff or {}).get("project_id")
    return pid if isinstance(pid, str) and pid else None


def _resolve_stage(args: dict, turn: Any) -> str | None:
    """Current research stage, read from the project's scratch project.json.

    Null for non-research calls (no project id resolvable) or when the file is absent;
    reading the persisted state keeps the observer free of any service/DB dependency.
    """
    pid = _project_id(args, turn)
    if not pid:
        return None
    root = Path(settings.research_scratch_dir)
    paths: list[Path] = []
    user_id = TraceContext.snapshot().get("user_id")
    if user_id:
        paths.append(root / user_id / pid / "project.json")
    paths.extend(sorted(root.glob(f"*/{pid}/project.json")))
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stage = data.get("stage") if isinstance(data, dict) else None
        return stage if isinstance(stage, str) else None
    return None


async def _stamp_start(exec_: Any, next_: Any) -> Any:
    """Pre-execute passthrough: record t0, then hand the decision chain straight through."""
    if len(_starts) > 10_000:  # safety valve against unbounded growth on lost results
        _starts.clear()
    _starts[exec_.call_id] = time.monotonic()
    return await next_()


def _make_result_observer(sink: AuditSink):
    async def _write_detail(payload: dict) -> None:
        exec_ = payload.get("exec")
        result = payload.get("result")
        if exec_ is None or result is None:
            return
        t0 = _starts.pop(exec_.call_id, None)
        turn = current_turn()
        args = exec_.arguments if isinstance(exec_.arguments, dict) else {}
        action = args.get("action")
        is_error = bool(getattr(result, "is_error", False))
        error_type: str | None = None
        error_message: str | None = None
        if is_error:
            failure = getattr(result, "error", None)
            if failure is not None:
                info = getattr(failure, "info", None) or {}
                error_type = info.get("name")
                if getattr(failure, "message", None) is not None:
                    error_message = str(failure.message)[:MAX_ERROR_CHARS]
        sink.write({
            "type": "tool-call-detail",
            "ts": datetime.now(timezone.utc).isoformat(),
            **TraceContext.snapshot(),
            "tool": exec_.name,
            "action": action if isinstance(action, str) else None,
            "stage": _resolve_stage(args, turn),
            "turn": turn.turn_id if turn is not None else None,
            "step": len(turn.step_usage) if turn is not None else None,
            "call_id": exec_.call_id,
            "is_error": is_error,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1) if t0 is not None else None,
            "error_type": error_type,
            "error_message": error_message,
            "sanitized_args": _render_args(args),
        })

    return _write_detail


def install(kernel: Any, *, sink: AuditSink | None = None) -> bool:
    """Mount the lifecycle hooks on ``kernel.runtime``; idempotent per process.

    Returns ``True`` on the first mount, ``False`` when already installed.
    """
    global _installed
    if _installed:
        return False
    audit = sink or AuditSink(settings.audit_log_path)
    events = kernel.runtime.events
    _disposers.append(events.on("tools/pre-execute", _stamp_start))
    _disposers.append(events.observe("tools/result", _make_result_observer(audit)))
    _installed = True
    return True


def uninstall() -> None:
    """Remove the hooks (tests / hot-reload); drops any in-flight timing state."""
    global _installed
    for dispose in _disposers:
        dispose()
    _disposers.clear()
    _starts.clear()
    _installed = False
