"""Visual engine: mmdc subprocess runner + deterministic SVG sanitization (Task 3).

Core stays zero-LLM and offline-self-contained: rendering is a controlled local
subprocess (docs/research/19 §8). Isolation knobs:

* hard wall-clock timeout with process-kill on expiry;
* POSIX ``RLIMIT_AS`` / ``RLIMIT_CPU`` via ``preexec_fn`` (Windows dev falls back to
  timeout-only — documented, not silent);
* input cap (``.mmd`` source bytes) and output cap (SVG bytes) *before* exec;
* sanitized output is what the compiler may reference — raw mmdc output never
  enters the document path.

Syntax-retry loops (feeding stderr back to an LLM) belong to the Skill layer; this
module exposes the pieces it needs: ``render_mermaid`` result objects with stderr.
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from artifact_compiler.plan import VisualSpec

_IS_POSIX = platform.system() != "Windows"


class RenderInputError(ValueError):
    """Source rejected before exec (size cap, empty, bad path)."""


class RenderTimeoutError(RuntimeError):
    """mmdc exceeded its wall-clock budget and was killed."""


@dataclass(frozen=True)
class MermaidConfig:
    mmdc_bin: str = "mmdc"
    timeout_s: float = 120.0
    mem_limit_mb: int = 2048
    cpu_limit_s: int = 150
    max_source_bytes: int = 64 * 1024
    max_svg_bytes: int = 8 * 1024 * 1024


@dataclass
class RenderResult:
    ok: bool
    svg_path: Path
    stderr: str = ""
    notes: list[str] = field(default_factory=list)  # e.g. rlimit-unsupported


# ── SVG sanitization (deterministic, allowlist-by-deletion) ──────────────────

_SCRIPT_BLOCK = re.compile(
    r"<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/\s*>", re.IGNORECASE | re.DOTALL
)
_EVENT_ATTR = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_FOBJECT_BLOCK = re.compile(
    r"<foreignObject\b[^>]*>.*?</foreignObject\s*>", re.IGNORECASE | re.DOTALL
)
# external references: href/xlink:href not starting with # (in-document) or data:
_EXTERNAL_HREF = re.compile(
    r"\s+(?:xlink:)?href\s*=\s*(\"|')(?!\s*(?:#|data:))[^\"']*\1",
    re.IGNORECASE,
)
_IMAGE_TAG_EXTERNAL = re.compile(
    r"<image\b[^>]*\s(?:xlink:)?href\s*=\s*(\"|')(?!\s*(?:#|data:))[^\"']*\1[^>]*/?>",
    re.IGNORECASE,
)
_STYLE_ATIMPORT = re.compile(r"@import\s+url\((?![\"']?#)[^)]*\)", re.IGNORECASE)


def sanitize_svg(svg: str) -> str:
    """Remove scripts, event handlers, foreignObject, external references and
    ``@import url(...)`` from a rendered SVG. Idempotent; benign markup untouched."""
    out = _SCRIPT_BLOCK.sub("", svg)
    out = _FOBJECT_BLOCK.sub("", out)
    out = _IMAGE_TAG_EXTERNAL.sub("", out)
    out = _EXTERNAL_HREF.sub("", out)
    out = _EVENT_ATTR.sub("", out)
    out = _STYLE_ATIMPORT.sub("", out)
    return out


def is_svg_clean(svg: str) -> bool:
    """Defense-in-depth gate applied *after* sanitization: anything still matching
    means the SVG is unsafe and the asset must fail closed."""
    return sanitize_svg(svg) == svg


# ── heuristic density gate (spec-level, pre-render) ──────────────────────────

def density_issues(spec: VisualSpec) -> list[str]:
    """Deterministic over-density heuristics (docs/research/19 §8): violations mean
    'split or simplify', never a silent pass."""
    issues: list[str] = []
    n = len(spec.entities)
    max_nodes = spec.constraints.max_nodes
    if n > max_nodes:
        issues.append(f"node count {n} exceeds max_nodes {max_nodes}")
    rels = spec.relationships
    if n > 0 and len(rels) > 2 * n:
        issues.append(f"edge count {len(rels)} > 2x nodes {n} (hairball risk)")
    labels = [r.label for r in rels if r.label]
    if labels:
        avg = sum(len(x) for x in labels) / len(labels)
        if avg > 40:
            issues.append(f"avg edge-label length {avg:.0f} > 40 chars")
    too_wide = [e for e in spec.entities if len(e) > 28]
    if too_wide:
        issues.append(f"entity labels >28 chars: {too_wide[:3]}")
    return issues


# ── subprocess runner ────────────────────────────────────────────────────────

def _limits(cfg: MermaidConfig):
    """preexec_fn applying RLIMITs on POSIX; None where unsupported (Windows)."""
    if not _IS_POSIX:  # pragma: no cover - dev platform
        return None
    import resource

    def _apply() -> None:  # runs post-fork, pre-exec in the child
        resource.setrlimit(resource.RLIMIT_AS, (cfg.mem_limit_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_limit_s, cfg.cpu_limit_s + 10))
        os.setsid()  # own process group so chromium children die with the parent

    return _apply


async def render_mermaid(
    code: str,
    src_path: Path,
    dest_svg_path: Path,
    *,
    cfg: MermaidConfig | None = None,
    extra_args: tuple[str, ...] = (),
) -> RenderResult:
    """Render one Mermaid source to a **sanitized** SVG file. Never raises for
    render failures (returns ok=False + stderr for the Skill's retry loop); raises
    only for input-contract violations."""
    cfg = cfg or MermaidConfig()
    src_path = Path(src_path)
    dest_svg_path = Path(dest_svg_path)
    if not code.strip():
        raise RenderInputError("empty mermaid source")
    if len(code.encode("utf-8")) > cfg.max_source_bytes:
        raise RenderInputError(
            f"mermaid source exceeds {cfg.max_source_bytes} bytes input cap"
        )
    mmdc = shutil.which(cfg.mmdc_bin)
    if mmdc is None:
        raise RenderInputError(f"mmdc binary {cfg.mmdc_bin!r} not found (preflight must pass first)")

    src_path.parent.mkdir(parents=True, exist_ok=True)
    dest_svg_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(code, encoding="utf-8")

    notes: list[str] = []
    if not _IS_POSIX:
        notes.append("RLIMITs unsupported on Windows; timeout-only isolation")

    cmd = [mmdc, "-i", str(src_path), "-o", str(dest_svg_path), *extra_args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits(cfg) if _IS_POSIX else None,
        )
    except OSError as exc:
        return RenderResult(ok=False, svg_path=dest_svg_path, stderr=f"spawn failed: {exc}")

    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(), timeout=cfg.timeout_s
        )
    except TimeoutError:
        _kill_tree(proc)
        return RenderResult(
            ok=False, svg_path=dest_svg_path,
            stderr=f"timeout after {cfg.timeout_s}s (process killed)",
        )

    stderr = err.decode("utf-8", "replace") if err else ""
    if proc.returncode != 0 or not dest_svg_path.exists():
        return RenderResult(ok=False, svg_path=dest_svg_path,
                            stderr=stderr or f"exit {proc.returncode}")

    raw = dest_svg_path.read_bytes()
    if len(raw) > cfg.max_svg_bytes:
        dest_svg_path.unlink(missing_ok=True)
        return RenderResult(ok=False, svg_path=dest_svg_path,
                            stderr=f"svg exceeds {cfg.max_svg_bytes} bytes output cap")
    cleaned = sanitize_svg(raw.decode("utf-8", "replace"))
    if not is_svg_clean(cleaned):  # defensive; sanitize is a fixpoint by construction
        dest_svg_path.unlink(missing_ok=True)
        return RenderResult(ok=False, svg_path=dest_svg_path,
                            stderr="svg failed closed sanitization check")
    dest_svg_path.write_text(cleaned, encoding="utf-8")
    return RenderResult(ok=True, svg_path=dest_svg_path, stderr=stderr, notes=notes)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group (chromium spawns children); fall back to direct kill."""
    import contextlib
    import signal

    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
