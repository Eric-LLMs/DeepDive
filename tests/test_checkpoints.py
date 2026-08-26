"""Workspace checkpoints: shadow-git snapshot / revert round-trips."""
import pytest
from agent.checkpoints import CheckpointError, CheckpointStore


async def test_snapshot_and_revert_round_trip(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("v1")
    store = CheckpointStore(ws, tmp_path / "shadow")

    c1 = store.snapshot("first")
    (ws / "a.txt").write_text("v2")
    (ws / "b.txt").write_text("new")
    c2 = store.snapshot("second")
    assert c1 != c2

    # Uncommitted edits are rolled back to the checkpoint.
    (ws / "a.txt").write_text("v3")
    store.revert(c1)
    assert (ws / "a.txt").read_text() == "v1"

    # Reverting to the later checkpoint restores its committed content.
    store.revert(c2)
    assert (ws / "a.txt").read_text() == "v2"
    assert (ws / "b.txt").read_text() == "new"


async def test_unchanged_snapshot_reuses_head(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    store = CheckpointStore(ws, tmp_path / "shadow")

    c1 = store.snapshot("first")
    c2 = store.snapshot("second")  # nothing changed
    assert c1 == c2


async def test_revert_unknown_id_raises(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    store = CheckpointStore(ws, tmp_path / "shadow")
    store.snapshot("baseline")

    with pytest.raises(CheckpointError):
        store.revert("deadbeefcafe")


async def test_shadow_dir_inside_workspace_is_not_tracked(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x")
    # Shadow lives inside the workspace (the production layout) — must not self-include.
    store = CheckpointStore(ws, ws / ".deepdive-snapshots")

    store.snapshot("first")
    # The shadow repo's own files are excluded from the snapshot.
    tracked = store._git("ls-files")
    assert ".deepdive-snapshots" not in tracked
    assert "a.txt" in tracked
