"""P3-4 guide-parity contract: the EVIDENCE guidance must teach the shipped pipeline.

Run 7's residual waste survived in *text*: the skill and the auto-run prompt kept
prescribing the retired per-claim ``fetch -> verify -> mutate_node`` retail pattern
while the batch machinery (``verify_batch`` / delta-``pending`` / digest
``chunk_hint``) was already merged and tested. This test pins the guidance to the
code so the two can never drift apart again — same pattern as the ``_LEGAL_NEXT``
parity tests: guidance text is code-owned, drift is a test failure, not a review miss.

Positive parity: both sources name the live verbs and fields.
Negative parity: neither source may contain the deprecated instructions.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_PATH = REPO / "skills" / "deep_research.skill.md"

# Fields/verbs the shipped pipeline exposes — the guidance must mention them.
POSITIVE_TOKENS = ("verify_batch", "pending", "chunk_hint")

# Deprecated retail-pattern prescriptions, matched case-insensitively. Each entry is
# (label, regex) — a hit means the text re-teaches the Run-7 double-anchor chain,
# UNLESS the sentence carrying the hit is itself a prohibition ("never do X"), which
# is how the protocol forbids the old pattern and must stay.
NEGATIVE_PATTERNS = (
    ("one verify per claim", r"one\s+`?verify`?\s+per\s+claim"),
    ("verify each claim", r"verify\s+each\s+claim"),
    ("mutate_node after verify", r"mutate_?node[^.]{0,40}after[^.]{0,40}verif"),
    ("verify/mutate reuse phrasing", r"for\s+verify\s*/\s*mutate"),
    ("batch per claim", r"batch\s+per\s+claim"),
)

_PROHIBITION_MARKERS = (
    "never", "do not", "don't", "forbidden", "not allowed", "retired", "instead of",
)


def _findings(text: str) -> list[str]:
    """Deprecated hits that appear OUTSIDE a prohibiting sentence."""
    bad: list[str] = []
    for label, pattern in NEGATIVE_PATTERNS:
        for hit in re.finditer(pattern, text, re.IGNORECASE):
            # The containing "sentence": split on periods/newlines around the hit.
            start = max(text.rfind(".", 0, hit.start()),
                        text.rfind("\n", 0, hit.start())) + 1
            end = text.find(".", hit.end())
            if end == -1:
                end = len(text)
            sentence = text[start:end].lower()
            if not any(marker in sentence for marker in _PROHIBITION_MARKERS):
                bad.append(f"{label}: {hit.group(0)!r} in {sentence.strip()[:80]!r}")
    return bad


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _prompt_text() -> str:
    """The rendered auto-run directive (imported, not regex-scraped from source)."""
    from plugins.research.workflow_adapter import auto_turn_prompt

    return auto_turn_prompt(
        task_name="t", project_id="p", stage="EVIDENCE", turn_index=1,
        consecutive_no_progress=1,  # forces the push paragraph in too
    )


class TestPositiveParity:
    def test_skill_teaches_the_batch_pipeline(self):
        text = _skill_text()
        for token in POSITIVE_TOKENS:
            assert token in text, f"skill must reference {token!r}"
        # The protocol block itself is the operative contract.
        assert "EVIDENCE PROTOCOL" in text

    def test_auto_prompt_teaches_the_batch_pipeline(self):
        text = _prompt_text()
        for token in POSITIVE_TOKENS:
            assert token in text, f"auto_turn_prompt must reference {token!r}"


class TestNegativeParity:
    def test_skill_never_prescribes_the_retail_pattern(self):
        bad = _findings(_skill_text())
        assert not bad, "skill still prescribes deprecated patterns: " + "; ".join(bad)

    def test_auto_prompt_never_prescribes_the_retail_pattern(self):
        bad = _findings(_prompt_text())
        assert not bad, "auto_turn_prompt still prescribes deprecated patterns: " + "; ".join(bad)


class TestSchemaParity:
    """The action table in the skill must equal the tools' wired action enums."""

    def test_evidence_actions_match_skill_table(self):
        from plugins.research.plugin import _EVIDENCE_ACTIONS

        row = next(
            line for line in _skill_text().splitlines()
            if "research_evidence" in line and "|" in line
        )
        for action in _EVIDENCE_ACTIONS:
            assert action in row, f"skill action table omits {action!r}"
