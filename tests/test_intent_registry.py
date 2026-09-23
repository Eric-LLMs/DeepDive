"""Intent Registry step-1 tests: value types + store control flow.

The fake-session pattern (queue canned ``execute`` results, record ``add``/
``commit``) pins the STATE MACHINE the store is responsible for — optimistic
conflicts, staged/active/failed transitions, rollback provenance, cache
coherence. SQL semantics themselves (partial unique index, JSONB round-trip)
are verified against the real PG out-of-band, same split as the 13575ce fix.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from core.application.chat.intent_funnel import registry as reg
from core.application.chat.intent_funnel.registry import store as reg_store
from core.application.chat.intent_funnel.registry import types as T
from core.infrastructure.db import CapabilityModel, RegistryVersionModel

_MISSING = object()


def _entry(cid="cap-a", **kw) -> T.CapabilityEntry:
    base = dict(
        capability_id=cid,
        tool_binding="create_folder",
        description="Create a folder.",
        patterns=("新建文件夹",),
        aliases=("建个目录",),
        examples=('create a folder named "x"',),
        negatives=("不要新建文件夹",),
        arg_slots={"name": "user_input"},
        permissions="",
        execution_policy="auto",
    )
    base.update(kw)
    return T.CapabilityEntry(**base)


def _ver_row(version=7, state=T.STATE_ACTIVE, entry=None, source=None, note=None):
    entry = entry or _entry()
    return SimpleNamespace(
        version=version, state=state, fingerprint=reg.content_fingerprint([entry]),
        payload={"capabilities": [entry.to_payload()]},
        source_version=source, actor_username="admin", note=note, error=None,
    )


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


class FakeSession:
    def __init__(self, results=(), get_map=None, commit_error=None):
        self.results = list(results)
        self.get_map = get_map or {}
        self.commit_error = commit_error
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        return self.results.pop(0)

    async def get(self, model, pk):
        return self.get_map.get((model, pk))

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
    it with none queued is a test failure (proves the code path stayed read-only)."""
    pool = list(sessions)

    def _f():
        assert pool, "store opened more sessions than canned"
        return pool.pop(0)

    return _f


# ── value types (pure) ─────────────────────────────────────────────────────────────

def test_entry_payload_roundtrip_preserves_every_field():
    import dataclasses
    e = _entry(enabled=False, status="deprecated", replacement_capability_id="cap-b",
               row_version=3)
    back = T.CapabilityEntry.from_payload(e.to_payload())
    assert back == dataclasses.replace(e, row_version=0)
    # row_version is draft bookkeeping — published payloads carry none
    assert back.row_version == 0


def test_fingerprint_order_independent_and_row_version_blind():
    a, b = _entry("cap-a"), _entry("cap-b", tool_binding="add_term")
    fp1 = reg.content_fingerprint([a, b])
    fp2 = reg.content_fingerprint([b, a])
    fp3 = reg.content_fingerprint([a, b, _entry("cap-c")])
    fp4 = reg.content_fingerprint([_entry("cap-a", row_version=9), b])
    assert fp1 == fp2 != fp3
    assert fp1 == fp4  # edit-token must not perturb content identity
    assert fp1.startswith("reg1-")


def test_to_capability_projects_only_routing_fields():
    cap = _entry().to_capability()
    assert cap.id == "cap-a" and cap.tool_binding == "create_folder"
    assert cap.examples == ('create a folder named "x"',)
    # patterns/aliases/arg_slots never cross into the QIR vocabulary
    assert not hasattr(cap, "patterns")


