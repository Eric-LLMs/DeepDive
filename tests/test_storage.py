"""Tests for the object storage abstraction (local-disk implementation).

Verifies the sharded key layout, the put/get/delete/exists contract, and traversal
protection — no real DB, files only under pytest's tmp_path.
"""
from core.infrastructure.storage import LocalStorage, object_key


def test_object_key_sharded_layout():
    sha = "a" * 64
    assert object_key(sha) == f"objects/{sha[0:2]}/{sha[2:4]}/{sha}"


async def test_put_get_roundtrip(tmp_path):
    store = LocalStorage(tmp_path)
    key = object_key("b" * 64)

    assert await store.exists(key) is False
    assert await store.get(key) is None

    await store.put(key, b"hello world")

    assert await store.exists(key) is True
    assert await store.get(key) == b"hello world"
    # file lands in the sharded subdirectory
    assert (tmp_path / key).is_file()


async def test_put_is_idempotent_same_key(tmp_path):
    store = LocalStorage(tmp_path)
    key = object_key("c" * 64)
    await store.put(key, b"one")
    await store.put(key, b"two")
    assert await store.get(key) == b"two"


async def test_delete_removes_file_and_ignores_missing(tmp_path):
    store = LocalStorage(tmp_path)
    key = object_key("d" * 64)
    await store.put(key, b"data")

    await store.delete(key)
    assert await store.exists(key) is False

    await store.delete(key)  # absent -> no error


async def test_traversal_escape_refused(tmp_path):
    store = LocalStorage(tmp_path)
    try:
        store.resolve("../escape")
    except ValueError:
        pass
    else:
        raise AssertionError("expected traversal to be refused")


async def test_upload_chunk_path_is_under_root(tmp_path):
    store = LocalStorage(tmp_path)
    path = store.upload_chunk_path("sess-1", 3)
    assert path.is_relative_to(tmp_path)
    assert path.name == "chunk_3"
