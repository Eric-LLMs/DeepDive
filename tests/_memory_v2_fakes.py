"""Shared hand-rolled fakes for chat session-memory v2 tests (no real DB).

One statement-text router drives every memory-layer query shape used by
``core.infrastructure.memory``:

- ``UPDATE sessions ...``            → CAS result (rowcount from ``cas_ok``), payload captured
- ``SELECT ... FROM sessions``       → checkpoint dict (compaction col) or the session row
- ``SELECT ... FROM messages``       → no ORDER BY = boundary scalar; WITH ORDER BY = row list
- ``INSERT INTO messages ...``       → RETURNING-shaped rows (write-queue tests)

Every access to the messages/sessions tables is counted so the zero-read hot-path
assertions (Turn N / Turn N+1 SELECT == 0) can be made directly.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

# RETURNING rows behave like ORM rows: tuple-compatible with ATTRIBUTE access.
ReturningRow = namedtuple("ReturningRow", "id created_at role")


def make_rows(n: int, *, start: datetime | None = None, size: int = 0):
    """``n`` model-facing rows: alternating user/assistant, ``msg i`` text (padded to ``size``)."""
    start = start or datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        text = f"msg {i}" + ("z" * size if size > len(f"msg {i}") else "")
        rows.append(
            SimpleNamespace(
                id=uuid4(),
                created_at=start + timedelta(seconds=i),
                role="user" if i % 2 == 0 else "assistant",
                text=text,
                embedding=[0.1],
            )
        )
    return rows


def tail_from(rows):
    """Message rows → the internal client-tail shape (with real ids)."""
    return [
        {"message_id": str(r.id), "role": r.role, "content": r.text} for r in rows
    ]


class Result:
    def __init__(self, scalar=None, rows=None, rowcount=1):
        self._scalar = scalar
        self._rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class Db:
    def __init__(
        self,
        *,
        rows=None,
        boundary="__rows_default__",
        checkpoint=None,
        session_row=None,
        cas_ok=True,
        insert_fail=False,
        insert_n=0,
        insert_roles=None,
    ):
        self.ordered_rows = list(rows or [])
        self.boundary_row = (
            (self.ordered_rows[-1] if self.ordered_rows else None)
            if boundary == "__rows_default__"
            else boundary
        )
        self.checkpoint = checkpoint
        self.session_row = session_row
        self.cas_ok = cas_ok
        self.insert_fail = insert_fail
        self.insert_n = insert_n
        self.insert_roles: list[str] = list(insert_roles or [])
        # counters + captured state
        self.messages_selects = 0
        self.sessions_selects = 0
        self.saved_payloads: list[str] = []
        self.inserts: list[int] = []  # row counts per INSERT
        self.added: list = []
        self.deleted: list = []

    def reset_reads(self):
        self.messages_selects = self.sessions_selects = 0


class FakeSession:
    def __init__(self, db: Db):
        self.db = db

    async def execute(self, stmt, params=None):
        sql = str(stmt).lower()
        db = self.db
        if sql.startswith("update"):
            db.saved_payloads.append(str((params or {}).get("payload")))
            return Result(rowcount=1 if db.cas_ok else 0)
        if "from sessions" in sql:
            db.sessions_selects += 1
            if "sessions.compaction" in sql:
                return Result(scalar=db.checkpoint)
            return Result(scalar=db.session_row)
        if "from session_events" in sql:
            return Result(rows=[])
        if "insert into messages" in sql:
            if db.insert_fail:
                raise RuntimeError("db down")
            n = db.insert_n or len(db.insert_roles) or 1
            roles = list(db.insert_roles)
            db.inserts.append(n)
            now = datetime.now(UTC)
            return Result(
                rows=[
                    ReturningRow(
                        uuid4(), now + timedelta(milliseconds=i),
                        roles[i] if i < len(roles) else "user",
                    )
                    for i in range(n)
                ]
            )
        if "from messages" in sql:
            db.messages_selects += 1
            if "order by" in sql:
                return Result(rows=db.ordered_rows)
            return Result(scalar=db.boundary_row)
        return Result()

    def add(self, obj):
        self.db.added.append(obj)

    async def delete(self, obj):
        self.db.deleted.append(obj)

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def refresh(self, obj):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def factory(db: Db):
    return lambda: FakeSession(db)


class Llm:
    def __init__(self, *, fail=False, reply="FAKE SUMMARY"):
        self.calls: list[tuple] = []  # (model, base_url, api_key)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self._fail = fail
        self._reply = reply

    async def complete(self, prompt, system_prompt="", model=None, base_url=None, api_key=None):
        self.calls.append((model, base_url, api_key))
        self.prompts.append(prompt)
        self.systems.append(system_prompt)
        if self._fail:
            raise RuntimeError("provider down")
        return self._reply


class EventsStore:
    """Stands in for SessionMemoryStore where only record_event is needed."""

    def __init__(self):
        self.events: list[tuple] = []

    def record_event(self, type_, payload):
        self.events.append((type_, payload))


async def ok_flush_hook() -> list[dict]:
    return []


def checkpoint_payload(summary="OLD SUMMARY", *, revision=3):
    mid = uuid4()
    return {
        "revision": revision,
        "through_message_id": str(mid),
        "through_created_at": datetime(2026, 9, 15, 11, 0, 0, tzinfo=UTC).isoformat(),
        "summary": summary,
        "summary_chars": len(summary),
        "last_compaction_at": None,
        "fold_count": 1,
    }