def test_view_projects_out_disabled_capabilities():
    live, off, dep = _entry("cap-live"), _entry("cap-off", enabled=False), \
        _entry("cap-dep", status="deprecated")
    row = SimpleNamespace(
        version=1, state=T.STATE_ACTIVE, fingerprint="reg1-x",
        payload={"capabilities": [live.to_payload(), off.to_payload(), dep.to_payload()]},
        source_version=None, actor_username=None, note=None, error=None,
    )
    view = T.RegistryVersionView.from_row(row)
    # ruling 4: disabled/deprecated are never executable candidates...
    assert [c.id for c in view.capabilities] == ["cap-live"]
    # ...but the full published set stays visible for admin/audit
    assert {e.capability_id for e in view.entries} == {"cap-live", "cap-off", "cap-dep"}


# ── draft CRUD ─────────────────────────────────────────────────────────────────────

async def test_update_draft_rejects_unknown_fields_before_touching_db():
    with pytest.raises(ValueError, match="non-draft fields"):
        await reg.update_draft(
            "cap-a", {"capability_id": "evil"}, 0,
            session_factory=factory(),  # empty pool: any open fails the test
        )


async def test_update_draft_bumps_token_on_hit():
    hit = FakeSession(results=[_Result(rowcount=1)])
    reread = FakeSession(results=[_Result(scalar=_entry(row_version=1))])
    # from_row reads attributes; hand it the row-like entry directly (duck-typed)
    out = await reg.update_draft(
        "cap-a", {"description": "Changed."}, 0,
        session_factory=factory(hit, reread),
    )
    assert hit.commits == 1 and hit.rollbacks == 0
    assert out.row_version == 1  # new token visible to the next writer


async def test_update_draft_conflict_is_not_a_silent_overwrite():
    miss = FakeSession(results=[
        _Result(rowcount=0),                # UPDATE matched nobody
        _Result(scalar="cap-a"),            # ... but the row EXISTS
    ])
    with pytest.raises(reg.RegistryConflictError):
        await reg.update_draft("cap-a", {"description": "x"}, 0, session_factory=factory(miss))
    assert miss.commits == 0 and miss.rollbacks == 1


async def test_update_draft_missing_row_raises_not_found():
    miss = FakeSession(results=[_Result(rowcount=0), _Result(scalar=None)])
    with pytest.raises(reg.RegistryNotFoundError):
        await reg.update_draft("cap-gone", {"description": "x"}, 0, session_factory=factory(miss))


async def test_create_draft_duplicate_id_is_a_conflict():
    boom = FakeSession(commit_error=IntegrityError("stmt", {}, Exception("dup key")))
    out = FakeSession(results=[_Result(scalar=_entry())])  # success-path re-read unused
    with pytest.raises(reg.RegistryConflictError, match="already exists"):
        await reg.create_draft(_entry(), session_factory=factory(boom, out))
    assert boom.rollbacks == 1


# ── versions: stage -> activate -> rollback ───────────────────────────────────────

async def test_stage_version_rejects_empty_without_touching_db():
    with pytest.raises(ValueError, match="empty"):
        await reg.stage_version([], session_factory=factory())  # empty pool


async def test_stage_version_freezes_next_number_as_staged():
    s = FakeSession(
        results=[_Result(scalar=7)],
        get_map={(RegistryVersionModel, 8): _ver_row(version=8, state=T.STATE_STAGED)},
    )
    view = await reg.stage_version([_entry()], actor_username="admin",
                                   session_factory=factory(s))
    added = s.added[0]
    assert added.version == 8 and added.state == T.STATE_STAGED
    assert added.fingerprint == reg.content_fingerprint([_entry()])
    assert s.commits == 1
    assert view.version == 8 and view.state == T.STATE_STAGED


async def test_stage_version_retries_the_number_race():
    loser = FakeSession(results=[_Result(scalar=7)],
                        commit_error=IntegrityError("s", {}, Exception("dup pk")))
    winner = FakeSession(
        results=[_Result(scalar=8)],
        get_map={(RegistryVersionModel, 9): _ver_row(version=9)},
    )
    view = await reg.stage_version([_entry()], session_factory=factory(loser, winner))
    assert winner.added[0].version == 9  # recomputed max on retry
    assert view.version == 9


