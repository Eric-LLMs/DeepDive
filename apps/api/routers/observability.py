"""Observability console (admin, Phase 6 minimal loop): the three read surfaces
the funnel work needs, without inventing a new log pipeline.

* ``logs`` — tail of the rotating ``logs/api.log`` (file read, filtered by level);
* ``funnel-events`` — the telemetry table 8.12 already writes (one row per route
  decision), newest first, with the dark-launched ``trace_json`` when present;
* ``latency`` — percentiles over ``chat_funnel_events.total_ms`` grouped by
  final route, LLM/embedding/database round-trip probes, and process CPU/RAM.

Permission posture follows the registry console (P1 ruling 5): every route rides
the existing ``require_admin`` gate — ordinary users cannot reach this router.
Everything here is read-only; the only "probe" is a cheap ``/health`` call and a
``SELECT 1``, both timed, neither billed.
"""
from __future__ import annotations

import os
import time

from api.auth import AuthAdmin, require_admin
from core.config import settings
from core.infrastructure.db import ChatFunnelEventModel, SessionLocal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

router = APIRouter(tags=["observability"])

# tail budget: enough to debug a bad minute without reading a 10 MB rotation
_LOG_TAIL_BYTES = 1_000_000
_LOG_MAX_LINES = 1000
_LOG_FILES = {"api": "api.log", "worker": "worker.log"}


