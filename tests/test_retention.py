"""Tests for the audit-event retention cron: the sweep task + the cron-string parser."""
from core.config import settings

from apps.worker.settings import _cron_parts
from apps.worker.tasks import prune_session_events


class _Result:
    rowcount = 7


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def execute(self, stmt):
        return _Result

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_prune_session_events_deletes_expired_rows(monkeypatch):
    monkeypatch.setattr(settings, "session_events_retention_days", 30)
    session = _FakeSession()

    result = await prune_session_events({"session_factory": lambda: session})

    assert result == {"deleted": 7}
    assert session.committed


def test_cron_parts_parses_standard_five_field_schedule():
    assert _cron_parts("17 4 * * *") == {
        "minute": {17},
        "hour": {4},
        "day": None,
        "month": None,
        "weekday": None,
    }
    assert _cron_parts("0,30 9 1-5 * *") == {
        "minute": {0, 30},
        "hour": {9},
        "day": {1, 2, 3, 4, 5},
        "month": None,
        "weekday": None,
    }
