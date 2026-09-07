"""Idempotent execution ledger: the pure record/finish algorithm over a document.

Same discipline as an activity log in every mature engine (Temporal's history, Conductor's
task instances, Step Functions' immutable execution history): a *deterministic* identity
lets a crash-rerun re-record the same execution as a no-op instead of minting a second
RUNNING row, and a SUCCESS row is final — never re-opened, never overwritten.

The functions are pure transforms over a plain ``{"executions": [...]}`` document; the
adapter owns loading, persistence, and any surrounding file locking. ``extra_fields``
lets the adapter stamp its own row columns (e.g. the scoped id) without the core naming
them.
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"


def record_into(
    doc: Mapping[str, Any],
    *,
    execution_id: str | None,
    tool: str,
    args: Mapping[str, Any],
    now_iso: str,
    extra_fields: Mapping[str, Any] | None = None,
) -> tuple[dict, bool]:
    """Append one RUNNING row, or return the existing one when the identity repeats.

    Returns ``(row, created)``. Idempotent replay (``created=False``) hands back the
    ORIGINAL row — status and all — so the caller can tell a rerun from a first run.
    """
    if execution_id is not None:
        for row in doc.get("executions", []):
            if row.get("execution_id") == execution_id:
                return dict(row), False
    row: dict[str, Any] = {
        "execution_id": execution_id or str(uuid.uuid4()),
        "tool": tool,
        "args": copy.deepcopy(dict(args)),
        "status": STATUS_RUNNING,
        "result": None,
        "created_at": now_iso,
    }
    if extra_fields:
        row.update(extra_fields)
    doc.setdefault("executions", []).append(row)
    return dict(row), True


def finish_into(
    doc: Mapping[str, Any],
    *,
    execution_id: str,
    result: Any,
    now_iso: str,
) -> dict:
    """Close a row SUCCESS-first-and-forever: unknown id raises, SUCCESS is immutable."""
    for row in doc.get("executions", []):
        if row.get("execution_id") == execution_id:
            if row["status"] == STATUS_SUCCESS:
                raise ValueError("execution is immutable: already finished")
            row["status"] = STATUS_SUCCESS
            row["result"] = result
            row["finished_at"] = now_iso
            return dict(row)
    raise ValueError(f"execution not found: {execution_id}")
