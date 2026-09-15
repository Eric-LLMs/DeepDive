"""Tests for session-memory v2 compaction primitives (core.infrastructure.memory).

Covers: threshold counting, the dual persistence barrier (client pending mutations /
server flush failure), the full re-fold from raw messages (one LLM call, never
summary-of-summary), the never-trim failure semantics, CAS conflicts, the budget
packing rule with an inclusive watermark, stale boundaries, and the stripped
per-turn re-summarization in ``finalize_session``.
"""
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from core.config import settings
from core.infrastructure.memory import (
    CompactionState,
    apply_compaction,
    finalize_session,
    load_compaction,
    needs_compaction,
    save_compaction,
)

from tests._memory_v2_fakes import (
    Db,
    Llm,
    checkpoint_payload,
    factory,
    make_rows,
    ok_flush_hook,
    tail_from,
)


def _tail(rows, count):
    return tail_from(rows[:count])


async def _apply(db, llm, tail, **kw):
    kw.setdefault("flush_hook", ok_flush_hook)
    return await apply_compaction(
        session_factory=factory(db),
        session_id=uuid4(),
        llm=llm,
        tail=tail,
        has_pending_mutations=False,
        **kw,
    )


# ── threshold counting ───────────────────────────────────────────────────────

def test_needs_compaction_below_threshold_is_false():
    tail = [{"role": "user", "content": "hi"}] * 5
    assert needs_compaction(summary=None, tail=tail, new_message="hello") is False


def test_needs_compaction_on_message_count():
    tail = [{"role": "user", "content": "m"}] * settings.history_max_messages
    assert needs_compaction(summary=None, tail=tail, new_message="x") is True
    # summary counts as one assembled message too
    assert needs_compaction(summary="s", tail=tail[:-1], new_message="x") is True


def test_needs_compaction_on_char_budget_only_above_keep(monkeypatch):
    monkeypatch.setattr(settings, "prompt_max_chars", 1_000)
    big = [{"role": "user", "content": "z" * 300}] * 5  # count ≤ keep → never compacts
    assert needs_compaction(summary=None, tail=big, new_message="x") is False
    bigger = [{"role": "user", "content": "z" * 300}] * 25  # over keep AND over budget
    assert needs_compaction(summary=None, tail=bigger, new_message="x") is True


# ── barrier 1: client pending mutations ──────────────────────────────────────

async def test_client_pending_mutations_defers_before_any_sql_read():
    db = Db(rows=make_rows(30))
    llm = Llm()
    outcome = await apply_compaction(
        session_factory=factory(db), session_id=uuid4(), llm=llm,
        tail=tail_from(db.ordered_rows), has_pending_mutations=True,
    )
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "client_pending_mutations"
    assert db.messages_selects == 0 and db.sessions_selects == 0
    assert llm.calls == []


# ── barrier 2: server-side write-queue flush ─────────────────────────────────

async def test_flush_failure_defers_before_any_fold_read():
    db = Db(rows=make_rows(30))
    llm = Llm()

    async def failing_hook():
        raise RuntimeError("queue flush exploded")

    outcome = await _apply(db, llm, tail_from(db.ordered_rows), flush_hook=failing_hook)
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "persist_barrier_failed"
    # SQL was never read for the fold: flush first, fold reads only after it passes.
    assert db.messages_selects == 0
    assert llm.calls == []


# ── the fold itself ───────────────────────────────────────────────────────────

async def test_fold_is_one_llm_call_from_raw_rows_and_moves_watermark():
    rows = make_rows(45)
    db = Db(rows=rows)  # boundary default = last row
    llm = Llm()
    tail = tail_from(rows)
    outcome = await _apply(
        db, llm, tail, model="chan", base_url="http://chan", api_key="k"
    )

    assert outcome.status == "compacted"
    assert len(llm.calls) == 1                      # exactly ONE fold call
    assert llm.calls[0] == ("chan", "http://chan", "k")
    # fold input = raw rows, never a previous summary
    assert "msg 0" in llm.prompts[0] and "msg 44" in llm.prompts[0]
    assert "FAKE SUMMARY" not in llm.prompts[0]
    # 5-section template is the system prompt
    assert "Primary intent" in llm.systems[0] and "Open tasks & questions" in llm.systems[0]
    # assembled context: summary block + kept tail (overflow fully folded)
    assert outcome.context[0] == {
        "role": "system", "content": "## Conversation summary\nFAKE SUMMARY"
    }
    assert [m["content"] for m in outcome.context[1:]] == [r.text for r in rows[25:]]
    # INCLUSIVE watermark = the boundary row itself
    assert outcome.compaction["through_message_id"] == str(rows[-1].id)
    assert outcome.compaction["revision"] == 1
    saved = json.loads(db.saved_payloads[0])
    assert saved["through_message_id"] == str(rows[-1].id)
    assert saved["revision"] == 1 and saved["fold_count"] == 1


async def test_second_fold_rereads_raw_rows_not_previous_summary():
    rows = make_rows(45)
    db = Db(rows=rows, checkpoint=checkpoint_payload("PREV-COMPACTED-TEXT", revision=7))
    llm = Llm()
    outcome = await _apply(db, llm, tail_from(rows))
    assert outcome.status == "compacted"
    assert "PREV-COMPACTED-TEXT" not in llm.prompts[0]   # full re-fold, no roll-up
    saved = json.loads(db.saved_payloads[0])
    assert saved["revision"] == 8 and saved["fold_count"] == 2


# ── failure NEVER trims (replaces the old "failure still drops overflow") ────