async def test_activate_rejects_non_staged_versions():
    s = FakeSession(
        results=[
            _Result(rowcount=1),   # supersede old active
            _Result(rowcount=0),   # target not staged...
        ],
        get_map={(RegistryVersionModel, 3): _ver_row(version=3, state=T.STATE_FAILED)},
    )
    with pytest.raises(reg.RegistryStateError, match="failed"):
        await reg.activate_version(3, session_factory=factory(s))
    assert s.commits == 0 and s.rollbacks == 1  # nothing half-published


async def test_activate_swap_supersedes_old_and_commits_once():
    s = FakeSession(results=[_Result(rowcount=1), _Result(rowcount=1)])
    after = FakeSession(get_map={(RegistryVersionModel, 4): _ver_row(version=4)})
    reg.invalidate_cache()
    view = await reg.activate_version(4, session_factory=factory(s, after))
    assert s.commits == 1
    assert view.version == 4


async def test_mark_failed_requires_staged():
    s = FakeSession(results=[_Result(rowcount=0)],
                    get_map={(RegistryVersionModel, 2): _ver_row(version=2, state=T.STATE_SUPERSEDED)})
    with pytest.raises(reg.RegistryStateError, match="superseded"):
        await reg.mark_failed(2, "index build blew up", session_factory=factory(s))
    assert s.commits == 0


async def test_rollback_stages_a_new_version_citing_the_old_one():
    old = _ver_row(version=2, entry=_entry("cap-x"))
    getter1 = FakeSession(get_map={(RegistryVersionModel, 2): old})
    stager = FakeSession(
        results=[_Result(scalar=2)],  # max(version) == 2 -> new version 3
        get_map={(RegistryVersionModel, 3): _ver_row(version=3, source=2, note="rollback to v2")},
    )
    activator = FakeSession(results=[_Result(rowcount=1), _Result(rowcount=1)])
    getter2 = FakeSession(get_map={(RegistryVersionModel, 3): _ver_row(version=3, source=2)})
    reg.invalidate_cache()
    view = await reg.rollback(2, actor_username="admin",
                              session_factory=factory(getter1, stager, activator, getter2))
    staged = stager.added[0]
    assert staged.version == 3 and staged.source_version == 2   # new version, provenance
    assert staged.payload == old.payload                        # byte-identical content
    assert view.version == 3


# ── runtime read: active version + cache ──────────────────────────────────────────

async def test_active_view_caches_until_the_version_marker_moves():
    row = _ver_row(version=5, state=T.STATE_ACTIVE)
    first_load = FakeSession(results=[_Result(first=(5, row.fingerprint))],
                             get_map={(RegistryVersionModel, 5): row})
    cached_hit = FakeSession(results=[_Result(first=(5, row.fingerprint))])  # NO get queued
    gone = FakeSession(results=[_Result(first=None)])                        # unpublish shape
    f = factory(first_load, cached_hit, gone)
    reg.invalidate_cache()
    v1 = await reg.active_view(session_factory=f)
    v2 = await reg.active_view(session_factory=f)
    assert v1 is not None and v2 is not None and v1.version == 5
    assert v1 is v2  # served from cache: second call only peeked at the marker
    v3 = await reg.active_view(session_factory=f)
    assert v3 is None and reg_store._active_cache is None  # pointer gone -> cache dropped


async def test_active_view_marker_change_reloads_payload():
    row5 = _ver_row(version=5)
    row6 = _ver_row(version=6, entry=_entry("cap-z"))
    s1 = FakeSession(results=[_Result(first=(5, row5.fingerprint))],
                     get_map={(RegistryVersionModel, 5): row5})
    s2 = FakeSession(results=[_Result(first=(6, row6.fingerprint))],
                     get_map={(RegistryVersionModel, 6): row6})
    reg.invalidate_cache()
    await reg.active_view(session_factory=factory(s1))
    v = await reg.active_view(session_factory=factory(s2))
    assert v.version == 6 and v.capabilities[0].id == "cap-z"
