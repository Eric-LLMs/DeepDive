"""P1 step 5 — execution_mode: the 8.14 billing hole, closed.

Three things must hold and are pinned here:
* the mode rides a contextvar that is production unless a scoped observer pins
  otherwise, and every pin is always released (set/reset symmetry);
* ``_log_usage`` NEVER debits the wallet for non-production rows — but still
  records them, tagged, with their true catalog cost (telemetry grouping);
* ``_usage_report`` (both the self-service and the admin view of a user's bill)
  only ever sees production rows.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from api.routers import _shared as shared
from core.infrastructure.request_context import (
    get_request_execution_mode,
    request_execution_mode,
    reset_request_execution_mode,
    set_request_execution_mode,
)


def _user():
    from types import SimpleNamespace
    return SimpleNamespace(
        user_id=uuid4(), token_id=uuid4(),
        role=SimpleNamespace(role_id="free"),
    )


# ── the context seam itself ───────────────────────────────────────────────────────

def test_mode_defaults_to_production_and_set_reset_is_symmetric():
    assert get_request_execution_mode() == "production"
    token = set_request_execution_mode("shadow")
    try:
        assert get_request_execution_mode() == "shadow"
    finally:
        reset_request_execution_mode(token)
    assert get_request_execution_mode() == "production"


def test_unknown_mode_is_refused_at_the_seam():
    with pytest.raises(ValueError, match="unknown execution mode"):
        set_request_execution_mode("debug")


# ── _log_usage: settlement isolation ─────────────────────────────────────────────

class _FakeSession:
    def __init__(self, sink):
        self.sink = sink
        self.commits = 0

    def add(self, row):
        self.sink.append(row)

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def billing(monkeypatch):
    """Patch every external of _log_usage at the router module's import site;
    returns (rows, deductions) — both observed, neither touching a DB."""
    rows, deductions = [], []

    async def prices(session, model):
        return Decimal("0.001"), Decimal("0.002")

    async def balance(session, uid):
        return Decimal(2)

    async def deduct(session, uid, amount, *, description, meta=None):
        deductions.append(amount)

    async def _noop(session):
        return None

    monkeypatch.setattr(shared, "get_model_prices", prices)
    monkeypatch.setattr(shared, "compute_cost", lambda *a: Decimal(5))
    monkeypatch.setattr(shared, "get_balance", balance)
    monkeypatch.setattr(shared, "deduct", deduct)
    # SessionLocal is a sessionmaker: patch the factory itself.
    monkeypatch.setattr(shared, "SessionLocal", lambda: _FakeSession(rows))
    return rows, deductions


def _usage():
    return {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


async def test_production_paid_still_debits_exactly_as_before(billing):
    rows, deductions = billing
    await shared._log_usage(_user(), "m", "chat", _usage(), paid=True)
    assert deductions == [Decimal(2)]  # clamped to the balance, unchanged semantics
    assert rows[0].execution_mode == "production"
    assert float(rows[0].cost_usd) == 5.0  # real cost recorded even if uncharged


async def test_shadow_context_records_the_row_but_never_debits(billing):
    rows, deductions = billing
    token = set_request_execution_mode("shadow")
    try:
        await shared._log_usage(_user(), "m", "chat", _usage(), paid=True)
    finally:
        reset_request_execution_mode(token)
    assert deductions == []
    assert rows[0].execution_mode == "shadow"
    assert rows[0].user_id is not None  # attribution kept for telemetry grouping
    assert float(rows[0].cost_usd) == 5.0


async def test_explicit_kwarg_beats_the_ambient_pin(billing):
    rows, _ = billing
    token = set_request_execution_mode("shadow")
    try:
        await shared._log_usage(_user(), "m", "chat", _usage(), paid=True,
                                execution_mode="preview")
    finally:
        reset_request_execution_mode(token)
    assert rows[0].execution_mode == "preview"


# ── _usage_report: the user's bill is production-only ─────────────────────────────

class _Result:
    def __init__(self, rows=(), scalar=0):
        self._rows, self._scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _SpySession:
    """Catches every compiled statement; answers empty. Proves WHICH predicates
    the report queries carry without needing a live DB."""

    def __init__(self):
        from sqlalchemy.dialects import postgresql
        self._compile = postgresql.dialect()
        self.statements: list[str] = []

    async def execute(self, stmt):
        self.statements.append(str(stmt.compile(
            dialect=self._compile, compile_kwargs={"literal_binds": True}
        )))
        return _Result(scalar=0)


async def test_usage_report_filters_to_production_rows(monkeypatch):
    async def no_txs(session, uid, *, limit):
        return []

    monkeypatch.setattr(shared, "list_transactions", no_txs)
    session = _SpySession()
    out = await shared._usage_report(session, uuid4(), None, None, None, 10, 0)
    assert out["logs"] == [] and out["total"] == 0
    # statements[1] is the COUNT and [2] the page over user_usage_logs
    log_stmts = [s for s in session.statements if "user_usage_logs" in s]
    assert len(log_stmts) == 2
    for s in log_stmts:
        assert "execution_mode" in s and "'production'" in s


def test_request_execution_mode_var_is_the_shared_channel():
    # _log_usage reads the same ContextVar the shadow observer pins — one seam,
    # not two bookkeeping systems.
    assert request_execution_mode.get() == "production"