async def test_fold_failure_defers_without_trimming_or_moving_watermark():
    rows = make_rows(45)
    db = Db(rows=rows)
    llm = Llm(fail=True)
    outcome = await _apply(db, llm, tail_from(rows))
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "fold_failed"
    assert outcome.context is None            # caller keeps the FULL context as-is
    assert outcome.compaction is None
    assert db.saved_payloads == []            # watermark did not move


async def test_cas_conflict_defers_and_does_not_double_fold():
    rows = make_rows(45)
    db = Db(rows=rows, cas_ok=False)
    llm = Llm()
    outcome = await _apply(db, llm, tail_from(rows))
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "cas_conflict"
    assert outcome.context is None and outcome.compaction is None
    assert len(llm.calls) == 1                # one attempt; the next turn reloads CAS


# ── boundary resolution ──────────────────────────────────────────────────────

async def test_overflow_without_message_ids_has_no_boundary():
    rows = make_rows(45)
    tail = [{"message_id": None, "role": r.role, "content": r.text} for r in rows]
    outcome = await _apply(Db(rows=rows), Llm(), tail)
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "no_boundary_id"


async def test_stale_boundary_row_defers_without_llm_call():
    rows = make_rows(45)
    db = Db(rows=rows, boundary=None)         # boundary id not in SQL (deleted)
    llm = Llm()
    outcome = await _apply(db, llm, tail_from(rows))
    assert outcome.status == "deferred"
    assert outcome.deferred_reason == "stale_boundary"
    assert llm.calls == [] and db.saved_payloads == []


# ── budget packing: oldest-first, uncovered rows stay at the tail top ────────

async def test_budget_packing_advances_watermark_only_to_last_covered_row(monkeypatch):
    monkeypatch.setattr(settings, "prompt_max_chars", 130)
    rows = make_rows(45)                      # "msg 0" .. "msg 44", 5-7 chars each
    db = Db(rows=rows)
    llm = Llm()
    outcome = await _apply(db, llm, tail_from(rows))
    assert outcome.status == "compacted"
    # budget 130: "msg 0"(5)+"msg 1"(6)+... fits ~21 rows, then a 7-8 char row busts it
    prompt = llm.prompts[0]
    lines = [l for l in prompt.splitlines() if l.startswith(("user: ", "assistant: "))]
    assert 1 < len(lines) < 45
    last_covered = rows[len(lines) - 1]
    assert outcome.compaction["through_message_id"] == str(last_covered.id)  # INCLUSIVE
    # precise check: everything after the fold point survives, oldest first
    contents = [m["content"] for m in outcome.context]
    expected = [r.text for r in rows[len(lines):]]
    assert contents[1:] == expected


# ── checkpoint CAS primitives ────────────────────────────────────────────────

async def test_load_compaction_parses_checkpoint():
    raw = checkpoint_payload("hello")
    db = Db(checkpoint=raw)
    state = await load_compaction(factory(db), uuid4())
    assert isinstance(state, CompactionState)
    assert state.summary == "hello" and state.revision == 3


async def test_save_compaction_first_write_requires_null_checkpoint():
    db = Db(cas_ok=True)
    state = CompactionState(
        revision=1, through_message_id=uuid4(),
        through_created_at=datetime.now(UTC), summary="s", summary_chars=1,
        last_compaction_at=None, fold_count=1,
    )
    assert await save_compaction(factory(db), uuid4(), state, expected_revision=None) is True
    assert db.saved_payloads


# ── finalize_session: per-turn re-summarization is gone ─────────────────────

class _FinResult:
    def __init__(self, msgs, sess):
        self._msgs, self._sess = msgs, sess

    def scalars(self):
        return self

    def all(self):
        return self._msgs

    def scalar_one_or_none(self):
        return self._sess


class _FinSession:
    def __init__(self, msgs, sess):
        self._res = _FinResult(msgs, sess)

    async def execute(self, stmt):
        return self._res

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _sess(**kw):
    base = {"title": "T", "summary": None, "closed_at": None, "compaction": None}
    base.update(kw)
    return SimpleNamespace(**base)


async def test_finalize_never_resummarizes_when_summary_exists(monkeypatch):
    monkeypatch.setattr(settings, "session_summary_enabled", True)
    sess = _sess(summary="already there", compaction={"revision": 1})
    msgs = [SimpleNamespace(role="user", text="q", embedding=[0.1])]
    llm = Llm()
    out = await finalize_session(lambda: _FinSession(msgs, sess), _NoopEmbedder(), llm, uuid4())
    assert out["embedded"] == 0
    assert llm.calls == []                      # NO summary, NO title (already set)
    assert sess.summary == "already there"


class _NoopEmbedder:
    async def embed(self, texts):
        return [[0.0] for _ in texts]


async def test_finalize_first_time_sidebar_summary_only(monkeypatch):
    monkeypatch.setattr(settings, "session_summary_enabled", True)
    sess = _sess(title=None)                    # no summary, no checkpoint, no title
    msgs = [
        SimpleNamespace(role="user", text="first question here", embedding=None),
        SimpleNamespace(role="assistant", text="answer", embedding=None),
    ]
    llm = Llm(reply="SIDEBAR")
    await finalize_session(lambda: _FinSession(msgs, sess), _NoopEmbedder(), llm, uuid4())
    assert len(llm.calls) == 2
    assert sess.summary == "SIDEBAR"


async def test_finalize_skips_sidebar_summary_once_compacted(monkeypatch):
    """A compacted session's sidebar column is owned by the checkpoint copy."""
    monkeypatch.setattr(settings, "session_summary_enabled", True)
    sess = _sess(compaction={"revision": 2, "summary": "S"})
    msgs = [SimpleNamespace(role="user", text="q", embedding=[0.1])]
    llm = Llm()
    await finalize_session(lambda: _FinSession(msgs, sess), _NoopEmbedder(), llm, uuid4())
    assert llm.calls == []
