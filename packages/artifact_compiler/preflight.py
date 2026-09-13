"""Environment preflight (Task 0.1): fail-fast before any spend.

Checks the three things that can silently ruin a run at 03:00 in the middle of a
compile: the ``typst`` CLI, the ``mmdc`` (mermaid-cli) binary, and the rendering font
stack — the last one probed with a *real* micro-compile containing a CJK glyph, so a
missing font fails here instead of degrading text in the final PDF.

The store of which binaries/fonts exist is the caller's (app config, CLI flags); this
module reads no application settings — Core stays standalone-usable (invariant 1).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_PROBE_TYP = "#set page(width: 6cm, height: 3cm)\n#set text(font: (\"Libertinus Serif\", \"Noto Sans CJK SC\", \"DejaVu Sans Mono\"))\nTest 文字 123 $int_0 x$\n"


class PreflightFailure(RuntimeError):
    """Raised when any mandatory preflight check fails; carries the full report."""


@dataclass(frozen=True)
class PreflightConfig:
    typst_bin: str = "typst"
    mmdc_bin: str = "mmdc"
    min_typst_version: tuple[int, ...] = (0, 11)
    font_probe: bool = True          # micro-compile that requires Latin+CJK+Mono+Math
    command_timeout_s: int = 30


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(PreflightCheck(name=name, ok=ok, detail=detail))


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=False, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_version(text: str) -> tuple[int, ...]:
    for token in text.replace("v", " ").split():
        if token[:1].isdigit():
            parts: list[int] = []
            for chunk in token.split(".")[:3]:
                digits = "".join(c for c in chunk if c.isdigit())
                parts.append(int(digits) if digits else 0)
            return tuple(parts)
    return ()


def run_preflight(cfg: PreflightConfig | None = None) -> PreflightReport:
    """Execute all checks; raise :class:`PreflightFailure` with the report text on any
    mandatory failure. Never leaves a torn probe file behind."""
    cfg = cfg or PreflightConfig()
    report = PreflightReport()

    # 1. typst CLI present + version floor
    typst_path = shutil.which(cfg.typst_bin)
    if typst_path is None:
        report.add("typst_cli", False, f"{cfg.typst_bin!r} not found on PATH")
    else:
        try:
            rc, out, err = _run([typst_path, "--version"], cfg.command_timeout_s)
            ver = _parse_version(out or err)
            if rc != 0 or not ver:
                report.add("typst_cli", False, f"--version failed rc={rc}")
            elif ver < cfg.min_typst_version:
                report.add("typst_cli", False, f"version {ver} < {cfg.min_typst_version}")
            else:
                report.add("typst_cli", True, f"version {ver}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            report.add("typst_cli", False, str(exc))

    # 2. mmdc present
    mmdc_path = shutil.which(cfg.mmdc_bin)
    if mmdc_path is None:
        report.add("mmdc_cli", False, f"{cfg.mmdc_bin!r} not found on PATH")
    else:
        try:
            rc, out, err = _run([mmdc_path, "--version"], cfg.command_timeout_s)
            report.add("mmdc_cli", rc == 0, (out or err).strip()[:200])
        except (subprocess.TimeoutExpired, OSError) as exc:
            report.add("mmdc_cli", False, str(exc))

    # 3. font stack probe: real micro-compile (Latin + CJK + Mono + Math glyphs)
    if cfg.font_probe:
        typst_ok = any(c.name == "typst_cli" and c.ok for c in report.checks)
        if not typst_ok:
            report.add("font_stack", False, "skipped: typst CLI unavailable")
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="ac-preflight-") as td:
                    src = Path(td) / "probe.typ"
                    dst = Path(td) / "probe.pdf"
                    src.write_text(_PROBE_TYP, encoding="utf-8")
                    rc, _out, err = _run(
                        [typst_path or cfg.typst_bin, "compile", str(src), str(dst)],
                        cfg.command_timeout_s,
                    )
                    if rc != 0 or not dst.exists():
                        report.add("font_stack", False, f"probe compile failed: {err.strip()[:300]}")
                    elif "unknown font" in err.lower() or "fallback" in err.lower():
                        report.add("font_stack", False, f"font fallbacks in probe: {err.strip()[:300]}")
                    else:
                        report.add("font_stack", True, "micro-compile clean")
            except (subprocess.TimeoutExpired, OSError) as exc:
                report.add("font_stack", False, str(exc))

    if not report.ok:
        lines = "\n".join(f"  - {c.name}: {'PASS' if c.ok else 'FAIL'} {c.detail}"
                          for c in report.checks)
        raise PreflightFailure(f"environment preflight failed:\n{lines}")
    return report
