"""Bash sandbox: where the ``bash`` tool's commands actually run.

Two backends behind one :class:`BashSandbox` protocol:

- :class:`HostBashSandbox` — hardened host fallback for LOCAL DEVELOPMENT ONLY. It is NOT a
  security boundary: the command runs directly on the host process. A best-effort
  workspace-escape guard (:func:`assert_no_escape`), a hard timeout, and an output cap are
  the only limits.
- :class:`DockerBashSandbox` — the production path: each command runs in a fresh, one-shot
  container (via docker-py) with the workspace mounted read-write, network disabled by
  default, memory/CPU caps, and a call-level timeout. docker-py is an optional dependency;
  when it is missing or the daemon is unreachable, a clear error tells the operator to fall
  back to the host sandbox.

:func:`get_bash_sandbox` reads ``settings.bash_sandbox`` (``"docker"`` | ``"host"``,
default ``"host"``) and returns the matching implementation.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from core.config import settings

logger = logging.getLogger(__name__)


def assert_no_escape(workspace: Path, command: str) -> None:
    """Best-effort workspace-escape guard (a safety net, not a security boundary).

    Rejects ``..`` path traversal, ``cd`` to a directory outside the workspace root, and
    absolute path targets whose resolved path is not inside the workspace root. Real
    isolation comes from the sandbox itself (``DockerBashSandbox``); this is the cheap
    first line of defense for the host fallback.
    """
    if ".." in command:
        raise ValueError(f"command escapes workspace: {command}")
    root = workspace.resolve()
    tokens = command.split()
    for i, token in enumerate(tokens):
        if token == "cd" and i + 1 < len(tokens):
            target = (root / tokens[i + 1]).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"command escapes workspace: {command}")
        elif _is_absolute_token(token):
            if not Path(token).resolve().is_relative_to(root):
                raise ValueError(f"command escapes workspace: {command}")


def _is_absolute_token(token: str) -> bool:
    """True for absolute-path tokens (``/etc``, ``C:\\foo``, ``C:/foo``)."""
    return token.startswith(("/", "\\")) or (len(token) >= 2 and token[1] == ":")


class BashSandbox(Protocol):
    """Runs a single shell command, returning its combined stdout+stderr (trimmed)."""

    async def run(self, command: str, timeout: int) -> str:
        """Run ``command``; return trimmed combined output, raise ``TimeoutError`` on timeout."""
        ...


class HostBashSandbox:
    """Hardened host fallback for LOCAL DEVELOPMENT ONLY.

    NOT a security boundary: the command runs directly on the host with the same privileges
    as the worker/API process. Use :class:`DockerBashSandbox` (settings.bash_sandbox="docker")
    for any untrusted command.
    """

    _MAX_OUTPUT = 50_000
    _TRUNCATED_MARKER = "\n…(truncated)"

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    async def run(self, command: str, timeout: int) -> str:
        assert_no_escape(self.workspace, command)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"command timed out after {timeout}s")
        output = stdout.decode("utf-8", errors="replace").strip()
        if len(output) > self._MAX_OUTPUT:
            output = output[: self._MAX_OUTPUT].rstrip() + self._TRUNCATED_MARKER
        if proc.returncode != 0 and not output:
            output = f"(exit {proc.returncode})"
        return output or f"(exit {proc.returncode})"


class DockerBashSandbox:
    """Production bash sandbox: one command per fresh container.

    Uses docker-py (the optional ``docker`` extra). Each ``run`` starts a one-shot container
    with the workspace mounted read-write, network disabled by default, memory/CPU caps, and
    a call-level timeout. On timeout the container is stopped and removed so nothing leaks.
    If docker-py is missing or the daemon is unreachable, a clear error tells the operator
    to fall back to :class:`HostBashSandbox`.
    """

    def __init__(
        self,
        workspace: Path,
        image: str = "debian:bookworm-slim",
        network: bool = False,
        mem_limit: str = "512m",
        cpus: float = 0.5,
        timeout: int = 30,
    ) -> None:
        self.workspace = Path(workspace)
        self.image = image
        self.network = network
        self.mem_limit = mem_limit
        self.cpus = cpus
        self.timeout = timeout

    @staticmethod
    def _import_docker():
        try:
            import docker
        except ImportError as exc:
            raise RuntimeError(
                "docker-py is not installed; install the 'docker' extra (docker>=7.0) or "
                "set settings.bash_sandbox=\"host\" to use the HostBashSandbox fallback"
            ) from exc
        return docker

    async def run(self, command: str, timeout: int) -> str:
        assert_no_escape(self.workspace, command)
        docker = self._import_docker()
        client = docker.from_env()
        workspace = str(self.workspace)
        cpu_quota = int(self.cpus * 100000)
        timeout = timeout or self.timeout
        container = None
        try:
            container = client.containers.run(
                image=self.image,
                command=["/bin/sh", "-c", command],
                network_disabled=not self.network,
                mem_limit=self.mem_limit,
                cpu_period=100000,
                cpu_quota=cpu_quota,
                volumes={workspace: {"bind": workspace, "mode": "rw"}},
                working_dir=workspace,
                detach=True,
                remove=True,
            )
            await asyncio.to_thread(container.wait, timeout=timeout)
            output = await asyncio.to_thread(container.logs, stdout=True, stderr=True)
        except Exception as exc:
            if _is_docker_timeout(exc):
                raise TimeoutError(f"command timed out after {timeout}s") from exc
            if isinstance(exc, docker.errors.DockerException):
                # TRY004: infra-unavailable, not a type error — the operator guidance matters.
                raise RuntimeError(  # noqa: TRY004
                    "docker sandbox unavailable; set settings.bash_sandbox=\"host\" "
                    f"to fall back to the HostBashSandbox ({exc})"
                ) from exc
            raise
        finally:
            if container is not None:
                for hook in (container.stop, container.remove):
                    try:
                        hook()
                    except Exception:
                        logger.warning("container cleanup failed", exc_info=True)
        return _decode_output(output)


def _is_docker_timeout(exc: Exception) -> bool:
    """True for docker-py's wait-timeout exception (``requests.exceptions.ReadTimeout``)."""
    try:
        from requests.exceptions import ReadTimeout
    except ImportError:  # pragma: no cover - requests ships with docker-py
        return False
    return isinstance(exc, ReadTimeout)


def _decode_output(raw) -> str:
    """Normalize docker-py's container output (bytes) to a trimmed string."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def get_bash_sandbox() -> BashSandbox:
    """Return the sandbox configured by ``settings.bash_sandbox`` ("docker" | "host")."""
    if settings.bash_sandbox == "docker":
        return DockerBashSandbox(
            workspace=settings.workspace_dir,
            image=settings.bash_sandbox_image,
            network=settings.bash_sandbox_network,
            mem_limit=settings.bash_sandbox_mem_limit,
            cpus=settings.bash_sandbox_cpus,
            timeout=settings.bash_sandbox_timeout,
        )
    return HostBashSandbox(settings.workspace_dir)
