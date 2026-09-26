"""Intent Registry tests — LIVE-table plane (migration 0014, final ruling).

Doctrine under test:

* the live tables ARE the runtime truth — no Draft, no Publish, no Projection;
* capability edits are optimistic-concurrency (row_version token);
* query writes are embed-then-write atomic (a failed embed aborts the whole
  modification); registry_versions is history only;
* the validation gate is the same pure function every write path calls.

The fake-session pattern (queue canned ``execute`` results, record ``add``/
``commit``) pins the STATE MACHINE the store is responsible for. SQL semantics
themselves (UNIQUE constraints, vector columns) are verified against the real
PG out-of-band by migration 0014 + scripts/embed_corpus.py.
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from uuid import UUID

import pytest
from core.application.chat.intent_funnel import registry as reg
from core.application.chat.intent_funnel.registry import entry as T
from core.application.chat.intent_funnel.registry import queries as q
from core.application.chat.intent_funnel.registry import snapshot
from core.application.chat.intent_funnel.registry import store as reg_store
from core.infrastructure.db import RegistryVersionModel
from sqlalchemy.exc import IntegrityError

_MISSING = object()

# The validation roster is a projection of ToolRuntime.schemas(); unit tests
# canned it here (tool -> {slot: max_len}).
ROSTER = {
    "create_folder": {"name": 120},
    "add_term": {"term": 120, "domain": 60},
}


def _std(qid, text, lang, **kw):
    base = {"id": qid, "query": text, "language": lang}
    base.update(kw)
    return T.QueryRecord(**base)


def _sim(qid, text, lang, parent, **kw):
    base = {"id": qid, "query": text, "language": lang, "standard_query_id": parent}
    base.update(kw)
    return T.QueryRecord(**base)


def _entry(cid="cap-a", **kw) -> T.CapabilityEntry:
    base = {
        "capability_id": cid,
        "tool_binding": "create_folder",
        "description": "Create a folder.",
        # 0014: the query rows ARE the corpus — one enabled Standard per
        # language makes a routable capability consistent.
        "standard_queries": (_std("s1", "新建文件夹", "zh"),
                          _std("s2", "create a folder", "en")),
        "similar_queries": (_sim("m1", "建个目录", "zh", "s1"),),
        # inert columns: storable but never match or recall anchors
        "patterns": (),
        "aliases": (),
        "request_query_examples": ('创建一个叫 "x" 的文件夹',),
        "negatives": ("不要新建文件夹",),
        "parameters": {"name": {"type": "string", "required": True,
                             "max_len": 120, "description": "folder name"}},
        "arg_slots": {"name": "user_input"},
        "permissions": "",
        "execution_policy": "auto",
    }
    base.update(kw)
    return T.CapabilityEntry(**base)


class _Result:
    def __init__(self, rows=None, scalar=_MISSING, first=_MISSING, rowcount=1):
        self._rows = rows or []
        self._scalar = scalar
        self._first = first
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        if self._scalar is _MISSING:
            raise AssertionError("scalar_one not canned")
        return self._scalar

    def scalar_one_or_none(self):
        if self._scalar is _MISSING:
            raise AssertionError("scalar_one_or_none not canned")
        return self._scalar

    def first(self):
        if self._first is _MISSING:
            raise AssertionError("first not canned")
        return self._first


def _cap_row(cid="cap-a", row_version=1, **kw):
    d = {
        "capability_id": cid, "tool_binding": "create_folder",
        "description": "Create a folder.", "patterns": [], "aliases": [],
        "request_query_examples": [], "parameters": {}, "arg_slots": {},
        "permissions": "", "execution_policy": "auto", "intent_kind": "action",
        "enabled": True, "status": "active", "replacement_capability_id": None,
        "row_version": row_version,
    }
    d.update(kw)
    return SimpleNamespace(**d)


def _read_sessions(entry_row):
    """The four executes get_capability performs: cap row, then empty
    std/sim/neg child sets."""
    return FakeSession(results=[
        _Result(scalar=entry_row), _Result(rows=[]),
        _Result(rows=[]), _Result(rows=[]),
    ])


class FakeSession:
    def __init__(self, results=(), get_map=None, commit_error=None):
        self.results = list(results)
        self.get_map = get_map or {}
        self.commit_error = commit_error
        self.added: list = []
        self.deleted: list = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def execute(self, stmt):
        return self.results.pop(0)

    async def get(self, model, pk):
        return self.get_map.get((model, pk))

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def factory(*sessions):
    """Mirrors SessionLocal: each call hands out the next canned session; calling
    it with none queued is a test failure (proves the code path stayed in its
    canned lane)."""
    pool = list(sessions)

    def _f():
        assert pool, "store opened more sessions than canned"
        return pool.pop(0)

    return _f


class FakeEmbedder:
    def __init__(self, vec=(1.0, 0.0), exc=None):
        self.vec, self.exc, self.calls = vec, exc, 0

    async def embed(self, texts):
        self.calls += 1
        if self.exc:
            raise self.exc
        return [list(self.vec) for _ in texts]


# ── value types (pure) ─────────────────────────────────────────────────────────────

def test_entry_payload_roundtrip_preserves_every_field():
    e = _entry(enabled=False, status="deprecated", replacement_capability_id="cap-b",
               row_version=3)
    back = T.CapabilityEntry.from_payload(e.to_payload())
    assert back == dataclasses.replace(e, row_version=0)
    # row_version is live-table bookkeeping — history payloads carry none
    assert back.row_version == 0


def test_intent_corpus_is_enabled_query_rows_only():
    e = _entry(
        standard_queries=(_std("s1", "新建文件夹", "zh"),
                          _std("s2", "create a folder", "en"),
                          _std("s3", "  ", "en"),
                          _std("s4", "关掉的标准", "zh", enabled=False)),
        similar_queries=(_sim("m1", "建个目录", "zh", "s1"),
                         _sim("m2", "make a dir", "en", "s2", enabled=False)),
    )
    # standards by position, then similars; blanks and disabled rows out
    assert e.intent_corpus == ("新建文件夹", "create a folder", "建个目录")
    # card context / card boundary never enter the corpus
    assert "不要新建文件夹" not in e.intent_corpus
    assert '创建一个叫 "x" 的文件夹' not in e.intent_corpus


def test_derive_language_frozen_rule():
    assert T.derive_language("新建文件夹") == "zh"
    assert T.derive_language("create a folder") == "en"
    assert T.derive_language("make 一个 mixed") == "zh"  # ANY Han char -> zh
    assert T.derive_language("") == "en"


def test_fingerprint_order_independent_and_row_version_blind():
    a, b = _entry("cap-a"), _entry("cap-b", tool_binding="add_term")
    fp1 = reg.content_fingerprint([a, b])
    fp2 = reg.content_fingerprint([b, a])
    fp3 = reg.content_fingerprint([a, b, _entry("cap-c")])
    fp4 = reg.content_fingerprint([_entry("cap-a", row_version=9), b])
    assert fp1 == fp2 != fp3
    assert fp1 == fp4  # edit-token must not perturb content identity
    assert fp1.startswith("live1-")


def test_corpus_fingerprint_moves_with_query_rows():
    a = _entry("cap-a", standard_queries=(_std("s1", "新建文件夹", "zh"),))
    b = _entry("cap-a", standard_queries=(
        _std("s1", "新建一个文件夹", "zh"),))  # same row id, new text
    assert reg.content_fingerprint([a]) != reg.content_fingerprint([b])


# ── capability CRUD (live row, optimistic concurrency) ─────────────────────────────

async def test_update_capability_rejects_unknown_fields_before_touching_db():
    with pytest.raises(ValueError, match="non-capability fields"):
        await reg.update_capability(
            "cap-a", {"capability_id": "evil"}, 0,
            session_factory=factory(),  # empty pool: any open fails the test
        )


async def test_update_capability_bumps_token_on_hit():
    hit = FakeSession(results=[_Result(rowcount=1)])
    out = _read_sessions(_cap_row(row_version=1))
    res = await reg.update_capability(
        "cap-a", {"description": "Changed."}, 0,
        session_factory=factory(hit, out),
    )
    assert hit.commits == 1 and hit.rollbacks == 0
    assert res.row_version == 1  # the token visible to the next writer


async def test_update_capability_conflict_is_not_a_silent_overwrite():
    miss = FakeSession(results=[
        _Result(rowcount=0),            # UPDATE matched nobody
        _Result(scalar="cap-a"),        # ... but the row EXISTS
    ])
    with pytest.raises(reg.RegistryConflictError):
        await reg.update_capability("cap-a", {"description": "x"}, 0,
                                    session_factory=factory(miss))
    assert miss.commits == 0 and miss.rollbacks == 1


async def test_update_capability_missing_row_raises_not_found():
    miss = FakeSession(results=[_Result(rowcount=0), _Result(scalar=None)])
    with pytest.raises(reg.RegistryNotFoundError):
        await reg.update_capability("cap-gone", {"description": "x"}, 0,
                                    session_factory=factory(miss))


async def test_create_capability_duplicate_id_is_a_conflict():
    boom = FakeSession(commit_error=IntegrityError("stmt", {}, Exception("dup key")))
    with pytest.raises(reg.RegistryConflictError, match="already exists"):
        await reg.create_capability(_entry(), session_factory=factory(boom))
    assert boom.rollbacks == 1


async def test_create_capability_writes_row_then_reads_it_back():
    ins = FakeSession()
    out = _read_sessions(_cap_row(row_version=0))
    res = await reg.create_capability(_entry(), session_factory=factory(ins, out))
    assert ins.commits == 1
    assert isinstance(ins.added[0], reg_store.CapabilityModel)
    assert res.capability_id == "cap-a"


# ── history snapshots (audit only — nothing runtime reads them) ───────────────────

async def test_snapshot_history_freezes_next_number_as_historical():
    s = FakeSession(
        results=[_Result(scalar=7)],
        get_map={(RegistryVersionModel, 8): SimpleNamespace(
            version=8, state="historical", payload={}, fingerprint="live1-x",
            source_version=None, actor_username="admin", note=None, error=None,
            created_at=None, activated_at=None)},
    )
    v = await reg.snapshot_history([_entry()], actor_username="admin",
                                   session_factory=factory(s))
    added = s.added[0]
    assert v == 8
    assert added.version == 8 and added.state == "historical"
    assert added.fingerprint == reg.content_fingerprint([_entry()])
    assert s.commits == 1


async def test_snapshot_history_retries_the_number_race():
    loser = FakeSession(results=[_Result(scalar=7)],
                        commit_error=IntegrityError("s", {}, Exception("dup pk")))
    winner = FakeSession(results=[_Result(scalar=8)])
    v = await reg.snapshot_history([_entry()], session_factory=factory(loser, winner))
    assert winner.added[0].version == 9  # recomputed max on retry
    assert v == 9


async def test_version_entries_rebuilds_from_payload():
    entry = _entry()
    s = FakeSession(get_map={(RegistryVersionModel, 3): SimpleNamespace(
        payload={"capabilities": [entry.to_payload()]})})
    out = await reg.version_entries(3, session_factory=factory(s))
    assert out == [dataclasses.replace(entry, row_version=0)]


async def test_get_version_missing_raises_not_found():
    s = FakeSession()
    with pytest.raises(reg.RegistryNotFoundError):
        await reg.get_version(99, session_factory=factory(s))


# ── runtime read: the live view, marker-cached ─────────────────────────────────────

def _marker_sessions(marker, *, caps=None, stds=(), sims=(), negs=()):
    """Two canned sessions per active_view MISS: the marker peek, then the
    four-query hydrate (caps/std/sim/neg all via ``.scalars().all()``)."""
    peek = FakeSession(results=[_Result(first=marker)])
    hydrate = FakeSession(results=[
        _Result(rows=[caps] if caps else []), _Result(rows=list(stds)),
        _Result(rows=list(sims)), _Result(rows=list(negs)),
    ])
    return [peek, hydrate]


async def test_active_view_caches_until_the_content_marker_moves():
    marker = ("1:ts", "2:ts", "1:ts", "0:-")
    a, b = _marker_sessions(marker, caps=_cap_row(),
                            stds=(_cap_std("s1"), _cap_std("s2", "create a folder", "en")))
    c = FakeSession(results=[_Result(first=marker)])  # cache: marker-only session
    d, e = _marker_sessions(("0:-", "0:-", "0:-", "0:-"))  # tables emptied
    f = factory(a, b, c, d, e)
    reg.invalidate_cache()
    v1 = await reg.active_view(session_factory=f)
    v2 = await reg.active_view(session_factory=f)
    assert v1 is not None and v2 is v1  # second call only peeked at the marker
    v3 = await reg.active_view(session_factory=f)
    assert v3 is None and reg_store._live_cache is None  # empty tables -> no view


async def test_active_view_marker_change_reloads_hydration():
    s = [x for x in _marker_sessions(("1:a", "2:a", "1:a", "0:-"),
                                     caps=_cap_row(),
                                     stds=(_cap_std("s1"),
                                           _cap_std("s2", "create a folder", "en")))]
    s += _marker_sessions(("2:b", "2:a", "1:a", "0:-"),
                          caps=_cap_row(row_version=4),
                          stds=(_cap_std("s1"),
                                _cap_std("s2", "create a folder", "en")))
    f = factory(*s)
    reg.invalidate_cache()
    await reg.active_view(session_factory=f)
    v = await reg.active_view(session_factory=f)
    assert v is not None and v.entries[0].capability_id == "cap-a"
    assert v.fingerprint == reg.content_fingerprint(list(v.entries))


def _cap_std(qid, text="新建文件夹", lang="zh", **kw):
    d = {"id": qid, "capability_id": "cap-a", "query": text, "language": lang,
             "enabled": True, "position": 0}
    d.update(kw)
    return SimpleNamespace(**d)


# ── query plane: embed-then-write atomicity ────────────────────────────────────────

async def test_add_standard_blank_rejects_without_touching_db():
    with pytest.raises(ValueError, match="blank"):
        await q.add_standard_query("cap-a", "   ", embedder=FakeEmbedder(),
                                   session_factory=factory())


async def test_add_standard_one_per_language_conflict():
    check = FakeSession(results=[
        _Result(scalar="cap-a"),   # capability exists
        _Result(scalar="zh"),      # a zh Standard is already taken
    ])
    emb = FakeEmbedder()
    with pytest.raises(reg.RegistryConflictError, match="one per language"):
        await q.add_standard_query("cap-a", "再建一个", embedder=emb,
                                   session_factory=factory(check))
    assert emb.calls == 0  # rejected BEFORE embedding


async def test_add_standard_embed_failure_aborts_the_whole_write():
    check = FakeSession(results=[_Result(scalar="cap-a"), _Result(scalar=None)])
    with pytest.raises(Exception, match="tei down"):
        # only ONE session canned: opening a second (the write) fails the test
        await q.add_standard_query("cap-a", "new folder please",
                                   embedder=FakeEmbedder(exc=RuntimeError("tei down")),
                                   session_factory=factory(check))


async def test_add_standard_success_embeds_then_inserts_vector():
    check = FakeSession(results=[_Result(scalar="cap-a"), _Result(scalar=None)])
    write = FakeSession()
    emb = FakeEmbedder(vec=(0.5, 0.5))
    out = await q.add_standard_query("cap-a", "新建一个文件夹", embedder=emb,
                                     session_factory=factory(check, write))
    assert emb.calls == 1
    row = write.added[0]
    assert row.language == "zh" and list(row.embedding) == [0.5, 0.5]
    assert out["language"] == "zh" and write.commits == 1


async def test_add_similar_language_must_match_its_standard():
    std = SimpleNamespace(id="s1", language="zh")
    sid = "00000000-0000-0000-0000-000000000005"
    check = FakeSession(get_map={(reg_store.CapabilityStandardQueryModel,
                                  UUID(sid)): std})
    emb = FakeEmbedder()
    with pytest.raises(ValueError, match="disagrees"):
        await q.add_similar_query(sid, "an English sentence", embedder=emb,
                                  session_factory=factory(check))
    assert emb.calls == 0


async def test_add_similar_unknown_standard_raises_not_found():
    check = FakeSession()  # get() finds nothing
    with pytest.raises(reg.RegistryNotFoundError):
        await q.add_similar_query("00000000-0000-0000-0000-000000000000",
                                  "建个目录", embedder=FakeEmbedder(),
                                  session_factory=factory(check))


async def test_add_negative_needs_no_embedder_but_needs_the_capability():
    check = FakeSession(results=[_Result(scalar=None)])  # capability missing
    with pytest.raises(reg.RegistryNotFoundError):
        await q.add_negative_query("cap-gone", "不要新建文件夹",
                                   session_factory=factory(check))
    assert check.rollbacks == 1


async def test_enable_refused_while_vector_missing():
    row = SimpleNamespace(id="x", embedding=None, enabled=True,
                          standard_query_id=None)
    qid = "00000000-0000-0000-0000-000000000001"
    s = FakeSession(get_map={(reg_store.CapabilityStandardQueryModel,
                              UUID(qid)): row})
    with pytest.raises(reg.RegistryConflictError, match="backfill"):
        await q.set_query_enabled("standard", qid, True,
                                  session_factory=factory(s))


async def test_disable_standard_is_independent_of_enabled_similar_children():
    """Ruling 2026-09-26: Standard/Similar enabled flags are independent. A
    Standard with an enabled Similar child disables cleanly; the child's own
    flag is untouched (recall SQL chains through the parent, so the child
    merely stops participating until the Standard is re-enabled)."""
    row = SimpleNamespace(id="x", embedding=[0.1], enabled=True)
    kid = SimpleNamespace(id="kid", enabled=True)
    qid = "00000000-0000-0000-0000-000000000002"
    s = FakeSession(
        get_map={(reg_store.CapabilityStandardQueryModel, UUID(qid)): row},
    )
    out = await q.set_query_enabled("standard", qid, False,
                                    session_factory=factory(s))
    assert out == {"id": qid, "enabled": False}
    assert row.enabled is False and s.commits == 1
    assert kid.enabled is True                     # no cascade onto the child
    # and re-enabling the Standard is a plain flag flip (embedding present)
    s2 = FakeSession(
        get_map={(reg_store.CapabilityStandardQueryModel, UUID(qid)): row},
    )
    out2 = await q.set_query_enabled("standard", qid, True,
                                     session_factory=factory(s2))
    assert out2 == {"id": qid, "enabled": True}
    assert row.enabled is True and s2.commits == 1


async def test_delete_standard_with_children_needs_cascade():
    qid = "00000000-0000-0000-0000-000000000003"
    row = SimpleNamespace(id="x")
    s = FakeSession(
        get_map={(reg_store.CapabilityStandardQueryModel, UUID(qid)): row},
        results=[_Result(rows=[SimpleNamespace(id="kid")])],
    )
    with pytest.raises(reg.RegistryConflictError, match="cascade_similar"):
        await q.delete_query("standard", qid, session_factory=factory(s))
    # with cascade the child rides along into the same delete session
    s2 = FakeSession(
        get_map={(reg_store.CapabilityStandardQueryModel, UUID(qid)): row},
        results=[_Result(rows=[SimpleNamespace(id="kid")])],
    )
    await q.delete_query("standard", qid, cascade_similar=True,
                         session_factory=factory(s2))
    assert len(s2.deleted) == 2 and s2.commits == 1


async def test_update_text_rejects_negative_table_without_touching_db():
    with pytest.raises(ValueError, match="standard.*similar"):
        await q.update_query_text("negative",
                                  "00000000-0000-0000-0000-000000000004", "x",
                                  embedder=FakeEmbedder(), session_factory=factory())


# ── validation gate (same function every write calls) ──────────────────────────────

def test_validate_refuses_an_empty_registry():
    assert any("empty Registry" in i for i in snapshot.validate_entries([]))


def test_validate_accepts_the_full_source_enum_including_plugin_forms():
    e = _entry(arg_slots={
        "a": "user_input", "b": "viewer.current_page", "c": {"source": "viewer.selection"},
        "d": "attachment", "e": "turn_context", "f": "fixed",
        "g": "plugin:quoted_folder_name",  # 8.1-b wiring: name must be in the roster
    })
    assert snapshot.validate_entries([e]) == []


@pytest.mark.parametrize("slot,bad", [
    ("regex:aliases", "minimal enum"),             # NOT in the frozen enum
    ("plugin:", "needs a name"),
    ("plugin:no_such_extractor", "not registered"),  # roster membership (8.1-b)
    (123, "must be a string"),
    ({"nope": "user_input"}, "must be a string"),
])
def test_validate_rejects_bad_arg_slot_sources(slot, bad):
    issues = snapshot.validate_entries([_entry(arg_slots={"x": slot})])
    assert issues and bad in issues[0]


def test_validate_rejects_tool_outside_the_live_roster():
    issues = snapshot.validate_entries([_entry(tool_binding="launch_missiles")],
                                       tool_schemas=ROSTER)
    assert any("ToolRuntime roster" in i for i in issues)


def test_routable_needs_enabled_standard_per_language():
    # missing the English Standard -> routable rows are refused...
    one_lang = _entry(standard_queries=(_std("s1", "新建文件夹", "zh"),),
                      similar_queries=())
    assert any("per language" in i for i in snapshot.validate_entries([one_lang]))
    # ...a DISABLED row may lawfully wait for its corpus (born-disabled create)
    off = dataclasses.replace(one_lang, enabled=False, status="disabled")
    assert not [i for i in snapshot.validate_entries([off])
                if "per language" in i or "corpus" in i]


def test_validate_requires_corpus_only_for_routable():
    inert = _entry(standard_queries=(), similar_queries=(),
                   enabled=False, status="disabled")
    assert not [i for i in snapshot.validate_entries([inert]) if "corpus" in i]
    live = _entry(standard_queries=(), similar_queries=())
    assert any("query corpus must be non-empty" in i
               for i in snapshot.validate_entries([live]))
    # card context alone never satisfies the corpus requirement
    card_only = _entry(standard_queries=(), similar_queries=(),
                       request_query_examples=("帮我建个文件夹",))
    assert any("corpus" in i for i in snapshot.validate_entries([card_only]))


def test_gate_bridges_registry_parameters_to_runtime_roster():
    missing = _entry(parameters={})                     # create_folder needs 'name'
    assert any("missing runtime slot" in i
               for i in snapshot.validate_entries([missing], tool_schemas=ROSTER))
    extra = _entry(tool_binding="add_term", parameters={
        "term": {"type": "string", "required": True, "max_len": 120, "description": "t"},
        "domain": {"type": "string", "required": True, "max_len": 60, "description": "d"},
        "ghost": {"type": "string", "required": True, "description": "g"},
    })
    assert any("unknown to runtime" in i
               for i in snapshot.validate_entries([extra], tool_schemas=ROSTER))
    drift = _entry(parameters={"name": {"type": "string", "required": True,
                                        "max_len": 999, "description": "n"}})
    assert any("max_len" in i
               for i in snapshot.validate_entries([drift], tool_schemas=ROSTER))
    # a runtime slot WITHOUT a stated maxLength carries no bound to contradict
    unbounded = _entry(tool_binding="add_term", parameters={
        "term": {"type": "string", "required": True, "max_len": 120, "description": "t"},
        "domain": {"type": "string", "required": True, "description": "d"},
    })
    assert not [i for i in snapshot.validate_entries([unbounded],
                                                     tool_schemas={
                                                         "add_term": {"term": 120, "domain": 0}})
                if "max_len" in i]
    shape = _entry(parameters={"name": {"description": "no type, no required"}})
    assert any(".type is required" in i
               for i in snapshot.validate_entries([shape], tool_schemas=ROSTER))
    # the agreeing default entry passes the bridge silently
    assert not [i for i in snapshot.validate_entries([_entry()],
                                                     tool_schemas=ROSTER)
                if "parameters" in i]


def test_validate_rejects_corpus_conflict_between_routable_caps():
    a = _entry("cap-a")
    b = _entry("cap-b", tool_binding="add_term", parameters={
        "term": {"type": "string", "required": True, "max_len": 120, "description": "t"},
        "domain": {"type": "string", "required": True, "max_len": 60, "description": "d"},
    }, standard_queries=(_std("t1", "Create a Folder", "en"),
                         _std("t2", "新建文件夹", "zh")),  # casefold collision with cap-a
        similar_queries=())
    issues = snapshot.validate_entries([a, b])
    assert any("deterministic conflict" in i for i in issues)
    # same literal under a DISABLED cap is not a conflict (never a candidate)
    off = dataclasses.replace(b, enabled=False, status="disabled")
    assert not any("conflict" in i for i in snapshot.validate_entries([a, off]))


def test_validate_rejects_language_derivation_and_orphan_similar():
    bad_lang = _entry(standard_queries=(_std("s1", "create a folder", "zh"),))
    assert any("derivation rule" in i
               for i in snapshot.validate_entries([bad_lang]))
    orphan = _entry(similar_queries=(_sim("m1", "建个目录", "zh", "nope"),))
    assert any("outside this capability" in i
               for i in snapshot.validate_entries([orphan]))


def test_validate_rejects_enabled_deprecated_and_unknown_policy():
    issues = snapshot.validate_entries([
        _entry("cap-a", enabled=True, status="deprecated"),
        _entry("cap-b", tool_binding="add_term", execution_policy="yolo"),
    ])
    assert any("contradicts" in i for i in issues)
    assert any("execution_policy" in i for i in issues)
