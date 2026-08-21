"""Object storage abstraction for the cloud drive.

A storage backend addresses bytes by ``storage_key`` — a relative, server-generated path.
The local-disk implementation writes under ``settings.object_store_root`` with a 2-level
SHA-256 fan-out; S3/MinIO/OSS can drop in later behind the same :class:`Storage` protocol
without touching the drive service or the worker ingest pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from core.config import settings


def object_key(sha256: str) -> str:
    """Sharded layout for a digest: ``objects/{sha[0:2]}/{sha[2:4]}/{sha}`` (fan-out 256²)."""
    return f"objects/{sha256[0:2]}/{sha256[2:4]}/{sha256}"


class Storage(Protocol):
    """Backend-agnostic byte store keyed by relative paths."""

    async def put(self, key: str, data: bytes) -> None:
        """Write ``data`` at ``key`` (idempotent: same bytes at same key overwrite safely)."""
        ...

    async def get(self, key: str) -> bytes | None:
        """Return bytes at ``key``, or ``None`` if absent."""
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present (no error when absent)."""
        ...

    async def exists(self, key: str) -> bool:
        """True when ``key`` holds bytes."""
        ...


class LocalStorage:
    """Filesystem-backed storage: ``{root}/{key}`` with traversal protection."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or settings.object_store_root).resolve()

    def resolve(self, key: str) -> Path:
        """Resolve ``key`` under the root, refusing any path that escapes it."""
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"key escapes storage root: {key}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self.resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes | None:
        path = self.resolve(key)
        return path.read_bytes() if path.is_file() else None

    async def delete(self, key: str) -> None:
        path = self.resolve(key)
        if path.is_file():
            path.unlink()

    async def exists(self, key: str) -> bool:
        return self.resolve(key).is_file()

    # ── Transient upload scratch (local-disk specific, used by the drive service) ──
    def upload_chunk_path(self, session_id, index: int) -> Path:
        """Path of an in-flight chunk during a chunked upload (before assembly)."""
        return self.root / "uploads" / str(session_id) / f"chunk_{index}"


def get_storage() -> Storage:
    """Process-wide storage instance (stateless; S3 impl would carry a client here)."""
    return LocalStorage()
