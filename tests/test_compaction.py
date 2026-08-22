"""Tests for compact_history: bounded agent history window with a compressed overflow.

Covers: under-limit passthrough, truncation with a reused session summary, synchronous LLM
summarization of the overflow, deterministic degradation when the summary call fails, and the
audit ``compaction`` event.
"""
from types import SimpleNamespace
from uuid import uuid4

from core.config import settings
from core.infrastructure.memory import compact_history


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows if isinstance(self._rows, list) else []


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return _Scalars(self._value)


class _EventRow:
    def __init__(self, payload):
        self.payload = payload


class _FakeSession:
    """Routes queries by entity: SessionModel → scalar summary, SessionEventModel → event rows."""

    def __init__(self, summary, events=None):
        self._summary = summary
        self._events = events or []

    async def execute(self, stmt):
        from core.infrastructure.db import SessionEventModel

        entities = [d.get("entity") for d in stmt.column_descriptions]
        if SessionEventModel in entities:
            return _Result(self._events)
        return _Result(SimpleNamespace(summary=self._summary) if self._summary is not None else None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(summary, events=None):
    return lambda: _FakeSession(summary, events)


class _Events:
    def __init__(self):
        self.events = []

    def record_event(self, type_, payload):
        self.events.append((type_, payload))


class _Llm:
    def __init__(self, *, fail=False):
        self.calls = []
        self._fail = fail

    async def complete(self, prompt, system_prompt="", model=None, base_url=None, api_key=None):
        self.calls.append((model, base_url, api_key))
        if self._fail:
            raise RuntimeError("provider down")
        return "FAKE SUMMARY"


def _history(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n)]


async def test_short_history_passes_through_unchanged():
    llm = _Llm()
    events = _Events()
    history = _history(5)

    out = await compact_history(
        history, session_factory=_factory("existing"), session_id=uuid4(),
        session_memory=events, llm=llm,
    )

    assert out is history
    assert events.events == []
    assert llm.calls == []


async def test_long_history_reuses_existing_session_summary():
    llm = _Llm()
    events = _Events()
    history = _history(45)  # max=40, keep=20 → drops 25

    out = await compact_history(
        history, session_factory=_factory("prior summary"), session_id=uuid4(),
        session_memory=events, llm=llm,
    )

    assert out[0] == {"role": "system", "content": "## Conversation summary\nprior summary"}
    assert [m["content"] for m in out[1:]] == [f"msg {i}" for i in range(25, 45)]
    assert llm.calls == []  # summary already exists → no LLM call
    assert events.events == [
        ("compaction", {"dropped": 25, "had_summary": True, "summary": "prior summary"})
    ]


async def test_long_history_summarizes_overflow_synchronously():
    llm = _Llm()
    events = _Events()
    history = _history(45)

    out = await compact_history(
        history, session_factory=_factory(None), session_id=uuid4(),
        session_memory=events, llm=llm, model="m", base_url="http://chan", api_key="k",
    )

    assert out[0] == {"role": "system", "content": "## Conversation summary\nFAKE SUMMARY"}
    assert llm.calls == [("m", "http://chan", "k")]  # routes through the chat channel
    assert events.events == [
        ("compaction", {"dropped": 25, "had_summary": True, "summary": "FAKE SUMMARY"})
    ]


async def test_summary_failure_still_drops_overflow_without_breaking():
    llm = _Llm(fail=True)
    events = _Events()
    history = _history(45)

    out = await compact_history(
        history, session_factory=_factory(None), session_id=uuid4(),
        session_memory=events, llm=llm,
    )

    assert out[0] == {"role": "assistant", "content": "msg 25"}  # tail head, no summary injected
    assert len(out) == 20
    assert events.events == [("compaction", {"dropped": 25, "had_summary": False})]


async def test_second_compaction_injects_two_tier_recap():
    """Prior compaction summaries (L2) render coarse ahead of the current summary (L1)."""
    llm = _Llm()
    events = _Events()
    history = _history(45)

    out = await compact_history(
        history,
        session_factory=_factory(None, events=[_EventRow({"summary": "earlier window recap"})]),
        session_id=uuid4(),
        session_memory=events,
        llm=llm,
    )

    assert out[0] == {
        "role": "system",
        "content": (
            "## Earlier conversation (coarse)\n"
            "earlier window recap\n\n"
            "## Conversation summary\nFAKE SUMMARY"
        ),
    }
    # the current window's summary is persisted so the next compaction sees it as L2
    assert events.events == [
        ("compaction", {"dropped": 25, "had_summary": True, "summary": "FAKE SUMMARY"})
    ]


async def test_char_budget_triggers_compaction_under_the_message_cap(monkeypatch):
    """A window below the message count but over the char budget still compacts (token-aware)."""
    monkeypatch.setattr(settings, "prompt_max_chars", 1_000)
    llm = _Llm()
    events = _Events()
    # 30 messages (max=40) × ~310 chars each ≫ the 1000-char budget → compact
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i:03d} " + "z" * 300}
        for i in range(30)
    ]

    out = await compact_history(
        history, session_factory=_factory(None), session_id=uuid4(),
        session_memory=events, llm=llm,
    )

    assert out[0] == {"role": "system", "content": "## Conversation summary\nFAKE SUMMARY"}
    assert [m["content"] for m in out[1:]] == [h["content"] for h in history[10:]]
    assert events.events == [
        ("compaction", {"dropped": 10, "had_summary": True, "summary": "FAKE SUMMARY"})
    ]


async def test_under_keep_messages_passes_through_even_when_large(monkeypatch):
    """Within the kept tail there is nothing to drop; oversized singles are left to the snip."""
    monkeypatch.setattr(settings, "prompt_max_chars", 100)
    llm = _Llm()
    events = _Events()
    history = [{"role": "user", "content": "x" * 5000}]  # one huge message, count 1 ≤ keep 20

    out = await compact_history(
        history, session_factory=_factory(None), session_id=uuid4(),
        session_memory=events, llm=llm,
    )

    assert out is history
    assert events.events == []
    assert llm.calls == []
