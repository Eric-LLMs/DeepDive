"""Phase 6 observability console: the read surfaces are admin-gated, file-based
or read-only SQL — pinned hermetically (tmp log files + canned session results).

What must hold: nothing reaches these routes without require_admin; the log
tail filters honestly (an ERROR keeps its stack continuation, a WARNING level
never drops INFO); the events endpoint is pure pass-through of the 8.12 table
(filters ride the ORM predicates); the latency probe reports WHAT WE CANNOT
MEASURE AS NULL instead of inventing numbers.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from api.auth import AuthAdmin, require_admin
from api.routers import observability as ob
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Res:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Sess:
    def __init__(self, results=()):
        self.results = list(results)
        self.executed: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return self.results.pop(0)


def _app(**overrides) -> TestClient:
    app = FastAPI()
    app.include_router(ob.router)
    if overrides.get("admin", True):
        app.dependency_overrides[require_admin] = lambda: AuthAdmin(
            username="root", token_id=uuid4())
    return TestClient(app)


# ── the gate ───────────────────────────────────────────────────────────────────────

def test_anonymous_requests_never_reach_a_handler():
    anon = _app(admin=False)
    for path in ("/admin/observability/logs", "/admin/observability/funnel-events",
                 "/admin/observability/latency"):
        assert anon.get(path).status_code in (401, 403), path


# ── logs: file tail, honest level filter ──────────────────────────────────────────

_LOG = """2026-09-25 10:00:01 INFO app started
2026-09-25 10:00:02 WARNING recall is slow
2026-09-25 10:00:03 ERROR boom at binder
Traceback (most recent call last):
  File "x.py", line 1, in <module>
ValueError: bad card
2026-09-25 10:00:04 INFO back to normal"""


@pytest.fixture()
def tmp_logs(tmp_path, monkeypatch):
    (tmp_path / "api.log").write_text(_LOG, encoding="utf-8")
    monkeypatch.setattr(ob.settings, "log_dir", tmp_path)
    return tmp_path


def test_logs_tail_all_lines(tmp_logs):
    out = _app().get("/admin/observability/logs?level=all").json()
    assert len(out["lines"]) == 7 and not out["missing"]
    assert out["lines"][-1].endswith("back to normal")   # tail = newest kept


def test_logs_error_level_keeps_the_stack_continuation(tmp_logs):
    out = _app().get("/admin/observability/logs?level=error").json()
    lines = out["lines"]
    assert any("boom at binder" in ln for ln in lines)
    assert 'ValueError: bad card' in lines[-1]           # indented trace rode along
    assert not any("INFO" in ln for ln in lines)


def test_logs_missing_file_is_empty_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(ob.settings, "log_dir", tmp_path)
    out = _app().get("/admin/observability/logs").json()
    assert out["missing"] is True and out["lines"] == []


def test_logs_rejects_unknown_file():
    assert _app().get("/admin/observability/logs?file=../etc/passwd").status_code == 422


# ── funnel-events: pass-through with predicates ───────────────────────────────────

class _Event:
    def __init__(self, n):
        self.id = uuid4()
        self.created_at = datetime(2026, 9, 25, tzinfo=UTC)
        self.execution_mode = "production"
        self.deepest_stage = "certified" if n == 0 else "recall"
        self.matcher = "HIT:cap-folder"
        self.recall_count = 1
        self.recall_top = "cap-folder@0.950"
        self.tool_intent = "CONFIDENT:cap-folder"
        self.final_route = "action" if n == 0 else "agent"
        self.fallback_reason = None
        self.capability_id = "cap-folder"
        self.registry_version = "reg1-x"
        self.index_version = "qir1-y"
        self.total_ms = 42 + n
        self.trace_json = {"query": "新建文件夹"} if n == 0 else None


def test_funnel_events_passthrough_and_projection(monkeypatch):
    sess = _Sess(results=[_Res([_Event(0), _Event(1)])])
    monkeypatch.setattr(ob, "SessionLocal", lambda: sess)
    out = _app().get("/admin/observability/funnel-events?limit=2").json()
    assert [e["final_route"] for e in out["events"]] == ["action", "agent"]
    assert out["events"][0]["trace_json"]["query"] == "新建文件夹"
    assert out["events"][1]["trace_json"] is None
    sql = sess.executed[0][0]
    assert "chat_funnel_events" in sql and "ORDER BY" in sql


def test_funnel_events_filters_become_predicates(monkeypatch):
    sess = _Sess(results=[_Res([])])
    monkeypatch.setattr(ob, "SessionLocal", lambda: sess)
    r = _app().get("/admin/observability/funnel-events?mode=preview&route=action"
                   "&capability=cap-folder")
    assert r.status_code == 200
    sql = sess.executed[0][0].lower()
    assert sql.count("where") == 1 and sql.count(" and ") == 2


# ── latency: honest probes ────────────────────────────────────────────────────────

def test_latency_reports_percentiles_and_nulls_latent_gaps(monkeypatch):
    pct = [_Res(rows=[("action", 5, [40.0, 90.0, 120.0]),
                      ("agent", 90, [8.0, 15.0, 20.0])]),
           _Res(rows=[("bge-local", 12, 3400, 0.01)]),
           _Res(rows=[(1,)])]
    monkeypatch.setattr(ob, "SessionLocal", lambda: _Sess(results=pct))

    import httpx

    async def dead_get(self, url, **kw):
        raise OSError("tei down")
    monkeypatch.setattr(httpx.AsyncClient, "get", dead_get)
    out = _app().get("/admin/observability/latency?hours=6").json()
    assert out["window_hours"] == 6
    assert out["funnel_ms"][0] == {"final_route": "action", "count": 5,
                                   "p50": 40.0, "p95": 90.0, "p99": 120.0}
    assert out["llm_activity"][0]["model"] == "bge-local"
    assert out["embed_health"]["ok"] is False        # a dead probe reports DOWN
    assert out["db_ping_ms"] >= 0                    # timed, not invented
    assert out["system"] is None or "cpu_percent" in out["system"]


def test_latency_db_fault_is_reported_not_raised(monkeypatch):
    class _Boom:
        def __call__(self):
            raise _BoomCtx()

    class _BoomCtx:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ob, "SessionLocal", _Boom())
    r = _app().get("/admin/observability/latency")
    assert r.status_code == 200
    assert r.json()["funnel_ms"] is None and "db_error" in r.json()
