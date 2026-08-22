"""Tests for compact_history: bounded agent history window with a compressed overflow.

Covers: under-limit passthrough, truncation with a reused session summary, synchronous LLM
summarization of the overflow, deterministic degradation when the summary call fails, and the
audit ``compaction`` event.
"""
from types import SimpleNamespace
from uuid import uuid4

from core.infrastructure.memory import compact_history


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, summary):
        self._summary = summary

    async def execute(self, stmt):
        return _Scalar(SimpleNamespace(summary=self._summary) if self._summary is not None else None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(summary):
    return lambda: _FakeSession(summary)


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
    assert events.events == [("compaction", {"dropped": 25, "had_summary": True})]


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
    assert events.events == [("compaction", {"dropped": 25, "had_summary": True})]


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