@router.get("/admin/observability/logs")
async def get_logs(
    file: str = Query("api", pattern="^(api|worker)$"),
    level: str = Query("all", pattern="^(all|warning|error)$"),
    limit: int = Query(200, ge=1, le=_LOG_MAX_LINES),
    _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Tail the process log. ``level`` keeps lines carrying that marker (and,
    for ``error``, the indented stack-trace continuations under them)."""
    import anyio

    path = os.path.join(str(settings.log_dir), _LOG_FILES[file])

    def _read_tail() -> tuple[bytes, int]:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > _LOG_TAIL_BYTES:
                fh.seek(size - _LOG_TAIL_BYTES)
                fh.readline()  # drop the partial first line
            return fh.read(), size

    try:
        blob_bytes, size = await anyio.to_thread.run_sync(_read_tail)
    except FileNotFoundError:
        return {"file": path, "missing": True, "lines": [],
                "total_matched": 0, "truncated": False}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read log: {exc!r}") from exc
    blob = blob_bytes.decode("utf-8", errors="replace")

    wanted = {"warning": ("WARNING",), "error": ("ERROR", "CRITICAL"),
              "all": None}[level]
    lines: list[str] = []
    keep_trace = False
    for raw in blob.splitlines():
        line = raw.rstrip()
        hit = wanted is None or any(m in line for m in wanted)
        # a log line begins with the formatter's timestamp; anything else while
        # inside a kept ERROR is stack-trace residue (frames, chained-cause
        # headers, and the final "ValueError: …" line, which is not indented)
        new_block = bool(line) and line[:4].isdigit()
        cont = bool(line) and not new_block
        if hit:
            lines.append(line)
            keep_trace = wanted is not None and wanted[0] == "ERROR"
        elif cont and keep_trace:
            lines.append(line)
        elif new_block:
            keep_trace = False
    return {
        "file": path,
        "missing": False,
        "lines": lines[-limit:],
        "total_matched": len(lines),
        "truncated": size > _LOG_TAIL_BYTES or len(lines) > limit,
    }


@router.get("/admin/observability/funnel-events")
async def get_funnel_events(
    mode: str | None = None,
    route: str | None = None,
    fallback: str | None = None,
    capability: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Newest funnel decisions (8.12 table). Filters are exact-match predicates
    on the indexed columns; ``trace_json`` (Phase 6 capture) rides along when
    the dark switch had it stored."""
    stmt = select(ChatFunnelEventModel).order_by(
        ChatFunnelEventModel.created_at.desc()).limit(limit)
    if mode:
        stmt = stmt.where(ChatFunnelEventModel.execution_mode == mode)
    if route:
        stmt = stmt.where(ChatFunnelEventModel.final_route == route)
    if fallback:
        stmt = stmt.where(ChatFunnelEventModel.fallback_reason == fallback)
    if capability:
        stmt = stmt.where(ChatFunnelEventModel.capability_id == capability)
    async with SessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return {"events": [{
        "id": str(r.id), "created_at": r.created_at.isoformat() if r.created_at else None,
        "execution_mode": r.execution_mode, "deepest_stage": r.deepest_stage,
        "matcher": r.matcher, "recall_count": r.recall_count, "recall_top": r.recall_top,
        "tool_intent": r.tool_intent, "final_route": r.final_route,
        "fallback_reason": r.fallback_reason, "capability_id": r.capability_id,
        "registry_version": r.registry_version, "index_version": r.index_version,
        "total_ms": r.total_ms, "trace_json": r.trace_json,
    } for r in rows]}


@router.get("/admin/observability/latency")
async def get_latency(
    hours: int = Query(24, ge=1, le=168),
    _: AuthAdmin = Depends(require_admin),
) -> dict:
    """Percentiles + honest live probes. What we cannot measure we report as
    null instead of guessing: ``user_usage_logs`` stores tokens/cost, not
    per-call latency — LLM latency is therefore NOT claimed here."""
    out: dict = {"window_hours": hours}

    # funnel total_ms percentiles per final route, over the window
    try:
        from sqlalchemy import text as sql_text
        async with SessionLocal() as session:
            rows = (await session.execute(sql_text(
                "SELECT final_route, count(*),"
                "       percentile_cont(array[0.5,0.95,0.99])"
                "         WITHIN GROUP (ORDER BY total_ms)"
                "  FROM chat_funnel_events"
                " WHERE created_at > now() - make_interval(hours => :h)"
                " GROUP BY final_route ORDER BY count(*) DESC"),
                {"h": hours})).all()
            out["funnel_ms"] = [
                {"final_route": r[0], "count": r[1],
                 "p50": float(r[2][0]), "p95": float(r[2][1]),
                 "p99": float(r[2][2])}
                for r in rows
            ]
            # LLM activity in the window (tokens/cost per model — no latency
            # column exists; the report says so rather than inventing one)
            llm_rows = (await session.execute(sql_text(
                "SELECT model_name, count(*), sum(total_tokens), sum(cost_usd)"
                "  FROM user_usage_logs"
                " WHERE execution_mode = 'production'"
                "   AND created_at > now() - make_interval(hours => :h)"
                " GROUP BY model_name ORDER BY count(*) DESC LIMIT 10"),
                {"h": hours})).all()
            out["llm_activity"] = [
                {"model": r[0] or "—", "calls": r[1],
                 "tokens": int(r[2] or 0), "cost_usd": float(r[3] or 0)}
                for r in llm_rows
            ]
            # DB round-trip: one trivial statement, timed
            t0 = time.perf_counter()
            await session.execute(sql_text("SELECT 1"))
            out["db_ping_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as exc:  # noqa: BLE001 - observability must not 500 loudly
        out["funnel_ms"] = None
        out["db_error"] = repr(exc)

    # embedding service probe (TEI /health — cheap, not a billed embed)
    try:
        import httpx
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=2.0) as client:
            res = await client.get(f"{settings.embedding_base_url.rstrip('/')}/health")
        out["embed_health"] = {
            "ok": res.status_code == 200,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        out["embed_health"] = {"ok": False, "error": repr(exc)}

    # process gauges — psutil is declared but tolerated-absent
    try:
        import psutil
        out["system"] = {
            "cpu_percent": psutil.cpu_percent(interval=0.15),
            "mem_percent": psutil.virtual_memory().percent,
            "rss_mb": round(
                psutil.Process(os.getpid()).memory_info().rss / 1048576, 1),
        }
    except Exception:  # noqa: BLE001 - absent probe reports null, never fails
        out["system"] = None
    return out
